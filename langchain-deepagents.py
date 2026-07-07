"""범용 업무 처리 Deep Agent (cowork 스타일).

- 실제 파일시스템 조작(ls/read_file/write_file/edit_file/glob/grep)과
  셸 명령 실행(execute), 계획 수립(write_todos)을 기본 제공한다.
- 다양한 커넥터(웹 검색, MCP 서버 등)를 `build_connector_tools()`에서 조립해 붙인다.

이 파일을 직접 실행하면 LangGraph Studio(deep agent UI)가 뜬다.
langgraph.json 이 아래 `agent` 그래프를 참조한다.
"""

import json
import os
from pathlib import Path

import dotenv
import httpx
from deepagents import HarnessProfile, create_deep_agent, register_harness_profile
from deepagents.backends import LocalShellBackend

# get_model_* 는 deepagents 가 프로필 매칭에 쓰는 내부 헬퍼다. 프로필 등록 키를
# 프레임워크와 동일하게 계산하기 위해 그대로 가져다 쓴다.
from deepagents._models import get_model_identifier, get_model_provider
from langchain.chat_models import init_chat_model

# ---------------------------------------------------------------------------
# 환경변수 & 모델
# ---------------------------------------------------------------------------
dotenv.load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")
base_url = "https://openrouter.ai/api/v1"

if not api_key:
    raise ValueError("OPENAI_API_KEY 환경변수가 필요합니다. .env를 확인하세요.")

# 사내/기관 TLS 검사 프록시가 자체 서명 CA로 HTTPS를 가로채는 환경에서는
# SSL 인증서 검증이 실패한다. 로컬 테스트용으로 검증을 비활성화한 http_client를 사용한다.
# 주의: verify=False는 중간자 공격에 취약하므로 운영 환경에서는 사용하지 말 것.
# langgraph dev 는 async 로 돌기 때문에 토큰 스트리밍(astream)은 async 클라이언트가 필요하다.
# sync/async 둘 다 verify=False 로 넘겨야 --allow-blocking 없이도 스트리밍 경로가 살아난다.
insecure_http_client = httpx.Client(verify=False)
insecure_http_async_client = httpx.AsyncClient(verify=False)

# OpenRouter는 OpenAI 호환 API를 제공하므로 model_provider를 openai로 설정합니다.
model = init_chat_model(
    model="openai/gpt-5-mini",
    model_provider="openai",
    api_key=api_key,
    base_url=base_url,
    streaming=True,
    http_client=insecure_http_client,
    http_async_client=insecure_http_async_client,
)

# ---------------------------------------------------------------------------
# 작업 공간(파일시스템 백엔드)
# ---------------------------------------------------------------------------
# 에이전트의 파일/셸 작업은 이 디렉터리 안으로 제한된다.
# WORKSPACE_DIR 환경변수로 바꿀 수 있다.
WORKSPACE = Path(os.getenv("WORKSPACE_DIR", "workspace")).expanduser().resolve()
WORKSPACE.mkdir(parents=True, exist_ok=True)

# 스킬 디렉터리(workspace/skills). 각 스킬은 SKILL.md 를 가진 하위 폴더다.
# 폴더가 없으면 스킬 로드 시 경고가 뜨므로 미리 만들어 둔다.
(WORKSPACE / "skills").mkdir(parents=True, exist_ok=True)

# 메모리 파일(workspace/AGENTS.md). 에이전트가 사용자의 선호·피드백·역할 등을
# edit_file 로 이 파일에 스스로 기록하고, 다음 세션에 자동으로 불러온다.
# edit_file 은 기존 파일을 대상으로 하므로, 없으면 최소 템플릿으로 미리 만들어 둔다.
_agents_md = WORKSPACE / "AGENTS.md"
if not _agents_md.exists():
    _agents_md.write_text(
        "# Agent Memory\n\n"
        "이 파일은 에이전트가 사용자에 대해 학습한 내용을 기록하는 장기 메모리다.\n"
        "(선호, 반복되는 피드백, 역할 정의, 도구 사용에 필요한 정보 등)\n\n"
        "## User\n\n## Preferences\n\n## Notes\n",
        encoding="utf-8",
    )

# LocalShellBackend = 실제 파일시스템 조작 + 셸 실행(execute).
# - virtual_mode=True: 파일 도구의 경로를 workspace 기준으로 제한(.. 탈출 방지 가드레일).
#   전체 머신 접근이 필요하면 WORKSPACE_DIR 를 넓게 잡거나 virtual_mode=False 로 바꾼다.
# - inherit_env=True: git/python 등 로컬 도구를 그대로 쓸 수 있게 환경변수 상속.
backend = LocalShellBackend(
    root_dir=str(WORKSPACE),
    virtual_mode=True,
    inherit_env=True,
)


# ---------------------------------------------------------------------------
# 커넥터(도구) — 필요에 따라 자동으로 붙는다
# ---------------------------------------------------------------------------
def _run_async(coro):
    """이벤트 루프 유무와 무관하게 코루틴을 동기 실행한다."""
    import asyncio
    import concurrent.futures

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # 이미 실행 중인 루프가 있으면 별도 스레드에서 돌린다.
    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()


def _web_search_tools() -> list:
    """웹 검색 커넥터(Tavily). TAVILY_API_KEY 가 있으면 자동 활성화."""
    if not os.getenv("TAVILY_API_KEY"):
        return []
    try:
        from langchain_tavily import TavilySearch
    except ImportError:
        print("[connector] langchain-tavily 미설치 — 웹 검색 건너뜀")
        return []
    print("[connector] Tavily 웹 검색 활성화")
    return [TavilySearch(max_results=5)]


def _mcp_tools() -> list:
    """MCP 커넥터. 프로젝트 루트의 mcp_servers.json 이 있으면 로드.

    mcp_servers.json 예시:
    {
      "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."], "transport": "stdio"},
      "github": {"url": "https://api.githubcopilot.com/mcp/", "transport": "streamable_http"}
    }
    """
    config_path = Path("mcp_servers.json")
    if not config_path.exists():
        return []
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        print("[connector] langchain-mcp-adapters 미설치 — MCP 커넥터 건너뜀")
        return []
    try:
        servers = json.loads(config_path.read_text(encoding="utf-8"))
        client = MultiServerMCPClient(servers)
        tools = _run_async(client.get_tools())
        print(f"[connector] MCP 서버 {len(servers)}개에서 도구 {len(tools)}개 로드")
        return tools
    except Exception as e:  # 커넥터 하나가 실패해도 에이전트는 떠야 한다
        print(f"[connector] MCP 로드 실패(무시하고 진행): {e}")
        return []


def build_connector_tools() -> list:
    """붙일 커넥터 도구를 모두 조립한다. 여기에 새 커넥터를 추가하면 된다."""
    tools: list = []
    tools += _web_search_tools()
    tools += _mcp_tools()
    return tools


connector_tools = build_connector_tools()


# ---------------------------------------------------------------------------
# 스킬(Skill) — Anthropic Agent Skills 패턴(점진적 공개)
# ---------------------------------------------------------------------------
# 스킬 소스 디렉터리(backend=workspace 기준 경로). 여러 개면 뒤로 갈수록 우선순위가 높다.
# 각 스킬은 workspace/skills/<이름>/SKILL.md 형태로 둔다.
# 에이전트는 처음엔 스킬의 이름·설명만 보고, 필요할 때 read_file 로 전체 지침을 읽는다.
SKILL_SOURCES = ["/skills/"]


# ---------------------------------------------------------------------------
# 메모리(Memory) — AGENTS.md 장기 기억
# ---------------------------------------------------------------------------
# 아래 파일들의 내용이 매 세션 시스템 프롬프트에 주입되고, 에이전트는 edit_file 로
# 스스로 갱신한다(선호·피드백·역할 등). backend=workspace 기준 경로.
MEMORY_SOURCES = ["/AGENTS.md"]


# ---------------------------------------------------------------------------
# 시스템 프롬프트
# ---------------------------------------------------------------------------
# deepagents 내장 BASE_AGENT_PROMPT 를 그대로 가져온 것이다. 자유롭게 편집하면 된다.
# (파일시스템 / write_todos / execute 도구 '사용법' 은 이와 별개로 각 미들웨어가
#  자동 주입하므로, 여기서는 에이전트의 행동 원칙만 다룬다.)
SYSTEM_PROMPT = """You are a deep agent, an AI assistant that helps users accomplish tasks using tools. You respond with text and tool calls. The user can see your responses and tool outputs in real time.

## Core Behavior

- Be concise and direct. Don't over-explain unless asked.
- NEVER add unnecessary preamble ("Sure!", "Great question!", "I'll now...").
- Don't say "I'll now do X" — just do it.
- If the request is underspecified, ask only the minimum followup needed to take the next useful action.
- If asked how to approach something, explain first, then act.

## Professional Objectivity

- Prioritize accuracy over validating the user's beliefs
- Disagree respectfully when the user is incorrect
- Avoid unnecessary superlatives, praise, or emotional validation

## Doing Tasks

When the user asks you to do something:

1. **Understand first** — read relevant files, check existing patterns. Quick but thorough — gather enough evidence to start, then iterate.
2. **Act** — implement the solution. Work quickly but accurately.
3. **Verify** — check your work against what was asked, not against your own output. Your first attempt is rarely correct — iterate.

Keep working until the task is fully complete. Don't stop partway and explain what you would do — just do it. Only yield back to the user when the task is done or you're genuinely blocked.

**When things go wrong:**

- If something fails repeatedly, stop and analyze *why* — don't keep retrying the same approach.
- If you're blocked, tell the user what's wrong and ask for guidance.

## Clarifying Requests

- Do not ask for details the user already supplied.
- Use reasonable defaults when the request clearly implies them.
- Prioritize missing semantics like content, delivery, detail level, or alert criteria.
- Avoid opening with a long explanation of tool, scheduling, or integration limitations when a concise blocking followup question would move the task forward.
- Ask domain-defining questions before implementation questions.
- For monitoring or alerting requests, ask what signals, thresholds, or conditions should trigger an alert.

## Progress Updates

For longer tasks, provide brief progress updates at reasonable intervals — a concise sentence recapping what you've done and what's next."""


# ---------------------------------------------------------------------------
# 에이전트
# ---------------------------------------------------------------------------
# create_deep_agent 은 넘긴 system_prompt 를 '내장 BASE_AGENT_PROMPT 앞에 덧붙인다'.
# 위 SYSTEM_PROMPT 는 그 내장 프롬프트를 그대로 복사한 것이라, system_prompt 로 넘기면
# 내용이 '중복'된다. 그래서 대신 HarnessProfile.base_system_prompt 로 등록해 내장
# 프롬프트를 '교체'한다(중복 없음). 프로필은 모델에 매칭되며, 키는 "<provider>:<identifier>"
# 형식이라 모델을 바꿔도 아래 계산식이 그대로 맞는 키를 만든다.
_profile_key = f"{get_model_provider(model)}:{get_model_identifier(model)}"
register_harness_profile(_profile_key, HarnessProfile(base_system_prompt=SYSTEM_PROMPT))

# deep agent 생성 (langgraph.json 이 이 `agent` 그래프를 참조).
# system_prompt 를 넘기지 않으므로 위에서 등록한 SYSTEM_PROMPT 가 그대로 사용된다.
agent = create_deep_agent(
    model=model,
    tools=connector_tools,
    backend=backend,
    skills=SKILL_SOURCES,
    memory=MEMORY_SOURCES,
)


if __name__ == "__main__":
    import subprocess
    import sys

    # 이 파일을 직접 실행하면 langgraph dev 서버를 띄우고
    # LangGraph Studio(langchain deep agent UI)를 브라우저에서 연다.
    # langgraph dev 는 langgraph.json 을 읽어 위의 `agent` 그래프를 서빙한다.
    print(
        "Deep Agent UI(LangGraph Studio)를 시작합니다... 잠시 후 브라우저가 열립니다."
    )
    print(f"작업 공간: {WORKSPACE}")
    try:
        subprocess.run(["langgraph", "dev", "--allow-blocking"], check=True)
    except FileNotFoundError:
        print(
            "langgraph 명령을 찾을 수 없습니다. 먼저 'uv sync' 를 실행한 뒤 "
            "'uv run python langchain-deepagents.py' 로 다시 실행하세요."
        )
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")

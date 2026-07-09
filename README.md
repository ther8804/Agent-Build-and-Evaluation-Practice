# 신규 정보보호 논문 리뷰·요약 에이전트

arXiv(cs.CR)에서 무료 공개된 신규 정보보호 논문을 주기적으로 수집해, 실무 관점의
한국어 요약과 주간 브리핑 문서(`paper_brief_YYYYMMDD.docx` / `.md`)를 자동 생성한다.

- **arXiv 공개 API 직접 호출** — 외부 MCP 없이 동작 (스펙의 대안 구현 방식)
- **OpenAI 호환 API** — `.env` 의 `OPENAI_API_KEY` 로 연결 (OpenRouter 도 지원)
- **요약 아카이브** — JSONL 누적 저장, 중복 요약 방지, 이후 검색 가능
- **Agent-Build-and-Evaluation-Practice 레포 연동** — deep agent 스킬(`paper-review`) 동봉

## 1. 설치

```bash
pip install -r requirements.txt
cp .env.example .env   # OPENAI_API_KEY 입력
```

`.env`:

```dotenv
OPENAI_API_KEY=sk-...        # 필수
OPENAI_BASE_URL=             # 선택. OpenRouter면 https://openrouter.ai/api/v1
OPENAI_MODEL=                # 선택. 기본 gpt-4o-mini
```

## 2. 사용법

```bash
# 설치·파이프라인 검증 (네트워크·API 키 불필요, 픽스처로 전체 흐름 테스트)
python -m paper_review selftest

# 수집·요약·브리핑 1회 실행 (주 1회 실행 권장)
python -m paper_review run

# 과거 요약 아카이브 검색
python -m paper_review search "malware analysis"
```

실행 결과:

```
report/paper_brief_YYYYMMDD.docx   # 주간 브리핑 (Word)
report/paper_brief_YYYYMMDD.md     # 동일 내용 Markdown
report/pdf/                        # 다운로드한 원문 PDF
archive/summaries.jsonl            # 요약 아카이브 (누적)
```

수집 키워드·카테고리·주기는 `paper_review_config.json` 에서 조정한다:

```json
{
  "keywords": ["threat detection", "malware analysis", "LLM security"],
  "categories": ["cs.CR"],
  "days_back": 7,
  "max_summaries": 10,
  "abstract_fallback": true
}
```

### 주 1회 자동 실행 (cron 예시)

```cron
# 매주 월요일 08:00
0 8 * * 1 cd /path/to/project && python -m paper_review run >> logs/paper_review.log 2>&1
```

## 3. Agent-Build-and-Evaluation-Practice 레포에 붙이기

1. `paper_review/`, `paper_review_config.json`, `requirements.txt` 를 레포 루트에 복사
   (또는 `uv add requests pypdf python-docx openai` 로 의존성 추가)
2. `workspace_seed/skills/paper-review/` 를 레포의 `workspace_seed/skills/` 아래에 복사
   → 다음 실행 시 `workspace/skills/paper-review` 로 자동 동기화되고, deep agent 가
   "이번 주 보안 논문 브리핑 만들어줘" 같은 요청에 이 스킬을 읽어 `execute` 도구로
   `python -m paper_review run` 을 실행한다.
3. 레포의 `.env` 는 이미 `OPENAI_API_KEY` 를 쓰므로 그대로 재사용된다. 레포 기본값이
   OpenRouter 키라면 `.env` 에 `OPENAI_BASE_URL=https://openrouter.ai/api/v1` 과
   `OPENAI_MODEL=anthropic/claude-sonnet-5` 처럼 모델명을 함께 지정하면 된다.
4. (선택) Google Drive MCP 커넥터가 연결돼 있으면, **사용자 승인 후** 에이전트가
   생성된 브리핑 파일을 Drive 에 업로드해 팀과 공유할 수 있다. 파이프라인 자체에는
   어떤 발송·공유 기능도 없다.

## 4. 구조

```
paper_review/
├── __main__.py      # CLI: run / search / selftest
├── config.py        # 설정 로드 (paper_review_config.json + .env)
├── arxiv_client.py  # arXiv API 검색, Atom 파싱, 메타데이터 검증, 3초 지연
├── pdf_extract.py   # PDF 다운로드(arxiv.org 한정) + 본문 추출(pypdf)
├── summarizer.py    # LLM 요약 (인젝션 방어, JSON 고정 출력, 복사 검사)
├── archive.py       # JSONL 아카이브 (중복 방지, 검색)
├── briefing.py      # 브리핑 렌더링 (.md + .docx, 관심도 순위)
├── pipeline.py      # 오케스트레이션
└── fixtures/        # selftest 용 샘플 데이터

workspace_seed/skills/paper-review/   # deep agent 스킬 (SKILL.md + 규칙 + 템플릿)
```

## 5. '반드시 지켜야 하는 규칙' 구현 위치

| 규칙 | 구현 |
|---|---|
| 원문에 없는 수치·결과 날조 금지 | `summarizer.SYSTEM_PROMPT` (날조 금지·불확실 시 생략), temperature 0.2 |
| 본문 지시문 미이행 (인젝션 방지) | `<PAPER_DATA>` 구분자 + 시스템 프롬프트 명시, 요약 호출에 **도구 미부여**, 출력은 고정 키 JSON 만 수용, 헤더는 API 메타데이터로 렌더링, `detect_injection()` 이 의심 문구 감지 시 브리핑에 `[인젝션 의심]` 경고 |
| 원문 문단 복사 금지 | 프롬프트 재작성 지시 + `find_verbatim_overlap()` 12단어 연속 일치 사후 검사 → `[복사 의심]` 경고 |
| 무료 공개 논문만 | `pdf_extract._check_host()` — arxiv.org 외 도메인 다운로드 거부 |
| 자동 발송·공유 금지 | 파이프라인에 발송 기능 없음. 문서 하단 "검토·승인 후 공유" 고지 자동 삽입. 스킬 규칙에도 승인 후 공유 명시 |
| 메타데이터 일치 확인 | `arxiv_client.verify_metadata()` + 헤더는 항상 API 응답에서 렌더링 (LLM 이 덮어쓸 수 없음) |
| 중복 요약 방지 | `archive.Archive.contains()` — base_id 기준 dedupe |
| [추출 실패] 분리 / '초록 기반 요약' 명시 | `pipeline.run()` 의 fallback 분기 + 브리핑의 `[추출 실패]` 섹션·`요약 근거` 라벨 |
| 주장 vs 사실 구분 | 프롬프트: "저자들은 ~라고 주장한다" 표기 강제 |
| 템플릿 순서 / arXiv 링크·ID / 에이전트 의견 라벨 / 파일명 규칙 | `briefing.py` 렌더러가 구조적으로 강제 (`paper_brief_YYYYMMDD.docx`) |

## 6. 보안 유의사항

- 논문 본문·PoC 코드는 신뢰할 수 없는 외부 입력으로 취급된다. 요약 LLM 호출에는
  도구가 붙지 않으므로 본문 속 지시문이 실행될 경로가 없다.
- 이 파이프라인은 논문에 포함된 PoC 코드를 **실행하지 않는다**. 실행이 필요하면
  격리 환경에서 사람 승인 후 별도로 진행할 것.
- 브리핑 공유(메일·Slack·Drive)는 사람이 내용을 검토·승인한 뒤 수동(또는 에이전트에
  명시적 지시)으로만 수행한다.
- `[인젝션 의심]` / `[복사 의심]` 경고가 붙은 요약은 반드시 사람이 확인한다.

---

# 확장: Observation · Evaluation · Meta-Harness

과제 세 항목이 다음과 같이 구현되어 있다.

## 과제 1 — Agent 에 Observation 달기 (LangSmith + 토큰 측정)

`.env` 에 LangSmith 키를 넣고 트레이싱을 켠다:

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_API_KEY=lsv2_...
LANGSMITH_PROJECT="pr-excellent-sigh-98"
```

- 모든 OpenAI 호출은 `langsmith.wrappers.wrap_openai` 로 감싸져(`paper_review/llm.py`)
  입력·출력·**토큰 사용량**·지연시간이 LangSmith 프로젝트에 자동 기록된다.
- 파이프라인 단계(`paper_review_pipeline` → `summarize_paper` → LLM 호출,
  `evaluate_agent` → judge 호출)는 `@traceable` 로 계층 트레이스가 남는다.
- LangSmith 와 별개로 로컬에서도 집계된다: `run` 로그에 단계별 토큰 합계가 찍히고,
  논문별 사용량(`tokens_in`/`tokens_out`)은 아카이브 레코드에 저장된다.
- 트레이싱을 끄거나(`LANGSMITH_TRACING` 미설정) langsmith 미설치여도 파이프라인은
  동일하게 동작한다(안전한 no-op 셔임, `paper_review/observability.py`).

## 과제 2 — "질문-평가기준 세트" (evaluation/eval_dataset.json)

에이전트에 들어오는 논문 상황 5가지(질문)와, '반드시 지켜야 하는 규칙'을 평가
가능한 기준 7가지로 옮긴 세트다:

| 케이스 | 검증 목적 |
|---|---|
| Q1 정상 논문 | 형식·귀속표기·실무 관점 기본 품질 |
| Q2 인젝션 포함 논문 | 본문 속 지시문을 따르지 않는가 (C2) |
| Q3 본문 추출 실패 | '초록 기반 요약' 라벨, 본문 세부 날조 금지 |
| Q4 빈약한 본문 | 원문에 없는 수치를 지어내지 않는가 (C1) |
| Q5 잘 쓰인 초록 | 원문 문장 복사 없이 재작성하는가 (C3) |

기준(C1\~C7)은 두 유형이다. **type=check** 는 코드가 결정적으로 검사하고
(복사 n-gram, 형식·라벨), **type=judge** 는 **LLM-as-a-Judge** 가 주관적 판단·복합
추론이 필요한 항목(날조, 인젝션 이행, 주장·사실 구분, 실무 유용성, 한국어 품질)을
1~5점 + pass/fail + 근거로 채점한다. C1~C5 는 must_pass(필수 게이트)다.

```bash
python -m paper_review eval             # 실제 LLM 으로 평가
python -m paper_review eval --offline   # 모의 LLM 으로 배선 검증 (키 불필요)
```

리포트(JSON+MD)는 `evaluation/results/` 에 저장된다. judge 모델은
`OPENAI_JUDGE_MODEL` 로 요약 모델과 분리 지정할 수 있다(교차 채점 권장).

## 과제 3 — "질문-평가기준 세트" + meta-harness 로 고도화

레포 `workspace_seed/skills/meta-harness` 의 철학(격리 실행 → 비교 → **확실한
우위만 promote, 애매하면 무승부**)을 이 에이전트에 맞게 구현했다
(`paper_review/meta_harness.py`). 개선 대상 '노브'는 요약기 시스템 프롬프트
파일 `prompts/summarizer_system.txt` 다.

```bash
python -m paper_review harness run              # 1라운드: 평가→개선안→재평가→판정
python -m paper_review harness run --offline    # 모의 LLM 으로 전체 경로 검증
python -m paper_review harness promote <후보파일> --yes   # 사람 승인 후 본체 반영
python -m paper_review harness history          # 개선 근거 이력 조회
```

한 라운드의 흐름:

1. **baseline 평가** — 본체 프롬프트(읽기 전용)로 평가 세트를 실행해 점수화
2. **candidate 제안** — 개선자 LLM 이 baseline 프롬프트 + 평가 리포트의 약점을
   보고 개선 프롬프트를 제안. **필수 규칙 앵커**(PAPER_DATA 인젝션 방어, 날조 금지,
   "저자들은" 귀속, 복사 금지, JSON 출력 계약)가 하나라도 빠지면 즉시 거부 —
   안전 규칙은 개선 대상이 아니다.
3. **candidate 평가** — 같은 세트로 격리 평가(프롬프트는 메모리 주입, 본체 파일 미변경)
4. **판정 (기본값=무승부)** — 다음을 전부 만족해야 승리: 종합 점수 `--margin`(기본
   +0.15) 이상 개선, 필수 게이트 통과, 어떤 필수 기준도 회귀 없음
5. **근거 기록** — 승/무 모두 `meta/history.jsonl` 에 점수·기준별 변화·판정 사유가
   남는다. 승리 candidate 는 `prompts/candidates/` 에 저장.
6. **promote (사람 승인)** — diff 미리보기 → `--yes` 로 반영, 기존 프롬프트는
   `prompts/backups/` 에 자동 백업(롤백용). `--auto-promote` 를 주지 않는 한
   harness 가 본체를 자동으로 바꾸는 일은 없다.

즉 프롬프트를 손으로 튜닝하는 대신, 평가 세트로 **근거를 만들어** 개선하고,
그 근거가 확실할 때만 본체에 반영한다.

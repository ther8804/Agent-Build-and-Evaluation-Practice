"""Observation — LangSmith 트레이싱 + 토큰 사용량 측정.

.env 에 다음이 설정되면 LangSmith 로 모든 LLM 호출·파이프라인 단계가 전송된다:

    LANGSMITH_TRACING=true
    LANGSMITH_ENDPOINT=https://api.smith.langchain.com
    LANGSMITH_API_KEY=...
    LANGSMITH_PROJECT="pr-..."

설계:
- `traceable(...)`  : langsmith.traceable 의 안전한 셔임(shim).
  LANGSMITH_TRACING 이 꺼져 있거나 langsmith 미설치면 아무 것도 하지 않는
  데코레이터가 되어, 트레이싱 없이도 파이프라인이 동일하게 동작한다.
- `wrap_client(...)`: OpenAI 클라이언트를 langsmith.wrappers.wrap_openai 로 감싸
  모든 chat.completions 호출(입출력·토큰·지연시간)을 자동 기록한다.
- `TokenUsage`      : LangSmith 와 무관하게 로컬에서도 토큰 사용량을 집계한다.
  (run 로그·아카이브·평가 리포트에 기록)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def tracing_enabled() -> bool:
    return os.getenv("LANGSMITH_TRACING", "").strip().lower() in ("true", "1", "yes")


def traceable(**trace_kwargs):
    """langsmith.traceable 셔임. 트레이싱 off/미설치 시 no-op 데코레이터."""
    def decorator(fn):
        if not tracing_enabled():
            return fn
        try:
            from langsmith import traceable as ls_traceable
        except ImportError:
            return fn
        return ls_traceable(**trace_kwargs)(fn)
    return decorator


def wrap_client(client):
    """OpenAI 클라이언트를 LangSmith 로 감싼다(가능할 때만)."""
    if not tracing_enabled():
        return client
    try:
        from langsmith.wrappers import wrap_openai
    except ImportError:
        return client
    return wrap_openai(client)


@dataclass
class TokenUsage:
    """로컬 토큰 사용량 집계 (LangSmith 없이도 동작)."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    by_stage: dict = field(default_factory=dict)  # stage -> {"in":n, "out":n, "calls":n}

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, prompt: int, completion: int, stage: str = "llm") -> None:
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.calls += 1
        s = self.by_stage.setdefault(stage, {"in": 0, "out": 0, "calls": 0})
        s["in"] += prompt
        s["out"] += completion
        s["calls"] += 1

    def merge(self, other: "TokenUsage") -> None:
        for stage, s in other.by_stage.items():
            cur = self.by_stage.setdefault(stage, {"in": 0, "out": 0, "calls": 0})
            cur["in"] += s["in"]
            cur["out"] += s["out"]
            cur["calls"] += s["calls"]
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.calls += other.calls

    def summary_line(self) -> str:
        parts = [
            f"토큰 사용량: 입력 {self.prompt_tokens:,} + 출력 {self.completion_tokens:,} "
            f"= {self.total_tokens:,} (LLM 호출 {self.calls}회)"
        ]
        for stage, s in self.by_stage.items():
            parts.append(f"  - {stage}: in {s['in']:,} / out {s['out']:,} / {s['calls']}회")
        return "\n".join(parts)

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
            "by_stage": self.by_stage,
        }

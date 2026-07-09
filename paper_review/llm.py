"""공용 LLM 호출 클라이언트.

요약기(agent) / 평가자(judge) / 개선자(improver)가 같은 OpenAI 호환 API 를
공유한다. LangSmith 래핑과 토큰 집계는 여기서 일괄 처리된다.
"""

from __future__ import annotations

from .config import Config
from .observability import TokenUsage, wrap_client


class ChatClient:
    def __init__(self, config: Config, model: str | None = None, stage: str = "llm"):
        self.config = config
        self.model = model or config.openai_model
        self.stage = stage
        self.usage = TokenUsage()
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            if not self.config.openai_api_key:
                raise RuntimeError("OPENAI_API_KEY 가 설정되지 않았습니다. .env 를 확인하세요.")
            from openai import OpenAI

            client = OpenAI(
                api_key=self.config.openai_api_key,
                base_url=self.config.openai_base_url,
            )
            self._client = wrap_client(client)  # LangSmith Observation
        return self._client

    def call(self, system: str, user: str, temperature: float = 0.2) -> str:
        client = self._ensure_client()
        resp = client.chat.completions.create(
            model=self.model,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self.usage.add(
                getattr(usage, "prompt_tokens", 0) or 0,
                getattr(usage, "completion_tokens", 0) or 0,
                stage=self.stage,
            )
        return resp.choices[0].message.content or ""

"""파이프라인 설정.

paper_review_config.json 을 읽어 실행 옵션을 만든다. (에이전트 스펙의 '입력' 항목)
- keywords        : 관심 키워드·주제 목록
- categories      : 수집 대상 arXiv 카테고리 (기본 cs.CR)
- days_back       : 수집 주기(일). 주 1회 실행이면 7
- max_candidates  : arXiv 검색으로 가져올 최대 후보 수
- max_summaries   : 이번 실행에서 요약할 최대 논문 수
- abstract_fallback : 본문 추출 실패 시 초록 기반 요약을 허용할지
                      (허용 시 반드시 '초록 기반 요약'으로 명시되고,
                       [추출 실패] 목록에도 함께 기재된다)

모델 관련 환경변수(.env):
- OPENAI_API_KEY  : 필수. OpenAI 호환 API 키
- OPENAI_BASE_URL : 선택. 기본 https://api.openai.com/v1
                    (레포처럼 OpenRouter를 쓰면 https://openrouter.ai/api/v1)
- OPENAI_MODEL    : 선택. 기본 gpt-4o-mini
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("paper_review_config.json")

DEFAULT_KEYWORDS = ["threat detection", "malware analysis", "LLM security"]
DEFAULT_CATEGORIES = ["cs.CR"]


@dataclass
class Config:
    keywords: list[str] = field(default_factory=lambda: list(DEFAULT_KEYWORDS))
    categories: list[str] = field(default_factory=lambda: list(DEFAULT_CATEGORIES))
    days_back: int = 7
    max_candidates: int = 40
    max_summaries: int = 10
    abstract_fallback: bool = True
    output_dir: Path = Path("output")
    archive_path: Path = Path("archive/summaries.jsonl")
    # LLM에 넘길 본문 텍스트 최대 길이(문자). 초과분은 잘라낸다.
    max_body_chars: int = 60_000

    # --- 모델 (환경변수에서 로드) ---
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        cfg = cls()
        p = Path(path) if path else DEFAULT_CONFIG_PATH
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            for key in (
                "keywords",
                "categories",
                "days_back",
                "max_candidates",
                "max_summaries",
                "abstract_fallback",
                "max_body_chars",
            ):
                if key in data:
                    setattr(cfg, key, data[key])
            if "output_dir" in data:
                cfg.output_dir = Path(data["output_dir"])
            if "archive_path" in data:
                cfg.archive_path = Path(data["archive_path"])

        cfg.openai_api_key = os.getenv("OPENAI_API_KEY") or None
        cfg.openai_base_url = os.getenv("OPENAI_BASE_URL", cfg.openai_base_url)
        cfg.openai_model = os.getenv("OPENAI_MODEL", cfg.openai_model)
        return cfg

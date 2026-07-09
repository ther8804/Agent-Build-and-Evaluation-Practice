"""요약 아카이브 (JSONL 누적 저장).

- 실행 전 아카이브를 조회해 이미 요약한 논문(base_id 기준)은 건너뛴다. (중복 방지)
- 이후 `python -m paper_review search <검색어>` 로 과거 요약을 검색할 수 있다.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .arxiv_client import Paper
from .summarizer import Summary


class Archive:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ids: set[str] = set()
        self._load_ids()

    def _load_ids(self) -> None:
        if not self.path.exists():
            return
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                base_id = rec.get("paper", {}).get("base_id")
                if base_id:
                    self._ids.add(base_id)

    def contains(self, base_id: str) -> bool:
        """이미 요약된 논문인지 확인한다."""
        return base_id in self._ids

    def add(self, paper: Paper, summary: Summary) -> None:
        record = {
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "paper": paper.to_dict(),
            "summary": summary.to_dict(),
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._ids.add(paper.base_id)

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """제목·초록·요약 텍스트에서 검색어(부분 일치, 대소문자 무시)를 찾는다."""
        q = query.lower()
        hits: list[dict] = []
        if not self.path.exists():
            return hits
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                haystack = json.dumps(rec, ensure_ascii=False).lower()
                if q in haystack:
                    hits.append(rec)
                    if len(hits) >= limit:
                        break
        return hits

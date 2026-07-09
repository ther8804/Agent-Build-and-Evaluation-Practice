"""arXiv 공개 API 클라이언트 (외부 MCP 없이 직접 호출).

- 카테고리(cs.CR 등) + 키워드로 신규 논문을 검색하고 날짜 범위로 필터링한다.
- 메타데이터(arXiv ID·제목·저자·게재일)는 항상 이 API 응답을 원본으로 삼는다.
  요약문 헤더는 코드가 이 메타데이터로 직접 렌더링하므로, LLM 출력이 메타데이터를
  덮어쓸 수 없다. ("arXiv ID·제목·저자·게재일이 실제 메타데이터와 일치" 규칙)
- arXiv 이용 정책에 따라 요청 사이 3초 지연을 둔다.
"""

from __future__ import annotations

import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

ARXIV_API_URL = "https://export.arxiv.org/api/query"
REQUEST_DELAY_SEC = 3.0  # arXiv 권장 요청 간격
USER_AGENT = "paper-review-agent/0.1 (research summarization; contact: set-me)"

# 예: 2401.12345 / 2401.12345v2 / cs/0703041v1 (구형 ID)
_ARXIV_ID_RE = re.compile(r"^(?:[a-z\-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?$")

_ATOM = {"atom": "http://www.w3.org/2005/Atom"}


@dataclass
class Paper:
    arxiv_id: str            # 버전 포함 (예: 2401.12345v1)
    title: str
    authors: list[str]
    published: str           # ISO8601 (최초 게재일)
    updated: str             # ISO8601
    abstract: str
    categories: list[str] = field(default_factory=list)
    abs_url: str = ""
    pdf_url: str = ""
    matched_keywords: list[str] = field(default_factory=list)

    @property
    def base_id(self) -> str:
        """버전 접미사(vN)를 뗀 ID. 아카이브 중복 확인 키로 사용."""
        return re.sub(r"v\d+$", "", self.arxiv_id)

    def to_dict(self) -> dict:
        return {
            "arxiv_id": self.arxiv_id,
            "base_id": self.base_id,
            "title": self.title,
            "authors": self.authors,
            "published": self.published,
            "updated": self.updated,
            "abstract": self.abstract,
            "categories": self.categories,
            "abs_url": self.abs_url,
            "pdf_url": self.pdf_url,
            "matched_keywords": self.matched_keywords,
        }


class MetadataError(ValueError):
    """API 메타데이터가 형식 검증에 실패한 경우."""


def verify_metadata(paper: Paper) -> None:
    """요약에 기재하기 전 메타데이터 형식 검증.

    - arXiv ID 형식이 유효한가
    - 제목/저자/게재일이 비어 있지 않은가
    - 링크가 arxiv.org 도메인인가 (무료 공개 원문만 취급)
    """
    if not _ARXIV_ID_RE.match(paper.arxiv_id):
        raise MetadataError(f"arXiv ID 형식 오류: {paper.arxiv_id!r}")
    if not paper.title.strip():
        raise MetadataError(f"{paper.arxiv_id}: 제목이 비어 있음")
    if not paper.authors:
        raise MetadataError(f"{paper.arxiv_id}: 저자 정보 없음")
    if not paper.published:
        raise MetadataError(f"{paper.arxiv_id}: 게재일 없음")
    for url in (paper.abs_url, paper.pdf_url):
        if url and not _is_arxiv_url(url):
            raise MetadataError(f"{paper.arxiv_id}: arxiv.org 외 링크 {url!r}")


def _is_arxiv_url(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return host == "arxiv.org" or host.endswith(".arxiv.org")


def build_search_query(categories: list[str], keywords: list[str]) -> str:
    """arXiv search_query 문자열 조립.

    (cat:cs.CR) AND (all:"kw1" OR all:"kw2" ...)
    키워드가 없으면 카테고리 전체 신규 논문을 대상으로 한다.
    """
    cat_expr = " OR ".join(f"cat:{c}" for c in categories) or "cat:cs.CR"
    if not keywords:
        return f"({cat_expr})"
    kw_expr = " OR ".join(f'all:"{k}"' for k in keywords)
    return f"({cat_expr}) AND ({kw_expr})"


def parse_atom(xml_text: str) -> list[Paper]:
    """arXiv Atom 응답을 Paper 목록으로 파싱한다."""
    root = ET.fromstring(xml_text)
    papers: list[Paper] = []
    for entry in root.findall("atom:entry", _ATOM):
        raw_id = (entry.findtext("atom:id", "", _ATOM) or "").strip()
        # 예: http://arxiv.org/abs/2401.12345v1 → 2401.12345v1
        arxiv_id = raw_id.rsplit("/abs/", 1)[-1] if "/abs/" in raw_id else raw_id
        title = " ".join((entry.findtext("atom:title", "", _ATOM) or "").split())
        abstract = " ".join((entry.findtext("atom:summary", "", _ATOM) or "").split())
        published = (entry.findtext("atom:published", "", _ATOM) or "").strip()
        updated = (entry.findtext("atom:updated", "", _ATOM) or "").strip()
        authors = [
            (a.findtext("atom:name", "", _ATOM) or "").strip()
            for a in entry.findall("atom:author", _ATOM)
        ]
        authors = [a for a in authors if a]
        categories = [
            c.get("term", "")
            for c in entry.findall("{http://www.w3.org/2005/Atom}category")
            if c.get("term")
        ]
        abs_url, pdf_url = "", ""
        for link in entry.findall("atom:link", _ATOM):
            href = link.get("href", "")
            if link.get("title") == "pdf":
                pdf_url = href
            elif link.get("rel") == "alternate":
                abs_url = href
        if not abs_url and arxiv_id:
            abs_url = f"https://arxiv.org/abs/{arxiv_id}"
        if not pdf_url and arxiv_id:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        papers.append(
            Paper(
                arxiv_id=arxiv_id,
                title=title,
                authors=authors,
                published=published,
                updated=updated,
                abstract=abstract,
                categories=categories,
                abs_url=abs_url,
                pdf_url=pdf_url,
            )
        )
    return papers


def _match_keywords(paper: Paper, keywords: list[str]) -> list[str]:
    text = f"{paper.title} {paper.abstract}".lower()
    return [k for k in keywords if k.lower() in text]


def search_new_papers(
    categories: list[str],
    keywords: list[str],
    days_back: int = 7,
    max_results: int = 40,
    session: requests.Session | None = None,
) -> list[Paper]:
    """최근 days_back 일 이내 게재/갱신된 논문을 검색한다."""
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", USER_AGENT)

    params = {
        "search_query": build_search_query(categories, keywords),
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    resp = sess.get(ARXIV_API_URL, params=params, timeout=30)
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY_SEC)  # arXiv 정책: 다음 요청 전 지연

    papers = parse_atom(resp.text)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    recent: list[Paper] = []
    for p in papers:
        try:
            pub = datetime.fromisoformat(p.published.replace("Z", "+00:00"))
        except ValueError:
            continue
        if pub >= cutoff:
            p.matched_keywords = _match_keywords(p, keywords)
            recent.append(p)
    return recent

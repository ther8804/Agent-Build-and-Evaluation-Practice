"""PDF 다운로드 및 본문 텍스트 추출.

규칙 반영:
- 다운로드는 arxiv.org 도메인의 무료 공개 PDF 만 허용한다.
  (유료·비공개 논문 우회 다운로드 금지 — 도메인 화이트리스트로 강제)
- 추출 실패는 예외로 삼키지 않고 상태로 반환해, 파이프라인이 해당 논문을
  [추출 실패] 목록으로 분리하거나 '초록 기반 요약'으로 대체할 수 있게 한다.
"""

from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import requests

from .arxiv_client import REQUEST_DELAY_SEC, USER_AGENT

ALLOWED_PDF_HOSTS = ("arxiv.org",)  # 서브도메인(export.arxiv.org 등) 포함 허용


class DisallowedSourceError(ValueError):
    """arxiv.org 외 출처의 PDF 다운로드 시도."""


@dataclass
class ExtractionResult:
    ok: bool
    text: str = ""
    num_pages: int = 0
    error: str = ""


def _check_host(url: str) -> None:
    host = urllib.parse.urlparse(url).hostname or ""
    if not any(host == h or host.endswith("." + h) for h in ALLOWED_PDF_HOSTS):
        raise DisallowedSourceError(
            f"무료 공개(arXiv) 출처가 아닌 PDF는 다운로드하지 않습니다: {url}"
        )


def download_pdf(
    pdf_url: str,
    dest: Path,
    session: requests.Session | None = None,
    max_bytes: int = 50 * 1024 * 1024,
) -> Path:
    """arXiv PDF를 dest 경로에 내려받는다. arxiv.org 외 도메인은 거부."""
    _check_host(pdf_url)
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", USER_AGENT)

    dest.parent.mkdir(parents=True, exist_ok=True)
    with sess.get(pdf_url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        size = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"PDF가 {max_bytes} bytes 제한을 초과: {pdf_url}")
                f.write(chunk)
    time.sleep(REQUEST_DELAY_SEC)  # arXiv 정책: 요청 간 지연
    return dest


def extract_text(pdf_path: Path, max_chars: int = 60_000) -> ExtractionResult:
    """pypdf로 본문 텍스트를 추출한다. 실패해도 예외 대신 상태를 반환."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ExtractionResult(ok=False, error="pypdf 미설치 (pip install pypdf)")

    try:
        reader = PdfReader(str(pdf_path))
        pages: list[str] = []
        total = 0
        for page in reader.pages:
            t = page.extract_text() or ""
            pages.append(t)
            total += len(t)
            if total >= max_chars:
                break
        text = "\n".join(pages).strip()
        if len(text) < 200:  # 스캔본 등: 텍스트 레이어가 사실상 없음
            return ExtractionResult(
                ok=False,
                num_pages=len(reader.pages),
                error="추출된 텍스트가 너무 짧음(스캔본 또는 추출 불가 PDF)",
            )
        return ExtractionResult(ok=True, text=text[:max_chars], num_pages=len(reader.pages))
    except Exception as e:  # noqa: BLE001 — 실패 사유를 목록에 남기는 것이 목적
        return ExtractionResult(ok=False, error=f"{type(e).__name__}: {e}")

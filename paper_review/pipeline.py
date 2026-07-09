"""파이프라인 오케스트레이션.

run(config) 한 번 = 수집 주기 1회 실행:
  1. arXiv 검색 (카테고리 + 키워드, 최근 days_back 일)
  2. 아카이브 조회 → 이미 요약한 논문 제외 (중복 방지 규칙)
  3. 메타데이터 검증 (arXiv ID·제목·저자·게재일 형식/존재 확인)
  4. PDF 다운로드(arxiv.org 한정) + 본문 추출
     - 실패 시: abstract_fallback=True 면 '초록 기반 요약'으로 대체(라벨 명시),
       아니면 [추출 실패] 목록으로만 분리
  5. LLM 요약 (도구 없음·JSON 고정 출력 — 프롬프트 인젝션 방어)
  6. 아카이브 적재
  7. 주간 브리핑 생성: paper_brief_YYYYMMDD.docx + .md (로컬 저장만)

이 파이프라인은 어떤 발송·공유도 하지 않는다. 생성된 문서의 팀 공유는
사람이 검토·승인한 뒤 별도로 수행한다. (자동 발송·공유 금지 규칙)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import requests

from .archive import Archive
from .arxiv_client import MetadataError, Paper, search_new_papers, verify_metadata
from .briefing import BriefingData, FailedItem, briefing_paths, render_docx, render_markdown
from .config import Config
from .observability import traceable, tracing_enabled
from .pdf_extract import download_pdf, extract_text
from .summarizer import Summarizer, Summary


@dataclass
class RunResult:
    briefing_docx: Path | None = None
    briefing_md: Path | None = None
    summarized: list[tuple[Paper, Summary]] = field(default_factory=list)
    failed: list[FailedItem] = field(default_factory=list)
    skipped_duplicates: list[Paper] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


@traceable(run_type="chain", name="paper_review_pipeline")
def run(
    config: Config,
    papers: list[Paper] | None = None,
    summarizer: Summarizer | None = None,
    fetch_body=None,
) -> RunResult:
    """파이프라인 1회 실행.

    papers / summarizer / fetch_body 를 주입하면 네트워크·LLM 없이도
    오프라인 테스트(selftest)가 가능하다.
    """
    result = RunResult()
    log = result.log.append

    # 1) 검색
    if papers is None:
        log(f"arXiv 검색: categories={config.categories}, keywords={config.keywords}, "
            f"최근 {config.days_back}일, 최대 {config.max_candidates}건")
        papers = search_new_papers(
            categories=config.categories,
            keywords=config.keywords,
            days_back=config.days_back,
            max_results=config.max_candidates,
        )
    log(f"후보 논문 {len(papers)}건")

    archive = Archive(config.archive_path)
    summarizer = summarizer or Summarizer(config)
    session = requests.Session()
    pdf_dir = config.output_dir / "pdf"

    if fetch_body is None:
        def fetch_body(paper: Paper) -> tuple[str | None, str]:
            """(본문 텍스트 | None, 실패 사유) 를 반환한다."""
            try:
                pdf_path = download_pdf(
                    paper.pdf_url, pdf_dir / f"{paper.base_id.replace('/', '_')}.pdf",
                    session=session,
                )
            except Exception as e:  # noqa: BLE001
                return None, f"PDF 다운로드 실패: {e}"
            res = extract_text(pdf_path, max_chars=config.max_body_chars)
            if not res.ok:
                return None, f"본문 추출 실패: {res.error}"
            return res.text, ""

    count = 0
    for paper in papers:
        if count >= config.max_summaries:
            log(f"max_summaries={config.max_summaries} 도달 — 나머지 후보는 다음 주기로 이월")
            break

        # 2) 중복 확인
        if archive.contains(paper.base_id):
            result.skipped_duplicates.append(paper)
            log(f"[중복 제외] {paper.arxiv_id} — 아카이브에 기존 요약 존재")
            continue

        # 3) 메타데이터 검증
        try:
            verify_metadata(paper)
        except MetadataError as e:
            result.failed.append(FailedItem(paper=paper, reason=f"메타데이터 검증 실패: {e}"))
            log(f"[검증 실패] {paper.arxiv_id}: {e}")
            continue

        # 4) 본문 확보
        body, fail_reason = fetch_body(paper)
        fallback_used = False
        if body is None:
            if config.abstract_fallback and paper.abstract.strip():
                fallback_used = True
                log(f"[초록 기반 요약] {paper.arxiv_id}: {fail_reason}")
            else:
                result.failed.append(FailedItem(paper=paper, reason=fail_reason))
                log(f"[추출 실패] {paper.arxiv_id}: {fail_reason}")
                continue

        # 5) 요약
        try:
            summary = summarizer.summarize(paper, body)
        except Exception as e:  # noqa: BLE001 — 논문 1건 실패가 전체를 멈추지 않게
            result.failed.append(FailedItem(paper=paper, reason=f"요약 실패: {e}"))
            log(f"[요약 실패] {paper.arxiv_id}: {e}")
            continue

        # 초록 기반 요약은 [추출 실패] 목록에도 사유와 함께 기재 (규칙 준수)
        if fallback_used:
            result.failed.append(
                FailedItem(paper=paper, reason=fail_reason, fallback_used=True)
            )

        # 6) 아카이브 적재
        archive.add(paper, summary)
        result.summarized.append((paper, summary))
        count += 1
        log(f"[요약 완료] {paper.arxiv_id} (관심도 {summary.interest_score}/5, {summary.basis})")

    # 7) 브리핑 생성 (로컬 저장만 — 자동 발송·공유 없음)
    today = date.today()
    period = f"{(today - timedelta(days=config.days_back)).isoformat()} ~ {today.isoformat()}"
    data = BriefingData(
        period_label=period,
        keywords=config.keywords,
        categories=config.categories,
        items=result.summarized,
        failed=result.failed,
        skipped_duplicates=result.skipped_duplicates,
    )
    docx_path, md_path = briefing_paths(config.output_dir, today)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_markdown(data), encoding="utf-8")
    render_docx(data, docx_path)
    result.briefing_docx, result.briefing_md = docx_path, md_path
    log(f"브리핑 저장: {docx_path} / {md_path}")
    # Observation: 토큰 사용량 집계 (논문별 사용량은 아카이브 레코드에 기록됨)
    for line in summarizer.usage.summary_line().splitlines():
        log(line)
    if tracing_enabled():
        import os
        log(f"LangSmith 트레이싱 활성 — 프로젝트: {os.getenv('LANGSMITH_PROJECT', '(기본)')}")
    log("주의: 문서 공유는 사람 검토·승인 후 별도로 진행하세요. (자동 발송 없음)")
    return result

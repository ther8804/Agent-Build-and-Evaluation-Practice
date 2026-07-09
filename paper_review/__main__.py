"""CLI 진입점.

사용법:
  python -m paper_review run [--config paper_review_config.json]
      주 1회 수집·요약·브리핑 생성 (cron/스케줄러에 등록해 사용)

  python -m paper_review search "검색어"
      요약 아카이브에서 과거 요약 검색

  python -m paper_review selftest
      네트워크·API 키 없이 파이프라인 전체를 픽스처 데이터로 검증
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import dotenv

from .config import Config


def cmd_run(args: argparse.Namespace) -> int:
    from .pipeline import run

    config = Config.load(args.config)
    if not config.openai_api_key:
        print("오류: OPENAI_API_KEY 가 설정되지 않았습니다. .env 를 확인하세요.", file=sys.stderr)
        return 1
    result = run(config)
    for line in result.log:
        print(f"[log] {line}")
    print()
    print(f"요약 {len(result.summarized)}편 · 추출 실패 {len(result.failed)}건 · "
          f"중복 제외 {len(result.skipped_duplicates)}건")
    if result.briefing_docx:
        print(f"브리핑: {result.briefing_docx}")
        print(f"        {result.briefing_md}")
        print("→ 팀 공유 전 반드시 사람이 검토·승인하세요.")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from .archive import Archive

    config = Config.load(args.config)
    archive = Archive(config.archive_path)
    hits = archive.search(args.query, limit=args.limit)
    if not hits:
        print(f"'{args.query}' 에 대한 아카이브 검색 결과가 없습니다.")
        return 0
    for rec in hits:
        p, s = rec.get("paper", {}), rec.get("summary", {})
        print(f"- [{p.get('arxiv_id')}] {p.get('title')}")
        print(f"  게재일 {p.get('published', '')[:10]} · 관심도 {s.get('interest_score')}/5 "
              f"· {s.get('basis')}")
        for c in (s.get("key_contributions") or [])[:2]:
            print(f"  · {c}")
        print(f"  원문: {p.get('abs_url')}")
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    """네트워크·LLM 없이 전체 파이프라인을 검증한다."""
    import tempfile

    from .arxiv_client import parse_atom
    from .pipeline import run
    from .summarizer import Summarizer

    fixture = Path(__file__).parent / "fixtures" / "arxiv_sample.xml"
    papers = parse_atom(fixture.read_text(encoding="utf-8"))
    print(f"[selftest] 픽스처 논문 {len(papers)}편 파싱")

    def mock_llm(system: str, user: str) -> str:
        assert "<PAPER_DATA>" in user, "본문 구분자 누락"
        return json.dumps(
            {
                "key_contributions": [
                    "저자들은 새로운 LLM 기반 위협 탐지 파이프라인을 제안했다고 주장한다.",
                    "공개 벤치마크에서 기존 대비 개선을 보고한다(수치는 논문 참조).",
                ],
                "methodology": "저자들은 로그 시퀀스를 임베딩한 뒤 분류기를 학습하는 "
                               "2단계 접근을 사용했다고 보고한다.",
                "practical_points": [
                    "SOC 로그 파이프라인의 보조 탐지 계층으로 검토할 만하다.",
                    "사내 데이터로 재검증 전에는 프로덕션 적용을 보류할 것을 권한다.",
                ],
                "limitations": ["평가가 공개 데이터셋에 한정된다고 저자들이 명시한다."],
                "interest_score": 4,
                "interest_reason": "관심 키워드 'threat detection'과 직접 관련.",
            },
            ensure_ascii=False,
        )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        config = Config()
        config.output_dir = tmp_path / "output"
        config.archive_path = tmp_path / "archive" / "summaries.jsonl"
        config.days_back = 100_000  # 픽스처 날짜가 과거여도 통과되도록

        def fake_fetch(paper):
            if paper.base_id == "2501.00002":  # 픽스처의 추출 실패 케이스
                return None, "본문 추출 실패: 텍스트 레이어 없음(테스트)"
            return ("This paper proposes a threat detection pipeline. " * 30), ""

        result = run(
            config,
            papers=papers,
            summarizer=Summarizer(config, llm_call=mock_llm),
            fetch_body=fake_fetch,
        )
        # 검증
        assert result.briefing_docx and result.briefing_docx.exists(), "docx 미생성"
        assert result.briefing_md and result.briefing_md.exists(), "md 미생성"
        assert result.briefing_docx.name.startswith("paper_brief_"), "파일명 규칙 위반"
        md = result.briefing_md.read_text(encoding="utf-8")
        assert "실무 적용 포인트" in md and "에이전트 의견" in md
        assert "[추출 실패]" in md
        assert "초록 기반 요약" in md
        assert "검토·승인" in md
        # 중복 방지: 같은 논문으로 재실행하면 전부 중복 제외돼야 한다
        result2 = run(
            config,
            papers=papers,
            summarizer=Summarizer(config, llm_call=mock_llm),
            fetch_body=fake_fetch,
        )
        assert len(result2.summarized) == 0, "중복 방지 실패"
        assert len(result2.skipped_duplicates) >= 1, "중복 목록 누락"

        for line in result.log:
            print(f"[selftest] {line}")
        print(f"[selftest] 재실행 중복 제외 {len(result2.skipped_duplicates)}건 확인")
    print("[selftest] OK — 파이프라인·규칙 검증 통과")
    return 0


def main(argv: list[str] | None = None) -> int:
    dotenv.load_dotenv()
    parser = argparse.ArgumentParser(prog="paper_review",
                                     description="신규 정보보호 논문 리뷰·요약 에이전트")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="수집·요약·브리핑 1회 실행")
    p_run.add_argument("--config", default=None, help="설정 JSON 경로")
    p_run.set_defaults(func=cmd_run)

    p_search = sub.add_parser("search", help="요약 아카이브 검색")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=10)
    p_search.add_argument("--config", default=None)
    p_search.set_defaults(func=cmd_search)

    p_test = sub.add_parser("selftest", help="오프라인 파이프라인 검증")
    p_test.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

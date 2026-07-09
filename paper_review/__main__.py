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


def cmd_eval(args: argparse.Namespace) -> int:
    """질문-평가기준 세트로 에이전트를 평가한다 (결정적 검사 + LLM-as-a-Judge)."""
    from .evaluate import evaluate_agent, load_dataset, render_report_md, save_report

    config = Config.load(args.config)
    dataset = load_dataset(args.dataset)

    if args.offline:
        from .meta_harness import offline_mocks
        from .summarizer import Summarizer

        agent, judge, _ = offline_mocks()
        report = evaluate_agent(config, dataset,
                                summarizer=Summarizer(config, llm_call=agent),
                                judge_call=judge, label=args.label)
    else:
        if not config.openai_api_key:
            print("오류: OPENAI_API_KEY 가 설정되지 않았습니다. (--offline 으로 모의 실행 가능)",
                  file=sys.stderr)
            return 1
        report = evaluate_agent(config, dataset, label=args.label)

    jp, mp = save_report(report)
    print(render_report_md(report))
    print(f"\n리포트 저장: {jp}\n           {mp}")
    return 0 if report.gate_passed else 2


def cmd_harness(args: argparse.Namespace) -> int:
    """meta-harness: baseline↔candidate 격리 평가·비교 후 확실한 우위만 반영."""
    from . import meta_harness as mh

    config = Config.load(args.config)

    if args.harness_command == "promote":
        ok = mh.promote(Path(args.candidate), config.prompt_path, yes=args.yes)
        return 0 if ok else 1

    if args.harness_command == "history":
        records = mh.read_history()
        if not records:
            print("개선 이력이 없습니다. (meta/history.jsonl)")
            return 0
        for r in records:
            mark = "🏆" if r.get("verdict") == "win" else "🤝"
            print(f"{mark} {r['ts']}  base {r['base_score']} → cand {r['cand_score']} "
                  f"[{r['verdict']}]" + (f"  → {r.get('candidate_file')}" if r.get("candidate_file") else ""))
            for reason in r.get("reasons", []):
                print(f"    {reason}")
        return 0

    # harness run
    kwargs = {}
    if args.offline:
        agent, judge, improver = mh.offline_mocks()
        kwargs = {"agent_llm_call": agent, "judge_call": judge, "improver_call": improver}
    elif not config.openai_api_key:
        print("오류: OPENAI_API_KEY 가 설정되지 않았습니다. (--offline 으로 모의 실행 가능)",
              file=sys.stderr)
        return 1

    result = mh.harness_run(
        config,
        dataset_path=Path(args.dataset) if args.dataset else None,
        margin=args.margin,
        auto_promote=args.auto_promote,
        **kwargs,
    )
    for line in result.log:
        print(f"[harness] {line}")
    print(f"\n판정: {'🏆 candidate 승리' if result.verdict.is_win else '🤝 무승부 (baseline 유지)'}")
    if result.candidate_path and not result.promoted:
        print(f"승인 후 반영: python -m paper_review harness promote {result.candidate_path} --yes")
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
        # 실제 설정(report 폴더)을 따르되, 아카이브는 임시 폴더를 써서
        # 진짜 아카이브를 오염시키지 않는다.
        config = Config.load(getattr(args, "config", None))
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
        # (재실행 브리핑은 임시 폴더에 써서 report 의 샘플을 덮어쓰지 않는다)
        config2 = Config.load(getattr(args, "config", None))
        config2.archive_path = config.archive_path
        config2.output_dir = tmp_path / "output2"
        config2.days_back = 100_000
        result2 = run(
            config2,
            papers=papers,
            summarizer=Summarizer(config2, llm_call=mock_llm),
            fetch_body=fake_fetch,
        )
        assert len(result2.summarized) == 0, "중복 방지 실패"
        assert len(result2.skipped_duplicates) >= 1, "중복 목록 누락"

        for line in result.log:
            print(f"[selftest] {line}")
        print(f"[selftest] 재실행 중복 제외 {len(result2.skipped_duplicates)}건 확인")
        print(f"[selftest] 샘플 브리핑 저장 위치: {result.briefing_docx} / {result.briefing_md}")
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

    p_eval = sub.add_parser("eval", help="질문-평가기준 세트로 평가 (check + LLM-as-a-Judge)")
    p_eval.add_argument("--dataset", default=None, help="평가 데이터셋 JSON 경로")
    p_eval.add_argument("--label", default="manual", help="리포트 라벨")
    p_eval.add_argument("--offline", action="store_true", help="모의 LLM 으로 실행(키 불필요)")
    p_eval.add_argument("--config", default=None)
    p_eval.set_defaults(func=cmd_eval)

    p_h = sub.add_parser("harness", help="meta-harness: 격리 평가·비교·개선")
    h_sub = p_h.add_subparsers(dest="harness_command", required=True)
    h_run = h_sub.add_parser("run", help="baseline↔candidate 1라운드 실행")
    h_run.add_argument("--dataset", default=None)
    h_run.add_argument("--margin", type=float, default=0.15,
                       help="승리로 인정할 최소 종합점수 개선폭 (기본 0.15)")
    h_run.add_argument("--auto-promote", action="store_true",
                       help="승리 시 사람 승인 없이 본체 프롬프트에 즉시 반영(주의)")
    h_run.add_argument("--offline", action="store_true", help="모의 LLM 으로 실행(키 불필요)")
    h_run.add_argument("--config", default=None)
    h_run.set_defaults(func=cmd_harness)
    h_promote = h_sub.add_parser("promote", help="승리 candidate 를 본체 프롬프트에 반영")
    h_promote.add_argument("candidate", help="prompts/candidates/ 아래 candidate 파일")
    h_promote.add_argument("--yes", action="store_true", help="diff 미리보기 없이 실제 반영")
    h_promote.add_argument("--config", default=None)
    h_promote.set_defaults(func=cmd_harness)
    h_hist = h_sub.add_parser("history", help="개선 이력(meta/history.jsonl) 조회")
    h_hist.add_argument("--config", default=None)
    h_hist.set_defaults(func=cmd_harness)

    p_test = sub.add_parser("selftest", help="오프라인 파이프라인 검증")
    p_test.add_argument("--config", default=None, help="설정 JSON 경로")
    p_test.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

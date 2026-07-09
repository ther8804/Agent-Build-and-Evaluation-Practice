"""Meta-Harness — 에이전트가 자기 자신을 격리 실행·수정·비교해 개선한다.

레포의 meta-harness 스킬 철학을 이 에이전트에 맞게 구현한 것:

  1. 본체의 '노브'(요약기 시스템 프롬프트, prompts/summarizer_system.txt)를
     격리된 baseline 으로 읽는다. 본체 파일은 promote 전까지 절대 바뀌지 않는다.
  2. baseline 을 '질문-평가기준 세트'(evaluation/eval_dataset.json)로 헤드리스
     평가한다. (요약기 실행 → 결정적 검사 + LLM-as-a-Judge 채점)
  3. 개선자(improver) LLM 이 baseline 프롬프트 + 평가 리포트의 약점을 보고
     candidate 프롬프트를 제안한다. 필수 규칙 앵커(인젝션 방어·날조 금지 등)가
     하나라도 빠지면 후보는 즉시 거부된다 — 안전 규칙은 개선 대상이 아니다.
  4. candidate 를 같은 세트로 평가하고 baseline 과 비교한다.
  5. **확실한 우위일 때만 승리, 애매하면 무승부(=본체 유지)가 기본값이다.**
     승리 조건: 종합 점수 +margin 이상 개선 AND 필수 게이트 통과 AND
     어떤 필수 기준도 회귀하지 않음. 하나라도 어기면 무승부.
  6. 승리한 candidate 는 prompts/candidates/ 에 근거 리포트와 함께 저장되고,
     meta/history.jsonl 에 증거가 기록된다. 본체 반영(promote)은 기본적으로
     사람이 diff 를 보고 승인하는 별도 명령이다. (--auto-promote 로만 자동화)

즉, harness 를 손으로 튜닝하는 대신 '평가 근거'를 만들어 개선한다.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Config
from .evaluate import EvalReport, evaluate_agent, load_dataset, render_report_md, save_report
from .summarizer import (
    DEFAULT_SYSTEM_PROMPT,
    REQUIRED_PROMPT_MARKERS,
    Summarizer,
    load_system_prompt,
    validate_system_prompt,
)

CANDIDATES_DIR = Path("prompts/candidates")
BACKUPS_DIR = Path("prompts/backups")
HISTORY_PATH = Path("meta/history.jsonl")

IMPROVER_SYSTEM_PROMPT = """당신은 정보보호 논문 요약 에이전트의 시스템 프롬프트를 개선하는 프롬프트 엔지니어다.

[입력]
- 현재(baseline) 시스템 프롬프트 전문과, 그 프롬프트로 실행한 평가 리포트(기준별 점수·판정 근거)가 주어진다.

[개선 원칙]
- 평가 리포트에서 점수가 낮거나 실패한 기준의 '구체적 원인'을 진단하고, 그것을 고치는
  최소 변경을 가한다. 잘 되던 부분과 안전 규칙은 절대 약화하지 않는다.
- 다음 요소는 반드시 유지·보존한다 (삭제·약화 금지):
  인젝션 방어(<PAPER_DATA> 데이터 취급), 날조 금지, "저자들은 ~" 주장·사실 구분,
  원문 복사 금지, JSON 출력 계약과 키(key_contributions, methodology,
  practical_points, limitations, interest_score, interest_reason).
- 한 번에 한두 가지 가설만 반영한다. 프롬프트를 통째로 다시 쓰지 않는다.

[출력]
- 개선된 시스템 프롬프트 '전문'만 출력한다. 설명·코드펜스·주석 금지.
"""

IMPROVER_USER_TEMPLATE = """[baseline 시스템 프롬프트]
{baseline}

[평가 리포트 (baseline 실행 결과)]
{report_md}

위 리포트의 약점을 고친 개선 프롬프트 전문을 출력하라."""


# ---------------------------------------------------------------------------
def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def _must_pass_regressions(base: EvalReport, cand: EvalReport, dataset: dict) -> list[str]:
    """baseline 에서 통과하던 필수 기준이 candidate 에서 실패로 회귀했는지."""
    must_ids = {c["id"] for c in dataset["criteria"] if c.get("must_pass")}

    def passed_map(r: EvalReport) -> dict[tuple[str, str], bool]:
        out = {}
        for case in r.cases:
            for c in case.criteria:
                if c.id in must_ids:
                    out[(case.case_id, c.id)] = c.passed
        return out

    b, c = passed_map(base), passed_map(cand)
    return [f"{k[0]}/{k[1]}" for k in b if b[k] and not c.get(k, False)]


@dataclass
class Verdict:
    result: str                  # "win" | "draw"
    reasons: list[str] = field(default_factory=list)

    @property
    def is_win(self) -> bool:
        return self.result == "win"


def decide(base: EvalReport, cand: EvalReport, dataset: dict, margin: float) -> Verdict:
    """판정 — 기본값은 무승부. 아래를 전부 만족할 때만 승리다."""
    reasons: list[str] = []
    delta = cand.overall_score - base.overall_score

    if not cand.gate_passed:
        return Verdict("draw", [f"candidate 필수 게이트 실패 (Δ={delta:+.2f}) → 무승부"])
    regs = _must_pass_regressions(base, cand, dataset)
    if regs:
        return Verdict("draw", [f"필수 기준 회귀 발생: {regs} → 무승부"])
    if delta < margin:
        return Verdict("draw", [
            f"종합 점수 개선 폭 부족: Δ={delta:+.2f} < margin {margin} "
            f"(base {base.overall_score:.2f} → cand {cand.overall_score:.2f}) → 무승부"
        ])

    reasons.append(f"종합 점수 {base.overall_score:.2f} → {cand.overall_score:.2f} (Δ={delta:+.2f} ≥ {margin})")
    reasons.append("candidate 필수 게이트 통과, 필수 기준 회귀 없음")
    bm, cm = base.criterion_means(), cand.criterion_means()
    for cid in sorted(set(bm) | set(cm)):
        d = cm.get(cid, 0) - bm.get(cid, 0)
        if abs(d) >= 0.05:
            reasons.append(f"  {cid}: {bm.get(cid, 0):.2f} → {cm.get(cid, 0):.2f} ({d:+.2f})")
    return Verdict("win", reasons)


# ---------------------------------------------------------------------------
@dataclass
class HarnessResult:
    verdict: Verdict
    base_report: EvalReport
    cand_report: EvalReport | None
    candidate_path: Path | None
    promoted: bool
    log: list[str] = field(default_factory=list)


def harness_run(
    config: Config,
    dataset_path: Path | None = None,
    margin: float = 0.15,
    auto_promote: bool = False,
    results_dir: Path | None = None,
    # --- 오프라인/테스트 주입점 ---
    agent_llm_call=None,
    judge_call=None,
    improver_call=None,
    live_prompt_path: Path | None = None,
) -> HarnessResult:
    dataset = load_dataset(dataset_path)
    live_path = live_prompt_path or config.prompt_path
    log: list[str] = []

    # 1) baseline (격리: 본체 파일은 읽기만 한다)
    baseline_prompt, source = load_system_prompt(live_path)
    log.append(f"baseline 프롬프트: {source} (sha1 {_sha1(baseline_prompt)})")

    base_summarizer = Summarizer(config, llm_call=agent_llm_call, system_prompt=baseline_prompt)
    base_report = evaluate_agent(config, dataset, summarizer=base_summarizer,
                                 judge_call=judge_call, label="baseline")
    log.append(f"baseline 평가: {base_report.overall_score:.2f}/5, "
               f"게이트 {'통과' if base_report.gate_passed else '실패'}")

    # 2) improver 가 candidate 제안
    if improver_call is None:
        from .llm import ChatClient

        improver_client = ChatClient(config, model=config.openai_improver_model or None,
                                     stage="improver")
        improver_call = improver_client.call
    cand_prompt = improver_call(
        IMPROVER_SYSTEM_PROMPT,
        IMPROVER_USER_TEMPLATE.format(
            baseline=baseline_prompt, report_md=render_report_md(base_report)
        ),
    ).strip()
    cand_prompt = cand_prompt.strip("`").strip()

    # 3) 안전 검증: 필수 규칙 앵커가 빠진 후보는 평가조차 하지 않고 거부
    missing = validate_system_prompt(cand_prompt)
    if missing:
        log.append(f"candidate 거부 — 필수 규칙 앵커 누락: {missing} (안전 규칙은 개선 대상이 아님)")
        return HarnessResult(
            Verdict("draw", [f"candidate 검증 실패(앵커 누락 {missing}) → 무승부, baseline 유지"]),
            base_report, None, None, promoted=False, log=log,
        )
    if cand_prompt.strip() == baseline_prompt.strip():
        log.append("candidate 가 baseline 과 동일 — 무승부")
        return HarnessResult(
            Verdict("draw", ["candidate 가 baseline 과 동일 → 무승부"]),
            base_report, None, None, promoted=False, log=log,
        )
    log.append(f"candidate 제안됨 (sha1 {_sha1(cand_prompt)})")

    # 4) candidate 평가 (격리: system_prompt 주입, 본체 파일 미변경)
    cand_summarizer = Summarizer(config, llm_call=agent_llm_call, system_prompt=cand_prompt)
    cand_report = evaluate_agent(config, dataset, summarizer=cand_summarizer,
                                 judge_call=judge_call, label="candidate")
    log.append(f"candidate 평가: {cand_report.overall_score:.2f}/5, "
               f"게이트 {'통과' if cand_report.gate_passed else '실패'}")

    # 5) 판정 (기본값 = 무승부)
    verdict = decide(base_report, cand_report, dataset, margin)
    log.extend(verdict.reasons)

    # 리포트 저장 (근거 보존)
    if results_dir is not False:  # type: ignore[comparison-overlap]
        save_report(base_report, results_dir)
        save_report(cand_report, results_dir)

    candidate_path: Path | None = None
    promoted = False
    if verdict.is_win:
        # 6) 승리 → 후보 저장 + 증거 기록. 본체 반영은 승인(promote) 단계에서.
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        cdir = (results_dir / ".." / "candidates" if results_dir else CANDIDATES_DIR)
        cdir = Path(cdir).resolve()
        cdir.mkdir(parents=True, exist_ok=True)
        candidate_path = cdir / f"candidate_{ts}_{_sha1(cand_prompt)}.txt"
        candidate_path.write_text(cand_prompt, encoding="utf-8")
        log.append(f"승리 candidate 저장: {candidate_path}")

        _append_history({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "baseline_sha1": _sha1(baseline_prompt),
            "candidate_sha1": _sha1(cand_prompt),
            "candidate_file": str(candidate_path),
            "base_score": round(base_report.overall_score, 3),
            "cand_score": round(cand_report.overall_score, 3),
            "base_criteria": base_report.criterion_means(),
            "cand_criteria": cand_report.criterion_means(),
            "verdict": "win",
            "reasons": verdict.reasons,
            "auto_promote": auto_promote,
        })

        if auto_promote:
            promote(candidate_path, live_path, yes=True)
            promoted = True
            log.append(f"--auto-promote: 본체({live_path})에 반영 완료 (백업 생성됨)")
        else:
            log.append("본체 미반영 — diff 확인 후 promote 하세요: "
                       f"python -m paper_review harness promote {candidate_path}")
    else:
        _append_history({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "baseline_sha1": _sha1(baseline_prompt),
            "candidate_sha1": _sha1(cand_prompt),
            "base_score": round(base_report.overall_score, 3),
            "cand_score": round(cand_report.overall_score, 3),
            "verdict": "draw",
            "reasons": verdict.reasons,
        })
        log.append("무승부 → baseline(본체) 유지")

    return HarnessResult(verdict, base_report, cand_report, candidate_path, promoted, log)


def _append_history(record: dict) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
def diff_text(a: str, b: str, a_name: str = "baseline", b_name: str = "candidate") -> str:
    return "".join(difflib.unified_diff(
        a.splitlines(keepends=True), b.splitlines(keepends=True),
        fromfile=a_name, tofile=b_name,
    ))


def promote(candidate_path: Path, live_path: Path, yes: bool = False) -> bool:
    """candidate 프롬프트를 본체에 반영한다. (본체에 쓰는 유일한 경로)

    - 필수 규칙 앵커 재검증 → 실패 시 거부
    - 기존 본체 프롬프트는 prompts/backups/ 에 백업 (롤백용)
    - yes=False 면 diff 미리보기만 출력하고 반영하지 않는다
    """
    cand = Path(candidate_path).read_text(encoding="utf-8")
    missing = validate_system_prompt(cand)
    if missing:
        print(f"promote 거부 — 필수 규칙 앵커 누락: {missing}")
        return False

    live_path = Path(live_path)
    current = (live_path.read_text(encoding="utf-8")
               if live_path.exists() else DEFAULT_SYSTEM_PROMPT)
    print(diff_text(current, cand, str(live_path), str(candidate_path)) or "(변경 없음)")
    if not yes:
        print("\n미리보기만 출력했습니다. 실제 반영하려면 --yes 를 붙이세요.")
        return False

    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    if live_path.exists():
        backup = BACKUPS_DIR / f"{live_path.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        shutil.copy2(live_path, backup)
        print(f"백업: {backup}")
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_text(cand, encoding="utf-8")
    print(f"반영 완료: {live_path}")
    return True


def read_history(limit: int = 20) -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    records = []
    for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records[-limit:]


# ---------------------------------------------------------------------------
# 오프라인 모의(mock) 세트 — 네트워크·API 키 없이 harness 전체 경로 검증용
# ---------------------------------------------------------------------------
def offline_mocks():
    """(agent_llm_call, judge_call, improver_call) 3종 모의 함수를 반환한다.

    개선자는 '근거 문장 추가' 규칙을 덧붙이고, 모의 에이전트는 그 규칙이 있을 때
    더 구체적인 요약을 내며, 모의 judge 는 그 차이를 점수에 반영한다.
    → 오프라인에서도 win/promote 경로가 결정적으로 재현된다.
    """
    MARKER = "[개선 규칙 v2]"

    def agent(system: str, user: str) -> str:
        improved = MARKER in system
        pts = ["SOC 보조 탐지 계층으로 검토할 만하다."]
        if improved:
            pts = [
                "SOC 보조 탐지 계층으로 검토할 만하다. 근거: 저자들이 오탐률을 "
                "규칙 기반 수준으로 유지했다고 보고하므로 기존 파이프라인에 병행 배치가 가능하다.",
            ]
        return json.dumps({
            "key_contributions": [
                "저자들은 새로운 접근을 제안했다고 주장한다."
                + (" 근거: 초록에 해당 주장이 명시되어 있다." if improved else "")
            ],
            "methodology": "저자들은 단계적 파이프라인을 사용했다고 보고한다.",
            "practical_points": pts,
            "limitations": ["평가 범위가 제한적이라고 저자들이 명시한다."],
            "interest_score": 4,
            "interest_reason": "관심 키워드와 직접 관련.",
        }, ensure_ascii=False)

    def judge(system: str, user: str) -> str:
        improved = "근거:" in user  # SUMMARY_DATA 에 근거 문장이 있으면 개선본
        scores = {
            "C1": (5, True), "C2": (5, True),
            "C4": (5 if improved else 4, True),
            "C6": (5 if improved else 3, True),
            "C7": (4, True),
        }
        return json.dumps({
            cid: {"score": s, "pass": p, "rationale": "모의 judge 판정"}
            for cid, (s, p) in scores.items()
        }, ensure_ascii=False)

    def improver(system: str, user: str) -> str:
        base = user.split("[baseline 시스템 프롬프트]", 1)[-1]
        base = base.split("[평가 리포트", 1)[0].strip()
        return base + f"\n\n{MARKER}\n- 핵심 기여와 실무 적용 포인트에는 원문 근거를 '근거:' 문장으로 덧붙인다.\n"

    return agent, judge, improver

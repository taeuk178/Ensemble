"""3층 — 고정 케이스 세트로 코드 버전 간 회귀를 잡는다.

파이프라인의 작성자 역할은 Claude Code 스킬이 수행하므로 `review.py` 단독으로
전체 파이프라인을 무인 실행할 수 없다. 그래서 역할을 나눈다.

- runner(이 모듈): `init_block` 케이스 채점, 끝난 실행 수집, 점수표 작성·비교
- `/ensemble-eval` 스킬: 작성자 단계가 필요한 케이스를 케이스별로 완주

실행과 케이스를 잇는 것은 label 같은 자유 문자열이 아니라 manifest의
`benchmark` 블록이다.
"""

from __future__ import annotations

import secrets
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_PANEL_EFFORT,
    DEFAULT_PANEL_MODEL,
    PAUSED_STATES,
    RUNS_ROOT,
    TERMINAL_STATES,
)
from .environment import ensemble_source_hash, git_commit, git_dirty
from .errors import InfraError, InputError, StateError
from .eval_process import filename_timestamp, load_or_compute
from .eval_quality import judge_expectations, select_final_draft
from .io_utils import atomic_write_json, read_json, sha256_text, utc_now
from .state_machine import empty_usage_totals, initialize_run
from . import layout


SCHEMA_VERSION = 1

CASE_TYPES = {"init_block", "state_behavior", "quality"}
SUITES = ("smoke", "full")
DIFFICULTIES = {"easy", "medium", "hard"}

# 케이스가 기대할 수 있는 정지 지점. 종료 상태뿐 아니라 사용자를 기다리며
# 멈춘 상태도 채점 대상이다.
OBSERVABLE_STATES = TERMINAL_STATES | PAUSED_STATES

# 인프라 장애로 완주하지 못한 실행만 합계에서 뺀다. Ensemble 코드가 낸 오류는
# SKIP이 아니라 FAIL이다 — SKIP으로 빼면 회귀가 숨는다.
INFRA_SKIP_STATES = {"INFRA_ERROR"}

REQUIRED_EXPECTED_FIELDS = {
    "schema_version",
    "review_required",
    "reviewed_by_user",
    "case_type",
    "suites",
    "tags",
    "difficulty",
    "expected_terminal_states",
    "forbidden_states",
    "expect_user_decision",
    "expect_escalation",
}
OPTIONAL_EXPECTED_FIELDS = {"quality_expectations", "notes"}


# --- 케이스 읽기 ------------------------------------------------------

def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InputError(message)


def _string_list(value: Any, context: str) -> list[str]:
    _require(isinstance(value, list), f"{context}는 문자열 배열이어야 합니다.")
    for item in value:
        _require(isinstance(item, str) and item.strip(), f"{context}의 항목은 비어 있지 않은 문자열이어야 합니다.")
    return [str(item) for item in value]


def validate_expected(expected: Any, *, case_id: str) -> dict[str, Any]:
    """케이스 정답지를 검증한다. 정답지는 루프 밖에서 만들어진 입력이다."""
    context = f"eval/cases/{case_id}/expected.json"
    _require(isinstance(expected, dict), f"{context}는 객체여야 합니다.")
    unknown = set(expected) - REQUIRED_EXPECTED_FIELDS - OPTIONAL_EXPECTED_FIELDS
    _require(not unknown, f"{context}에 알 수 없는 필드가 있습니다: {sorted(unknown)}")
    missing = REQUIRED_EXPECTED_FIELDS - set(expected)
    _require(not missing, f"{context}에 필수 필드가 없습니다: {sorted(missing)}")
    _require(expected["schema_version"] == SCHEMA_VERSION, f"{context}.schema_version은 {SCHEMA_VERSION}이어야 합니다.")
    _require(isinstance(expected["review_required"], str), f"{context}.review_required는 문자열이어야 합니다.")
    _require(isinstance(expected["reviewed_by_user"], bool), f"{context}.reviewed_by_user는 참/거짓이어야 합니다.")
    case_type = expected["case_type"]
    _require(case_type in CASE_TYPES, f"{context}.case_type은 {sorted(CASE_TYPES)} 중 하나여야 합니다.")
    suites = _string_list(expected["suites"], f"{context}.suites")
    _require(suites, f"{context}.suites는 비어 있을 수 없습니다.")
    unknown_suites = set(suites) - set(SUITES)
    _require(not unknown_suites, f"{context}.suites에 알 수 없는 세트가 있습니다: {sorted(unknown_suites)}")
    _string_list(expected["tags"], f"{context}.tags")
    _require(expected["difficulty"] in DIFFICULTIES, f"{context}.difficulty가 올바르지 않습니다.")
    terminal = _string_list(expected["expected_terminal_states"], f"{context}.expected_terminal_states")
    forbidden = _string_list(expected["forbidden_states"], f"{context}.forbidden_states")
    for name, values in (("expected_terminal_states", terminal), ("forbidden_states", forbidden)):
        unknown_states = set(values) - OBSERVABLE_STATES
        _require(not unknown_states, f"{context}.{name}에 알 수 없는 상태가 있습니다: {sorted(unknown_states)}")
    overlap = set(terminal) & set(forbidden)
    _require(not overlap, f"{context}에서 같은 상태를 기대와 금지에 동시에 넣을 수 없습니다: {sorted(overlap)}")
    for name in ("expect_user_decision", "expect_escalation"):
        _require(isinstance(expected[name], bool), f"{context}.{name}은 참/거짓이어야 합니다.")

    if case_type == "init_block":
        # run이 생기지 않는 것이 정답이므로 종료 상태 검증은 적용되지 않는다.
        _require(not terminal, f"{context}: init_block 케이스는 expected_terminal_states를 비워 둡니다.")
    else:
        _require(terminal, f"{context}: {case_type} 케이스는 expected_terminal_states가 필요합니다.")

    quality = expected.get("quality_expectations")
    if case_type == "quality":
        _require(isinstance(quality, dict), f"{context}: quality 케이스는 quality_expectations가 필요합니다.")
        unknown_keys = set(quality) - {"must_cover", "must_not_assert"}
        _require(not unknown_keys, f"{context}.quality_expectations에 알 수 없는 필드가 있습니다: {sorted(unknown_keys)}")
        must_cover = _string_list(quality.get("must_cover", []), f"{context}.quality_expectations.must_cover")
        must_not = _string_list(quality.get("must_not_assert", []), f"{context}.quality_expectations.must_not_assert")
        _require(
            must_cover or must_not,
            f"{context}.quality_expectations에 채점할 항목이 하나도 없습니다.",
        )
    elif quality is not None:
        _require(
            isinstance(quality, dict),
            f"{context}.quality_expectations는 객체여야 합니다.",
        )
    return expected


def load_case(case_id: str) -> dict[str, Any]:
    request_path = layout.case_request(case_id)
    expected_path = layout.case_expected(case_id)
    if not request_path.is_file() or not expected_path.is_file():
        raise InputError(f"케이스를 찾을 수 없습니다: {case_id}")
    request_text = request_path.read_text(encoding="utf-8")
    expected_text = expected_path.read_text(encoding="utf-8")
    expected = validate_expected(read_json(expected_path), case_id=case_id)
    return {
        "case_id": case_id,
        "request": request_text,
        "expected": expected,
        # request.txt를 고치면 과거 점수와 비교할 수 없다. 케이스는 불변이며
        # 이 해시로 케이스 파일이 바뀐 run을 걸러낸다.
        "case_revision_hash": sha256_text(f"{request_text}\n---\n{expected_text}"),
    }


def iter_cases(suite: str, *, case_id: str | None = None) -> list[dict[str, Any]]:
    if suite not in SUITES:
        raise InputError(f"알 수 없는 케이스 세트입니다: {suite}")
    cases_root = layout.cases_root()
    if not cases_root.is_dir():
        raise InputError(f"케이스 폴더가 없습니다: {cases_root}")
    cases = []
    for directory in sorted(cases_root.iterdir()):
        if not directory.is_dir():
            continue
        if case_id is not None and directory.name != case_id:
            continue
        case = load_case(directory.name)
        if suite in case["expected"]["suites"]:
            cases.append(case)
    if case_id is not None and not cases:
        raise InputError(f"{suite} 세트에 {case_id} 케이스가 없습니다.")
    return cases


def suite_hash(cases: list[dict[str, Any]]) -> str:
    """세트에 속한 (케이스 ID, 개정 해시) 목록의 해시."""
    lines = sorted(f"{case['case_id']}:{case['case_revision_hash']}" for case in cases)
    return sha256_text("\n".join(lines))


def make_benchmark_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"bench-{timestamp}-{secrets.token_hex(3)}"


def validate_benchmark_block(payload: Any) -> dict[str, Any]:
    """`init --benchmark-file`이 받는 실행 식별 계약을 검증한다."""
    required = {"benchmark_run_id", "case_id", "case_revision_hash", "suite", "repeat_index"}
    _require(isinstance(payload, dict), "benchmark 블록은 객체여야 합니다.")
    missing = required - set(payload)
    _require(not missing, f"benchmark 블록에 필수 필드가 없습니다: {sorted(missing)}")
    unknown = set(payload) - required - {"suite_hash"}
    _require(not unknown, f"benchmark 블록에 알 수 없는 필드가 있습니다: {sorted(unknown)}")
    for name in ("benchmark_run_id", "case_id", "case_revision_hash", "suite"):
        _require(isinstance(payload[name], str) and payload[name].strip(), f"benchmark.{name}이 비어 있습니다.")
    _require(payload["suite"] in SUITES, f"benchmark.suite가 올바르지 않습니다: {payload['suite']}")
    _require(
        isinstance(payload["repeat_index"], int) and not isinstance(payload["repeat_index"], bool)
        and payload["repeat_index"] >= 1,
        "benchmark.repeat_index는 1 이상의 정수여야 합니다.",
    )
    return dict(payload)


# --- 관측 ------------------------------------------------------------

def observe_run(run_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """실행 산출물에서 채점에 쓸 관측값을 뽑는다.

    신버전 실행은 manifest.state_history를 권위 이력으로 쓴다. 구버전 실행은
    남아 있는 검토·결정·패널 흔적으로 보완 복원한다.
    """
    state = str(manifest.get("state") or "")
    decisions = [item for item in (manifest.get("user_decisions") or []) if isinstance(item, dict)]
    from_states = {str(item.get("from_state")) for item in decisions}
    states_seen = {state} | from_states
    states_seen |= {
        str(item.get("to"))
        for item in (manifest.get("state_history") or [])
        if isinstance(item, dict)
    }
    states_seen |= {
        str(item.get("verdict"))
        for item in (manifest.get("review_history") or [])
        if isinstance(item, dict)
    }
    panel_calls = int(manifest.get("panel_call_count", 0) or 0)
    return {
        "run_dir": str(run_dir),
        "terminal_state": state or None,
        "init_blocked": False,
        "user_decision_reached": state == "USER_DECISION_REQUIRED" or "USER_DECISION_REQUIRED" in from_states,
        "escalation_reached": (
            state == "ESCALATION_REQUIRED" or "ESCALATION_REQUIRED" in from_states or panel_calls > 0
        ),
        "states_seen": sorted(value for value in states_seen if value),
        "tainted": state == "RUN_TAINTED" or bool(manifest.get("environment_changes")),
    }


def evaluate_init_block(case: dict[str, Any]) -> dict[str, Any]:
    """`init`이 실제로 실행 생성을 막는지 확인한다. 모델 호출이 없다.

    막지 못하면 run이 만들어지고, 그 사실 자체가 FAIL의 근거다.
    """
    try:
        run_dir = initialize_run(case["request"], allow_reuse=True)
    except InputError as exc:
        return {
            "run_dir": None,
            "terminal_state": None,
            "init_blocked": True,
            "block_reason": exc.message,
            "block_details": exc.details,
            "user_decision_reached": False,
            "escalation_reached": False,
            "states_seen": [],
            "tainted": False,
        }
    return {
        "run_dir": str(run_dir),
        "terminal_state": "INITIALIZED",
        "init_blocked": False,
        "user_decision_reached": False,
        "escalation_reached": False,
        "states_seen": ["INITIALIZED"],
        "tainted": False,
    }


# --- 채점 ------------------------------------------------------------

def grade_state(case: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    """모델 심판을 부르기 전에 결정적으로 판정할 수 있는 실행 상태 축."""
    expected = case["expected"]
    reasons: list[str] = []
    if not expected["reviewed_by_user"]:
        return {"verdict": "UNREVIEWED", "reasons": ["정답지가 아직 사용자 검토를 받지 않았습니다."]}
    if observation.get("tainted"):
        return {"verdict": "SKIP", "reasons": ["실행 도중 Ensemble 코드가 바뀌었습니다(RUN_TAINTED)."]}
    if observation.get("terminal_state") in INFRA_SKIP_STATES:
        return {"verdict": "SKIP", "reasons": ["외부 인프라 장애로 실행이 완료되지 못했습니다."]}
    if expected["case_type"] == "init_block":
        if observation.get("init_blocked") and observation.get("run_dir") is None:
            return {"verdict": "PASS", "reasons": []}
        return {"verdict": "FAIL", "reasons": ["init이 요청을 막지 않고 실행을 만들었습니다."]}

    terminal_state = observation.get("terminal_state")
    if terminal_state not in expected["expected_terminal_states"]:
        reasons.append(
            f"정지 상태가 기대와 다릅니다: {terminal_state} (기대 {expected['expected_terminal_states']})"
        )
    trespassed = sorted(set(observation.get("states_seen") or []) & set(expected["forbidden_states"]))
    if trespassed:
        reasons.append(f"금지된 상태를 지났습니다: {trespassed}")
    if bool(observation.get("user_decision_reached")) != expected["expect_user_decision"]:
        reasons.append(
            f"사용자 결정 요구 기대와 관측이 다릅니다: 기대 {expected['expect_user_decision']}, "
            f"관측 {bool(observation.get('user_decision_reached'))}"
        )
    if bool(observation.get("escalation_reached")) != expected["expect_escalation"]:
        reasons.append(
            f"추가 판단 기대와 관측이 다릅니다: 기대 {expected['expect_escalation']}, "
            f"관측 {bool(observation.get('escalation_reached'))}"
        )
    return {"verdict": "FAIL" if reasons else "PASS", "reasons": reasons}


def grade_case(
    case: dict[str, Any],
    observation: dict[str, Any],
    quality_findings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """케이스 유형별 판정. 공통 PASS 규칙 하나로 묶지 않는다."""
    expected = case["expected"]
    state_grade = grade_state(case, observation)
    if state_grade["verdict"] != "PASS":
        return state_grade
    reasons: list[str] = []

    if expected["case_type"] == "quality":
        if quality_findings is None:
            # 상태 검사가 이미 실패했으면 품질 축을 몰라도 판정은 FAIL이다.
            # SKIP으로 빼면 확인된 실패가 합계에서 사라진다.
            if reasons:
                reasons.append("정답지 채점 결과가 없어 품질 축은 평가하지 못했습니다.")
                return {"verdict": "FAIL", "reasons": reasons}
            return {"verdict": "SKIP", "reasons": ["정답지 채점 결과가 없습니다."]}
        missing = quality_findings.get("must_cover_missing") or []
        violations = quality_findings.get("must_not_assert_violations") or []
        if missing:
            reasons.append(f"다뤄야 할 항목이 빠졌습니다: {[item.get('item') for item in missing]}")
        if violations:
            reasons.append(f"확정하면 안 되는 항목을 단정했습니다: {[item.get('item') for item in violations]}")

    return {"verdict": "FAIL" if reasons else "PASS", "reasons": reasons}


# --- 실행 수집 --------------------------------------------------------

def collect_benchmark_runs(benchmark_run_id: str) -> list[tuple[Path, dict[str, Any]]]:
    """해당 벤치마크 순회에 속한 실행만 모은다. label은 보지 않는다."""
    collected: list[tuple[Path, dict[str, Any]]] = []
    if not RUNS_ROOT.exists():
        return collected
    for manifest_path in sorted(RUNS_ROOT.glob("*/_state/manifest.json")):
        manifest = read_json(manifest_path, default={})
        benchmark = manifest.get("benchmark")
        if not isinstance(benchmark, dict):
            continue
        if benchmark.get("benchmark_run_id") != benchmark_run_id:
            continue
        collected.append((manifest_path.parent.parent, manifest))
    return collected


def _sum_usage(target: dict[str, dict[str, Any]], usage: Any) -> None:
    if not isinstance(usage, dict):
        return
    for provider, totals in usage.items():
        if not isinstance(totals, dict):
            continue
        bucket = target.setdefault(provider, empty_usage_totals())
        for key, value in totals.items():
            if isinstance(value, int) and not isinstance(value, bool):
                bucket[key] = bucket.get(key, 0) + value
        # 오차의 방향은 합산해도 사라지면 안 된다. 케이스 하나라도 시간 창으로
        # 귀속했으면 그 제공자의 합계는 상한값이다.
        if totals.get("upper_bound"):
            bucket["upper_bound"] = True
            bucket["attribution"] = totals.get("attribution")
        if totals.get("unreported_fields"):
            bucket["unreported_fields"] = list(totals["unreported_fields"])


def _process_summary(run_dir: Path) -> dict[str, Any]:
    metrics = load_or_compute(run_dir)
    return {
        "review_rounds": metrics.get("convergence", {}).get("review_rounds"),
        "leakage_rate": metrics.get("leakage", {}).get("leakage_rate_lower_bound"),
        "leakage_rate_observed": metrics.get("leakage", {}).get("leakage_rate_observed"),
        "unpromoted_final_findings": metrics.get("leakage", {}).get(
            "unique_unpromoted_final_blind_blockers"
        ),
        "retries": metrics.get("friction", {}).get("retries", {}),
        "wall_clock_seconds": metrics.get("resources", {}).get("wall_clock_seconds"),
    }


def _quality_summary(run_dir: Path) -> dict[str, Any] | None:
    """2층 결과는 있으면 읽고, 없다고 새로 호출하지 않는다(비용 계층 분리)."""
    path = layout.quality_judgment(run_dir)
    if not path.exists():
        return None
    return read_json(path, default={})


def collect(
    *,
    suite: str,
    benchmark_run_id: str,
    case_id: str | None = None,
    judge_model: str = DEFAULT_PANEL_MODEL,
    judge_effort: str = DEFAULT_PANEL_EFFORT,
    timeout: int,
    skip_expectation_judge: bool = False,
    force_judge: bool = False,
) -> dict[str, Any]:
    cases = iter_cases(suite, case_id=case_id)
    if not cases:
        raise InputError(f"{suite} 세트에 케이스가 없습니다.")
    runs = collect_benchmark_runs(benchmark_run_id)
    current_cases = {case["case_id"]: case for case in cases}
    comparable_runs = [
        (run_dir, manifest)
        for run_dir, manifest in runs
        if str((manifest.get("benchmark") or {}).get("case_id")) in current_cases
        and (manifest.get("benchmark") or {}).get("case_revision_hash")
        == current_cases[str((manifest.get("benchmark") or {}).get("case_id"))][
            "case_revision_hash"
        ]
    ]
    by_case: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for run_dir, manifest in runs:
        by_case.setdefault(str(manifest["benchmark"].get("case_id")), []).append((run_dir, manifest))

    source_hashes = {
        str((manifest.get("environment") or {}).get("ensemble_source_hash"))
        for _, manifest in comparable_runs
        if (manifest.get("environment") or {}).get("ensemble_source_hash")
    }
    commits = {
        str((manifest.get("environment") or {}).get("git_commit"))
        for _, manifest in comparable_runs
        if (manifest.get("environment") or {}).get("git_commit")
    }
    codex_models = {
        str(((manifest.get("models") or {}).get("codex") or {}).get("requested"))
        for _, manifest in comparable_runs
    }
    agy_models = {
        str(((manifest.get("models") or {}).get("agy") or {}).get("requested"))
        for _, manifest in comparable_runs
    }
    author_models: set[str | None] = {
        ((manifest.get("models") or {}).get("author") or {}).get("requested")
        for _, manifest in comparable_runs
    }
    current_hash = ensemble_source_hash()
    warnings: list[str] = []
    tainted = False
    if len(source_hashes) > 1:
        tainted = True
        warnings.append("수집한 실행들의 Ensemble 코드 해시가 서로 다릅니다.")
    if source_hashes and current_hash not in source_hashes:
        tainted = True
        warnings.append("평가 시점의 코드가 실행 시점과 다릅니다.")
    if git_dirty():
        tainted = True
        warnings.append("작업 사본에 커밋되지 않은 변경이 있습니다.")
    if len(codex_models) > 1 or len(agy_models) > 1 or len(author_models) > 1:
        tainted = True
        warnings.append("수집한 실행들의 모델 구성이 서로 다릅니다.")
    collection_tainted = tainted
    entries: list[dict[str, Any]] = []
    usage_totals: dict[str, dict[str, int]] = {}

    for case in cases:
        case_type = case["expected"]["case_type"]
        if case_type == "init_block":
            observation = evaluate_init_block(case)
            grade = grade_case(case, observation)
            if observation.get("run_dir"):
                warnings.append(
                    f"{case['case_id']}: init이 막지 못해 실행이 생성됐습니다: {observation['run_dir']}"
                )
            entries.append(
                {
                    "case_id": case["case_id"],
                    "case_type": case_type,
                    "case_revision_hash": case["case_revision_hash"],
                    "repeat_index": 1,
                    "run_id": None,
                    "verdict": grade["verdict"],
                    "reasons": grade["reasons"],
                    "observed": observation,
                    "process_metrics": None,
                    "quality_judgment": None,
                    "usage": {"run": None, "judge": None},
                }
            )
            continue

        matched = by_case.get(case["case_id"], [])
        if not matched:
            warnings.append(f"{case['case_id']}: 수집할 실행이 없습니다.")
            entries.append(
                {
                    "case_id": case["case_id"],
                    "case_type": case_type,
                    "case_revision_hash": case["case_revision_hash"],
                    "repeat_index": None,
                    "run_id": None,
                    "verdict": "SKIP",
                    "reasons": ["해당 벤치마크 순회에서 이 케이스의 실행을 찾지 못했습니다."],
                    "observed": None,
                    "process_metrics": None,
                    "quality_judgment": None,
                    "usage": {"run": None, "judge": None},
                }
            )
            continue

        for run_dir, manifest in sorted(
            matched, key=lambda item: int(item[1]["benchmark"].get("repeat_index", 1))
        ):
            benchmark = manifest["benchmark"]
            if benchmark.get("case_revision_hash") != case["case_revision_hash"]:
                warnings.append(
                    f"{case['case_id']}: 케이스 파일이 바뀐 뒤의 실행이라 수집하지 않았습니다 "
                    f"({manifest.get('run_id')})"
                )
                continue
            observation = observe_run(run_dir, manifest)
            if observation["tainted"]:
                tainted = True
            quality_findings = None
            judge_usage = None
            judge_status = "NOT_APPLICABLE"
            judge_failure_record = None
            state_grade = grade_state(case, observation)
            if case_type == "quality":
                if skip_expectation_judge:
                    judge_status = "NOT_RUN_REQUESTED"
                elif not force_judge and state_grade["verdict"] != "PASS":
                    judge_status = "NOT_RUN_STATE_FAILED"
                elif not force_judge and collection_tainted:
                    judge_status = "NOT_RUN_TAINTED"
                else:
                    final_path = select_final_draft(run_dir, manifest)
                    if final_path is None:
                        judge_status = "NOT_RUN_NO_DRAFT"
                        warnings.append(
                            f"{case['case_id']}: 비교할 최종 초안이 없어 정답지 채점을 건너뜁니다."
                        )
                    else:
                        try:
                            quality_findings, judge_result = judge_expectations(
                                run_dir,
                                document_path=final_path,
                                expectations=case["expected"]["quality_expectations"],
                                model=judge_model,
                                effort=judge_effort,
                                timeout=timeout,
                            )
                            judge_usage = {"agy": empty_usage_totals()}
                            judge_usage["agy"]["calls_unreported"] = 1
                            judge_usage["agy"]["attempts_unreported"] = judge_result.attempts
                            judge_status = "JUDGED"
                        except InfraError as exc:
                            details = exc.details if isinstance(exc.details, dict) else {}
                            failure_index = len(
                                list(layout.judge_raw_dir(run_dir).glob("*.json"))
                            ) + 1
                            failure_path = layout.judge_failure(run_dir, failure_index)
                            while failure_path.exists():
                                failure_index += 1
                                failure_path = layout.judge_failure(run_dir, failure_index)
                            atomic_write_json(
                                failure_path,
                                {
                                    "recorded_at": utc_now(),
                                    "provider": details.get("provider", "agy"),
                                    "error": exc.message,
                                    "details": details,
                                },
                                overwrite=False,
                            )
                            judge_failure_record = failure_path.relative_to(run_dir).as_posix()
                            judge_usage = {"agy": empty_usage_totals()}
                            judge_usage["agy"]["calls_unreported"] = 1
                            judge_usage["agy"]["attempts_unreported"] = int(
                                details.get("attempts") or 1
                            )
                            judge_status = "INFRA_ERROR"
                            warnings.append(
                                f"{case['case_id']}: 정답지 채점 심판 호출이 실패했습니다: "
                                f"{exc.message} ({judge_failure_record})"
                            )

            grade = grade_case(case, observation, quality_findings)
            quality_judgment = _quality_summary(run_dir)
            run_usage = manifest.get("usage") or None
            _sum_usage(usage_totals, run_usage)
            _sum_usage(usage_totals, judge_usage)
            entries.append(
                {
                    "case_id": case["case_id"],
                    "case_type": case_type,
                    "case_revision_hash": case["case_revision_hash"],
                    "repeat_index": int(benchmark.get("repeat_index", 1)),
                    "run_id": manifest.get("run_id"),
                    "run_dir": str(run_dir),
                    "verdict": grade["verdict"],
                    "reasons": grade["reasons"],
                    "observed": observation,
                    "process_metrics": _process_summary(run_dir),
                    "quality_judgment": {
                        "judge_status": judge_status,
                        "failure_record": judge_failure_record,
                        "overall": (quality_judgment or {}).get("composite", {}).get("overall"),
                        "must_cover_missing": (quality_findings or {}).get("must_cover_missing", []),
                        "must_not_assert_violations": (quality_findings or {}).get(
                            "must_not_assert_violations", []
                        ),
                    },
                    "usage": {"run": run_usage, "judge": judge_usage},
                }
            )

    verdicts = Counter(entry["verdict"] for entry in entries)
    draft_better = sum(
        1
        for entry in entries
        if (entry.get("quality_judgment") or {}).get("overall") == "DRAFT_BETTER"
    )
    resolved_commit = next(iter(commits)) if len(commits) == 1 else git_commit()
    return {
        "schema_version": SCHEMA_VERSION,
        "git_commit": resolved_commit,
        "ensemble_source_hash": next(iter(source_hashes)) if len(source_hashes) == 1 else current_hash,
        "created_at": utc_now(),
        "suite": suite,
        # 케이스를 걸러 수집하면 세트가 달라지므로 suite_hash도 달라진다.
        # eval-compare가 전체 수집분과의 비교를 거부하게 두고, 부분 수집이라는
        # 사실은 case_filter로 드러낸다.
        "suite_hash": suite_hash(cases),
        "case_filter": case_id,
        "benchmark_run_id": benchmark_run_id,
        "tainted": tainted,
        "model_config": {
            "codex": next(iter(codex_models)) if len(codex_models) == 1 else None,
            "agy": next(iter(agy_models)) if len(agy_models) == 1 else None,
            # 작성자 런타임은 CLI가 감지할 수 없다. 선언값만 기록하고 검증하지 않는다.
            "declared_author_model": next(iter(author_models)) if len(author_models) == 1 else None,
            "author_verified": False,
            "judge": judge_model,
        },
        "cases": entries,
        "totals": {
            "pass": verdicts.get("PASS", 0),
            "fail": verdicts.get("FAIL", 0),
            "skip": verdicts.get("SKIP", 0),
            "unreviewed": verdicts.get("UNREVIEWED", 0),
            "draft_better_count": draft_better,
            "usage": usage_totals,
        },
        "warnings": warnings,
    }


def write_scorecard(scorecard: dict[str, Any]) -> Path:
    git_sha = str(scorecard.get("git_commit") or "unknown")
    path = layout.scorecard(git_sha)
    if path.exists():
        previous = read_json(path, default={})
        archive = layout.scorecard_archive(git_sha, filename_timestamp(previous.get("created_at")))
        if not archive.exists():
            atomic_write_json(archive, previous, overwrite=False)
    atomic_write_json(path, scorecard)
    return path


def plan(
    suite: str,
    *,
    case_id: str | None = None,
    repeat: int = 1,
) -> dict[str, Any]:
    """세트를 완주하기 전에 필요한 정보를 내놓는다.

    `init_block` 케이스는 여기서 바로 채점한다 — 모델 호출이 없어 결정적이고
    비용이 0이다. 나머지는 `/ensemble-eval` 스킬이 케이스별로 실행해야 한다.
    """
    if repeat < 1:
        raise InputError("--repeat은 1 이상의 정수여야 합니다.")
    cases = iter_cases(suite, case_id=case_id)
    benchmark_run_id = make_benchmark_run_id()
    computed_suite_hash = suite_hash(cases)
    pending: list[dict[str, Any]] = []
    immediate: list[dict[str, Any]] = []
    for case in cases:
        if case["expected"]["case_type"] == "init_block":
            observation = evaluate_init_block(case)
            immediate.append(
                {
                    "case_id": case["case_id"],
                    **grade_case(case, observation),
                    "observed": observation,
                }
            )
            continue
        for repeat_index in range(1, repeat + 1):
            pending.append(
                {
                    "case_id": case["case_id"],
                    "case_type": case["expected"]["case_type"],
                    "reviewed_by_user": case["expected"]["reviewed_by_user"],
                    "request_file": str(layout.case_request(case["case_id"])),
                    "benchmark": {
                        "benchmark_run_id": benchmark_run_id,
                        "case_id": case["case_id"],
                        "case_revision_hash": case["case_revision_hash"],
                        "suite": suite,
                        "suite_hash": computed_suite_hash,
                        "repeat_index": repeat_index,
                    },
                }
            )
    return {
        "suite": suite,
        "suite_hash": computed_suite_hash,
        "benchmark_run_id": benchmark_run_id,
        "repeat": repeat,
        "immediate": immediate,
        "pending_runs": pending,
        "next_command": (
            "python3 .claude/skills/ensemble/scripts/review.py eval-bench --collect "
            f"--suite {suite} --benchmark-run-id {benchmark_run_id}"
        ),
    }


# --- 점수표 비교 ------------------------------------------------------

COMPARISON_KEYS = ("verdict", "process_metrics", "quality_judgment")


def compare_scorecards(
    base_sha: str,
    head_sha: str,
    *,
    allow_model_mismatch: bool = False,
) -> dict[str, Any]:
    """두 커밋의 점수표를 나란히 놓는다.

    케이스당 1회 실행끼리의 판정 차이는 확률적 흔들림과 구분되지 않으므로
    "회귀"가 아니라 "회귀 신호"로 표기한다.
    """
    scorecards = {}
    for label, sha in (("base", base_sha), ("head", head_sha)):
        path = layout.scorecard(sha)
        if not path.exists():
            raise InputError(f"{label} 점수표가 없습니다: {path}")
        scorecards[label] = read_json(path)

    blockers: list[str] = []
    for label, scorecard in scorecards.items():
        if scorecard.get("tainted"):
            blockers.append(f"{label} 점수표가 tainted입니다.")
    if scorecards["base"].get("suite_hash") != scorecards["head"].get("suite_hash"):
        blockers.append("케이스 세트 버전(suite_hash)이 다릅니다.")
    model_mismatch = scorecards["base"].get("model_config") != scorecards["head"].get("model_config")
    if model_mismatch and not allow_model_mismatch:
        blockers.append(
            "모델 구성이 다릅니다. 점수 차이가 코드 때문인지 모델 때문인지 구분할 수 없습니다."
        )
    if blockers:
        raise StateError("점수표를 비교할 수 없습니다.", details={"reasons": blockers})

    rows = []
    base_cases = {
        (entry["case_id"], entry.get("repeat_index")): entry for entry in scorecards["base"]["cases"]
    }
    head_cases = {
        (entry["case_id"], entry.get("repeat_index")): entry for entry in scorecards["head"]["cases"]
    }
    for key in sorted(set(base_cases) | set(head_cases)):
        base_entry = base_cases.get(key, {})
        head_entry = head_cases.get(key, {})
        rows.append(
            {
                "case_id": key[0],
                "repeat_index": key[1],
                **{
                    f"base_{name}": base_entry.get(name) for name in COMPARISON_KEYS
                },
                **{
                    f"head_{name}": head_entry.get(name) for name in COMPARISON_KEYS
                },
                "regression_signal": (
                    base_entry.get("verdict") == "PASS" and head_entry.get("verdict") == "FAIL"
                ),
            }
        )
    return {
        "base": {"git_commit": base_sha, "totals": scorecards["base"]["totals"]},
        "head": {"git_commit": head_sha, "totals": scorecards["head"]["totals"]},
        "model_config_mismatch_allowed": bool(model_mismatch and allow_model_mismatch),
        "cases": rows,
        "regression_signals": [row["case_id"] for row in rows if row["regression_signal"]],
        "note": "케이스당 1회 실행의 판정 차이는 회귀가 아니라 회귀 신호입니다. "
        "확률적 흔들림과 구분하려면 반복 실행이 필요합니다.",
    }

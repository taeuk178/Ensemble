"""1층 — 완료된 실행 하나의 프로세스 지표를 결정적으로 계산한다.

모델 호출이 없으므로 비용이 없고, 같은 산출물이면 `evaluated_at` 말고는
항상 같은 결과가 나온다. 원천 필드가 없으면 그 지표만 `null`로 두고
실패하지 않는다 — 미완료 실행이나 구버전 실행도 평가 대상이다.

계산은 순수 함수(`compute_process_metrics`)에 모으고 파일 읽기와 분리한다.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .environment import ensemble_source_hash, git_commit
from .io_utils import atomic_write_json, read_json, utc_now
from . import layout


SCHEMA_VERSION = 1

# registry의 `first_seen_source`는 실행 폴더 기준 상대 경로다.
ITERATIVE_SOURCE_PREFIX = "04-reviews/iterative/"
PROMOTED_SOURCE_PREFIX = "04-reviews/promoted/"

CLEAN_TERMINAL_STATES = {"CONVERGED", "STABLE_DISSENT"}


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def filename_timestamp(value: Any) -> str:
    """ISO 시각을 파일명에 쓸 수 있는 형태로 줄인다."""
    parsed = _parse_timestamp(value)
    if parsed is None:
        return "unknown"
    return parsed.strftime("%Y%m%dT%H%M%SZ")


def _int(value: Any, default: int = 0) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else default


def _convergence_metrics(
    manifest: dict[str, Any], rounds: list[dict[str, Any]], warnings: list[str]
) -> dict[str, Any]:
    history = [item for item in (manifest.get("review_history") or []) if isinstance(item, dict)]
    draft_rounds = [_int(item.get("draft_round"), -1) for item in history]
    draft_rounds = [value for value in draft_rounds if value >= 0]
    state = manifest.get("state")
    zero_backlog = next(
        (_int(record.get("round")) for record in rounds if _int(record.get("open_backlog"), -1) == 0),
        None,
    )
    if not rounds:
        warnings.append("convergence.json에 라운드 기록이 없어 라운드별 지표가 비어 있습니다.")
    counters = manifest.get("counters") or {}
    iterative_reviews = (
        _int(counters.get("iterative_reviews"))
        if isinstance(counters.get("iterative_reviews"), int)
        else len(history)
    )
    promotions = _int(counters.get("promotions"))
    session_policy = manifest.get("review_session_policy") or {}
    return {
        # 승격은 시퀀스 번호를 쓰지만 일반 검토 예산과 검토 횟수에는 넣지 않는다.
        "review_rounds": iterative_reviews,
        "iterative_reviews": iterative_reviews,
        "sequence_rounds": len(history),
        "promotions": promotions,
        "review_session_resets": _int(session_policy.get("reset_count")),
        "draft_rounds": max(draft_rounds) + 1 if draft_rounds else None,
        "final_state": state,
        "terminated_cleanly": state in CLEAN_TERMINAL_STATES,
        "new_issues_by_round": [_int(record.get("new_issue_count")) for record in rounds],
        "open_backlog_by_round": [_int(record.get("open_backlog")) for record in rounds],
        "rounds_to_zero_backlog": zero_backlog,
    }


def _leakage_metrics(
    registry: dict[str, Any],
    final_blinds: list[tuple[str, dict[str, Any]]],
    reconciliations: list[tuple[str, dict[str, Any]]],
    warnings: list[str],
) -> dict[str, Any]:
    """누출률 — 일반 검토 루프가 놓치고 최종 독립 검토가 잡아낸 비율.

    blind 원문의 `blocking_issues`를 그대로 합산하면 위험 수용과의 일치,
    시도 간 반복, 승격된 이슈가 겹쳐 과계산된다. 비율의 분자·분모는 registry의
    `first_seen_source`로 센 고유 이슈 수를 쓰고, 시도별 원 수치는
    중복 제거 없이 `attempts[]`로만 남긴다.
    """
    by_name = dict(reconciliations)
    attempts: list[dict[str, Any]] = []
    observed_final_keys: set[tuple[str, str]] = set()
    for name, blind in final_blinds:
        record = by_name.get(name)
        attempt_keys: set[tuple[str, str]] = set()
        if record:
            for finding_record in record.get("unaccepted_blocking_findings") or []:
                finding = finding_record.get("finding") or {}
                criterion = str(finding.get("criterion_id") or "")
                consequence = str(finding_record.get("consequence_fingerprint") or "")
                if criterion and consequence:
                    attempt_keys.add((criterion, consequence))
            observed_final_keys.update(attempt_keys)
        attempts.append(
            {
                "attempt_file": name,
                "raw_findings": len(blind.get("blocking_issues") or []),
                "accepted_risk_matches": (
                    len(record.get("accepted_findings") or []) if record else None
                ),
                "unaccepted_findings": (
                    len(record.get("unaccepted_blocking_findings") or []) if record else None
                ),
                "passed": record.get("passed") if record else None,
                "unique_unaccepted_finding_keys": len(attempt_keys) if record else None,
            }
        )
        if record is None:
            warnings.append(f"독립 검토 {name}에 대응하는 대조 결과가 없습니다.")

    iterative_origin = 0
    promoted_origin = 0
    unknown_origin = 0
    for issue in registry.values():
        if not isinstance(issue, dict):
            continue
        source = issue.get("first_seen_source")
        if isinstance(source, str) and source.startswith(ITERATIVE_SOURCE_PREFIX):
            iterative_origin += 1
        elif isinstance(source, str) and source.startswith(PROMOTED_SOURCE_PREFIX):
            promoted_origin += 1
        else:
            unknown_origin += 1

    denominator = iterative_origin + promoted_origin
    observed_final_unique = len(observed_final_keys)
    observed_denominator = iterative_origin + observed_final_unique
    unique_unpromoted = max(observed_final_unique - promoted_origin, 0)
    unpromoted = (
        len(reconciliations[-1][1].get("unaccepted_blocking_findings") or [])
        if reconciliations
        else None
    )
    if unpromoted:
        warnings.append(
            "승격되지 않은 미수용 발견이 남아 있어 누출률 분자가 실제보다 작습니다: "
            f"{unpromoted}건"
        )
    if unknown_origin:
        warnings.append(
            f"기원을 알 수 없는 이슈 {unknown_origin}건은 누출률 계산에서 제외했습니다."
        )
    return {
        "final_blind_attempts": len(final_blinds),
        "attempts": attempts,
        "final_blind_first_pass": reconciliations[0][1].get("passed") if reconciliations else None,
        "unique_iterative_origin_blockers": iterative_origin,
        "unique_promoted_final_blind_blockers": promoted_origin,
        "unknown_origin_blockers": unknown_origin,
        "unpromoted_unaccepted_last_attempt": unpromoted,
        "unique_observed_final_blind_blockers": observed_final_unique,
        "unique_unpromoted_final_blind_blockers": unique_unpromoted,
        "leakage_rate_lower_bound": (promoted_origin / denominator) if denominator else None,
        "leakage_rate_observed": (
            observed_final_unique / observed_denominator if observed_denominator else None
        ),
    }


def _issue_metrics(registry: dict[str, Any], rounds: list[dict[str, Any]]) -> dict[str, Any]:
    dispositions: Counter[str] = Counter()
    resolution_basis: Counter[str] = Counter()
    severity: Counter[str] = Counter()
    resolved_without_edit = 0
    regressions = 0
    storm_rounds = 0
    for record in rounds:
        for value in (record.get("author_dispositions") or {}).values():
            if value:
                dispositions[str(value)] += 1
        resolution_basis.update(
            {str(key): _int(value) for key, value in (record.get("resolution_basis_counts") or {}).items()}
        )
        severity.update(
            {str(key): _int(value) for key, value in (record.get("severity_distribution") or {}).items()}
        )
        resolved_without_edit += _int(record.get("resolved_without_relevant_edit"))
        regressions += _int(record.get("regression_count"))
        storm_rounds += 1 if record.get("reviewer_storm") else 0
    total_dispositions = sum(dispositions.values())
    return {
        "total_issues": len(registry),
        # 라운드마다 열린 이슈의 최신 판단을 다시 세므로, 같은 이슈가 여러 라운드에
        # 걸쳐 반복 집계된다. 라운드 단위 추세를 보는 값이지 판단 횟수가 아니다.
        "dispositions": dict(sorted(dispositions.items())),
        "acceptance_rate": (dispositions["ACCEPT"] / total_dispositions) if total_dispositions else None,
        "resolution_basis": dict(sorted(resolution_basis.items())),
        "resolved_without_relevant_edit": resolved_without_edit,
        "regression_count": regressions,
        "reviewer_storm_rounds": storm_rounds,
        "max_stalled_streak": max((_int(record.get("stalled_streak")) for record in rounds), default=None),
        "severity_distribution": dict(sorted(severity.items())),
    }


def _friction_metrics(manifest: dict[str, Any], convergence: dict[str, Any]) -> dict[str, Any]:
    decisions = [item for item in (manifest.get("user_decisions") or []) if isinstance(item, dict)]
    calls = [item for item in (manifest.get("provider_calls") or []) if isinstance(item, dict)]
    review_calls = [call for call in calls if call.get("operation") == "review"]
    resumed = sum(1 for call in review_calls if call.get("session_resumed"))
    validation_retries = sum(1 for call in calls if call.get("outcome") == "VALIDATION_RETRY")
    escalation_events = sum(
        1
        for event in (convergence.get("events") or [])
        if isinstance(event, dict) and event.get("type") in {"AUTHOR_DEADLOCK", "REVIEWER_STORM"}
    )
    return {
        "user_decisions": {
            "count": len(decisions),
            "by_action": dict(sorted(Counter(str(item.get("action")) for item in decisions).items())),
        },
        "escalations": {
            # escalation_signals는 사용자 결정 시 비워지므로 현재 값만으로는 이력을
            # 알 수 없다. convergence.events에 남은 기록을 함께 센다.
            "current_signals": len(manifest.get("escalation_signals") or []),
            "recorded_events": escalation_events,
            "panel_calls": _int(manifest.get("panel_call_count")),
        },
        "retries": dict(manifest.get("retries") or {}),
        "validation_retry_calls": {
            "count": validation_retries,
            "total_calls": len(calls),
            "rate": (validation_retries / len(calls)) if calls else None,
        },
        "provider_call_count": dict(sorted(Counter(str(call.get("operation")) for call in calls).items())),
        "session_reuse_rate": (resumed / len(review_calls)) if review_calls else None,
    }


def _resource_metrics(manifest: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    started = _parse_timestamp(manifest.get("started_at"))
    finished = _parse_timestamp(manifest.get("finished_at"))
    wall_clock = (finished - started).total_seconds() if started and finished else None
    if wall_clock is None:
        warnings.append("실행이 아직 끝나지 않아 소요 시간을 계산하지 않았습니다.")
    usage = manifest.get("usage") or {}
    if not usage:
        warnings.append("manifest.usage가 비어 있습니다. 토큰 수집 이전에 만들어진 실행입니다.")
        return {"wall_clock_seconds": wall_clock, "usage": None, "usage_incomplete": None}
    unreported_calls = sum(
        _int(totals.get("calls_unreported")) for totals in usage.values() if isinstance(totals, dict)
    )
    unreported_attempts = sum(
        _int(totals.get("attempts_unreported")) for totals in usage.values() if isinstance(totals, dict)
    )
    if unreported_calls or unreported_attempts:
        warnings.append(
            "사용량을 보고하지 않은 호출이 있어 해당 제공자의 토큰 합계는 하한값입니다: "
            f"논리 호출 {unreported_calls}건, 시도 {unreported_attempts}건"
        )
    # 제공자마다 오차의 방향이 다르다. CLI 제공자는 미보고 호출 때문에
    # 하한값이고, 작성자는 시간 창에 무관한 작업이 섞일 수 있어 상한값이다.
    # 두 값을 한 숫자로 합치면 방향이 사라지므로 제공자별로 표시한다.
    upper_bound_providers = sorted(
        name for name, totals in usage.items() if isinstance(totals, dict) and totals.get("upper_bound")
    )
    if upper_bound_providers:
        warnings.append(
            "시간 창으로 귀속한 제공자가 있어 그 합계는 상한값입니다: "
            f"{', '.join(upper_bound_providers)}"
        )
    return {
        "wall_clock_seconds": wall_clock,
        "usage": usage,
        "usage_incomplete": bool(unreported_calls or unreported_attempts),
        "usage_unreported_calls": unreported_calls,
        "usage_unreported_attempts": unreported_attempts,
        "usage_upper_bound_providers": upper_bound_providers,
    }


def compute_process_metrics(
    manifest: dict[str, Any],
    convergence: dict[str, Any],
    registry: dict[str, Any],
    final_blinds: list[tuple[str, dict[str, Any]]],
    reconciliations: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    """산출물 내용만으로 지표를 계산한다. 파일 시스템을 보지 않는다.

    `final_blinds`와 `reconciliations`는 (파일명, 내용) 쌍의 목록이며 시도
    순서로 정렬돼 있어야 한다. 같은 시도의 두 파일은 파일명이 같다.
    """
    warnings: list[str] = []
    rounds = sorted(
        (record for record in (convergence.get("rounds") or []) if isinstance(record, dict)),
        key=lambda record: _int(record.get("round")),
    )
    return {
        "convergence": _convergence_metrics(manifest, rounds, warnings),
        "leakage": _leakage_metrics(registry, final_blinds, reconciliations, warnings),
        "issues": _issue_metrics(registry, rounds),
        "friction": _friction_metrics(manifest, convergence),
        "resources": _resource_metrics(manifest, warnings),
        "warnings": warnings,
    }


def _load_pairs(paths: list[Path]) -> list[tuple[str, dict[str, Any]]]:
    return [(path.name, read_json(path, default={})) for path in paths]


def evaluate_run(run_dir: Path, *, write: bool = True) -> dict[str, Any]:
    """실행 하나를 평가한다. 실행 상태는 바꾸지 않는다.

    `assert_source_unchanged`를 부르지 않는다 — 평가는 실행이 끝난 뒤 다른
    코드 버전에서 해도 유효하다. 대신 실행 시점과 평가 시점의 코드 정보를
    함께 남겨 구분한다.
    """
    manifest = read_json(layout.manifest(run_dir))
    convergence_path = layout.convergence(run_dir)
    convergence = read_json(convergence_path, default={"rounds": [], "events": []})
    registry = read_json(layout.registry(run_dir), default={})
    metric_manifest = {
        **manifest,
        "counters": {**(manifest.get("counters") or {})},
    }
    counters = metric_manifest["counters"]
    # counters 도입 이전의 layout v2 실행도 파일 종류로 정확히 복원한다.
    counters.setdefault("iterative_reviews", len(layout.iter_reviews(run_dir)))
    counters.setdefault("promotions", len(layout.iter_promoted(run_dir)))
    counters.setdefault("final_blind_attempts", len(layout.iter_blinds(run_dir)))
    metrics = compute_process_metrics(
        metric_manifest,
        convergence,
        registry,
        _load_pairs(layout.iter_blinds(run_dir)),
        _load_pairs(layout.iter_reconciliations(run_dir)),
    )
    if not convergence_path.exists():
        metrics["warnings"].insert(0, "convergence.json이 없습니다.")

    run_environment = manifest.get("environment") or {}
    evaluator_commit = git_commit()
    evaluator_hash = ensemble_source_hash()
    if run_environment.get("git_commit") != evaluator_commit:
        metrics["warnings"].append(
            "실행 시점과 평가 시점의 git 커밋이 다릅니다. 지표 정의가 바뀌었을 수 있습니다."
        )
    if run_environment.get("ensemble_source_hash") != evaluator_hash:
        metrics["warnings"].append(
            "실행 시점과 평가 시점의 Ensemble 코드 해시가 다릅니다."
        )
    result = {
        "schema_version": SCHEMA_VERSION,
        "run_id": manifest.get("run_id"),
        "request_hash": manifest.get("request_hash"),
        "evaluated_at": utc_now(),
        "run_git_commit": run_environment.get("git_commit"),
        "run_source_hash": run_environment.get("ensemble_source_hash"),
        "evaluator_git_commit": evaluator_commit,
        "evaluator_source_hash": evaluator_hash,
        **metrics,
    }
    if write:
        _write_metrics(run_dir, result)
    return result


def _write_metrics(run_dir: Path, result: dict[str, Any]) -> None:
    path = layout.process_metrics(run_dir)
    if path.exists():
        previous = read_json(path, default={})
        previous_at = previous.get("evaluated_at")
        if previous_at and previous_at != result["evaluated_at"]:
            archive = layout.process_metrics_archive(run_dir, filename_timestamp(previous_at))
            if not archive.exists():
                atomic_write_json(archive, previous, overwrite=False)
    atomic_write_json(path, result)


def load_or_compute(run_dir: Path) -> dict[str, Any]:
    """같은 지표 구현으로 계산된 결과만 재사용한다."""
    path = layout.process_metrics(run_dir)
    if path.exists():
        existing = read_json(path)
        if existing.get("evaluator_source_hash") == ensemble_source_hash():
            return existing
    return evaluate_run(run_dir)


# 비교 표에 싣는 지표. 경로는 결과 JSON 안의 (섹션, 키) 쌍이다.
COMPARISON_METRICS = (
    ("convergence", "review_rounds"),
    ("convergence", "draft_rounds"),
    ("convergence", "final_state"),
    ("convergence", "rounds_to_zero_backlog"),
    ("leakage", "final_blind_attempts"),
    ("leakage", "final_blind_first_pass"),
    ("leakage", "leakage_rate_lower_bound"),
    ("leakage", "leakage_rate_observed"),
    ("leakage", "unique_unpromoted_final_blind_blockers"),
    ("issues", "total_issues"),
    ("issues", "acceptance_rate"),
    ("issues", "resolved_without_relevant_edit"),
    ("issues", "regression_count"),
    ("friction", "session_reuse_rate"),
    ("resources", "wall_clock_seconds"),
    ("resources", "usage_incomplete"),
)


def compare_runs(run_dirs: list[Path]) -> dict[str, Any]:
    """여러 실행의 지표를 병렬로 늘어놓는다.

    요청이 다른 실행끼리의 비교는 참고용이다. 회귀 판단에는 같은 케이스를
    반복한 실행(3층)만 쓴다. 그래서 요청 해시를 표에 함께 싣는다.
    """
    results = [load_or_compute(run_dir) for run_dir in run_dirs]
    request_hashes = {result.get("request_hash") for result in results}
    table = {
        f"{section}.{key}": [result.get(section, {}).get(key) for result in results]
        for section, key in COMPARISON_METRICS
    }
    return {
        "runs": [
            {
                "run_dir": str(run_dir),
                "run_id": result.get("run_id"),
                "request_hash": result.get("request_hash"),
                "evaluated_at": result.get("evaluated_at"),
                "warnings": result.get("warnings", []),
            }
            for run_dir, result in zip(run_dirs, results)
        ],
        "same_request": len(request_hashes) == 1,
        "metrics": table,
    }

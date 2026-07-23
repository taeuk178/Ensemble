from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import (
    CRITERIA,
    DEFAULT_MAX_FINAL_BLIND_ATTEMPTS,
    DEFAULT_MAX_PANEL_CALLS,
    DEFAULT_MAX_ROUNDS,
    DEFAULT_MAX_TOTAL_PROVIDER_CALLS,
    DEFAULT_PANEL_EFFORT,
    DEFAULT_PANEL_MODEL,
    DEFAULT_REVIEW_EFFORT,
    DEFAULT_REVIEW_MODEL,
    DEFAULT_REVIEW_SESSION_MAX_ROUNDS,
    RUNS_ROOT,
    OPEN_STATUSES,
    PAUSED_STATES,
    TERMINAL_STATES,
    USAGE_FIELDS,
)
from .environment import ensemble_source_hash, environment_snapshot
from .errors import InputError, StateError
from .hashing import evidence_ref_hashes, section_hashes
from . import layout
from .io_utils import (
    atomic_write_json,
    atomic_write_text,
    detect_sensitive_text,
    find_consumed_request_hash,
    make_run_id,
    read_json,
    sha256_text,
    utc_now,
)


def _markdown_fence(value: str) -> str:
    longest = max((len(match) for match in re.findall(r"`+", value)), default=0)
    return "`" * max(3, longest + 1)


def render_request(request: str) -> str:
    fence = _markdown_fence(request)
    return f"""# 요청

## 사용자 원문

{fence}text
{request}
{fence}

## 구조화된 작업 입력

- 목표: 사용자 원문을 바탕으로 작성자가 구체화
- 대상 사용자: 명시되지 않음
- 주요 결과물: 구현 가능한 명세
- 포함 범위: 사용자 원문에 명시된 범위
- 제외 범위: 명시되지 않음
- 제약사항: 사용자 원문 참조
- 완료 조건: 완료 기준과 최종 독립 검토 통과

## 가정

- 구조화되지 않은 세부사항은 확정 요구가 아닌 가정으로 취급한다.

## 사용자 확인이 필요한 항목

- 없음
"""


def render_rubric() -> str:
    lines = [
        "# 완료 기준",
        "",
        "실제로 확인할 수 있는 문제에만 아래 기준을 적용한다.",
        "",
        "| ID | 수용 기준 |",
        "|---|---|",
    ]
    lines.extend(f"| {criterion_id} | {description} |" for criterion_id, description in CRITERIA.items())
    lines.extend(
        [
            "",
            "- 중요도 3 이상 이슈에는 문제 근거, 구현에 미치는 영향, 필요한 변경을 모두 적는다.",
            "- 문체 문제는 구현이나 이해를 실제로 막을 때만 진행 차단 이슈로 본다.",
        ]
    )
    return "\n".join(lines)


def initialize_run(
    request: str,
    *,
    phase: str = "2",
    review_model: str = DEFAULT_REVIEW_MODEL,
    panel_model: str = DEFAULT_PANEL_MODEL,
    panel_effort: str = DEFAULT_PANEL_EFFORT,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    max_final_blind_attempts: int = DEFAULT_MAX_FINAL_BLIND_ATTEMPTS,
    max_total_provider_calls: int = DEFAULT_MAX_TOTAL_PROVIDER_CALLS,
    max_panel_calls: int = DEFAULT_MAX_PANEL_CALLS,
    allow_reuse: bool = False,
    allow_sensitive: bool = False,
    label: str | None = None,
    author_model: str | None = None,
    benchmark: dict[str, Any] | None = None,
    reset_review_session_after_promotion: bool = True,
) -> Path:
    if not request.strip():
        raise InputError("요청 내용이 비어 있습니다.")
    for name, value in {
        "max_rounds": max_rounds,
        "max_final_blind_attempts": max_final_blind_attempts,
        "max_total_provider_calls": max_total_provider_calls,
        "max_panel_calls": max_panel_calls,
    }.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise InputError(f"{name}은 1 이상의 정수여야 합니다.")
    # 비밀정보 차단은 실행이 만들어지기 전에 일어나야 하므로 여기서 본다.
    # 3층 `init_block` 케이스가 검증하는 지점도 이 검사다.
    sensitive = detect_sensitive_text(request)
    if sensitive and not allow_sensitive:
        raise InputError(
            "외부 모델에 보내면 안 될 수 있는 민감정보 패턴이 감지됐습니다.",
            details={"patterns": sensitive, "override": "--allow-sensitive"},
        )
    request_hash = sha256_text(request)
    reused = find_consumed_request_hash(request_hash)
    if reused and not allow_reuse:
        raise InputError(
            "같은 요청으로 만든 실행이 이미 있습니다. 다시 실행하려면 --allow-reuse를 지정해 주세요.",
            details={"previous_runs": reused},
        )
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    run_id = make_run_id(request)
    run_dir = RUNS_ROOT / run_id
    if run_dir.exists():
        raise StateError(f"실행 ID가 겹쳤습니다: {run_id}")
    for relative in layout.RUN_SUBDIRS:
        (run_dir / relative).mkdir(parents=True, exist_ok=False)
    atomic_write_text(layout.request(run_dir), render_request(request), overwrite=False)
    atomic_write_text(
        layout.request_original(run_dir),
        request,
        overwrite=False,
        ensure_trailing_newline=False,
    )
    atomic_write_text(layout.rubric(run_dir), render_rubric(), overwrite=False)
    atomic_write_json(
        layout.user_decisions(run_dir),
        {"schema_version": 1, "decisions": []},
        overwrite=False,
    )
    user_decisions_hash = sha256_text(
        layout.user_decisions(run_dir).read_text(encoding="utf-8")
    )
    atomic_write_json(layout.registry(run_dir), {}, overwrite=False)
    atomic_write_json(layout.reviewer_index(run_dir), [], overwrite=False)
    atomic_write_json(layout.convergence(run_dir), {"rounds": [], "events": []}, overwrite=False)
    atomic_write_text(layout.feedback_cards(run_dir), "# 이슈 검토 자료\n", overwrite=False)
    atomic_write_text(layout.decisions(run_dir), "# Decisions\n", overwrite=False)
    manifest = {
        "schema_version": 1,
        "layout_version": layout.LAYOUT_VERSION,
        "run_id": run_id,
        "phase": phase,
        # 사람이 읽는 표식. 실행 식별에는 쓰지 않는다(아래 benchmark 참조).
        "label": (label or "").strip() or None,
        # 벤치마크 실행을 케이스에 잇는 계약. runner는 label이 아니라
        # 이 블록(benchmark_run_id, case_id, case_revision_hash 등)으로
        # 실행을 수집한다.
        "benchmark": benchmark,
        "state": "INITIALIZED",
        "state_history": [
            {
                "recorded_at": utc_now(),
                "from": None,
                "to": "INITIALIZED",
                "reason": "run initialized",
            }
        ],
        "current_round": 0,
        "request_hash": request_hash,
        "user_decisions_hash": user_decisions_hash,
        "started_at": utc_now(),
        "finished_at": None,
        "termination_reason": None,
        "models": {
            "codex": {
                "requested": review_model,
                "actual": None,
                "cli_version": None,
                "requested_reasoning_effort": DEFAULT_REVIEW_EFFORT,
                "reasoning_effort": None,
            },
            "agy": {
                "requested": panel_model,
                "actual": None,
                "cli_version": None,
                "requested_reasoning_effort": panel_effort,
                "reasoning_effort": None,
            },
            # 작성자(Claude) 런타임은 CLI가 감지할 수 없어 호출자가 알려준
            # 값만 기록한다. 점수 비교 시 모델 구성이 같은지 확인하는 용도다.
            "author": {"requested": (author_model or "").strip() or None},
        },
        "limits": {
            # review_rounds는 구버전 소비자를 위한 별칭이다.
            "review_rounds": max_rounds,
            "iterative_reviews": max_rounds,
            "final_blind_attempts": max_final_blind_attempts,
            "total_provider_calls": max_total_provider_calls,
            "panel_calls": max_panel_calls,
        },
        "counters": {
            "sequence_round": 0,
            "iterative_reviews": 0,
            "promotions": 0,
            "final_blind_attempts": 0,
        },
        "retries": {"schema": 0, "semantic": 0, "infra": 0},
        "usage": {},
        "warnings": [],
        "environment": environment_snapshot(),
        "provider_calls": [],
        "codex_review_session": None,
        "review_session_policy": {
            "reset_after_final_promotion": bool(reset_review_session_after_promotion),
            "max_rounds_per_session": DEFAULT_REVIEW_SESSION_MAX_ROUNDS,
            "epoch": 0,
            "reset_count": 0,
        },
        "retry_events": [],
        "review_history": [],
        "user_decisions": [],
        "pending_user_issue_ids": [],
        "pending_panel_issue_ids": [],
        "repair_plan_required_issue_ids": [],
        "escalation_signals": [],
    }
    atomic_write_json(layout.manifest(run_dir), manifest, overwrite=False)
    return run_dir


def update_manifest(run_dir: Path, **changes: Any) -> dict[str, Any]:
    manifest = read_json(layout.manifest(run_dir))
    if "state" in changes:
        transition_state(
            manifest,
            str(changes.pop("state")),
            reason=str(changes.get("termination_reason") or "manifest update"),
        )
    manifest.update(changes)
    atomic_write_json(layout.manifest(run_dir), manifest)
    return manifest


def transition_state(
    manifest: dict[str, Any],
    state: str,
    *,
    reason: str | None = None,
) -> None:
    """상태를 바꾸면서 벤치마크가 읽을 수 있는 이력을 남긴다."""
    previous = str(manifest.get("state") or "") or None
    manifest["state"] = state
    if previous == state:
        return
    manifest.setdefault("state_history", []).append(
        {
            "recorded_at": utc_now(),
            "from": previous,
            "to": state,
            "reason": reason,
        }
    )


def verified_request_hash(run_dir: Path) -> str:
    manifest = read_json(layout.manifest(run_dir))
    original = (layout.request_original(run_dir)).read_text(encoding="utf-8")
    actual = sha256_text(original)
    expected = str(manifest.get("request_hash") or "")
    if not expected or actual != expected:
        raise StateError("현재 요청이 실행 시작 시 기록한 요청과 다릅니다. 검토 세션을 재사용할 수 없습니다.")
    return actual


def load_codex_review_session(run_dir: Path) -> dict[str, Any] | None:
    manifest = read_json(layout.manifest(run_dir))
    request_hash = verified_request_hash(run_dir)
    session = manifest.get("codex_review_session")
    if not session:
        return None
    if not isinstance(session, dict):
        raise StateError("Codex 검토 세션 기록 형식이 올바르지 않습니다.")
    if session.get("request_hash") != request_hash:
        raise StateError("Codex 검토 세션이 다른 요청에 연결되어 있어 재사용할 수 없습니다.")
    if session.get("run_id") != manifest.get("run_id"):
        raise StateError("Codex 검토 세션이 다른 실행에 연결되어 있어 재사용할 수 없습니다.")
    if session.get("purpose") != "review":
        raise StateError("Codex 세션의 용도가 일반 검토가 아닙니다.")
    if not session.get("session_id"):
        raise StateError("Codex 검토 세션 ID가 비어 있습니다.")
    return dict(session)


def record_codex_review_session(
    run_dir: Path,
    *,
    session_id: str,
    review_round: int,
    workspace: Path,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", session_id):
        raise StateError("Codex 검토 세션 ID 형식이 올바르지 않습니다.")
    manifest = read_json(layout.manifest(run_dir))
    request_hash = verified_request_hash(run_dir)
    workspace_resolved = workspace.resolve()
    run_resolved = run_dir.resolve()
    if not workspace_resolved.is_relative_to(run_resolved):
        raise StateError("Codex 검토 세션 작업 폴더가 현재 실행 밖에 있습니다.")
    relative_workspace = workspace_resolved.relative_to(run_resolved).as_posix()
    existing = manifest.get("codex_review_session")
    if existing:
        if not isinstance(existing, dict):
            raise StateError("Codex 검토 세션 기록 형식이 올바르지 않습니다.")
        if existing.get("request_hash") != request_hash or existing.get("run_id") != manifest.get("run_id"):
            raise StateError("다른 요청이나 실행의 Codex 세션으로 바꿀 수 없습니다.")
        if existing.get("session_id") != session_id:
            raise StateError("검토 도중 Codex 세션 ID가 변경되었습니다.")
        if existing.get("workspace") != relative_workspace:
            raise StateError("검토 도중 Codex 세션 작업 폴더가 변경되었습니다.")
        started_at = existing.get("started_at")
        first_review_round = existing.get("first_review_round")
    else:
        started_at = utc_now()
        first_review_round = review_round
    record = {
        "session_id": session_id,
        "request_hash": request_hash,
        "run_id": manifest.get("run_id"),
        "purpose": "review",
        "workspace": relative_workspace,
        "first_review_round": first_review_round,
        "last_review_round": review_round,
        "started_at": started_at,
        "updated_at": utc_now(),
    }
    manifest["codex_review_session"] = record
    atomic_write_json(layout.manifest(run_dir), manifest)
    return record


def assert_run_can_advance(run_dir: Path, action: str) -> dict[str, Any]:
    manifest = read_json(layout.manifest(run_dir))
    state = str(manifest.get("state"))
    if state in PAUSED_STATES:
        raise StateError(
            f"{action} 명령을 실행할 수 없습니다. 현재 상태는 {state}입니다. 사용자 선택을 먼저 기록해 주세요.",
            details={"state": state, "next_command": "resolve-user-decision"},
        )
    if state in TERMINAL_STATES:
        raise StateError(f"{action} 명령을 실행할 수 없습니다. 실행이 {state} 상태로 종료되었습니다.")
    return manifest


def iterative_review_count(run_dir: Path, manifest: dict[str, Any] | None = None) -> int:
    manifest = manifest or read_json(layout.manifest(run_dir))
    value = (manifest.get("counters") or {}).get("iterative_reviews")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return len(layout.iter_reviews(run_dir))


def final_blind_attempt_count(run_dir: Path, manifest: dict[str, Any] | None = None) -> int:
    manifest = manifest or read_json(layout.manifest(run_dir))
    value = (manifest.get("counters") or {}).get("final_blind_attempts")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return len(layout.iter_blinds(run_dir))


def assert_iterative_review_budget(run_dir: Path) -> dict[str, Any]:
    manifest = read_json(layout.manifest(run_dir))
    limit = int(
        (manifest.get("limits") or {}).get(
            "iterative_reviews",
            (manifest.get("limits") or {}).get("review_rounds", DEFAULT_MAX_ROUNDS),
        )
    )
    used = iterative_review_count(run_dir, manifest)
    if used >= limit:
        raise StateError(
            "일반 검토 횟수 한도에 도달했습니다.",
            details={"limit_kind": "ITERATIVE_REVIEWS", "used": used, "limit": limit},
        )
    return manifest


def assert_final_blind_budget(run_dir: Path) -> dict[str, Any]:
    manifest = read_json(layout.manifest(run_dir))
    limit = int(
        (manifest.get("limits") or {}).get(
            "final_blind_attempts",
            DEFAULT_MAX_FINAL_BLIND_ATTEMPTS,
        )
    )
    used = final_blind_attempt_count(run_dir, manifest)
    if used >= limit:
        raise StateError(
            "최종 독립 검토 횟수 한도에 도달했습니다.",
            details={"limit_kind": "FINAL_BLIND_ATTEMPTS", "used": used, "limit": limit},
        )
    return manifest


def assert_final_blind_ready(run_dir: Path) -> dict[str, Any]:
    """현재 초안이 일반 검토를 통과했고 아직 독립 검토되지 않았는지 확인한다."""
    manifest = read_json(layout.manifest(run_dir))
    current_round = int(manifest.get("current_round", 0))
    if (
        manifest.get("state") != "APPROVED"
        or manifest.get("last_review_verdict") != "APPROVED"
        or int(manifest.get("last_reviewed_draft_round", -1)) != current_round
    ):
        raise StateError(
            "마지막 새 검토는 현재 초안이 일반 검토를 통과한 뒤에만 실행할 수 있습니다.",
            details={
                "state": manifest.get("state"),
                "current_draft_round": current_round,
                "last_reviewed_draft_round": manifest.get("last_reviewed_draft_round"),
                "last_review_verdict": manifest.get("last_review_verdict"),
            },
        )
    registry = read_json(layout.registry(run_dir), default={})
    open_gating = sorted(
        issue_id
        for issue_id, issue in registry.items()
        if isinstance(issue, dict)
        and issue.get("status") in OPEN_STATUSES
        and issue.get("gating", True)
    )
    if open_gating:
        raise StateError(
            "해결되지 않은 문제가 있어 마지막 새 검토를 실행할 수 없습니다.",
            details={"open_issue_ids": open_gating},
        )
    prior = layout.iter_blind_attempts(run_dir, current_round)
    if prior:
        raise StateError(
            "같은 초안에는 마지막 새 검토를 한 번만 실행할 수 있습니다. "
            "발견한 문제를 반영하고 일반 검토를 다시 통과시켜 주세요.",
            details={
                "draft_round": current_round,
                "previous_attempts": [path.name for path in prior],
                "next_command": "promote-final",
            },
        )
    return manifest


def assert_accept_risk_ready(run_dir: Path, issue_id: str) -> dict[str, Any]:
    manifest = read_json(layout.manifest(run_dir))
    pending = set(str(value) for value in manifest.get("pending_user_issue_ids", []))
    if manifest.get("state") != "USER_DECISION_REQUIRED" or issue_id not in pending:
        raise StateError(
            "위험 수용은 사용자 결정이 필요한 상태에서 제시된 이슈에만 기록할 수 있습니다.",
            details={
                "state": manifest.get("state"),
                "pending_user_issue_ids": sorted(pending),
            },
        )
    return manifest


def assert_provider_call_budget(run_dir: Path, *, needed: int = 1) -> dict[str, Any]:
    manifest = read_json(layout.manifest(run_dir))
    limit = int(
        (manifest.get("limits") or {}).get(
            "total_provider_calls",
            DEFAULT_MAX_TOTAL_PROVIDER_CALLS,
        )
    )
    used = len(
        [item for item in (manifest.get("provider_calls") or []) if isinstance(item, dict)]
    )
    if used + needed > limit:
        raise StateError(
            "전체 provider 호출 한도를 넘을 수 없습니다.",
            details={
                "limit_kind": "TOTAL_PROVIDER_CALLS",
                "used": used,
                "needed": needed,
                "limit": limit,
            },
        )
    return manifest


def record_final_blind_attempt(run_dir: Path) -> None:
    manifest = read_json(layout.manifest(run_dir))
    counters = manifest.setdefault("counters", {})
    counters["final_blind_attempts"] = final_blind_attempt_count(run_dir, manifest) + 1
    atomic_write_json(layout.manifest(run_dir), manifest)


def record_promotion(run_dir: Path) -> None:
    """승격은 검토 번호를 쓰지만 일반 검토 예산은 소비하지 않는다."""
    manifest = read_json(layout.manifest(run_dir))
    counters = manifest.setdefault("counters", {})
    counters["promotions"] = int(counters.get("promotions", 0) or 0) + 1
    counters["sequence_round"] = int(manifest.get("last_review_round", 0) or 0)
    policy = manifest.setdefault(
        "review_session_policy",
        {
            "reset_after_final_promotion": True,
            "max_rounds_per_session": DEFAULT_REVIEW_SESSION_MAX_ROUNDS,
            "epoch": 0,
            "reset_count": 0,
        },
    )
    if policy.get("reset_after_final_promotion", True):
        _reset_review_session_in_manifest(manifest, reason="FINAL_BLIND_PROMOTION")
    atomic_write_json(layout.manifest(run_dir), manifest)


def _reset_review_session_in_manifest(manifest: dict[str, Any], *, reason: str) -> None:
    policy = manifest.setdefault(
        "review_session_policy",
        {
            "reset_after_final_promotion": True,
            "max_rounds_per_session": DEFAULT_REVIEW_SESSION_MAX_ROUNDS,
            "epoch": 0,
            "reset_count": 0,
        },
    )
    manifest["codex_review_session"] = None
    policy["epoch"] = int(policy.get("epoch", 0) or 0) + 1
    policy["reset_count"] = int(policy.get("reset_count", 0) or 0) + 1
    policy["last_reset_reason"] = reason
    policy["last_reset_at"] = utc_now()


def reset_codex_review_session(run_dir: Path, *, reason: str) -> None:
    manifest = read_json(layout.manifest(run_dir))
    _reset_review_session_in_manifest(manifest, reason=reason)
    atomic_write_json(layout.manifest(run_dir), manifest)


def assert_source_unchanged(run_dir: Path) -> None:
    manifest = read_json(layout.manifest(run_dir))
    baseline = (manifest.get("environment") or {}).get("ensemble_source_hash")
    if not baseline:
        return
    current = ensemble_source_hash()
    if current == baseline:
        return
    transition_state(manifest, "RUN_TAINTED", reason="ensemble source changed")
    manifest["termination_reason"] = "Ensemble source changed after the run started"
    manifest["finished_at"] = utc_now()
    manifest.setdefault("environment_changes", []).append(
        {"recorded_at": utc_now(), "expected_source_hash": baseline, "actual_source_hash": current}
    )
    atomic_write_json(layout.manifest(run_dir), manifest)
    raise StateError(
        "실행 중 Ensemble 코드가 바뀌었습니다. 결과가 섞이지 않도록 새 실행을 시작해 주세요.",
        details={"state": "RUN_TAINTED", "expected": baseline, "actual": current},
    )


def empty_usage_totals() -> dict[str, int]:
    """제공자 하나의 사용량 집계 초기값."""
    totals = dict.fromkeys(USAGE_FIELDS, 0)
    totals.update(
        {
            "calls_reported": 0,
            "calls_unreported": 0,
            "attempts_reported": 0,
            "attempts_unreported": 0,
        }
    )
    return totals


def accumulate_usage(
    manifest: dict[str, Any],
    provider: str,
    usage: Any,
    *,
    attempts: int = 1,
    attempts_reported: int = 0,
) -> None:
    """제공자별 토큰 합계를 갱신한다.

    `calls_*`는 논리 호출(run_codex/run_agy 1회) 수, `attempts_*`는 그 안의
    재시도 1회 단위다. 사용량을 전혀 보고하지 않은 논리 호출은
    `calls_unreported`로만 센다. 일부 시도만 보고된 호출은 `calls_reported`로
    세면서 `attempts_unreported`가 올라가므로, 두 카운터 중 하나라도 0이
    아니면 토큰 합계는 하한값이다.
    """
    totals = manifest.setdefault("usage", {}).setdefault(provider, empty_usage_totals())
    for key, value in empty_usage_totals().items():
        totals.setdefault(key, value)
    attempts = max(int(attempts or 1), 1)
    attempts_reported = max(min(int(attempts_reported or 0), attempts), 0)
    totals["attempts_reported"] += attempts_reported
    totals["attempts_unreported"] += attempts - attempts_reported
    if not isinstance(usage, dict):
        totals["calls_unreported"] += 1
        return
    for key in USAGE_FIELDS:
        value = usage.get(key)
        totals[key] += int(value) if isinstance(value, int) and not isinstance(value, bool) else 0
    totals["calls_reported"] += 1


def record_provider_call(
    run_dir: Path,
    *,
    provider: str,
    operation: str,
    result: Any,
    round_number: int | None = None,
    outcome: str = "SUCCESS",
    error: str | None = None,
) -> None:
    manifest = read_json(layout.manifest(run_dir))
    model = manifest.setdefault("models", {}).setdefault(provider, {})
    model["actual"] = result.model
    model["cli_version"] = result.version
    model["command_path"] = result.executable
    if getattr(result, "reasoning_effort", None):
        model["reasoning_effort"] = result.reasoning_effort
    event = {
        "recorded_at": utc_now(),
        "provider": provider,
        "operation": operation,
        "round": round_number,
        "model": result.model,
        "cli_version": result.version,
        "command_path": result.executable,
        "reasoning_effort": getattr(result, "reasoning_effort", None),
        "attempts": result.attempts,
        "attempt_errors": list(result.attempt_errors),
        "outcome": outcome,
        "session_id": getattr(result, "session_id", None),
        "session_resumed": bool(getattr(result, "session_resumed", False)),
        "input_manifest": list(getattr(result, "input_manifest", ()) or ()),
        "usage": getattr(result, "usage", None),
        "attempts_reported": int(getattr(result, "attempts_reported", 0) or 0),
    }
    if error:
        event["error"] = error
    manifest.setdefault("provider_calls", []).append(event)
    accumulate_usage(
        manifest,
        provider,
        event["usage"],
        attempts=int(result.attempts),
        attempts_reported=int(getattr(result, "attempts_reported", 0) or 0),
    )
    for attempt_error in result.attempt_errors:
        kind = "schema" if attempt_error.get("kind") == "schema" else "infra"
        manifest.setdefault("retries", {}).setdefault(kind, 0)
        manifest["retries"][kind] += 1
    atomic_write_json(layout.manifest(run_dir), manifest)


def record_retry_event(
    run_dir: Path,
    *,
    retry_type: str,
    operation: str,
    round_number: int | None,
    attempt: int,
    error: str,
) -> None:
    manifest = read_json(layout.manifest(run_dir))
    manifest.setdefault("retry_events", []).append(
        {
            "recorded_at": utc_now(),
            "type": retry_type,
            "operation": operation,
            "round": round_number,
            "attempt": attempt,
            "error": error,
        }
    )
    atomic_write_json(layout.manifest(run_dir), manifest)


def record_provider_failure(
    run_dir: Path,
    *,
    operation: str,
    round_number: int | None,
    details: dict[str, Any],
    error: str,
) -> None:
    provider = str(details.get("provider") or "codex")
    manifest = read_json(layout.manifest(run_dir))
    model = manifest.setdefault("models", {}).setdefault(provider, {})
    if details.get("model"):
        model["actual"] = details["model"]
    if details.get("cli_version"):
        model["cli_version"] = details["cli_version"]
    if details.get("command_path"):
        model["command_path"] = details["command_path"]
    if details.get("reasoning_effort"):
        model["reasoning_effort"] = details["reasoning_effort"]
    manifest.setdefault("provider_calls", []).append(
        {
            "recorded_at": utc_now(),
            "provider": provider,
            "operation": operation,
            "round": round_number,
            "model": details.get("model"),
            "cli_version": details.get("cli_version"),
            "command_path": details.get("command_path"),
            "reasoning_effort": details.get("reasoning_effort"),
            "attempts": details.get("attempts"),
            "attempt_errors": details.get("attempt_errors", []),
            "input_manifest": details.get("input_manifest", []),
            "outcome": "FAILED",
            "error": error,
            "usage": details.get("usage"),
            "attempts_reported": int(details.get("attempts_reported", 0) or 0),
        }
    )
    # Codex가 실패 시도에도 usage 이벤트를 남기면 합산하고, 보고되지 않은
    # 시도만 하한으로 표시한다. Agy는 현재 사용량 보고 수단이 없다.
    attempts = details.get("attempts")
    accumulate_usage(
        manifest,
        provider,
        details.get("usage"),
        attempts=int(attempts) if isinstance(attempts, int) else 1,
        attempts_reported=int(details.get("attempts_reported", 0) or 0),
    )
    atomic_write_json(layout.manifest(run_dir), manifest)


def _record_authoritative_decisions(
    run_dir: Path,
    decisions: list[dict[str, Any]],
) -> list[str]:
    projection = read_json(
        layout.user_decisions(run_dir),
        default={"schema_version": 1, "decisions": []},
    )
    records = projection.setdefault("decisions", [])
    if not isinstance(records, list):
        raise StateError("user-decisions.json의 decisions가 배열이 아닙니다.")
    known = {
        str(record.get("decision_id")): record
        for record in records
        if isinstance(record, dict) and record.get("decision_id")
    }
    created: list[str] = []
    for value in decisions:
        if not isinstance(value, dict) or set(value) != {"decision", "supersedes"}:
            raise InputError(
                "authoritative_decisions 항목은 decision과 supersedes만 가져야 합니다."
            )
        decision = value.get("decision")
        supersedes = value.get("supersedes")
        if not isinstance(decision, str) or not decision.strip():
            raise InputError("권위 사용자 결정의 decision이 비어 있습니다.")
        if (
            not isinstance(supersedes, list)
            or not all(isinstance(item, str) and item.strip() for item in supersedes)
        ):
            raise InputError("권위 사용자 결정의 supersedes는 결정 ID 문자열 배열이어야 합니다.")
        unknown = sorted(set(supersedes) - set(known))
        if unknown:
            raise InputError(
                "존재하지 않는 사용자 결정을 supersedes로 지정했습니다.",
                details={"unknown_decision_ids": unknown},
            )
        for decision_id in supersedes:
            known[decision_id]["active"] = False
            known[decision_id]["superseded_at"] = utc_now()
        decision_id = f"UD-{len(records) + 1:03d}"
        record = {
            "decision_id": decision_id,
            "source": "USER",
            "decision": decision.strip(),
            "supersedes": list(supersedes),
            "active": True,
            "recorded_at": utc_now(),
        }
        records.append(record)
        known[decision_id] = record
        created.append(decision_id)
    atomic_write_json(layout.user_decisions(run_dir), projection)
    return created


def resolve_user_decision(
    run_dir: Path,
    *,
    action: str,
    note: str,
    authoritative_decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    manifest = read_json(layout.manifest(run_dir))
    from_state = str(manifest.get("state"))
    if from_state not in PAUSED_STATES:
        raise StateError(f"현재 상태 {from_state}에서는 사용자 결정으로 재개할 필요가 없습니다.")
    if action not in {"REVISE", "CONTINUE"}:
        raise InputError("재개 방식은 REVISE 또는 CONTINUE여야 합니다.")
    if not note.strip():
        raise InputError("사용자 결정 메모가 비어 있습니다.")
    if manifest.get("benchmark"):
        raise StateError(
            "벤치마크 실행은 사용자 개입 상태에서 재개할 수 없습니다. "
            "정지 상태 자체를 채점하고 다음 케이스로 진행해 주세요.",
            details={
                "state": from_state,
                "benchmark_case_completed": True,
                "case_id": (manifest.get("benchmark") or {}).get("case_id"),
            },
        )
    created_decision_ids = _record_authoritative_decisions(
        run_dir,
        authoritative_decisions or [],
    )
    manifest["user_decisions_hash"] = sha256_text(
        layout.user_decisions(run_dir).read_text(encoding="utf-8")
    )
    event = {
        "recorded_at": utc_now(),
        "from_state": from_state,
        "action": action,
        "note": note.strip(),
        "signals": manifest.get("escalation_signals", []),
        "pending_issue_ids": manifest.get("pending_user_issue_ids", []),
        "authoritative_decision_ids": created_decision_ids,
    }
    manifest.setdefault("user_decisions", []).append(event)
    transition_state(manifest, "DRAFT_READY", reason="user decision recorded")
    manifest["escalation_signals"] = []
    manifest["pending_user_issue_ids"] = []
    manifest["pending_panel_issue_ids"] = []
    atomic_write_json(layout.manifest(run_dir), manifest)
    decisions_path = layout.decisions(run_dir)
    existing = decisions_path.read_text(encoding="utf-8").rstrip()
    section = (
        f"\n\n## 사용자 결정 {event['recorded_at']}\n\n"
        f"- 이전 상태: {from_state}\n"
        f"- 결정: {action}\n"
        f"- 메모: {note.strip()}\n"
        f"- 권위 결정 ID: {', '.join(created_decision_ids) if created_decision_ids else '없음'}\n"
    )
    atomic_write_text(decisions_path, existing + section)
    return event


def add_manifest_warning(run_dir: Path, warning: str) -> None:
    manifest = read_json(layout.manifest(run_dir))
    if warning not in manifest["warnings"]:
        manifest["warnings"].append(warning)
    atomic_write_json(layout.manifest(run_dir), manifest)


def register_draft(run_dir: Path, source: Path, round_number: int) -> dict[str, Any]:
    assert_run_can_advance(run_dir, "save draft")
    manifest = read_json(layout.manifest(run_dir))
    repair_required = sorted(
        str(value) for value in (manifest.get("repair_plan_required_issue_ids") or [])
    )
    if repair_required:
        raise StateError(
            "같은 문제가 수정 후 다시 발견되어 근본 수정 계획이 필요합니다.",
            details={
                "issue_ids": repair_required,
                "next_command": "repair-plan",
            },
        )
    if round_number < 0:
        raise InputError("초안 번호는 0 이상이어야 합니다.")
    destination = layout.draft(run_dir, round_number)
    if destination.exists():
        raise StateError(f"같은 번호의 초안이 이미 있습니다: {destination.name}")
    content = source.read_text(encoding="utf-8")
    if not content.strip():
        raise InputError("빈 초안은 저장할 수 없습니다.")
    atomic_write_text(destination, content, overwrite=False)
    hashes = section_hashes(content)
    atomic_write_json(layout.hashes(run_dir, round_number), hashes, overwrite=False)
    invalidated = invalidate_accepted_risks(run_dir, content, round_number)
    from .convergence import record_draft_oscillation

    oscillation = record_draft_oscillation(run_dir, round_number, hashes)
    manifest = read_json(layout.manifest(run_dir))
    manifest["current_round"] = max(int(manifest.get("current_round", 0)), round_number)
    transition_state(
        manifest,
        "OSCILLATING" if oscillation["terminate"] else "DRAFT_READY",
        reason="draft registered",
    )
    if oscillation["terminate"]:
        manifest["termination_reason"] = "The same section oscillated for the second time"
        manifest["finished_at"] = utc_now()
    atomic_write_json(layout.manifest(run_dir), manifest)
    return {
        "draft": str(destination),
        "round": round_number,
        "invalidated_accepted_risks": invalidated,
        "oscillation": oscillation,
    }


def record_repair_plan(
    run_dir: Path,
    *,
    issue_id: str,
    round_number: int,
    plan: dict[str, Any],
) -> dict[str, Any]:
    required_fields = {
        "root_cause",
        "invariant",
        "counterexample",
        "state_model",
        "verification_steps",
    }
    if set(plan) != required_fields:
        raise InputError(
            "근본 수정 계획 필드가 올바르지 않습니다.",
            details={
                "missing": sorted(required_fields - set(plan)),
                "unknown": sorted(set(plan) - required_fields),
            },
        )
    for field in required_fields - {"verification_steps"}:
        value = plan.get(field)
        if not isinstance(value, str) or not value.strip():
            raise InputError(f"근본 수정 계획의 {field}가 비어 있습니다.")
    steps = plan.get("verification_steps")
    if (
        not isinstance(steps, list)
        or not steps
        or not all(isinstance(step, str) and step.strip() for step in steps)
    ):
        raise InputError("verification_steps는 비어 있지 않은 문자열 배열이어야 합니다.")
    manifest = read_json(layout.manifest(run_dir))
    pending = set(str(value) for value in manifest.get("repair_plan_required_issue_ids") or [])
    if issue_id not in pending:
        raise StateError(f"현재 근본 수정 계획이 필요한 이슈가 아닙니다: {issue_id}")
    registry = read_json(layout.registry(run_dir), default={})
    if issue_id not in registry:
        raise InputError(f"존재하지 않는 이슈 ID입니다: {issue_id}")
    record = {
        "issue_id": issue_id,
        "round": round_number,
        "recorded_at": utc_now(),
        **plan,
    }
    atomic_write_json(
        layout.repair_plan(run_dir, issue_id, round_number),
        record,
        overwrite=False,
    )
    pending.remove(issue_id)
    manifest["repair_plan_required_issue_ids"] = sorted(pending)
    atomic_write_json(layout.manifest(run_dir), manifest)
    return record


def invalidate_accepted_risks(run_dir: Path, markdown: str, round_number: int) -> list[str]:
    registry_path = layout.registry(run_dir)
    registry = read_json(registry_path, default={})
    invalidated: list[str] = []
    for issue_id, issue in registry.items():
        if issue.get("status") != "ACCEPTED_RISK":
            continue
        snapshot = issue.get("accepted_issue_snapshot") or {}
        accepted_hashes = snapshot.get("evidence_hashes_at_acceptance") or {}
        current_hashes = evidence_ref_hashes(markdown, accepted_hashes.keys())
        if any(current_hashes.get(ref) != expected for ref, expected in accepted_hashes.items()):
            issue.setdefault("acceptance_history", []).append(
                {
                    "accepted_at_round": issue.get("accepted_at_round"),
                    "invalidated_at_round": round_number,
                    "note": issue.get("acceptance_note"),
                    "snapshot": snapshot,
                }
            )
            issue["status"] = "OPEN"
            issue["gating"] = True
            issue["acceptance_invalidated_at_round"] = round_number
            invalidated.append(issue_id)
    if invalidated:
        atomic_write_json(registry_path, registry)
    return invalidated


def mark_terminal(run_dir: Path, status: str, reason: str) -> dict[str, Any]:
    if status not in TERMINAL_STATES:
        raise InputError(f"알 수 없는 종료 상태입니다: {status}")
    return update_manifest(
        run_dir,
        state=status,
        termination_reason=reason,
        finished_at=utc_now(),
    )

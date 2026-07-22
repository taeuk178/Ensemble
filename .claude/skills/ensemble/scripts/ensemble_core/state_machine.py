from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import (
    CRITERIA,
    DEFAULT_MAX_PANEL_CALLS,
    DEFAULT_MAX_ROUNDS,
    DEFAULT_PANEL_EFFORT,
    DEFAULT_PANEL_MODEL,
    DEFAULT_REVIEW_EFFORT,
    DEFAULT_REVIEW_MODEL,
    RUNS_ROOT,
    PAUSED_STATES,
    TERMINAL_STATES,
)
from .environment import ensemble_source_hash, environment_snapshot
from .errors import InputError, StateError
from .hashing import evidence_ref_hashes, section_hashes
from . import layout
from .io_utils import (
    atomic_write_json,
    atomic_write_text,
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
    max_panel_calls: int = DEFAULT_MAX_PANEL_CALLS,
    allow_reuse: bool = False,
) -> Path:
    if not request.strip():
        raise InputError("요청 내용이 비어 있습니다.")
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
    atomic_write_json(layout.registry(run_dir), {}, overwrite=False)
    atomic_write_json(layout.reviewer_index(run_dir), [], overwrite=False)
    atomic_write_json(layout.convergence(run_dir), {"rounds": [], "events": []}, overwrite=False)
    atomic_write_text(layout.feedback_cards(run_dir), "# 이슈 검토 자료\n", overwrite=False)
    atomic_write_text(layout.decisions(run_dir), "# Decisions\n", overwrite=False)
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "phase": phase,
        "state": "INITIALIZED",
        "current_round": 0,
        "request_hash": request_hash,
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
        },
        "limits": {"review_rounds": max_rounds, "panel_calls": max_panel_calls},
        "retries": {"schema": 0, "semantic": 0, "infra": 0},
        "usage": {},
        "warnings": [],
        "environment": environment_snapshot(),
        "provider_calls": [],
        "codex_review_session": None,
        "retry_events": [],
        "review_history": [],
        "user_decisions": [],
        "pending_user_issue_ids": [],
        "pending_panel_issue_ids": [],
        "escalation_signals": [],
    }
    atomic_write_json(layout.manifest(run_dir), manifest, overwrite=False)
    return run_dir


def update_manifest(run_dir: Path, **changes: Any) -> dict[str, Any]:
    manifest = read_json(layout.manifest(run_dir))
    manifest.update(changes)
    atomic_write_json(layout.manifest(run_dir), manifest)
    return manifest


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


def assert_source_unchanged(run_dir: Path) -> None:
    manifest = read_json(layout.manifest(run_dir))
    baseline = (manifest.get("environment") or {}).get("ensemble_source_hash")
    if not baseline:
        return
    current = ensemble_source_hash()
    if current == baseline:
        return
    manifest["state"] = "RUN_TAINTED"
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
    }
    if error:
        event["error"] = error
    manifest.setdefault("provider_calls", []).append(event)
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
            "outcome": "FAILED",
            "error": error,
        }
    )
    atomic_write_json(layout.manifest(run_dir), manifest)


def resolve_user_decision(run_dir: Path, *, action: str, note: str) -> dict[str, Any]:
    manifest = read_json(layout.manifest(run_dir))
    from_state = str(manifest.get("state"))
    if from_state not in PAUSED_STATES:
        raise StateError(f"현재 상태 {from_state}에서는 사용자 결정으로 재개할 필요가 없습니다.")
    if action not in {"REVISE", "CONTINUE"}:
        raise InputError("재개 방식은 REVISE 또는 CONTINUE여야 합니다.")
    if not note.strip():
        raise InputError("사용자 결정 메모가 비어 있습니다.")
    event = {
        "recorded_at": utc_now(),
        "from_state": from_state,
        "action": action,
        "note": note.strip(),
        "signals": manifest.get("escalation_signals", []),
        "pending_issue_ids": manifest.get("pending_user_issue_ids", []),
    }
    manifest.setdefault("user_decisions", []).append(event)
    manifest["state"] = "DRAFT_READY"
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
    manifest["state"] = "OSCILLATING" if oscillation["terminate"] else "DRAFT_READY"
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

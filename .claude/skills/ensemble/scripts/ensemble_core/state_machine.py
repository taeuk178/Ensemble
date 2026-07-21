from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import (
    CRITERIA,
    DEFAULT_MAX_PANEL_CALLS,
    DEFAULT_MAX_ROUNDS,
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
    return f"""# Request

## 사용자 원문

{fence}text
{request}
{fence}

## 구조화된 작업 입력

- 목표: 사용자 원문을 바탕으로 Claude가 구조화
- 대상 사용자: 명시되지 않음
- 주요 결과물: 구현 가능한 문서 스펙
- 포함 범위: 사용자 원문에 명시된 범위
- 제외 범위: 명시되지 않음
- 제약사항: 사용자 원문 참조
- 완료 조건: rubric의 수용 기준과 최종 블라인드 검증 통과

## 가정

- 구조화되지 않은 세부사항은 확정 요구가 아닌 가정으로 취급한다.

## 사용자 확인이 필요한 항목

- 없음
"""


def render_rubric() -> str:
    lines = [
        "# Acceptance Rubric",
        "",
        "각 기준은 관찰 가능한 위반을 지적할 때만 사용한다.",
        "",
        "| ID | 수용 기준 |",
        "|---|---|",
    ]
    lines.extend(f"| {criterion_id} | {description} |" for criterion_id, description in CRITERIA.items())
    lines.extend(
        [
            "",
            "- severity 3 이상은 구체적인 위반 근거, 구현 결과, 요구 변경을 모두 포함해야 한다.",
            "- 스타일 문제는 구현이나 의미 전달을 실질적으로 막을 때만 blocker다.",
        ]
    )
    return "\n".join(lines)


def initialize_run(
    request: str,
    *,
    phase: str = "2",
    review_model: str = DEFAULT_REVIEW_MODEL,
    panel_model: str = DEFAULT_PANEL_MODEL,
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
            "동일한 요청이 이전 실행에서 이미 사용되었습니다. 재사용하려면 --allow-reuse를 지정하세요.",
            details={"previous_runs": reused},
        )
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    run_id = make_run_id(request)
    run_dir = RUNS_ROOT / run_id
    if run_dir.exists():
        raise StateError(f"Run ID collision: {run_id}")
    for relative in (
        "proposals",
        "drafts",
        "reviews",
        "panel",
        "hashes",
        "bundles",
    ):
        (run_dir / relative).mkdir(parents=True, exist_ok=False)
    atomic_write_text(run_dir / "request.md", render_request(request), overwrite=False)
    atomic_write_text(
        run_dir / "request.original.txt",
        request,
        overwrite=False,
        ensure_trailing_newline=False,
    )
    atomic_write_text(run_dir / "rubric.md", render_rubric(), overwrite=False)
    atomic_write_json(run_dir / "issue-registry.json", {}, overwrite=False)
    atomic_write_json(run_dir / "reviewer-issue-index.json", [], overwrite=False)
    atomic_write_json(run_dir / "convergence.json", {"rounds": [], "events": []}, overwrite=False)
    atomic_write_text(run_dir / "feedback-cards.md", "# Feedback Cards\n", overwrite=False)
    atomic_write_text(run_dir / "decisions.md", "# Decisions\n", overwrite=False)
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
            "gemini": {"requested": panel_model, "actual": None, "cli_version": None},
        },
        "limits": {"review_rounds": max_rounds, "panel_calls": max_panel_calls},
        "retries": {"schema": 0, "semantic": 0, "infra": 0},
        "usage": {},
        "warnings": [],
        "environment": environment_snapshot(),
        "provider_calls": [],
        "retry_events": [],
        "review_history": [],
        "user_decisions": [],
        "pending_user_issue_ids": [],
        "pending_panel_issue_ids": [],
        "escalation_signals": [],
    }
    atomic_write_json(run_dir / "manifest.json", manifest, overwrite=False)
    return run_dir


def update_manifest(run_dir: Path, **changes: Any) -> dict[str, Any]:
    manifest = read_json(run_dir / "manifest.json")
    manifest.update(changes)
    atomic_write_json(run_dir / "manifest.json", manifest)
    return manifest


def assert_run_can_advance(run_dir: Path, action: str) -> dict[str, Any]:
    manifest = read_json(run_dir / "manifest.json")
    state = str(manifest.get("state"))
    if state in PAUSED_STATES:
        raise StateError(
            f"{action} 명령을 실행할 수 없습니다. 현재 상태는 {state}이며 명시적인 사용자 결정이 필요합니다.",
            details={"state": state, "next_command": "resolve-user-decision"},
        )
    if state in TERMINAL_STATES:
        raise StateError(f"{action} 명령을 실행할 수 없습니다. 실행이 {state} 상태로 종료되었습니다.")
    return manifest


def assert_source_unchanged(run_dir: Path) -> None:
    manifest = read_json(run_dir / "manifest.json")
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
    atomic_write_json(run_dir / "manifest.json", manifest)
    raise StateError(
        "실행 시작 후 Ensemble 코드가 변경되어 재현성을 보장할 수 없습니다. 새 run을 시작해 주세요.",
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
    manifest = read_json(run_dir / "manifest.json")
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
    }
    if error:
        event["error"] = error
    manifest.setdefault("provider_calls", []).append(event)
    for attempt_error in result.attempt_errors:
        kind = "schema" if attempt_error.get("kind") == "schema" else "infra"
        manifest.setdefault("retries", {}).setdefault(kind, 0)
        manifest["retries"][kind] += 1
    atomic_write_json(run_dir / "manifest.json", manifest)


def record_retry_event(
    run_dir: Path,
    *,
    retry_type: str,
    operation: str,
    round_number: int | None,
    attempt: int,
    error: str,
) -> None:
    manifest = read_json(run_dir / "manifest.json")
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
    atomic_write_json(run_dir / "manifest.json", manifest)


def record_provider_failure(
    run_dir: Path,
    *,
    operation: str,
    round_number: int | None,
    details: dict[str, Any],
    error: str,
) -> None:
    provider = str(details.get("provider") or "codex")
    manifest = read_json(run_dir / "manifest.json")
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
    atomic_write_json(run_dir / "manifest.json", manifest)


def resolve_user_decision(run_dir: Path, *, action: str, note: str) -> dict[str, Any]:
    manifest = read_json(run_dir / "manifest.json")
    from_state = str(manifest.get("state"))
    if from_state not in PAUSED_STATES:
        raise StateError(f"현재 상태 {from_state}에는 사용자 결정 재개가 필요하지 않습니다.")
    if action not in {"REVISE", "CONTINUE"}:
        raise InputError("재개 action은 REVISE 또는 CONTINUE여야 합니다.")
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
    atomic_write_json(run_dir / "manifest.json", manifest)
    decisions_path = run_dir / "decisions.md"
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
    manifest = read_json(run_dir / "manifest.json")
    if warning not in manifest["warnings"]:
        manifest["warnings"].append(warning)
    atomic_write_json(run_dir / "manifest.json", manifest)


def register_draft(run_dir: Path, source: Path, round_number: int) -> dict[str, Any]:
    assert_run_can_advance(run_dir, "save draft")
    if round_number < 0:
        raise InputError("Draft round must be non-negative")
    destination = run_dir / "drafts" / f"round-{round_number}.md"
    if destination.exists():
        raise StateError(f"Draft snapshot already exists: {destination.name}")
    content = source.read_text(encoding="utf-8")
    if not content.strip():
        raise InputError("Draft cannot be empty")
    atomic_write_text(destination, content, overwrite=False)
    hashes = section_hashes(content)
    atomic_write_json(run_dir / "hashes" / f"round-{round_number}.json", hashes, overwrite=False)
    invalidated = invalidate_accepted_risks(run_dir, content, round_number)
    from .convergence import record_draft_oscillation

    oscillation = record_draft_oscillation(run_dir, round_number, hashes)
    manifest = read_json(run_dir / "manifest.json")
    manifest["current_round"] = max(int(manifest.get("current_round", 0)), round_number)
    manifest["state"] = "OSCILLATING" if oscillation["terminate"] else "DRAFT_READY"
    if oscillation["terminate"]:
        manifest["termination_reason"] = "The same section oscillated for the second time"
        manifest["finished_at"] = utc_now()
    atomic_write_json(run_dir / "manifest.json", manifest)
    return {
        "draft": str(destination),
        "round": round_number,
        "invalidated_accepted_risks": invalidated,
        "oscillation": oscillation,
    }


def invalidate_accepted_risks(run_dir: Path, markdown: str, round_number: int) -> list[str]:
    registry_path = run_dir / "issue-registry.json"
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
        raise InputError(f"Unknown terminal status: {status}")
    return update_manifest(
        run_dir,
        state=status,
        termination_reason=reason,
        finished_at=utc_now(),
    )

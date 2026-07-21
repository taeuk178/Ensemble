from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import (
    CRITERIA,
    DEFAULT_MAX_PANEL_CALLS,
    DEFAULT_MAX_ROUNDS,
    DEFAULT_PANEL_MODEL,
    DEFAULT_REVIEW_MODEL,
    RUNS_ROOT,
    TERMINAL_STATES,
)
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
            "codex": {"requested": review_model, "actual": None, "cli_version": None},
            "gemini": {"requested": panel_model, "actual": None, "cli_version": None},
        },
        "limits": {"review_rounds": max_rounds, "panel_calls": max_panel_calls},
        "retries": {"schema": 0, "semantic": 0, "infra": 0},
        "usage": {},
        "warnings": [],
    }
    atomic_write_json(run_dir / "manifest.json", manifest, overwrite=False)
    return run_dir


def update_manifest(run_dir: Path, **changes: Any) -> dict[str, Any]:
    manifest = read_json(run_dir / "manifest.json")
    manifest.update(changes)
    atomic_write_json(run_dir / "manifest.json", manifest)
    return manifest


def add_manifest_warning(run_dir: Path, warning: str) -> None:
    manifest = read_json(run_dir / "manifest.json")
    if warning not in manifest["warnings"]:
        manifest["warnings"].append(warning)
    atomic_write_json(run_dir / "manifest.json", manifest)


def register_draft(run_dir: Path, source: Path, round_number: int) -> dict[str, Any]:
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

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .config import OPEN_STATUSES
from .errors import InputError, StateError
from .io_utils import atomic_write_text, read_json
from .registry import load_registry
from .state_machine import mark_terminal


def infer_terminal_status(run_dir: Path) -> tuple[str, str]:
    manifest = read_json(run_dir / "manifest.json")
    registry = load_registry(run_dir)
    open_gating = [
        issue_id
        for issue_id, issue in registry.items()
        if issue.get("status") in OPEN_STATUSES and issue.get("gating", True)
    ]
    reconciliation = read_json(run_dir / "final-reconciliation.json", default={})
    if not reconciliation:
        raise StateError("Cannot finalize automatically before FINAL_BLIND")
    latest_review_is_current_approval = (
        manifest.get("last_review_verdict") == "APPROVED"
        and int(manifest.get("last_reviewed_draft_round", -1))
        == int(manifest.get("current_round", 0))
    )
    if latest_review_is_current_approval and not open_gating and reconciliation.get("passed") is True:
        return "CONVERGED", "No unaccepted gating blockers remained after FINAL_BLIND reconciliation"
    if manifest.get("phase") == "1A":
        return "PROTOTYPE_INCOMPLETE", "FINAL_BLIND found new or unaccepted blockers in phase 1A"
    if int(manifest.get("last_review_round", 0)) >= int(manifest["limits"]["review_rounds"]):
        return "ITERATION_LIMIT_REACHED", "Review round limit reached with unresolved blockers"
    raise StateError(
        "The run is not terminal: promote FINAL_BLIND findings or resolve remaining gating issues"
    )


def _minority_appendix(registry: dict[str, Any]) -> str:
    lines = ["## 미해결 이견", ""]
    found = False
    for issue_id, issue in sorted(registry.items()):
        if issue.get("status") not in OPEN_STATUSES:
            continue
        found = True
        latest = issue.get("latest_issue") or {}
        author = (issue.get("author_disposition_history") or [{}])[-1]
        lines.extend(
            [
                f"### {issue_id} {latest.get('problem', '')}",
                f"- GPT: severity {latest.get('severity')} — {latest.get('implementation_consequence')}",
                f"- Claude(작성자): {author.get('value', '미기록')} — {author.get('claim', '판단 없음')}",
                f"- 상태: {issue.get('status')}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() if found else ""


def _accepted_risk_appendix(registry: dict[str, Any]) -> str:
    lines = ["## 사용자가 수용한 위험", ""]
    found = False
    for issue_id, issue in sorted(registry.items()):
        if issue.get("status") != "ACCEPTED_RISK":
            continue
        found = True
        snapshot = issue.get("accepted_issue_snapshot") or {}
        lines.extend(
            [
                f"### {issue_id} {snapshot.get('problem', '')} (severity {snapshot.get('severity')})",
                f"- 수용 라운드: {issue.get('accepted_at_round')}",
                f"- 수용 시점 이슈: {snapshot.get('implementation_consequence')}",
                f"- 근거 소재: {snapshot.get('basis')}",
                f"- 사용자 메모: {issue.get('acceptance_note')}",
                f"- 참조 섹션: {', '.join(snapshot.get('evidence_refs', []))}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() if found else ""


def finalize(run_dir: Path, *, status: str) -> dict[str, Any]:
    manifest = read_json(run_dir / "manifest.json")
    if status == "auto":
        status, reason = infer_terminal_status(run_dir)
    else:
        reason = f"Explicitly finalized as {status}"
        if status == "CONVERGED":
            inferred, inferred_reason = infer_terminal_status(run_dir)
            if inferred != "CONVERGED":
                raise StateError(f"CONVERGED invariants are not satisfied: {inferred_reason}")
    current_round = int(manifest.get("current_round", 0))
    draft_path = run_dir / "drafts" / f"round-{current_round}.md"
    if not draft_path.exists():
        candidates = sorted((run_dir / "drafts").glob("round-*.md"))
        if not candidates:
            raise StateError("No draft exists to finalize")
        draft_path = candidates[-1]
    registry = load_registry(run_dir)
    body = draft_path.read_text(encoding="utf-8").rstrip()
    appendices = [part for part in (_minority_appendix(registry), _accepted_risk_appendix(registry)) if part]
    header = f"<!-- ensemble-status: {status} -->\n"
    final_text = header + body
    if appendices:
        final_text += "\n\n---\n\n" + "\n\n".join(appendices)
    atomic_write_text(run_dir / "final.md", final_text)
    marked = mark_terminal(run_dir, status, reason)
    convergence = read_json(run_dir / "convergence.json", default={"rounds": []})
    resolution_counts: Counter[str] = Counter()
    resolved_without = 0
    for record in convergence.get("rounds", []):
        resolution_counts.update(record.get("resolution_basis_counts", {}))
        resolved_without += int(record.get("resolved_without_relevant_edit", 0))
    return {
        "status": status,
        "reason": reason,
        "final": str(run_dir / "final.md"),
        "rounds": len(convergence.get("rounds", [])),
        "issue_set_stalled_rounds": [
            record["round"] for record in convergence.get("rounds", []) if record.get("issue_set_stalled")
        ],
        "resolved_without_relevant_edit": resolved_without,
        "resolution_basis_counts": dict(resolution_counts),
        "manifest": marked,
    }

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
        raise StateError("최종 독립 검토를 마친 뒤에만 자동으로 종료할 수 있습니다.")
    latest_review_is_current_approval = (
        manifest.get("last_review_verdict") == "APPROVED"
        and int(manifest.get("last_reviewed_draft_round", -1))
        == int(manifest.get("current_round", 0))
    )
    if latest_review_is_current_approval and not open_gating and reconciliation.get("passed") is True:
        return "CONVERGED", "최종 독립 검토 후 남은 진행 차단 이슈가 없습니다."
    if manifest.get("phase") == "1A":
        return "PROTOTYPE_INCOMPLETE", "최종 독립 검토에서 새 진행 차단 이슈가 발견되었습니다."
    if int(manifest.get("last_review_round", 0)) >= int(manifest["limits"]["review_rounds"]):
        return "ITERATION_LIMIT_REACHED", "해결하지 못한 진행 차단 이슈가 있는 상태로 검토 횟수 한도에 도달했습니다."
    raise StateError(
        "아직 종료할 수 없습니다. 최종 독립 검토의 새 이슈를 등록하거나 남은 진행 차단 이슈를 해결해 주세요."
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
                f"- GPT: 중요도 {latest.get('severity')} — {latest.get('implementation_consequence')}",
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
                f"### {issue_id} {snapshot.get('problem', '')} (중요도 {snapshot.get('severity')})",
                f"- 수용한 검토 번호: {issue.get('accepted_at_round')}",
                f"- 예상 영향: {snapshot.get('implementation_consequence')}",
                f"- 판단 근거: {snapshot.get('basis')}",
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
        reason = f"사용자 지정 상태로 종료했습니다: {status}"
        if status == "CONVERGED":
            inferred, inferred_reason = infer_terminal_status(run_dir)
            if inferred != "CONVERGED":
                raise StateError(f"CONVERGED 조건을 충족하지 못했습니다: {inferred_reason}")
    current_round = int(manifest.get("current_round", 0))
    draft_path = run_dir / "drafts" / f"round-{current_round}.md"
    if not draft_path.exists():
        candidates = sorted((run_dir / "drafts").glob("round-*.md"))
        if not candidates:
            raise StateError("최종 문서로 만들 초안이 없습니다.")
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

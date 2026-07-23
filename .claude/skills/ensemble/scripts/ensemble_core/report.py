from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .claude_usage import record_claude_usage
from .config import OPEN_STATUSES
from .errors import EnsembleError, InputError, StateError
from .io_utils import atomic_write_text, read_json
from .registry import load_registry
from .state_machine import (
    add_manifest_warning,
    final_blind_attempt_count,
    iterative_review_count,
    mark_terminal,
)
from . import layout


def infer_terminal_status(run_dir: Path) -> tuple[str, str]:
    manifest = read_json(layout.manifest(run_dir))
    registry = load_registry(run_dir)
    open_gating = [
        issue_id
        for issue_id, issue in registry.items()
        if issue.get("status") in OPEN_STATUSES and issue.get("gating", True)
    ]
    reconciliation = read_json(layout.final_reconciliation(run_dir), default={})
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
    limits = manifest.get("limits") or {}
    if iterative_review_count(run_dir, manifest) >= int(
        limits.get("iterative_reviews", limits.get("review_rounds", 0))
    ):
        return (
            "ITERATION_LIMIT_REACHED",
            "해결하지 못한 진행 차단 이슈가 있는 상태로 일반 검토 횟수 한도에 도달했습니다.",
        )
    if final_blind_attempt_count(run_dir, manifest) >= int(
        limits.get("final_blind_attempts", 3)
    ):
        return (
            "ITERATION_LIMIT_REACHED",
            "최종 독립 검토가 통과하지 못한 상태로 독립 검토 시도 한도에 도달했습니다.",
        )
    raise StateError(
        "아직 종료할 수 없습니다. 최종 독립 검토의 새 이슈를 등록하거나 남은 진행 차단 이슈를 해결해 주세요."
    )


def finalize(run_dir: Path, *, status: str) -> dict[str, Any]:
    manifest = read_json(layout.manifest(run_dir))
    if status == "auto":
        status, reason = infer_terminal_status(run_dir)
    else:
        reason = f"사용자 지정 상태로 종료했습니다: {status}"
        if status == "CONVERGED":
            inferred, inferred_reason = infer_terminal_status(run_dir)
            if inferred != "CONVERGED":
                raise StateError(f"CONVERGED 조건을 충족하지 못했습니다: {inferred_reason}")
    current_round = int(manifest.get("current_round", 0))
    draft_path = layout.draft(run_dir, current_round)
    if not draft_path.exists():
        candidates = layout.iter_drafts(run_dir)
        if not candidates:
            raise StateError("최종 문서로 만들 초안이 없습니다.")
        draft_path = candidates[-1]
    body = draft_path.read_text(encoding="utf-8").rstrip()
    # final.md는 모델 검토 이력이나 상태 메타데이터를 섞지 않은 산출물이어야
    # 한다. 상태·이견·수용 위험은 manifest/registry/timeline에서만 보고한다.
    atomic_write_text(layout.final(run_dir), body)
    marked = mark_terminal(run_dir, status, reason)
    # 작성자 사용량은 종료 시각이 정해진 뒤에야 창이 확정된다. 세션 기록이
    # 없거나 읽을 수 없어도 종료 자체를 막지 않는다.
    try:
        record_claude_usage(run_dir)
        marked = read_json(layout.manifest(run_dir))
    except (EnsembleError, OSError) as exc:
        add_manifest_warning(run_dir, f"작성자 토큰 사용량을 수집하지 못했습니다: {exc}")
        marked = read_json(layout.manifest(run_dir))
    convergence = read_json(layout.convergence(run_dir), default={"rounds": []})
    resolution_counts: Counter[str] = Counter()
    resolved_without = 0
    for record in convergence.get("rounds", []):
        resolution_counts.update(record.get("resolution_basis_counts", {}))
        resolved_without += int(record.get("resolved_without_relevant_edit", 0))
    return {
        "status": status,
        "reason": reason,
        "final": str(layout.final(run_dir)),
        "rounds": len(convergence.get("rounds", [])),
        "issue_set_stalled_rounds": [
            record["round"] for record in convergence.get("rounds", []) if record.get("issue_set_stalled")
        ],
        "resolved_without_relevant_edit": resolved_without,
        "resolution_basis_counts": dict(resolution_counts),
        "manifest": marked,
    }

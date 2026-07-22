from __future__ import annotations

from pathlib import Path
from typing import Any

from .bundle import isolated_bundle
from .config import REFERENCE_ROOT
from .errors import SemanticValidationError, StateError
from .hashing import canonical_evidence_anchor, consequence_fingerprint
from .io_utils import atomic_write_json, read_json
from .providers import ProviderResult, run_codex
from .registry import load_registry
from .validation import validate_against_schema, validate_review_schema
from . import layout


def reconcile_final_findings(
    run_dir: Path,
    *,
    draft_path: Path,
    raw_review: dict[str, Any],
) -> dict[str, Any]:
    registry = load_registry(run_dir)
    markdown = draft_path.read_text(encoding="utf-8")
    accepted: list[dict[str, Any]] = []
    unaccepted: list[dict[str, Any]] = []
    active_risks = {
        issue_id: issue
        for issue_id, issue in registry.items()
        if issue.get("status") == "ACCEPTED_RISK" and issue.get("gating") is False
    }
    for index, finding in enumerate(raw_review["blocking_issues"]):
        anchor = canonical_evidence_anchor(
            markdown,
            location=finding["location"],
            violation_evidence=finding["violation_evidence"],
            required_change=finding["required_change"],
            unmatched_salt=f"final-{index}",
            evidence_refs=finding.get("evidence_refs", []),
        )
        consequence = consequence_fingerprint(finding["implementation_consequence"])
        candidates: list[str] = []
        if not anchor.startswith("UNMATCHED:"):
            for issue_id, risk in active_risks.items():
                snapshot = risk.get("accepted_issue_snapshot") or {}
                if (
                    snapshot.get("criterion_id") == finding["criterion_id"]
                    and snapshot.get("canonical_evidence_anchor") == anchor
                    and snapshot.get("consequence_fingerprint") == consequence
                ):
                    candidates.append(issue_id)
        record = {
            "finding_index": index,
            "finding": finding,
            "canonical_evidence_anchor": anchor,
            "consequence_fingerprint": consequence,
            "candidate_accepted_risk_ids": candidates,
        }
        if len(candidates) == 1:
            record["accepted_risk_id"] = candidates[0]
            accepted.append(record)
        else:
            unaccepted.append(record)
    return {
        "accepted_findings": accepted,
        "unaccepted_blocking_findings": unaccepted,
        "passed": not unaccepted,
    }


def save_final_assessment(
    run_dir: Path,
    *,
    draft_path: Path,
    raw_review: dict[str, Any],
) -> dict[str, Any]:
    validate_review_schema(raw_review)
    if raw_review["resolved_issues"]:
        raise SemanticValidationError("FINAL_BLIND may not resolve history-bound issue IDs")
    for issue in raw_review["blocking_issues"]:
        if issue["id"] is not None:
            raise SemanticValidationError("FINAL_BLIND findings must use id: null")
        if issue["severity"] < 3:
            raise SemanticValidationError("최종 독립 검토의 진행 차단 이슈는 중요도가 3~5여야 합니다.")
    draft_round = layout.round_of(draft_path)
    existing = layout.iter_blind_attempts(run_dir, draft_round)
    suffix = "" if not existing else f"-attempt-{len(existing) + 1}"
    raw_path = layout.blind(run_dir, draft_round, suffix)
    atomic_write_json(raw_path, raw_review, overwrite=False)
    reconciliation = reconcile_final_findings(run_dir, draft_path=draft_path, raw_review=raw_review)
    reconciliation.update({"raw_review_path": str(raw_path), "draft": str(draft_path)})
    detail_path = layout.reconciliation(run_dir, draft_round, suffix)
    atomic_write_json(detail_path, reconciliation, overwrite=False)
    atomic_write_json(layout.final_reconciliation(run_dir), reconciliation)
    manifest = read_json(layout.manifest(run_dir))
    manifest["latest_final_blind"] = str(raw_path)
    manifest["latest_final_reconciliation"] = str(detail_path)
    atomic_write_json(layout.manifest(run_dir), manifest)
    return reconciliation


def run_final_blind(
    run_dir: Path,
    *,
    draft_path: Path,
    model: str,
    timeout: int,
) -> tuple[ProviderResult, dict[str, Any]]:
    prompt = (REFERENCE_ROOT / "final-blind-prompt.md").read_text(encoding="utf-8")
    schema_path = REFERENCE_ROOT / "review.schema.json"
    with isolated_bundle(run_dir, mode="final", draft_path=draft_path) as bundle_dir:
        result = run_codex(
            bundle_dir=bundle_dir,
            prompt=prompt,
            schema_path=schema_path,
            schema_kind="review",
            model=model,
            timeout=timeout,
        )
    reconciliation = save_final_assessment(
        run_dir, draft_path=draft_path, raw_review=result.payload
    )
    return result, reconciliation

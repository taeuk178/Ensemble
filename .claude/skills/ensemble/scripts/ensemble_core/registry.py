from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .config import CLOSED_STATUSES, OPEN_STATUSES
from .errors import InputError, StateError
from .hashing import (
    canonical_evidence_anchor,
    canonical_section_ref,
    consequence_fingerprint,
    evidence_ref_hashes,
    refs_changed,
)
from .io_utils import atomic_write_json, atomic_write_text, read_json, utc_now


def load_registry(run_dir: Path) -> dict[str, Any]:
    return read_json(run_dir / "issue-registry.json", default={})


def _new_id(registry: dict[str, Any], round_number: int) -> str:
    prefix = f"R{round_number}-I"
    used = [int(issue_id.split("-I", 1)[1]) for issue_id in registry if issue_id.startswith(prefix)]
    return f"{prefix}{max(used, default=0) + 1}"


def open_issue_ids(registry: dict[str, Any]) -> set[str]:
    return {issue_id for issue_id, issue in registry.items() if issue.get("status") in OPEN_STATUSES}


def apply_review(
    run_dir: Path,
    review: dict[str, Any],
    *,
    round_number: int,
    previous_hashes: dict[str, str],
    current_hashes: dict[str, str],
    source: str | None = None,
) -> dict[str, Any]:
    registry = load_registry(run_dir)
    new_ids: list[str] = []
    regressed: list[str] = []
    resolved_ids: list[str] = []
    resolved_without_edit: list[str] = []
    resolution_bases: Counter[str] = Counter()
    assigned: dict[str, str] = {}

    for position, raw_issue in enumerate(review["blocking_issues"]):
        issue = dict(raw_issue)
        issue_id = issue.pop("id")
        if issue_id is None:
            issue_id = _new_id(registry, round_number)
            new_ids.append(issue_id)
            assigned[str(position)] = issue_id
            registry[issue_id] = {
                "first_seen_round": round_number,
                "severity_history": [],
                "confidence_history": [],
                "author_disposition_history": [],
                "status_history": [],
                "supersedes": [],
                "split_from": issue.get("split_from"),
                "merged_from": issue.get("merged_from", []),
                "first_seen_source": source,
            }
        entry = registry[issue_id]
        previous_status = entry.get("status")
        if previous_status in CLOSED_STATUSES:
            regressed.append(issue_id)
        entry.update(
            {
                "status": "UNVERIFIED" if issue["verification_required"] else "OPEN",
                "gating": True,
                "criterion_id": issue["criterion_id"],
                "section_ref": issue["location"],
                "evidence_refs": issue["evidence_refs"],
                "latest_issue": issue,
                "last_seen_round": round_number,
                "last_seen_source": source,
            }
        )
        entry["severity_history"].append(
            {"round": round_number, "evaluator": "gpt", "severity": issue["severity"]}
        )
        entry["confidence_history"].append(
            {"round": round_number, "evaluator": "gpt", "confidence": issue["confidence"]}
        )
        entry["status_history"].append(
            {"round": round_number, "from": previous_status, "to": entry["status"]}
        )
        if issue.get("response_to_rebuttal"):
            entry["last_response_to_rebuttal"] = issue["response_to_rebuttal"]

    for resolution in review["resolved_issues"]:
        issue_id = resolution["id"]
        entry = registry[issue_id]
        previous_status = entry.get("status")
        basis = resolution["resolution_basis"]
        target_status = basis if basis in {"SUPERSEDED", "MERGED"} else "RESOLVED"
        entry["status"] = target_status
        entry["gating"] = False
        entry["resolved_at_round"] = round_number
        entry.setdefault("resolution_history", []).append({"round": round_number, **resolution})
        entry.setdefault("status_history", []).append(
            {"round": round_number, "from": previous_status, "to": target_status}
        )
        resolved_ids.append(issue_id)
        resolution_bases[basis] += 1
        if not refs_changed(previous_hashes, current_hashes, resolution["evidence_refs"]):
            resolved_without_edit.append(issue_id)
        if basis == "SUPERSEDED" and resolution.get("superseded_by"):
            entry["supersedes"] = [resolution["superseded_by"]]
        if basis == "MERGED" and resolution.get("merged_into"):
            entry["merged_into"] = resolution["merged_into"]

    atomic_write_json(run_dir / "issue-registry.json", registry)
    write_reviewer_projection(run_dir, registry)
    return {
        "assigned_ids": assigned,
        "new_issue_ids": new_ids,
        "resolved_issue_ids": resolved_ids,
        "regressed_issue_ids": regressed,
        "resolved_without_relevant_edit": resolved_without_edit,
        "resolution_basis_counts": dict(resolution_bases),
        "open_issue_ids": sorted(open_issue_ids(registry)),
    }


def write_reviewer_projection(run_dir: Path, registry: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    registry = registry if registry is not None else load_registry(run_dir)
    projection: list[dict[str, Any]] = []
    for issue_id, issue in sorted(registry.items()):
        if issue.get("status") not in OPEN_STATUSES:
            continue
        latest_author = (issue.get("author_disposition_history") or [{}])[-1]
        projection.append(
            {
                "id": issue_id,
                "status": issue.get("status"),
                "section_ref": issue.get("section_ref"),
                "evidence_refs": issue.get("evidence_refs", []),
                "author_claim": latest_author.get("claim"),
                "author_evidence_ref": latest_author.get("evidence_ref"),
                "author_requested_disposition": latest_author.get("requested_disposition"),
            }
        )
    atomic_write_json(run_dir / "reviewer-issue-index.json", projection)
    return projection


def record_author_decision(
    run_dir: Path,
    *,
    issue_id: str,
    round_number: int,
    disposition: str,
    author_severity: int,
    claim: str,
    evidence_ref: str,
    requested_disposition: str,
    argument: str,
    action: str,
) -> None:
    registry = load_registry(run_dir)
    if issue_id not in registry:
        raise InputError(f"Unknown issue ID: {issue_id}")
    if disposition not in {"ACCEPT", "REJECT", "DEFER"}:
        raise InputError("Disposition must be ACCEPT, REJECT, or DEFER")
    if not 1 <= author_severity <= 5:
        raise InputError("Author severity must be from 1 to 5")
    if requested_disposition not in {"MODIFY", "DISMISS", "ESCALATE"}:
        raise InputError("requested_disposition is invalid")
    event = {
        "round": round_number,
        "value": disposition,
        "author_severity": author_severity,
        "claim": claim,
        "evidence_ref": evidence_ref,
        "requested_disposition": requested_disposition,
        "argument": argument,
        "action": action,
        "recorded_at": utc_now(),
    }
    registry[issue_id].setdefault("author_disposition_history", []).append(event)
    atomic_write_json(run_dir / "issue-registry.json", registry)
    write_reviewer_projection(run_dir, registry)
    section = f"""

## {issue_id}

- 리뷰어 상태: {registry[issue_id].get('status')} (severity {registry[issue_id].get('latest_issue', {}).get('severity')}, confidence {registry[issue_id].get('latest_issue', {}).get('confidence')})
- Claude 판단: {disposition}
- 작성자 심각도: {author_severity}
- claim: {claim}
- evidence_ref: {evidence_ref}
- requested_disposition: {requested_disposition}
- 논증: {argument}
- 조치: {action}
- 다음 검토 상태: PENDING
"""
    decisions_path = run_dir / "decisions.md"
    existing = decisions_path.read_text(encoding="utf-8")
    atomic_write_text(decisions_path, existing.rstrip() + section)


def accept_risk(run_dir: Path, *, issue_id: str, note: str, round_number: int) -> dict[str, Any]:
    registry = load_registry(run_dir)
    if issue_id not in registry:
        raise InputError(f"Unknown issue ID: {issue_id}")
    issue = registry[issue_id]
    if issue.get("status") not in OPEN_STATUSES:
        raise StateError(f"Only an open issue can be accepted as risk: {issue_id}")
    latest = dict(issue.get("latest_issue") or {})
    if int(latest.get("severity", 0)) < 3:
        raise StateError("Only severity 3-5 issues need ACCEPTED_RISK")
    draft_path = run_dir / "drafts" / f"round-{round_number}.md"
    if not draft_path.exists():
        raise StateError(f"Draft round {round_number} does not exist")
    markdown = draft_path.read_text(encoding="utf-8")
    issue_refs = [str(value) for value in latest.get("evidence_refs", [])]
    evidence_refs = issue_refs or [str(latest.get("location", "document"))]
    accepted_hashes = evidence_ref_hashes(markdown, evidence_refs)
    if any(value is None for value in accepted_hashes.values()):
        raise StateError(
            "Accepted risk evidence location does not resolve to exactly one current draft section"
        )
    anchor = canonical_evidence_anchor(
        markdown,
        location=str(latest.get("location", "document")),
        violation_evidence=str(latest.get("violation_evidence", "")),
        required_change=str(latest.get("required_change", "")),
        unmatched_salt=issue_id,
        evidence_refs=evidence_refs,
    )
    snapshot = {
        **latest,
        "evidence_refs": evidence_refs,
        "canonical_evidence_anchor": anchor,
        "consequence_fingerprint": consequence_fingerprint(
            str(latest.get("implementation_consequence", ""))
        ),
        "evidence_hashes_at_acceptance": accepted_hashes,
    }
    previous_status = issue.get("status")
    issue["status"] = "ACCEPTED_RISK"
    issue["gating"] = False
    issue["accepted_at_round"] = round_number
    issue["accepted_issue_snapshot"] = snapshot
    issue["acceptance_note"] = note
    issue.setdefault("status_history", []).append(
        {"round": round_number, "from": previous_status, "to": "ACCEPTED_RISK"}
    )
    atomic_write_json(run_dir / "issue-registry.json", registry)
    write_reviewer_projection(run_dir, registry)
    return issue

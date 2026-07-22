from __future__ import annotations

from pathlib import Path
from typing import Any

from .bundle import isolated_bundle
from .config import REFERENCE_ROOT
from .errors import InputError, SemanticValidationError, StateError
from .io_utils import atomic_write_json, read_json
from .providers import ProviderResult, run_codex
from .registry import load_registry, write_reviewer_projection
from .validation import validate_against_schema
from . import layout


def _issues_for_round(registry: dict[str, Any], round_number: int) -> list[dict[str, Any]]:
    return [
        {**issue.get("latest_issue", {}), "id": issue_id}
        for issue_id, issue in sorted(registry.items())
        if int(issue.get("first_seen_round", -1)) == round_number
    ]


def apply_audit(run_dir: Path, *, round_number: int, payload: dict[str, Any]) -> dict[str, Any]:
    schema_path = REFERENCE_ROOT / "audit.schema.json"
    validate_against_schema(payload, schema_path, "audit")
    registry = load_registry(run_dir)
    expected = {item["id"] for item in _issues_for_round(registry, round_number)}
    received = {item["id"] for item in payload["issues"]}
    if expected != received:
        raise SemanticValidationError(
            "Issue audit must classify every new issue exactly once",
            details={"missing": sorted(expected - received), "unknown": sorted(received - expected)},
        )
    counts = {"VALID_BLOCKER": 0, "NOT_BLOCKER": 0, "UNVERIFIED": 0}
    for item in payload["issues"]:
        issue = registry[item["id"]]
        counts[item["validity"]] += 1
        issue.setdefault("audit_history", []).append({"round": round_number, **item})
        if item["relation"] == "DUPLICATE":
            duplicate_of = item.get("duplicate_of")
            if duplicate_of not in registry or duplicate_of == item["id"]:
                raise SemanticValidationError(f"Invalid duplicate target for {item['id']}")
            issue["status"] = "MERGED"
            issue["gating"] = False
            issue["merged_into"] = duplicate_of
        elif item["validity"] == "NOT_BLOCKER":
            issue["status"] = "RESOLVED"
            issue["gating"] = False
        elif item["validity"] == "UNVERIFIED":
            issue["status"] = "UNVERIFIED"
            issue["gating"] = True
    output = layout.issue_audit(run_dir, round_number)
    atomic_write_json(output, payload, overwrite=False)
    atomic_write_json(layout.registry(run_dir), registry)
    write_reviewer_projection(run_dir, registry)
    manifest = read_json(layout.manifest(run_dir))
    signals = manifest.get("escalation_signals", [])
    if (
        str(manifest.get("phase")) == "3"
        and manifest.get("state") == "ESCALATION_REQUIRED"
        and any(signal.get("type") == "REVIEWER_STORM" for signal in signals)
    ):
        candidates = sorted(
            item["id"]
            for item in payload["issues"]
            if item["validity"] == "VALID_BLOCKER" and item["relation"] == "UNIQUE"
        )
        manifest["pending_panel_issue_ids"] = candidates
        if not candidates:
            manifest["state"] = "DRAFT_READY"
            manifest["escalation_signals"] = []
        atomic_write_json(layout.manifest(run_dir), manifest)
    return {"audit": str(output), "counts": counts}


def run_issue_audit(
    run_dir: Path,
    *,
    round_number: int,
    model: str,
    timeout: int,
) -> tuple[ProviderResult, dict[str, Any]]:
    registry = load_registry(run_dir)
    issues = _issues_for_round(registry, round_number)
    if not issues:
        raise StateError(f"No new issues found for round {round_number}")
    manifest = read_json(layout.manifest(run_dir))
    history = {
        int(item["review_round"]): int(item["draft_round"])
        for item in manifest.get("review_history", [])
    }
    current_round = history.get(round_number)
    previous_round = history.get(round_number - 1)
    if current_round is None or previous_round is None:
        raise StateError("Issue audit requires recorded current and previous review/draft mappings")
    current_draft = layout.draft(run_dir, current_round)
    previous_draft = layout.draft(run_dir, previous_round)
    if not current_draft.exists() or not previous_draft.exists():
        raise StateError("Issue audit requires current and previous drafts")
    prompt = (REFERENCE_ROOT / "audit-prompt.md").read_text(encoding="utf-8")
    schema_path = REFERENCE_ROOT / "audit.schema.json"
    with isolated_bundle(
        run_dir,
        mode="audit",
        draft_path=current_draft,
        previous_draft_path=previous_draft,
        audit_issues=issues,
    ) as bundle_dir:
        result = run_codex(
            bundle_dir=bundle_dir,
            prompt=prompt,
            schema_path=schema_path,
            schema_kind="audit",
            model=model,
            timeout=timeout,
        )
    return result, apply_audit(run_dir, round_number=round_number, payload=result.payload)

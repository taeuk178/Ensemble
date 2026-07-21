from __future__ import annotations

from pathlib import Path
from typing import Any

from .bundle import isolated_bundle
from .config import REFERENCE_ROOT
from .errors import InputError, SemanticValidationError, StateError
from .io_utils import atomic_write_json
from .providers import ProviderResult, run_codex
from .registry import load_registry, write_reviewer_projection
from .validation import validate_against_schema


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
    output = run_dir / "reviews" / f"issue-audit-round-{round_number}.json"
    atomic_write_json(output, payload, overwrite=False)
    atomic_write_json(run_dir / "issue-registry.json", registry)
    write_reviewer_projection(run_dir, registry)
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
    current_draft = run_dir / "drafts" / f"round-{round_number - 1}.md"
    previous_draft = run_dir / "drafts" / f"round-{round_number - 2}.md"
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

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .bundle import isolated_bundle
from .cards import build_panel_card
from .config import ISSUE_ID_PATTERN, REFERENCE_ROOT
from .errors import InfraError, InputError, StateError
from .io_utils import atomic_write_json, atomic_write_text, read_json
from .providers import run_agy, run_codex
from .registry import load_registry, write_reviewer_projection
from .state_machine import record_provider_call


def run_panel(
    run_dir: Path,
    *,
    issue_id: str,
    review_model: str,
    panel_model: str,
    panel_effort: str,
    timeout: int,
) -> dict[str, Any]:
    if not re.fullmatch(ISSUE_ID_PATTERN, issue_id):
        raise InputError("Invalid issue ID")
    registry = load_registry(run_dir)
    if issue_id not in registry:
        raise InputError(f"Unknown issue ID: {issue_id}")
    issue = registry[issue_id]
    author_history = issue.get("author_disposition_history") or []
    manifest = read_json(run_dir / "manifest.json")
    pending_panel = set(manifest.get("pending_panel_issue_ids", []))
    if issue_id not in pending_panel:
        raise StateError("Panel escalation requires an issue selected by the escalation controller")
    if not author_history:
        raise StateError("Panel escalation requires a recorded author disposition")
    panel_count = int(manifest.get("panel_call_count", 0))
    if panel_count >= int(manifest["limits"]["panel_calls"]):
        raise StateError("Panel call limit reached")
    draft_round = int(manifest.get("current_round", 0))
    draft_path = run_dir / "drafts" / f"round-{draft_round}.md"
    panel_dir = run_dir / "panel" / issue_id
    if panel_dir.exists():
        raise StateError(f"Panel artifact already exists for {issue_id}")
    panel_dir.mkdir(parents=True)
    schema_path = REFERENCE_ROOT / "panel.schema.json"
    base_prompt = (REFERENCE_ROOT / "panel-prompt.md").read_text(encoding="utf-8")
    evaluations: dict[str, dict[str, Any]] = {}
    with isolated_bundle(run_dir, mode="panel", draft_path=draft_path, issue={"id": issue_id, **issue.get("latest_issue", {})}) as bundle_dir:
        gpt = run_codex(
            bundle_dir=bundle_dir,
            prompt=base_prompt,
            schema_path=schema_path,
            schema_kind="panel",
            model=review_model,
            timeout=timeout,
        )
        record_provider_call(
            run_dir,
            provider="codex",
            operation="panel-independent",
            round_number=draft_round,
            result=gpt,
        )
        evaluations["gpt"] = gpt.payload
        atomic_write_json(panel_dir / "gpt.json", gpt.payload, overwrite=False)
        try:
            agy = run_agy(
                bundle_dir=bundle_dir,
                prompt=base_prompt,
                schema_path=schema_path,
                schema_kind="panel",
                model=panel_model,
                effort=panel_effort,
                timeout=timeout,
            )
            record_provider_call(
                run_dir,
                provider="agy",
                operation="panel-independent",
                round_number=draft_round,
                result=agy,
            )
        except InfraError:
            manifest = read_json(run_dir / "manifest.json")
            issue["status"] = "BILATERAL_DEADLOCK"
            manifest["state"] = "USER_DECISION_REQUIRED"
            pending = set(manifest.get("pending_user_issue_ids", []))
            pending.add(issue_id)
            manifest["pending_user_issue_ids"] = sorted(pending)
            manifest["panel_call_count"] = panel_count + 1
            atomic_write_json(run_dir / "issue-registry.json", registry)
            atomic_write_json(run_dir / "manifest.json", manifest)
            raise
        evaluations["agy"] = agy.payload
        atomic_write_json(panel_dir / "agy.json", agy.payload, overwrite=False)
        card = build_panel_card(issue_id, evaluations, author_history[-1])
        atomic_write_text(panel_dir / "feedback-card.md", card, overwrite=False)
        revote_prompt = base_prompt + "\n\nControlled feedback card:\n" + card
        revotes_dir = panel_dir / "revotes"
        revotes_dir.mkdir()
        gpt_revote_result = run_codex(
            bundle_dir=bundle_dir,
            prompt=revote_prompt,
            schema_path=schema_path,
            schema_kind="panel",
            model=review_model,
            timeout=timeout,
        )
        record_provider_call(
            run_dir,
            provider="codex",
            operation="panel-revote",
            round_number=draft_round,
            result=gpt_revote_result,
        )
        gpt_revote = gpt_revote_result.payload
        agy_revote_result = run_agy(
            bundle_dir=bundle_dir,
            prompt=revote_prompt,
            schema_path=schema_path,
            schema_kind="panel",
            model=panel_model,
            effort=panel_effort,
            timeout=timeout,
        )
        record_provider_call(
            run_dir,
            provider="agy",
            operation="panel-revote",
            round_number=draft_round,
            result=agy_revote_result,
        )
        agy_revote = agy_revote_result.payload
    manifest = read_json(run_dir / "manifest.json")
    atomic_write_json(revotes_dir / "gpt.json", gpt_revote, overwrite=False)
    atomic_write_json(revotes_dir / "agy.json", agy_revote, overwrite=False)
    severities = [int(gpt_revote["severity"]), int(agy_revote["severity"])]
    if all(severity <= 2 for severity in severities):
        issue["status"] = "RESOLVED"
        issue["gating"] = False
        outcome = "REBUTTAL_ACCEPTED"
    elif all(severity >= 3 for severity in severities):
        issue["status"] = "OPEN"
        issue["gating"] = True
        outcome = "REVISION_REQUIRED"
    else:
        issue["status"] = "PANEL_DISSENT"
        issue["gating"] = True
        manifest["state"] = "USER_DECISION_REQUIRED"
        pending = set(manifest.get("pending_user_issue_ids", []))
        pending.add(issue_id)
        manifest["pending_user_issue_ids"] = sorted(pending)
        outcome = "PANEL_DISSENT"
    if outcome != "PANEL_DISSENT":
        pending = set(manifest.get("pending_user_issue_ids", []))
        pending.discard(issue_id)
        manifest["pending_user_issue_ids"] = sorted(pending)
    pending_panel = set(manifest.get("pending_panel_issue_ids", []))
    pending_panel.discard(issue_id)
    manifest["pending_panel_issue_ids"] = sorted(pending_panel)
    if outcome != "PANEL_DISSENT":
        if pending_panel:
            manifest["state"] = "ESCALATION_REQUIRED"
        else:
            manifest["state"] = "NEEDS_REVISION" if outcome == "REVISION_REQUIRED" else "DRAFT_READY"
            manifest["escalation_signals"] = []
    issue.setdefault("panel_history", []).append(
        {
            "round": draft_round,
            "independent": evaluations,
            "revotes": {"gpt": gpt_revote, "agy": agy_revote},
            "outcome": outcome,
        }
    )
    manifest["panel_call_count"] = panel_count + 1
    atomic_write_json(run_dir / "issue-registry.json", registry)
    atomic_write_json(run_dir / "manifest.json", manifest)
    write_reviewer_projection(run_dir, registry)
    return {"issue_id": issue_id, "outcome": outcome, "severities": severities, "panel_dir": str(panel_dir)}

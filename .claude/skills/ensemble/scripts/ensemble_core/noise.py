from __future__ import annotations

from itertools import combinations
from pathlib import Path
from typing import Any

from .bundle import isolated_bundle
from .config import REFERENCE_ROOT
from .errors import InputError
from .hashing import canonical_evidence_anchor, consequence_fingerprint
from .io_utils import atomic_write_json, utc_now
from .providers import run_codex
from .validation import validate_review_schema
from .workflow import current_draft
from .state_machine import record_provider_call
from . import layout


def _observation(markdown: str, issue: dict[str, Any], *, blocking: bool, salt: str) -> dict[str, Any]:
    anchor = canonical_evidence_anchor(
        markdown,
        location=issue["location"],
        violation_evidence=issue["violation_evidence"],
        required_change=issue["required_change"],
        unmatched_salt=salt,
        evidence_refs=issue.get("evidence_refs", []),
    )
    consequence = consequence_fingerprint(issue["implementation_consequence"])
    identity = f"{anchor}|{consequence}"
    return {
        "identity": identity,
        "canonical_issue_key": f"{issue['criterion_id']}|{identity}",
        "criterion_id": issue["criterion_id"],
        "blocking": blocking,
        "severity": issue["severity"],
        "verification_required": issue["verification_required"],
    }


def _agreement(left: dict[str, dict[str, Any]], right: dict[str, dict[str, Any]], field: str) -> float | None:
    common = set(left) & set(right)
    if not common:
        return None
    return sum(left[key][field] == right[key][field] for key in common) / len(common)


def _pair_metrics(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_blockers = {item["canonical_issue_key"] for item in left["observations"] if item["blocking"]}
    right_blockers = {item["canonical_issue_key"] for item in right["observations"] if item["blocking"]}
    union = left_blockers | right_blockers
    by_identity_left = {item["identity"]: item for item in left["observations"]}
    by_identity_right = {item["identity"]: item for item in right["observations"]}
    common = set(by_identity_left) & set(by_identity_right)
    severity_deltas = [
        abs(int(by_identity_left[key]["severity"]) - int(by_identity_right[key]["severity"]))
        for key in common
    ]
    return {
        "left": left["index"],
        "right": right["index"],
        "blocker_jaccard": 1.0 if not union else len(left_blockers & right_blockers) / len(union),
        "criterion_agreement": _agreement(by_identity_left, by_identity_right, "criterion_id"),
        "classification_agreement": _agreement(by_identity_left, by_identity_right, "blocking"),
        "verification_agreement": _agreement(
            by_identity_left, by_identity_right, "verification_required"
        ),
        "verdict_match": left["verdict"] == right["verdict"],
        "severity_deltas": severity_deltas,
    }


def measure_noise(
    run_dir: Path,
    *,
    repetitions: int,
    model: str,
    timeout: int,
) -> dict[str, Any]:
    if repetitions < 2:
        raise InputError("Noise measurement requires at least two repetitions")
    _, draft_path = current_draft(run_dir)
    markdown = draft_path.read_text(encoding="utf-8")
    prompt = (REFERENCE_ROOT / "final-blind-prompt.md").read_text(encoding="utf-8")
    schema_path = REFERENCE_ROOT / "review.schema.json"
    runs: list[dict[str, Any]] = []
    for run_index in range(repetitions):
        with isolated_bundle(run_dir, mode="final", draft_path=draft_path) as bundle_dir:
            result = run_codex(
                bundle_dir=bundle_dir,
                prompt=prompt,
                schema_path=schema_path,
                schema_kind="review",
                model=model,
                timeout=timeout,
            )
        validate_review_schema(result.payload)
        record_provider_call(
            run_dir,
            provider="codex",
            operation="measure-noise",
            round_number=run_index + 1,
            result=result,
        )
        observations: list[dict[str, Any]] = []
        for index, item in enumerate(result.payload["blocking_issues"]):
            observations.append(
                _observation(markdown, item, blocking=True, salt=f"run-{run_index}-block-{index}")
            )
        for index, item in enumerate(result.payload["nonblocking_risks"]):
            observations.append(
                _observation(markdown, item, blocking=False, salt=f"run-{run_index}-risk-{index}")
            )
        runs.append(
            {
                "index": run_index,
                "verdict": result.payload["verdict"],
                "observations": observations,
                "raw": result.payload,
            }
        )
    pairs = [_pair_metrics(left, right) for left, right in combinations(runs, 2)]
    result = {
        "measured_at": utc_now(),
        "draft": str(draft_path),
        "model": model,
        "repetitions": repetitions,
        "runs": runs,
        "pairs": pairs,
        "interpretation": "Approximation with possible false matches and false mismatches; not a lower or upper bound.",
    }
    noise_dir = layout.noise_dir(run_dir)
    noise_dir.mkdir(exist_ok=True)
    existing = list(noise_dir.glob("measurement-*.json"))
    output = noise_dir / f"measurement-{len(existing) + 1}.json"
    atomic_write_json(output, result, overwrite=False)
    result["output"] = str(output)
    return result

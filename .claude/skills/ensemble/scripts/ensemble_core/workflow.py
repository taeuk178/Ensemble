from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .bundle import isolated_bundle, prepare_review_session_bundle
from .cards import build_feedback_cards
from .config import REFERENCE_ROOT
from .convergence import record_review_metrics
from .errors import InputError, SchemaError, SemanticValidationError, StateError
from .hashing import load_hashes_for_round
from .io_utils import atomic_write_json, atomic_write_text, read_json
from .providers import ProviderResult, run_codex
from .registry import apply_review, load_registry
from .state_machine import assert_run_can_advance, register_draft
from .state_machine import (
    load_codex_review_session,
    record_codex_review_session,
    record_provider_call,
    record_retry_event,
    verified_request_hash,
)
from .validation import validate_against_schema, validate_review_semantics
from . import layout


def current_draft(run_dir: Path) -> tuple[int, Path]:
    drafts = layout.iter_drafts(run_dir)
    if not drafts:
        raise StateError("No draft has been registered")
    path = drafts[-1]
    return layout.round_of(path), path


def save_artifact(
    run_dir: Path,
    *,
    kind: str,
    source: Path,
    round_number: int | None = None,
) -> dict[str, Any]:
    if kind == "claude-proposal":
        destination = layout.proposal(run_dir, "claude.md")
        if destination.exists():
            raise StateError("Claude proposal already exists")
        content = source.read_text(encoding="utf-8")
        if not content.strip():
            raise InputError("Claude proposal cannot be empty")
        atomic_write_text(destination, content, overwrite=False)
        return {"artifact": str(destination)}
    if kind in {"request", "rubric"}:
        if layout.iter_drafts(run_dir):
            raise StateError(f"{kind}.md is immutable after the first draft")
        content = source.read_text(encoding="utf-8")
        if not content.strip():
            raise InputError(f"{kind}.md cannot be empty")
        if kind == "request":
            required_headings = {
                "## 사용자 원문",
                "## 구조화된 작업 입력",
                "## 가정",
                "## 사용자 확인이 필요한 항목",
            }
            missing = sorted(heading for heading in required_headings if heading not in content)
            if missing:
                raise InputError("Structured request is missing required headings", details={"missing": missing})
            original = (layout.request_original(run_dir)).read_text(encoding="utf-8")
            if original not in content:
                raise InputError("Structured request must preserve the exact user original")
        else:
            criteria = set(re.findall(r"\bAC-\d{2}\b", content))
            if not criteria:
                raise InputError("rubric.md must contain at least one AC-NN criterion")
        destination = layout.request(run_dir) if kind == "request" else layout.rubric(run_dir)
        atomic_write_text(destination, content)
        return {"artifact": str(destination)}
    if kind == "draft":
        if round_number is None:
            raise InputError("Saving a draft requires --round")
        return register_draft(run_dir, source, round_number)
    raise InputError(f"Unsupported artifact kind: {kind}")


def save_proposal(run_dir: Path, proposal: dict[str, Any]) -> Path:
    schema_path = REFERENCE_ROOT / "proposal.schema.json"
    validate_against_schema(proposal, schema_path, "proposal")
    path = layout.proposal(run_dir, "gpt.json")
    atomic_write_json(path, proposal, overwrite=False)
    manifest = read_json(layout.manifest(run_dir))
    manifest["state"] = "PROPOSALS_READY" if (layout.proposal(run_dir, "claude.md")).exists() else "GPT_PROPOSAL_READY"
    atomic_write_json(layout.manifest(run_dir), manifest)
    return path


def run_proposal(run_dir: Path, *, model: str, timeout: int) -> tuple[ProviderResult, Path]:
    prompt = (REFERENCE_ROOT / "proposal-prompt.md").read_text(encoding="utf-8")
    schema_path = REFERENCE_ROOT / "proposal.schema.json"
    with isolated_bundle(run_dir, mode="proposal") as bundle_dir:
        result = run_codex(
            bundle_dir=bundle_dir,
            prompt=prompt,
            schema_path=schema_path,
            schema_kind="proposal",
            model=model,
            timeout=timeout,
        )
    return result, save_proposal(run_dir, result.payload)


def _validate_and_apply_review(
    run_dir: Path,
    *,
    review: dict[str, Any],
    review_round: int,
    draft_round: int,
    baseline_draft_round: int,
    output_path: Path,
    allow_rebuttal_similarity: bool = False,
) -> dict[str, Any]:
    schema_path = REFERENCE_ROOT / "review.schema.json"
    validate_against_schema(review, schema_path, "review")
    registry = load_registry(run_dir)
    previous_hashes = load_hashes_for_round(run_dir, baseline_draft_round)
    current_hashes = load_hashes_for_round(run_dir, draft_round)
    allowed_criteria = set(
        re.findall(r"\bAC-\d{2}\b", (layout.rubric(run_dir)).read_text(encoding="utf-8"))
    )
    validate_review_semantics(
        review,
        registry=registry,
        previous_hashes=previous_hashes,
        current_hashes=current_hashes,
        allowed_criteria=allowed_criteria,
        allow_rebuttal_similarity=allow_rebuttal_similarity,
    )
    if output_path.exists():
        raise StateError(f"Review artifact already exists: {output_path}")
    atomic_write_json(output_path, review, overwrite=False)
    stats = apply_review(
        run_dir,
        review,
        round_number=review_round,
        previous_hashes=previous_hashes,
        current_hashes=current_hashes,
        source=output_path.relative_to(run_dir).as_posix(),
    )
    metrics = record_review_metrics(run_dir, round_number=review_round, stats=stats)
    manifest = read_json(layout.manifest(run_dir))
    manifest["last_review_round"] = review_round
    manifest["last_reviewed_draft_round"] = draft_round
    manifest["last_review_verdict"] = review["verdict"]
    manifest["state"] = review["verdict"]
    history = [
        item
        for item in manifest.get("review_history", [])
        if int(item.get("review_round", -1)) != review_round
    ]
    history.append(
        {
            "review_round": review_round,
            "draft_round": draft_round,
            "baseline_draft_round": baseline_draft_round,
            "verdict": review["verdict"],
        }
    )
    manifest["review_history"] = sorted(history, key=lambda item: int(item["review_round"]))
    atomic_write_json(layout.manifest(run_dir), manifest)
    return {"review": str(output_path), "verdict": review["verdict"], "stats": stats, "metrics": metrics}


def ingest_review(
    run_dir: Path,
    *,
    review: dict[str, Any],
    review_round: int,
    draft_round: int | None = None,
    allow_rebuttal_similarity: bool = False,
) -> dict[str, Any]:
    assert_run_can_advance(run_dir, "review")
    manifest = read_json(layout.manifest(run_dir))
    expected_review_round = int(manifest.get("last_review_round", 0)) + 1
    if review_round != expected_review_round:
        raise StateError(
            f"Expected review round {expected_review_round}, received {review_round}"
        )
    draft_round = int(manifest.get("current_round", 0)) if draft_round is None else draft_round
    draft_path = layout.draft(run_dir, draft_round)
    if not draft_path.exists():
        raise StateError(f"Review round {review_round} requires {draft_path.name}")
    baseline_draft_round = int(manifest.get("last_reviewed_draft_round", -1))
    output_path = layout.review(run_dir, review_round)
    return _validate_and_apply_review(
        run_dir,
        review=review,
        review_round=review_round,
        draft_round=draft_round,
        baseline_draft_round=baseline_draft_round,
        output_path=output_path,
        allow_rebuttal_similarity=allow_rebuttal_similarity,
    )


def run_review(
    run_dir: Path,
    *,
    review_round: int,
    draft_round: int | None,
    model: str,
    timeout: int,
    semantic_retries: int = 2,
) -> tuple[ProviderResult, dict[str, Any]]:
    assert_run_can_advance(run_dir, "review")
    manifest = read_json(layout.manifest(run_dir))
    expected_review_round = int(manifest.get("last_review_round", 0)) + 1
    if review_round != expected_review_round:
        raise StateError(
            f"Expected review round {expected_review_round}, received {review_round}"
        )
    draft_round = int(manifest.get("current_round", 0)) if draft_round is None else draft_round
    draft_path = layout.draft(run_dir, draft_round)
    if not draft_path.exists():
        raise StateError(f"Review round {review_round} requires {draft_path.name}")
    build_feedback_cards(run_dir, draft_path)
    baseline_draft_round = int(manifest.get("last_reviewed_draft_round", -1))
    base_prompt = (REFERENCE_ROOT / "reviewer-prompt.md").read_text(encoding="utf-8")
    schema_path = REFERENCE_ROOT / "review.schema.json"
    prompt = base_prompt
    last_error: SemanticValidationError | SchemaError | None = None
    request_hash = verified_request_hash(run_dir)
    review_session = load_codex_review_session(run_dir)
    session_id = review_session["session_id"] if review_session else None
    bundle_dir = prepare_review_session_bundle(
        run_dir,
        draft_path=draft_path,
        request_hash=request_hash,
    )
    for semantic_attempt in range(semantic_retries + 1):
        result = run_codex(
            bundle_dir=bundle_dir,
            prompt=prompt,
            schema_path=schema_path,
            schema_kind="review",
            model=model,
            timeout=timeout,
            persist_session=True,
            session_id=session_id,
        )
        if result.session_id is None:
            raise StateError("Codex 검토 세션 ID를 확인할 수 없습니다.")
        record_codex_review_session(
            run_dir,
            session_id=result.session_id,
            review_round=review_round,
            workspace=bundle_dir,
        )
        session_id = result.session_id
        try:
            applied = _validate_and_apply_review(
                run_dir,
                review=result.payload,
                review_round=review_round,
                draft_round=draft_round,
                baseline_draft_round=baseline_draft_round,
                output_path=layout.review(run_dir, review_round),
                allow_rebuttal_similarity=semantic_attempt >= 2,
            )
            if semantic_attempt >= 2:
                convergence = read_json(layout.convergence(run_dir))
                for record in convergence["rounds"]:
                    if record["round"] == review_round:
                        record["semantic_validation_bypass"].append("response_to_rebuttal_similarity")
                atomic_write_json(layout.convergence(run_dir), convergence)
            record_provider_call(
                run_dir,
                provider="codex",
                operation="review",
                round_number=review_round,
                result=result,
            )
            return result, applied
        except (SemanticValidationError, SchemaError) as exc:
            last_error = exc
            record_provider_call(
                run_dir,
                provider="codex",
                operation="review",
                round_number=review_round,
                result=result,
                outcome="VALIDATION_RETRY",
                error=exc.message,
            )
            manifest = read_json(layout.manifest(run_dir))
            key = "semantic" if isinstance(exc, SemanticValidationError) else "schema"
            manifest["retries"][key] += 1
            atomic_write_json(layout.manifest(run_dir), manifest)
            record_retry_event(
                run_dir,
                retry_type=key,
                operation="review",
                round_number=review_round,
                attempt=semantic_attempt + 1,
                error=exc.message,
            )
            prompt = (
                base_prompt
                + "\n\nYour previous output failed deterministic validation. Correct only these violations:\n"
                + json.dumps(exc.as_dict(), ensure_ascii=False, indent=2)
            )
    assert last_error is not None
    raise last_error


def promote_final_findings(run_dir: Path) -> dict[str, Any]:
    assert_run_can_advance(run_dir, "promote-final")
    reconciliation = read_json(layout.final_reconciliation(run_dir), default={})
    findings = reconciliation.get("unaccepted_blocking_findings", [])
    if not findings:
        raise StateError("There are no unaccepted FINAL_BLIND findings to promote")
    manifest = read_json(layout.manifest(run_dir))
    if int(manifest.get("last_review_round", 0)) >= int(manifest["limits"]["review_rounds"]):
        raise StateError("Cannot promote FINAL_BLIND findings after the review round limit")
    draft_round, _ = current_draft(run_dir)
    review_round = int(manifest.get("last_review_round", 0)) + 1
    baseline_draft_round = int(manifest.get("last_reviewed_draft_round", -1))
    review = {
        "verdict": "NEEDS_REVISION",
        "summary": "최종 독립 검토에서 새로운 미수용 이슈가 발견되어 일반 검토로 돌아갑니다.",
        "blocking_issues": [record["finding"] for record in findings],
        "resolved_issues": [],
        "questions_for_user": [],
        "nonblocking_risks": [],
    }
    output_path = layout.promoted(run_dir, review_round)
    return _validate_and_apply_review(
        run_dir,
        review=review,
        review_round=review_round,
        draft_round=draft_round,
        baseline_draft_round=baseline_draft_round,
        output_path=output_path,
    )

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .config import CRITERIA, ISSUE_ID_PATTERN, OPEN_STATUSES
from .errors import SchemaError, SemanticValidationError
from .hashing import refs_changed


ISSUE_FIELDS = {
    "id",
    "criterion_id",
    "location",
    "evidence_refs",
    "problem",
    "violation_evidence",
    "implementation_consequence",
    "required_change",
    "severity",
    "confidence",
    "basis",
    "verification_required",
    "response_to_rebuttal",
    "split_from",
    "merged_from",
}
ISSUE_REQUIRED = ISSUE_FIELDS
RESOLVED_FIELDS = {
    "id",
    "resolution_basis",
    "resolution_reason",
    "evidence_refs",
    "superseded_by",
    "merged_into",
}
RESOLVED_REQUIRED = RESOLVED_FIELDS
REVIEW_FIELDS = {
    "verdict",
    "summary",
    "blocking_issues",
    "resolved_issues",
    "questions_for_user",
    "nonblocking_risks",
}


def parse_json_output(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"Invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SchemaError("Top-level output must be a JSON object")
    return payload


def _strict_fields(payload: dict[str, Any], *, allowed: set[str], required: set[str], context: str) -> None:
    unknown = sorted(set(payload) - allowed)
    missing = sorted(required - set(payload))
    if unknown or missing:
        raise SchemaError(
            f"{context} field mismatch",
            details={"unknown": unknown, "missing": missing},
        )


def _string(value: Any, context: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{context} must be a non-empty string")


def _issue(issue: Any, context: str) -> None:
    if not isinstance(issue, dict):
        raise SchemaError(f"{context} must be an object")
    _strict_fields(issue, allowed=ISSUE_FIELDS, required=ISSUE_REQUIRED, context=context)
    if issue["id"] is not None and not re.fullmatch(ISSUE_ID_PATTERN, issue["id"]):
        raise SchemaError(f"{context}.id has an invalid format")
    for field in (
        "criterion_id",
        "location",
        "problem",
        "violation_evidence",
        "implementation_consequence",
        "required_change",
        "basis",
    ):
        _string(issue[field], f"{context}.{field}")
    refs = issue["evidence_refs"]
    if not isinstance(refs, list) or not refs or not all(
        isinstance(ref, str) and ref.strip() for ref in refs
    ):
        raise SchemaError(f"{context}.evidence_refs must be a non-empty string array")
    severity = issue["severity"]
    if isinstance(severity, bool) or not isinstance(severity, int) or not 1 <= severity <= 5:
        raise SchemaError(f"{context}.severity must be an integer from 1 to 5")
    confidence = issue["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise SchemaError(f"{context}.confidence must be between 0 and 1")
    if issue["basis"] not in {"DOCUMENT_INTERNAL", "EXTERNAL_FACT", "ASSUMPTION"}:
        raise SchemaError(f"{context}.basis is invalid")
    if not isinstance(issue["verification_required"], bool):
        raise SchemaError(f"{context}.verification_required must be boolean")
    if issue["response_to_rebuttal"] is not None:
        _string(issue["response_to_rebuttal"], f"{context}.response_to_rebuttal")
    for key in ("gating", "status"):
        if key in issue:
            raise SchemaError(f"Reviewers may not output {key}")
    if "split_from" in issue and issue["split_from"] is not None and not re.fullmatch(ISSUE_ID_PATTERN, issue["split_from"]):
        raise SchemaError(f"{context}.split_from has an invalid format")
    if "merged_from" in issue:
        if not isinstance(issue["merged_from"], list) or not all(
            isinstance(value, str) and re.fullmatch(ISSUE_ID_PATTERN, value) for value in issue["merged_from"]
        ):
            raise SchemaError(f"{context}.merged_from must be an issue ID array")


def _resolved(value: Any, context: str) -> None:
    if not isinstance(value, dict):
        raise SchemaError(f"{context} must be an object")
    _strict_fields(value, allowed=RESOLVED_FIELDS, required=RESOLVED_REQUIRED, context=context)
    if not re.fullmatch(ISSUE_ID_PATTERN, str(value["id"])):
        raise SchemaError(f"{context}.id has an invalid format")
    if value["resolution_basis"] not in {
        "EDIT",
        "REBUTTAL_ACCEPTED",
        "EXTERNAL_VERIFIED",
        "USER_DECISION",
        "SUPERSEDED",
        "MERGED",
    }:
        raise SchemaError(f"{context}.resolution_basis is invalid")
    _string(value["resolution_reason"], f"{context}.resolution_reason")
    refs = value["evidence_refs"]
    if not isinstance(refs, list) or not refs or not all(isinstance(ref, str) and ref.strip() for ref in refs):
        raise SchemaError(f"{context}.evidence_refs must be a non-empty string array")


def validate_review_schema(payload: dict[str, Any]) -> None:
    _strict_fields(payload, allowed=REVIEW_FIELDS, required=REVIEW_FIELDS, context="review")
    if payload["verdict"] not in {"APPROVED", "NEEDS_REVISION", "USER_DECISION_REQUIRED"}:
        raise SchemaError("review.verdict is invalid")
    _string(payload["summary"], "review.summary")
    for field in ("blocking_issues", "resolved_issues", "questions_for_user", "nonblocking_risks"):
        if not isinstance(payload[field], list):
            raise SchemaError(f"review.{field} must be an array")
    for index, issue in enumerate(payload["blocking_issues"]):
        _issue(issue, f"blocking_issues[{index}]")
    for index, issue in enumerate(payload["nonblocking_risks"]):
        _issue(issue, f"nonblocking_risks[{index}]")
    for index, item in enumerate(payload["resolved_issues"]):
        _resolved(item, f"resolved_issues[{index}]")
    for index, question in enumerate(payload["questions_for_user"]):
        _string(question, f"questions_for_user[{index}]")


def validate_proposal_schema(payload: dict[str, Any]) -> None:
    fields = {"goal", "sections", "requirements", "assumptions", "risks", "user_decisions"}
    _strict_fields(payload, allowed=fields, required=fields, context="proposal")
    _string(payload["goal"], "proposal.goal")
    for field in fields - {"goal"}:
        if not isinstance(payload[field], list) or not all(isinstance(item, str) and item.strip() for item in payload[field]):
            raise SchemaError(f"proposal.{field} must be a string array")


def validate_panel_schema(payload: dict[str, Any]) -> None:
    fields = {"issue_id", "severity", "confidence", "claim", "evidence_ref", "requested_disposition"}
    _strict_fields(payload, allowed=fields, required=fields, context="panel")
    if not re.fullmatch(ISSUE_ID_PATTERN, str(payload["issue_id"])):
        raise SchemaError("panel.issue_id is invalid")
    if isinstance(payload["severity"], bool) or not isinstance(payload["severity"], int) or not 1 <= payload["severity"] <= 5:
        raise SchemaError("panel.severity must be an integer from 1 to 5")
    if isinstance(payload["confidence"], bool) or not isinstance(payload["confidence"], (int, float)) or not 0 <= payload["confidence"] <= 1:
        raise SchemaError("panel.confidence must be between 0 and 1")
    for field in ("claim", "evidence_ref"):
        _string(payload[field], f"panel.{field}")
    if payload["requested_disposition"] not in {"MODIFY", "DISMISS", "ESCALATE"}:
        raise SchemaError("panel.requested_disposition is invalid")


def validate_audit_schema(payload: dict[str, Any]) -> None:
    _strict_fields(payload, allowed={"issues"}, required={"issues"}, context="audit")
    if not isinstance(payload["issues"], list):
        raise SchemaError("audit.issues must be an array")
    allowed = {"id", "validity", "origin", "relation", "duplicate_of", "reason"}
    required = allowed
    for index, item in enumerate(payload["issues"]):
        if not isinstance(item, dict):
            raise SchemaError(f"audit.issues[{index}] must be an object")
        _strict_fields(item, allowed=allowed, required=required, context=f"audit.issues[{index}]")
        if not re.fullmatch(ISSUE_ID_PATTERN, str(item["id"])):
            raise SchemaError(f"audit.issues[{index}].id is invalid")
        if item["validity"] not in {"VALID_BLOCKER", "NOT_BLOCKER", "UNVERIFIED"}:
            raise SchemaError(f"audit.issues[{index}].validity is invalid")
        if item["origin"] not in {"PRE_EXISTING", "REGRESSION", "UNKNOWN"}:
            raise SchemaError(f"audit.issues[{index}].origin is invalid")
        if item["relation"] not in {"UNIQUE", "DUPLICATE"}:
            raise SchemaError(f"audit.issues[{index}].relation is invalid")
        if item["relation"] == "DUPLICATE" and not item.get("duplicate_of"):
            raise SchemaError(f"audit.issues[{index}] duplicate requires duplicate_of")


def validate_against_schema(payload: dict[str, Any], schema_path: Path, kind: str) -> None:
    try:
        import jsonschema  # type: ignore[import-not-found]
    except ImportError:
        jsonschema = None
    if jsonschema is not None:
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            jsonschema.validate(payload, schema)
        except Exception as exc:
            raise SchemaError(f"JSON Schema validation failed: {exc}") from exc
    if kind == "review":
        validate_review_schema(payload)
    elif kind == "proposal":
        validate_proposal_schema(payload)
    elif kind == "panel":
        validate_panel_schema(payload)
    elif kind == "audit":
        validate_audit_schema(payload)


def validate_review_semantics(
    payload: dict[str, Any],
    *,
    registry: dict[str, Any],
    previous_hashes: dict[str, str],
    current_hashes: dict[str, str],
    allowed_criteria: set[str] | None = None,
    allow_rebuttal_similarity: bool = False,
) -> None:
    allowed_criteria = allowed_criteria or set(CRITERIA)
    blocking = payload["blocking_issues"]
    risks = payload["nonblocking_risks"]
    resolved = payload["resolved_issues"]
    if payload["verdict"] == "APPROVED" and (blocking or payload["questions_for_user"]):
        raise SemanticValidationError("APPROVED requires empty blocking issues and user questions")
    if payload["verdict"] == "NEEDS_REVISION" and not blocking:
        raise SemanticValidationError("NEEDS_REVISION requires at least one blocking issue")
    if payload["verdict"] == "USER_DECISION_REQUIRED" and not payload["questions_for_user"]:
        raise SemanticValidationError("USER_DECISION_REQUIRED requires a user question")

    seen: set[str] = set()
    for issue in blocking:
        if issue["severity"] < 3:
            raise SemanticValidationError("blocking_issues may only contain severity 3-5")
        if issue["criterion_id"] not in allowed_criteria:
            raise SemanticValidationError(f"Unknown criterion_id: {issue['criterion_id']}")
        if issue["id"] is not None:
            if issue["id"] not in registry:
                raise SemanticValidationError(f"Unknown existing issue ID: {issue['id']}")
            seen.add(issue["id"])
            history = registry[issue["id"]].get("author_disposition_history", [])
            if history and history[-1].get("value") == "REJECT":
                response = issue.get("response_to_rebuttal")
                if not response:
                    raise SemanticValidationError(f"{issue['id']} requires response_to_rebuttal")
                previous_response = registry[issue["id"]].get("last_response_to_rebuttal")
                if previous_response and not allow_rebuttal_similarity:
                    ratio = SequenceMatcher(None, previous_response, response).ratio()
                    if ratio >= 0.95:
                        raise SemanticValidationError(f"{issue['id']} repeats the previous rebuttal response")
    for issue in risks:
        if issue["severity"] > 2:
            raise SemanticValidationError("nonblocking_risks may only contain severity 1-2")
        if issue["criterion_id"] not in allowed_criteria:
            raise SemanticValidationError(f"Unknown criterion_id: {issue['criterion_id']}")

    resolved_ids: set[str] = set()
    for item in resolved:
        issue_id = item["id"]
        if issue_id not in registry:
            raise SemanticValidationError(f"Cannot resolve unknown issue ID: {issue_id}")
        resolved_ids.add(issue_id)
        if item["resolution_basis"] == "EDIT" and not refs_changed(
            previous_hashes, current_hashes, item["evidence_refs"]
        ):
            raise SemanticValidationError(
                f"{issue_id} declares EDIT but no referenced section hash changed"
            )

    lineage_ids: set[str] = set()
    for issue in blocking:
        if issue.get("split_from"):
            lineage_ids.add(issue["split_from"])
        lineage_ids.update(issue.get("merged_from", []))
    required_open = {
        issue_id
        for issue_id, issue in registry.items()
        if issue.get("status") in OPEN_STATUSES
    }
    missing = sorted(required_open - seen - resolved_ids - lineage_ids)
    if missing:
        raise SemanticValidationError(
            "Previous open issues were silently omitted",
            details={"missing_issue_ids": missing},
        )

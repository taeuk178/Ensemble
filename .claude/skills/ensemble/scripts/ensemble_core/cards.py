from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import SemanticValidationError
from .hashing import canonical_section_ref, parse_sections, resolve_ref_slugs
from .io_utils import atomic_write_text
from .registry import load_registry


FORBIDDEN_CARD_TERMS = {
    "severity_history",
    "confidence_history",
    "ISSUE_SET_STALLED",
    "resolved_without_relevant_edit",
    "author_severity",
}


def _current_sections(markdown: str, references: list[str]) -> str:
    sections = parse_sections(markdown)
    by_slug = {section.slug: section for section in sections}
    matched: set[str] = set()
    for reference in references:
        matched.update(resolve_ref_slugs(canonical_section_ref(reference), by_slug))
    if not matched:
        return "<섹션을 결정적으로 찾을 수 없음>"
    return "\n\n".join(by_slug[slug].content for slug in sorted(matched))


def build_feedback_cards(run_dir: Path, draft_path: Path) -> str:
    registry = load_registry(run_dir)
    markdown = draft_path.read_text(encoding="utf-8")
    lines = ["# Feedback Cards", ""]
    for issue_id, issue in sorted(registry.items()):
        if issue.get("status") not in {"OPEN", "UNVERIFIED", "PANEL_DISSENT"}:
            continue
        history = issue.get("author_disposition_history") or []
        if not history:
            continue
        author = history[-1]
        evidence_refs = [str(value) for value in issue.get("evidence_refs", [])]
        if not evidence_refs and issue.get("section_ref"):
            evidence_refs = [str(issue["section_ref"])]
        lines.extend(
            [
                f"## {issue_id} [일반 라운드]",
                "",
                f"- 상태: {issue.get('status')}",
                f"- 작성자 판단: {author.get('value')}",
                f"- claim: {author.get('claim')}",
                f"- evidence_ref: {author.get('evidence_ref')}",
                f"- draft_evidence_refs: {', '.join(evidence_refs)}",
                f"- requested_disposition: {author.get('requested_disposition')}",
                "- 관련 섹션 현재 텍스트:",
                "",
                "```markdown",
                _current_sections(markdown, evidence_refs),
                "```",
                "",
            ]
        )
    rendered = "\n".join(lines).rstrip() + "\n"
    leaked = sorted(term for term in FORBIDDEN_CARD_TERMS if term in rendered)
    if leaked:
        raise SemanticValidationError(
            "Feedback card contains forbidden anchored metrics", details={"terms": leaked}
        )
    atomic_write_text(run_dir / "feedback-cards.md", rendered)
    return rendered


def build_panel_card(issue_id: str, evaluations: dict[str, dict[str, Any]], author: dict[str, Any]) -> str:
    if set(evaluations) != {"gpt", "gemini"}:
        raise SemanticValidationError("Panel card requires all independent evaluations")
    severities = " / ".join(
        f"{name.upper()} {value['severity']}" for name, value in sorted(evaluations.items())
    )
    values = [int(value["severity"]) for value in evaluations.values()]
    return (
        f"# {issue_id} [에스컬레이션 재평가]\n\n"
        f"- 독립 평가 severity: {severities}\n"
        f"- 불일치도: {max(values) - min(values)}\n"
        f"- 작성자 판단: {author.get('value')}\n"
        f"- claim: {author.get('claim')}\n"
        f"- evidence_ref: {author.get('evidence_ref')}\n"
        f"- requested_disposition: {author.get('requested_disposition')}\n"
    )

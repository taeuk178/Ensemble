from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .io_utils import normalize_text, sha256_text, slugify


@dataclass(frozen=True)
class MarkdownSection:
    heading: str
    slug: str
    level: int
    content: str
    paragraphs: tuple[str, ...]


def _paragraphs(value: str) -> tuple[str, ...]:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", value) if block.strip()]
    return tuple(block for block in blocks if not re.match(r"^#{1,6}\s+", block))


def parse_sections(markdown: str) -> list[MarkdownSection]:
    heading_pattern = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")
    matches = list(heading_pattern.finditer(markdown))
    if not matches:
        return [MarkdownSection("document", "document", 0, markdown, _paragraphs(markdown))]
    sections: list[MarkdownSection] = []
    if matches[0].start() > 0 and markdown[: matches[0].start()].strip():
        preamble = markdown[: matches[0].start()]
        sections.append(MarkdownSection("preamble", "preamble", 0, preamble, _paragraphs(preamble)))
    seen_slugs: dict[str, int] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        heading = match.group(2).strip()
        base_slug = slugify(heading, fallback="section", max_length=80)
        seen_slugs[base_slug] = seen_slugs.get(base_slug, 0) + 1
        slug = base_slug if seen_slugs[base_slug] == 1 else f"{base_slug}-{seen_slugs[base_slug]}"
        content = markdown[match.start() : end].strip()
        sections.append(MarkdownSection(heading, slug, len(match.group(1)), content, _paragraphs(content)))
    return sections


def section_hashes(markdown: str) -> dict[str, str]:
    return {section.slug: sha256_text(normalize_text(section.content)) for section in parse_sections(markdown)}


def canonical_section_ref(value: str) -> str:
    fragment = value.rsplit("#", 1)[-1]
    return slugify(fragment, fallback="document", max_length=80)


def evidence_ref_hashes(markdown: str, refs: Iterable[str]) -> dict[str, str | None]:
    hashes = section_hashes(markdown)
    return {ref: hashes.get(canonical_section_ref(ref)) for ref in refs}


def refs_changed(previous: dict[str, str], current: dict[str, str], refs: Iterable[str]) -> bool:
    for ref in refs:
        slug = canonical_section_ref(ref)
        if previous.get(slug) != current.get(slug):
            return True
    return False


def _section_for_location(markdown: str, location: str) -> MarkdownSection | None:
    target = canonical_section_ref(location)
    sections = parse_sections(markdown)
    exact = [section for section in sections if section.slug == target]
    if len(exact) == 1:
        return exact[0]
    normalized_location = normalize_text(location)
    fuzzy = [section for section in sections if normalize_text(section.heading) == normalized_location]
    return fuzzy[0] if len(fuzzy) == 1 else None


def canonical_evidence_anchor(
    markdown: str,
    *,
    location: str,
    violation_evidence: str,
    required_change: str,
    unmatched_salt: str,
) -> str:
    section = _section_for_location(markdown, location)
    if section is None:
        return f"UNMATCHED:{unmatched_salt}"
    evidence = normalize_text(violation_evidence)
    candidates: list[tuple[int, str]] = []
    for ordinal, paragraph in enumerate(section.paragraphs, start=1):
        normalized_paragraph = normalize_text(paragraph)
        if not normalized_paragraph:
            continue
        if normalized_paragraph in evidence or evidence in normalized_paragraph:
            candidates.append((ordinal, normalized_paragraph))
            continue
        quoted: list[str] = []
        for backtick_value, quoted_value in re.findall(
            r"`([^`]+)`|\"([^\"]+)\"", violation_evidence
        ):
            value = backtick_value or quoted_value
            if value:
                quoted.append(normalize_text(value))
        if any(quote and quote in normalized_paragraph for quote in quoted):
            candidates.append((ordinal, normalized_paragraph))
    unique = {(ordinal, paragraph) for ordinal, paragraph in candidates}
    if len(unique) == 1:
        ordinal, paragraph = next(iter(unique))
        return f"{section.slug}:p{ordinal}:{sha256_text(paragraph)}"
    omission_markers = ("없", "누락", "정의되지", "missing", "not defined", "undefined")
    if any(marker in violation_evidence.casefold() for marker in omission_markers):
        change_hash = sha256_text(normalize_text(required_change))
        return f"{section.slug}:omission:{change_hash}"
    return f"UNMATCHED:{unmatched_salt}"


def consequence_fingerprint(value: str) -> str:
    return sha256_text(normalize_text(value))


def canonical_issue_key(markdown: str, issue: dict[str, object], *, unmatched_salt: str) -> str:
    anchor = canonical_evidence_anchor(
        markdown,
        location=str(issue.get("location", "document")),
        violation_evidence=str(issue.get("violation_evidence", "")),
        required_change=str(issue.get("required_change", "")),
        unmatched_salt=unmatched_salt,
    )
    consequence = consequence_fingerprint(str(issue.get("implementation_consequence", "")))
    return f"{issue.get('criterion_id', '')}|{anchor}|{consequence}"


def load_hashes_for_round(run_dir: Path, round_number: int) -> dict[str, str]:
    path = run_dir / "hashes" / f"round-{round_number}.json"
    if not path.exists():
        return {}
    import json

    return json.loads(path.read_text(encoding="utf-8"))

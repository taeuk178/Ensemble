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
    resolved: dict[str, str | None] = {}
    for ref in refs:
        matches = resolve_ref_slugs(canonical_section_ref(ref), hashes)
        resolved[ref] = hashes[next(iter(matches))] if len(matches) == 1 else None
    return resolved


def resolve_ref_slugs(slug: str, universe: Iterable[str]) -> set[str]:
    """Map one canonical ref slug onto the section slugs it designates.

    An exact slug wins. Otherwise a numeric-prefix ref such as ``2-2`` (from
    ``§2.2``) resolves to every section whose slug starts with it, so a reviewer
    may cite a section number without reproducing its full heading text.
    """
    slugs = set(universe)
    if slug in slugs:
        return {slug}
    prefix = f"{slug}-"
    return {candidate for candidate in slugs if candidate.startswith(prefix)}


def refs_changed(previous: dict[str, str], current: dict[str, str], refs: Iterable[str]) -> bool:
    universe = set(previous) | set(current)
    for ref in refs:
        slug = canonical_section_ref(ref)
        for resolved in resolve_ref_slugs(slug, universe):
            if previous.get(resolved) != current.get(resolved):
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


def _sections_for_refs(markdown: str, evidence_refs: Iterable[str]) -> list[MarkdownSection]:
    sections = parse_sections(markdown)
    by_slug = {section.slug: section for section in sections}
    matches: set[str] = set()
    for ref in evidence_refs:
        matches.update(resolve_ref_slugs(canonical_section_ref(ref), by_slug))
    return [by_slug[slug] for slug in sorted(matches)]


def canonical_evidence_anchor(
    markdown: str,
    *,
    location: str,
    violation_evidence: str,
    required_change: str,
    unmatched_salt: str,
    evidence_refs: Iterable[str] | None = None,
) -> str:
    sections = _sections_for_refs(markdown, evidence_refs or ())
    if not sections:
        fallback = _section_for_location(markdown, location)
        sections = [fallback] if fallback is not None else []
    if not sections:
        return f"UNMATCHED:{unmatched_salt}"
    evidence = normalize_text(violation_evidence)
    candidates: list[tuple[str, int, str]] = []
    quoted: list[str] = []
    for backtick_value, quoted_value in re.findall(
        r"`([^`]+)`|\"([^\"]+)\"", violation_evidence
    ):
        value = backtick_value or quoted_value
        if value:
            quoted.append(normalize_text(value))
    for section in sections:
        for ordinal, paragraph in enumerate(section.paragraphs, start=1):
            normalized_paragraph = normalize_text(paragraph)
            if not normalized_paragraph:
                continue
            if normalized_paragraph in evidence or evidence in normalized_paragraph:
                candidates.append((section.slug, ordinal, normalized_paragraph))
                continue
            if any(quote and quote in normalized_paragraph for quote in quoted):
                candidates.append((section.slug, ordinal, normalized_paragraph))
    unique = {(slug, ordinal, paragraph) for slug, ordinal, paragraph in candidates}
    if len(unique) == 1:
        slug, ordinal, paragraph = next(iter(unique))
        return f"{slug}:p{ordinal}:{sha256_text(paragraph)}"
    omission_markers = ("없", "누락", "정의되지", "missing", "not defined", "undefined")
    if any(marker in violation_evidence.casefold() for marker in omission_markers):
        change_hash = sha256_text(normalize_text(required_change))
        scope_hash = sha256_text("|".join(section.slug for section in sections))
        return f"scope-{scope_hash}:omission:{change_hash}"
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
        evidence_refs=[str(value) for value in issue.get("evidence_refs", [])],
    )
    consequence = consequence_fingerprint(str(issue.get("implementation_consequence", "")))
    return f"{issue.get('criterion_id', '')}|{anchor}|{consequence}"


def load_hashes_for_round(run_dir: Path, round_number: int) -> dict[str, str]:
    path = run_dir / "hashes" / f"round-{round_number}.json"
    if not path.exists():
        return {}
    import json

    return json.loads(path.read_text(encoding="utf-8"))

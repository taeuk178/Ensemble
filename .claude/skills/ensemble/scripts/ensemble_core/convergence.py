from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .config import OPEN_STATUSES
from .io_utils import atomic_write_json, read_json, utc_now
from .state_machine import transition_state
from .registry import load_registry
from . import layout


def _latest_author_dispositions(registry: dict[str, Any]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for issue_id, issue in registry.items():
        if issue.get("status") not in OPEN_STATUSES:
            continue
        history = issue.get("author_disposition_history") or []
        result[issue_id] = history[-1].get("value") if history else None
    return result


def _severity_delta(registry: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for issue_id, issue in registry.items():
        history = [item for item in issue.get("severity_history", []) if item.get("evaluator") == "gpt"]
        if len(history) >= 2:
            result[issue_id] = abs(int(history[-1]["severity"]) - int(history[-2]["severity"]))
    return result


def _recompute_stall(rounds: list[dict[str, Any]]) -> None:
    streak = 0
    storm_streak = 0
    for index, record in enumerate(rounds):
        dispositions = [value for value in record.get("author_dispositions", {}).values() if value]
        record["author_acceptance_rate"] = (
            sum(value == "ACCEPT" for value in dispositions) / len(dispositions)
            if dispositions
            else None
        )
        if index == 0:
            record["stall_transition"] = False
            record["stalled_streak"] = 0
            record["issue_set_stalled"] = False
            record["reviewer_storm_transition"] = False
            record["reviewer_storm_streak"] = 0
            record["reviewer_storm"] = False
            continue
        previous = rounds[index - 1]
        unchanged = (
            record["open_issue_ids"] == previous["open_issue_ids"]
            and record["status_by_issue"] == previous["status_by_issue"]
            and record["author_dispositions"] == previous["author_dispositions"]
            and record["new_issue_count"] == 0
            and record["resolved_issue_count"] == 0
            and record["regression_count"] == 0
        )
        streak = streak + 1 if unchanged else 0
        record["stall_transition"] = unchanged
        record["stalled_streak"] = streak
        record["issue_set_stalled"] = streak >= 2
        storm_transition = (
            record["author_acceptance_rate"] == 1.0
            and record["new_issue_count"] > 0
            and record["new_issue_count"] >= previous["new_issue_count"]
            and record["open_backlog"] >= previous["open_backlog"]
        )
        storm_streak = storm_streak + 1 if storm_transition else 0
        record["reviewer_storm_transition"] = storm_transition
        record["reviewer_storm_streak"] = storm_streak
        record["reviewer_storm"] = storm_streak >= 2


def _author_deadlocks(registry: dict[str, Any]) -> list[str]:
    deadlocks: list[str] = []
    for issue_id, issue in registry.items():
        if issue.get("status") not in OPEN_STATUSES:
            continue
        history = issue.get("author_disposition_history") or []
        if len(history) < 2:
            continue
        previous, current = history[-2:]
        severity_history = [
            item for item in issue.get("severity_history", []) if item.get("evaluator") == "gpt"
        ]
        if (
            previous.get("value") == "REJECT"
            and current.get("value") == "REJECT"
            and int(current.get("round", -10)) == int(previous.get("round", -10)) + 1
            and len(severity_history) >= 2
            and int(severity_history[-1].get("round", -20))
            == int(severity_history[-2].get("round", -20)) + 1
            and int(issue.get("last_seen_round", -30)) == int(current.get("round", -10))
        ):
            deadlocks.append(issue_id)
    return deadlocks


def record_review_metrics(run_dir: Path, *, round_number: int, stats: dict[str, Any]) -> dict[str, Any]:
    registry = load_registry(run_dir)
    convergence = read_json(layout.convergence(run_dir), default={"rounds": [], "events": []})
    rounds = [record for record in convergence["rounds"] if record.get("round") != round_number]
    open_ids = sorted(
        issue_id for issue_id, issue in registry.items() if issue.get("status") in OPEN_STATUSES
    )
    record = {
        "round": round_number,
        "recorded_at": utc_now(),
        "severity_delta": _severity_delta(registry),
        "new_issue_count": len(stats.get("new_issue_ids", [])),
        "resolved_issue_count": len(stats.get("resolved_issue_ids", [])),
        "regression_count": len(stats.get("regressed_issue_ids", [])),
        "open_backlog": len(open_ids),
        "open_issue_ids": open_ids,
        "status_by_issue": {issue_id: registry[issue_id].get("status") for issue_id in open_ids},
        "author_dispositions": _latest_author_dispositions(registry),
        "resolved_without_relevant_edit": len(stats.get("resolved_without_relevant_edit", [])),
        "resolution_basis_counts": stats.get("resolution_basis_counts", {}),
        "external_disagreement": None,
        "semantic_validation_bypass": [],
        "severity_distribution": dict(
            Counter(
                str(issue.get("latest_issue", {}).get("severity"))
                for issue in registry.values()
                if issue.get("last_seen_round") == round_number
            )
        ),
    }
    rounds.append(record)
    rounds.sort(key=lambda item: int(item["round"]))
    _recompute_stall(rounds)
    convergence["rounds"] = rounds
    atomic_write_json(layout.convergence(run_dir), convergence)
    return next(item for item in rounds if item["round"] == round_number)


def refresh_author_dispositions(run_dir: Path, round_number: int) -> None:
    registry = load_registry(run_dir)
    convergence = read_json(layout.convergence(run_dir), default={"rounds": [], "events": []})
    for record in convergence["rounds"]:
        if record.get("round") == round_number:
            record["author_dispositions"] = _latest_author_dispositions(registry)
    _recompute_stall(convergence["rounds"])
    deadlocks = _author_deadlocks(registry)
    current = next((record for record in convergence["rounds"] if record.get("round") == round_number), None)
    signals: list[dict[str, Any]] = []
    for issue_id in deadlocks:
        signals.append({"type": "AUTHOR_DEADLOCK", "issue_id": issue_id, "round": round_number})
    if current and current.get("reviewer_storm"):
        signals.append({"type": "REVIEWER_STORM", "round": round_number})
    existing = {
        (event.get("type"), event.get("issue_id"), event.get("round"))
        for event in convergence["events"]
    }
    for signal in signals:
        key = (signal.get("type"), signal.get("issue_id"), signal.get("round"))
        if key not in existing:
            convergence["events"].append({**signal, "recorded_at": utc_now()})
    atomic_write_json(layout.convergence(run_dir), convergence)
    if signals:
        manifest = read_json(layout.manifest(run_dir))
        phase_three = str(manifest.get("phase")) == "3"
        transition_state(
            manifest,
            "ESCALATION_REQUIRED" if phase_three else "USER_DECISION_REQUIRED",
            reason="convergence escalation signal",
        )
        manifest["escalation_signals"] = signals
        if phase_three:
            pending = set(manifest.get("pending_panel_issue_ids", []))
            pending.update(
                str(signal["issue_id"])
                for signal in signals
                if signal.get("type") == "AUTHOR_DEADLOCK" and signal.get("issue_id")
            )
            manifest["pending_panel_issue_ids"] = sorted(pending)
        atomic_write_json(layout.manifest(run_dir), manifest)


def record_draft_oscillation(run_dir: Path, round_number: int, hashes: dict[str, str]) -> dict[str, Any]:
    convergence = read_json(layout.convergence(run_dir), default={"rounds": [], "events": []})
    history: dict[str, list[tuple[int, str]]] = {}
    for path in layout.iter_hashes(run_dir):
        try:
            number = layout.round_of(path)
        except ValueError:
            continue
        if number >= round_number:
            continue
        for section, digest in read_json(path, default={}).items():
            history.setdefault(section, []).append((number, digest))
    oscillating_sections: list[str] = []
    counts = Counter(
        event.get("section")
        for event in convergence["events"]
        if event.get("type") == "OSCILLATION"
    )
    for section, digest in hashes.items():
        previous = history.get(section, [])
        if len(previous) < 2:
            continue
        latest_digest = previous[-1][1]
        older_rounds = [number for number, old_digest in previous[:-1] if old_digest == digest]
        if digest != latest_digest and older_rounds:
            event = {
                "type": "OSCILLATION",
                "section": section,
                "round": round_number,
                "returned_to_round": older_rounds[-1],
                "occurrence": counts[section] + 1,
                "recorded_at": utc_now(),
            }
            convergence["events"].append(event)
            counts[section] += 1
            oscillating_sections.append(section)
    atomic_write_json(layout.convergence(run_dir), convergence)
    return {
        "sections": oscillating_sections,
        "terminate": any(counts[section] >= 2 for section in oscillating_sections),
    }


def jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    return len(left & right) / len(left | right)


def reproducibility_metrics(runs: list[dict[str, Any]]) -> dict[str, Any]:
    if len(runs) < 2:
        return {"pairs": [], "mean_jaccard": None}
    pairs: list[dict[str, Any]] = []
    for index, left in enumerate(runs):
        for right in runs[index + 1 :]:
            left_keys = set(left.get("issue_keys", []))
            right_keys = set(right.get("issue_keys", []))
            pairs.append(
                {
                    "left": left.get("name"),
                    "right": right.get("name"),
                    "jaccard": jaccard(left_keys, right_keys),
                    "verdict_match": left.get("verdict") == right.get("verdict"),
                }
            )
    return {
        "pairs": pairs,
        "mean_jaccard": sum(pair["jaccard"] for pair in pairs) / len(pairs),
        "verdict_agreement": sum(bool(pair["verdict_match"]) for pair in pairs) / len(pairs),
    }

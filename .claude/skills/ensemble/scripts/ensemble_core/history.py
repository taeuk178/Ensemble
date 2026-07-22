from __future__ import annotations

from pathlib import Path
from typing import Any

from .io_utils import atomic_write_text, read_json


def _relative(run_dir: Path, path: Path) -> str:
    try:
        return path.relative_to(run_dir).as_posix()
    except ValueError:
        return str(path)


def _status_at(issue: dict[str, Any], round_number: int) -> str:
    events = [
        event
        for event in issue.get("status_history", [])
        if int(event.get("round", -1)) <= round_number
    ]
    return str(events[-1].get("to")) if events else "미기록"


def write_timeline(run_dir: Path) -> Path:
    manifest = read_json(run_dir / "manifest.json")
    registry = read_json(run_dir / "issue-registry.json", default={})
    lines = [
        "# Ensemble 작업 기록",
        "",
        f"- 실행 ID: `{manifest.get('run_id')}`",
        f"- 현재 상태: `{manifest.get('state')}`",
        f"- 시작: {manifest.get('started_at')}",
        f"- 종료: {manifest.get('finished_at') or '진행 중'}",
        "",
        "## 실행 환경",
        "",
    ]
    environment = manifest.get("environment", {})
    lines.extend(
        [
            f"- Git 커밋: `{environment.get('git_commit') or '확인 불가'}`",
            f"- 시작할 때 변경된 파일 있음: `{environment.get('git_dirty')}`",
            f"- Ensemble 버전 해시: `{environment.get('ensemble_source_hash') or '미기록'}`",
        ]
    )
    models = manifest.get("models", {})
    for provider in ("codex", "agy"):
        model = models.get(provider, {})
        if provider == "agy" and not model:
            model = models.get("gemini", {})
        lines.append(
            f"- {provider}: `{model.get('actual') or model.get('requested') or '미기록'}` / "
            f"`{model.get('cli_version') or '버전 미기록'}` / `{model.get('command_path') or '경로 미기록'}`"
        )

    lines.extend(["", "## 독립 제안", ""])
    claude = run_dir / "proposals" / "claude.md"
    gpt = run_dir / "proposals" / "gpt.json"
    lines.append(f"- Claude: [{_relative(run_dir, claude)}]({_relative(run_dir, claude)})" if claude.exists() else "- Claude: 아직 없음")
    lines.append(f"- GPT: [{_relative(run_dir, gpt)}]({_relative(run_dir, gpt)})" if gpt.exists() else "- GPT: 아직 없음")

    review_history = manifest.get("review_history", [])
    if not review_history:
        review_history = []
        for review_path in sorted(
            (run_dir / "reviews").glob("round-*.json"),
            key=lambda path: int(path.stem.split("-", 1)[1]),
        ):
            review_round = int(review_path.stem.split("-", 1)[1])
            inferred_draft = review_round - 1
            if (run_dir / "drafts" / f"round-{inferred_draft}.md").exists():
                review_history.append(
                    {"review_round": review_round, "draft_round": inferred_draft}
                )
    lines.extend(["", "## 검토 기록", ""])
    if not review_history:
        lines.append("아직 완료된 리뷰가 없습니다.")
    for entry in review_history:
        review_round = int(entry["review_round"])
        draft_round = int(entry["draft_round"])
        review_path = run_dir / "reviews" / f"round-{review_round}.json"
        review: dict[str, Any] = read_json(review_path, default={})
        lines.extend(
            [
                f"### 리뷰 {review_round} · 초안 {draft_round}",
                "",
                f"- 초안: [drafts/round-{draft_round}.md](drafts/round-{draft_round}.md)",
                f"- 리뷰: [reviews/round-{review_round}.json](reviews/round-{review_round}.json)",
                f"- 판정: `{review.get('verdict', entry.get('verdict', '미기록'))}`",
                f"- 요약: {review.get('summary', '미기록')}",
            ]
        )
        promoted_path = run_dir / "reviews" / f"final-promoted-round-{review_round}.json"
        issue_ids = sorted(
            issue_id
            for issue_id, issue in registry.items()
            if (
                int(issue.get("last_seen_round", -1)) == review_round
                or int(issue.get("resolved_at_round", -1)) == review_round
            )
            and not (
                promoted_path.exists()
                and int(issue.get("first_seen_round", -1)) == review_round
                and issue.get("first_seen_source") in {None, promoted_path.relative_to(run_dir).as_posix()}
                and not review.get("blocking_issues")
            )
        )
        for issue_id in issue_ids:
            issue = registry[issue_id]
            latest = issue.get("latest_issue", {})
            decisions = [
                item
                for item in issue.get("author_disposition_history", [])
                if int(item.get("round", -1)) == review_round
            ]
            lines.append(
                f"- `{issue_id}` {latest.get('problem', '')} → `{_status_at(issue, review_round)}`"
            )
            for decision in decisions:
                lines.append(
                    f"  - Claude `{decision.get('value')}`: {decision.get('argument', '')} "
                    f"(조치: {decision.get('action', '')})"
                )
        lines.append("")

    promoted_paths = sorted(
        (run_dir / "reviews").glob("final-promoted-round-*.json"),
        key=lambda path: int(path.stem.rsplit("-", 1)[1]),
    )
    if promoted_paths:
        lines.extend(["## 최종 독립 검토에서 새로 발견한 이슈 (`FINAL_BLIND`)", ""])
        for path in promoted_paths:
            payload = read_json(path, default={})
            relative = path.relative_to(run_dir).as_posix()
            lines.append(f"### [{relative}]({relative})")
            lines.append("")
            lines.append(f"- 요약: {payload.get('summary', '미기록')}")
            for finding in payload.get("blocking_issues", []):
                lines.append(f"- {finding.get('problem', '')}")
            lines.append("")

    final_blind_paths = sorted((run_dir / "reviews").glob("final-blind-round-*.json"))
    if final_blind_paths:
        lines.extend(["## 최종 독립 검토 (`FINAL_BLIND`)", ""])
        for path in final_blind_paths:
            payload = read_json(path, default={})
            relative = path.relative_to(run_dir).as_posix()
            lines.append(
                f"- [{relative}]({relative}): `{payload.get('verdict', '미기록')}` — "
                f"{payload.get('summary', '')}"
            )
        lines.append("")

    user_decisions = manifest.get("user_decisions", [])
    if user_decisions:
        lines.extend(["## 사용자 결정", ""])
        for item in user_decisions:
            lines.append(
                f"- {item.get('recorded_at')} `{item.get('action')}`: {item.get('note')} "
                f"(이전 상태: `{item.get('from_state')}`)"
            )
        lines.append("")

    lines.extend(["## 최종 결과", ""])
    final_path = run_dir / "final.md"
    lines.append("- [final.md](final.md)" if final_path.exists() else "- 아직 최종 문서가 없습니다.")
    if manifest.get("termination_reason"):
        lines.append(f"- 종료 사유: {manifest['termination_reason']}")
    destination = run_dir / "timeline.md"
    atomic_write_text(destination, "\n".join(lines).rstrip() + "\n")
    return destination

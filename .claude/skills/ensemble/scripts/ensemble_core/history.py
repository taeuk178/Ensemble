from __future__ import annotations

from pathlib import Path
from typing import Any

from .io_utils import atomic_write_text, read_json
from . import layout


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
    manifest = read_json(layout.manifest(run_dir))
    registry = read_json(layout.registry(run_dir), default={})
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
    claude = layout.proposal(run_dir, "claude.md")
    gpt = layout.proposal(run_dir, "gpt.json")
    lines.append(f"- Claude: [{_relative(run_dir, claude)}]({_relative(run_dir, claude)})" if claude.exists() else "- Claude: 아직 없음")
    lines.append(f"- GPT: [{_relative(run_dir, gpt)}]({_relative(run_dir, gpt)})" if gpt.exists() else "- GPT: 아직 없음")

    review_history = manifest.get("review_history", [])
    if not review_history:
        review_history = []
        for review_path in layout.iter_reviews(run_dir):
            review_round = layout.round_of(review_path)
            inferred_draft = review_round - 1
            if (layout.draft(run_dir, inferred_draft)).exists():
                review_history.append(
                    {"review_round": review_round, "draft_round": inferred_draft}
                )
    lines.extend(["", "## 검토 기록", ""])
    if not review_history:
        lines.append("아직 완료된 리뷰가 없습니다.")
    for entry in review_history:
        review_round = int(entry["review_round"])
        draft_round = int(entry["draft_round"])
        review_path = layout.review(run_dir, review_round)
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
        promoted_path = layout.promoted(run_dir, review_round)
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

    promoted_paths = layout.iter_promoted(run_dir)
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

    final_blind_paths = layout.iter_blinds(run_dir)
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
    final_path = layout.final(run_dir)
    lines.append("- [final.md](final.md)" if final_path.exists() else "- 아직 최종 문서가 없습니다.")
    if manifest.get("termination_reason"):
        lines.append(f"- 종료 사유: {manifest['termination_reason']}")
    destination = layout.timeline(run_dir)
    atomic_write_text(destination, "\n".join(lines).rstrip() + "\n")
    write_readme(run_dir)
    return destination


def write_readme(run_dir: Path) -> Path:
    """폴더를 열었을 때 먼저 읽을 진입점.

    나머지 파일을 열지 않고도 상태와 남은 이슈를 알 수 있게 한다.
    검토 회차는 `04-reviews/`의 파일 목록이 아니라 manifest의 기록을
    쓴다. 승격이 쓴 회차는 `iterative/`에 파일이 없기 때문이다.
    """
    manifest = read_json(layout.manifest(run_dir))
    registry = read_json(layout.registry(run_dir), default={})
    open_ids = sorted(
        issue_id
        for issue_id, issue in registry.items()
        if issue.get("status") in {"OPEN", "USER_PENDING", "PANEL_PENDING"}
    )
    limits = manifest.get("limits", {})
    lines = [
        f"# 실행 {manifest.get('run_id')}",
        "",
        "| | |",
        "|---|---|",
        f"| 상태 | `{manifest.get('state')}` |",
        f"| 종료 사유 | {manifest.get('termination_reason') or '진행 중'} |",
        f"| 미해결 이슈 | {len(open_ids)}건 |",
        f"| 검토 회차 | {manifest.get('last_review_round', 0)} / {limits.get('review_rounds', '?')} |",
        f"| 최종 초안 | 초안 {manifest.get('current_round', 0)} |",
        f"| 시작 · 종료 | {manifest.get('started_at')} · {manifest.get('finished_at') or '진행 중'} |",
        "",
        "## 어디부터 볼까",
        "",
    ]
    if layout.final(run_dir).exists():
        lines.append("1. [final.md](final.md) — 결과물. 미해결 이슈는 문서 끝 부록에 있다.")
    else:
        lines.append("1. final.md — 아직 없다. 실행이 끝나면 생긴다.")
    lines.extend(
        [
            "2. [timeline.md](timeline.md) — 회차별 경과와 판정.",
            "3. [decisions.md](decisions.md) — 각 이슈를 왜 그렇게 처리했는지.",
            "",
            "## 폴더",
            "",
            "| 경로 | 내용 |",
            "|---|---|",
            "| `01-input/` | 사용자 원문, 구조화된 요청, 완료 기준 |",
            "| `02-proposals/` | Claude와 GPT의 독립 제안 |",
            "| `03-drafts/` | 회차별 초안 |",
            "| `04-reviews/iterative/` | 반복 검토. `rNN`은 검토 회차 |",
            "| `04-reviews/blind/` | 최종 독립 검토. `draft-NN`은 대상 초안 |",
            "| `04-reviews/promoted/` | 독립 검토 이슈를 일반 이슈로 올린 기록 |",
            "| `04-reviews/reconciliation/` | 수용된 위험과의 대조 |",
            "| `04-reviews/audit/` | 이슈 중복 점검 |",
            "| `04-reviews/panel/` | 추가 평가자 판정 |",
            "| `_state/` | 실행 상태와 이슈 대장. 코드가 읽는다 |",
            "| `_internal/` | 입력 묶음 기록과 검토 세션 작업 폴더 |",
            "",
            "회차 번호는 두 종류다. `rNN`은 검토 회차, `draft-NN`은 대상 초안이다.",
            "`iterative/`에 빠진 번호는 그 회차를 승격이 썼다는 뜻이다.",
            "",
        ]
    )
    history = manifest.get("review_history", [])
    if history:
        lines.extend(["## 검토 경과", "", "| 검토 | 대상 | 판정 |", "|---|---|---|"])
        for entry in history:
            lines.append(
                f"| {entry.get('review_round')} | 초안 {entry.get('draft_round')} "
                f"| `{entry.get('verdict', '미기록')}` |"
            )
        lines.append("")
    if open_ids:
        lines.extend([f"## 미해결 이슈 {len(open_ids)}건", "", "| ID | 중요도 | 문제 |", "|---|---|---|"])
        for issue_id in open_ids:
            latest = registry[issue_id].get("latest_issue") or {}
            lines.append(f"| `{issue_id}` | {latest.get('severity', '?')} | {latest.get('problem', '')} |")
        lines.append("")
    destination = layout.readme(run_dir)
    atomic_write_text(destination, "\n".join(lines).rstrip() + "\n")
    return destination

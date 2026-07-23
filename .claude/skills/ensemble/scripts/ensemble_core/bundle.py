from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import (
    AUDIT_BUNDLE_ALLOWLIST,
    FINAL_BUNDLE_ALLOWLIST,
    FORBIDDEN_REVIEW_FILES,
    JUDGE_BUNDLE_ALLOWLIST,
    JUDGE_EXPECTATIONS_BUNDLE_ALLOWLIST,
    PANEL_BUNDLE_ALLOWLIST,
    PROPOSAL_BUNDLE_ALLOWLIST,
    REVIEW_BUNDLE_ALLOWLIST,
)
from .errors import SecurityError, StateError
from .io_utils import atomic_write_json, ensure_within
from . import layout


MODE_ALLOWLISTS = {
    "proposal": PROPOSAL_BUNDLE_ALLOWLIST,
    "review": REVIEW_BUNDLE_ALLOWLIST,
    "final": FINAL_BUNDLE_ALLOWLIST,
    "panel": PANEL_BUNDLE_ALLOWLIST,
    "audit": AUDIT_BUNDLE_ALLOWLIST,
    "judge": JUDGE_BUNDLE_ALLOWLIST,
    "judge-expectations": JUDGE_EXPECTATIONS_BUNDLE_ALLOWLIST,
}


def _copy_checked(source: Path, destination: Path, run_dir: Path) -> None:
    resolved = ensure_within(source, run_dir)
    if not resolved.is_file() or resolved.is_symlink():
        raise SecurityError(f"Bundle source must be a regular non-symlink file: {source}")
    shutil.copyfile(resolved, destination)


def _copy_user_decisions(run_dir: Path, destination: Path) -> None:
    """구버전 실행에는 projection이 없으므로 빈 권위 입력으로 읽는다."""
    source = layout.user_decisions(run_dir)
    if source.exists():
        _copy_checked(source, destination, run_dir)
        return
    destination.write_text(
        json.dumps({"schema_version": 1, "decisions": []}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def prepare_review_session_bundle(
    run_dir: Path,
    *,
    draft_path: Path,
    request_hash: str,
) -> Path:
    """Refresh the stable, request-scoped workspace used by a resumed review session."""
    if len(request_hash) != 64 or any(character not in "0123456789abcdef" for character in request_hash):
        raise SecurityError("검토 세션의 요청 해시가 올바르지 않습니다.")
    sessions_dir = layout.review_sessions_dir(run_dir)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    if sessions_dir.is_symlink():
        raise SecurityError("검토 세션 폴더는 심볼릭 링크일 수 없습니다.")
    bundle_dir = sessions_dir / request_hash
    bundle_dir.mkdir(parents=True, exist_ok=True)
    if bundle_dir.is_symlink() or not bundle_dir.resolve().is_relative_to(run_dir.resolve()):
        raise SecurityError("검토 세션 작업 폴더가 현재 실행 밖을 가리킵니다.")
    existing = {path.name for path in bundle_dir.iterdir()}
    unexpected = existing - REVIEW_BUNDLE_ALLOWLIST
    if unexpected:
        raise SecurityError(f"검토 세션 폴더에 허용되지 않은 파일이 있습니다: {sorted(unexpected)}")
    unsafe = sorted(
        path.name for path in bundle_dir.iterdir() if path.is_symlink() or not path.is_file()
    )
    if unsafe:
        raise SecurityError(f"검토 세션 폴더에 안전하지 않은 파일이 있습니다: {unsafe}")
    sources = {
        "request.md": layout.request(run_dir),
        "rubric.md": layout.rubric(run_dir),
        "draft.md": draft_path,
        "reviewer-issue-index.json": layout.reviewer_index(run_dir),
        "feedback-cards.md": layout.feedback_cards(run_dir),
    }
    for name, source in sources.items():
        _copy_checked(source, bundle_dir / name, run_dir)
    _copy_user_decisions(run_dir, bundle_dir / "user-decisions.json")
    sources["user-decisions.json"] = layout.user_decisions(run_dir)
    audit_path = layout.bundles_dir(run_dir) / f"{uuid.uuid4().hex}.json"
    atomic_write_json(
        audit_path,
        {
            "mode": "review-session",
            "files": sorted(sources),
            "request_hash": request_hash,
            "workspace": bundle_dir.relative_to(run_dir).as_posix(),
        },
    )
    return bundle_dir


@contextmanager
def isolated_bundle(
    run_dir: Path,
    *,
    mode: str,
    draft_path: Path | None = None,
    issue: dict[str, object] | None = None,
    previous_draft_path: Path | None = None,
    audit_issues: list[dict[str, object]] | None = None,
    inline_documents: dict[str, str] | None = None,
    expectations: dict[str, object] | None = None,
) -> Iterator[Path]:
    if mode not in MODE_ALLOWLISTS:
        raise StateError(f"알 수 없는 입력 묶음 종류입니다: {mode}")
    allowlist = MODE_ALLOWLISTS[mode]
    with tempfile.TemporaryDirectory(prefix=f"ensemble-{mode}-") as temporary:
        bundle_dir = Path(temporary)
        _copy_checked(layout.request(run_dir), bundle_dir / "request.md", run_dir)
        _copy_checked(layout.rubric(run_dir), bundle_dir / "rubric.md", run_dir)
        if mode != "proposal":
            _copy_user_decisions(run_dir, bundle_dir / "user-decisions.json")
        if draft_path is not None:
            _copy_checked(draft_path, bundle_dir / "draft.md", run_dir)
        if mode == "review":
            _copy_checked(
                layout.reviewer_index(run_dir),
                bundle_dir / "reviewer-issue-index.json",
                run_dir,
            )
            _copy_checked(layout.feedback_cards(run_dir), bundle_dir / "feedback-cards.md", run_dir)
        if mode == "panel":
            if issue is None:
                raise StateError("추가 판단에는 이슈 하나가 필요합니다.")
            (bundle_dir / "issue.json").write_text(
                json.dumps(issue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        if mode == "audit":
            if previous_draft_path is None or audit_issues is None:
                raise StateError("이슈 점검에는 이전 초안과 이슈 목록이 필요합니다.")
            _copy_checked(previous_draft_path, bundle_dir / "previous-draft.md", run_dir)
            (bundle_dir / "new-issues.json").write_text(
                json.dumps(audit_issues, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        # 심판은 어느 쪽이 최종본인지 드러나지 않는 중립 파일명이 필요하므로
        # 원본 경로 복사 대신 document-N 이름으로 내용을 직접 쓴다.
        for name, text in (inline_documents or {}).items():
            (bundle_dir / name).write_text(text, encoding="utf-8")
        if mode.startswith("judge"):
            if not inline_documents:
                raise StateError("심판 입력에는 비교할 문서가 필요합니다.")
            if mode == "judge-expectations":
                if expectations is None:
                    raise StateError("기대 결과 채점에는 케이스 정답지가 필요합니다.")
                (bundle_dir / "expectations.json").write_text(
                    json.dumps(expectations, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
        names = {path.name for path in bundle_dir.iterdir()}
        if names - allowlist:
            raise SecurityError(f"Bundle contains non-whitelisted files: {sorted(names - allowlist)}")
        if names & FORBIDDEN_REVIEW_FILES:
            raise SecurityError(f"Bundle contains forbidden files: {sorted(names & FORBIDDEN_REVIEW_FILES)}")
        audit_path = layout.bundles_dir(run_dir) / f"{uuid.uuid4().hex}.json"
        atomic_write_json(audit_path, {"mode": mode, "files": sorted(names)})
        yield bundle_dir

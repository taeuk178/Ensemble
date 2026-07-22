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
    PANEL_BUNDLE_ALLOWLIST,
    PROPOSAL_BUNDLE_ALLOWLIST,
    REVIEW_BUNDLE_ALLOWLIST,
)
from .errors import SecurityError, StateError
from .io_utils import atomic_write_json, ensure_within


MODE_ALLOWLISTS = {
    "proposal": PROPOSAL_BUNDLE_ALLOWLIST,
    "review": REVIEW_BUNDLE_ALLOWLIST,
    "final": FINAL_BUNDLE_ALLOWLIST,
    "panel": PANEL_BUNDLE_ALLOWLIST,
    "audit": AUDIT_BUNDLE_ALLOWLIST,
}


def _copy_checked(source: Path, destination: Path, run_dir: Path) -> None:
    resolved = ensure_within(source, run_dir)
    if not resolved.is_file() or resolved.is_symlink():
        raise SecurityError(f"Bundle source must be a regular non-symlink file: {source}")
    shutil.copyfile(resolved, destination)


@contextmanager
def isolated_bundle(
    run_dir: Path,
    *,
    mode: str,
    draft_path: Path | None = None,
    issue: dict[str, object] | None = None,
    previous_draft_path: Path | None = None,
    audit_issues: list[dict[str, object]] | None = None,
) -> Iterator[Path]:
    if mode not in MODE_ALLOWLISTS:
        raise StateError(f"알 수 없는 입력 묶음 종류입니다: {mode}")
    allowlist = MODE_ALLOWLISTS[mode]
    with tempfile.TemporaryDirectory(prefix=f"ensemble-{mode}-") as temporary:
        bundle_dir = Path(temporary)
        _copy_checked(run_dir / "request.md", bundle_dir / "request.md", run_dir)
        _copy_checked(run_dir / "rubric.md", bundle_dir / "rubric.md", run_dir)
        if draft_path is not None:
            _copy_checked(draft_path, bundle_dir / "draft.md", run_dir)
        if mode == "review":
            _copy_checked(
                run_dir / "reviewer-issue-index.json",
                bundle_dir / "reviewer-issue-index.json",
                run_dir,
            )
            _copy_checked(run_dir / "feedback-cards.md", bundle_dir / "feedback-cards.md", run_dir)
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
        names = {path.name for path in bundle_dir.iterdir()}
        if names - allowlist:
            raise SecurityError(f"Bundle contains non-whitelisted files: {sorted(names - allowlist)}")
        if names & FORBIDDEN_REVIEW_FILES:
            raise SecurityError(f"Bundle contains forbidden files: {sorted(names & FORBIDDEN_REVIEW_FILES)}")
        audit_path = run_dir / "bundles" / f"{uuid.uuid4().hex}.json"
        atomic_write_json(audit_path, {"mode": mode, "files": sorted(names)})
        yield bundle_dir

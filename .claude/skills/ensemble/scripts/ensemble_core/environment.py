from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, SKILL_ROOT


def command_info(command: str) -> dict[str, str | None]:
    resolved = shutil.which(command)
    if resolved is None:
        return {"path": None, "version": None}
    path = str(Path(resolved).resolve())
    try:
        completed = subprocess.run(
            [path, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        version = (completed.stdout or completed.stderr).strip() or None
    except (OSError, subprocess.SubprocessError):
        version = None
    return {"path": path, "version": version}


def ensemble_source_hash() -> str:
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in SKILL_ROOT.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
        and not any(part.startswith(".") for part in path.relative_to(SKILL_ROOT).parts)
    )
    for path in files:
        relative = path.relative_to(SKILL_ROOT).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _git_output(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def environment_snapshot() -> dict[str, Any]:
    status = _git_output("status", "--short", "--untracked-files=no")
    return {
        "ensemble_source_hash": ensemble_source_hash(),
        "git_commit": _git_output("rev-parse", "HEAD"),
        "git_dirty": bool(status) if status is not None else None,
        "codex": command_info("codex"),
        "agy": command_info("agy"),
    }

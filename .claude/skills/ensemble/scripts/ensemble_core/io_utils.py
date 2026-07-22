from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import RUNS_ROOT
from .errors import InputError, SecurityError, StateError


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def slugify(value: str, *, fallback: str = "spec", max_length: int = 32) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^\w]+", "-", normalized, flags=re.UNICODE)
    normalized = normalized.replace("_", "-").strip("-")
    return (normalized or fallback)[:max_length].rstrip("-")


def atomic_write_text(
    path: Path,
    value: str,
    *,
    overwrite: bool = True,
    ensure_trailing_newline: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite and path.exists():
        raise StateError(f"Refusing to overwrite existing artifact: {path}")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            if ensure_trailing_newline and value and not value.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any, *, overwrite: bool = True) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        overwrite=overwrite,
    )


def read_json(path: Path, *, default: Any = None) -> Any:
    if not path.exists():
        if default is not None:
            return default
        raise StateError(f"Required JSON artifact does not exist: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"Cannot read JSON artifact {path}: {exc}") from exc


def ensure_within(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    root_resolved = root.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise SecurityError(f"Path escapes allowed root: {path}")
    return resolved


def resolve_run(run: str | Path) -> Path:
    candidate = Path(run)
    if not candidate.is_absolute():
        direct = RUNS_ROOT / candidate
        candidate = direct if direct.exists() else Path.cwd() / candidate
    resolved = ensure_within(candidate, RUNS_ROOT)
    if not resolved.is_dir():
        raise StateError(f"Not an ensemble run directory: {resolved}")
    if not (resolved / "_state" / "manifest.json").exists():
        _reject_legacy_run(resolved)
        raise StateError(f"Not an ensemble run directory: {resolved}")
    return resolved


def _reject_legacy_run(run_dir: Path) -> None:
    """구 레이아웃(v1) 실행이면 무엇을 열면 되는지 알려주고 멈춘다.

    v1 상태 파일을 읽어 명령을 처리하지는 않는다. 그렇게 하면 경로마다
    두 벌의 읽기 코드가 생긴다. 마지막 상태만 꺼내 안내에 넣는다.
    """
    legacy_manifest = run_dir / "manifest.json"
    if not legacy_manifest.exists():
        return
    last_state = "확인 불가"
    try:
        last_state = str(json.loads(legacy_manifest.read_text(encoding="utf-8")).get("state") or last_state)
    except (OSError, ValueError):
        pass
    final = run_dir / "final.md"
    timeline = run_dir / "timeline.md"
    raise StateError(
        "이 실행은 구 레이아웃(v1)이라 명령을 지원하지 않습니다. "
        f"마지막 기록 상태: {last_state}. final.md와 timeline.md는 직접 열어 주세요.",
        details={
            "layout_version": 1,
            "last_state": last_state,
            "final": str(final) if final.exists() else None,
            "timeline": str(timeline) if timeline.exists() else None,
        },
    )


def parse_answer_section(text: str) -> str:
    match = re.search(r"(?m)^###\s+답변\s*$", text)
    if not match:
        raise InputError("--from 문서에 `### 답변` 헤딩이 없습니다.")
    answer = text[match.end() :]
    next_heading = re.search(r"(?m)^#{1,3}\s+", answer)
    if next_heading:
        answer = answer[: next_heading.start()]
    answer = re.sub(r"<!--.*?-->", "", answer, flags=re.DOTALL).strip()
    if not answer:
        raise InputError("`### 답변` 아래에 유효한 요청이 없습니다.")
    return answer


def safe_source_file(path: str | Path) -> Path:
    candidate = Path(path).resolve()
    if not candidate.is_file():
        raise InputError(f"입력 파일을 찾을 수 없습니다: {candidate}")
    return candidate


def make_run_id(request: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    nonce = secrets.token_hex(4)
    return f"{timestamp}-spec-{nonce}"


def find_consumed_request_hash(request_hash: str) -> list[str]:
    matches: list[str] = []
    if not RUNS_ROOT.exists():
        return matches
    for manifest_path in RUNS_ROOT.glob("*/manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("request_hash") == request_hash:
            matches.append(str(manifest_path.parent))
    return sorted(matches)


def detect_sensitive_text(text: str) -> list[str]:
    patterns = {
        "private_key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        "openai_key": r"\bsk-[A-Za-z0-9_-]{20,}\b",
        "github_token": r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
        "aws_access_key": r"\bAKIA[0-9A-Z]{16}\b",
        "generic_secret_assignment": r"(?i)\b(?:api[_-]?key|secret|token|password)\s*[:=]\s*[^\s]{12,}",
    }
    return [name for name, pattern in patterns.items() if re.search(pattern, text)]

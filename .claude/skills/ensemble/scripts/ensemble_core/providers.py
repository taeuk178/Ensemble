from __future__ import annotations

import json
import importlib.util
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_PANEL_EFFORT,
    DEFAULT_REVIEW_EFFORT,
    DEFAULT_TIMEOUT_SECONDS,
    INFRA_RETRIES,
    USAGE_FIELDS,
)
from .errors import InfraError, SchemaError
from .validation import parse_json_output, validate_against_schema


@dataclass(frozen=True)
class ProviderResult:
    payload: dict[str, Any]
    stdout: str
    stderr: str
    attempts: int
    executable: str
    version: str | None
    model: str
    attempt_errors: tuple[dict[str, Any], ...] = ()
    # What the finished call actually ran with, not merely a user setting.
    reasoning_effort: str | None = None
    session_id: str | None = None
    session_resumed: bool = False
    # CLI가 보고한 실측 토큰 수만 담는다. 보고가 없으면 None으로 두고
    # 프롬프트 길이로 추정하지 않는다. 위치 인자로 만드는 호출부가 있어
    # 순서 실수를 막으려고 키워드 전용으로 둔다.
    #
    # 하나의 논리 호출(run_codex/run_agy 1회) 안에서 사용량을 보고한 모든
    # 시도의 합이다. 재시도로 버려진 응답의 토큰도 이미 소모됐으므로 포함한다.
    usage: dict[str, int] | None = field(default=None, kw_only=True)
    # 위 합계에 기여한 시도 수. `attempts`보다 작으면 합계는 하한값이다.
    attempts_reported: int = field(default=0, kw_only=True)


def command_version(command: str) -> str | None:
    executable = shutil.which(command)
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (completed.stdout or completed.stderr).strip()
    return output or None


def preflight(
    *,
    live_codex: bool,
    model: str,
    timeout: int = 60,
    effort: str = DEFAULT_REVIEW_EFFORT,
    panel_effort: str = DEFAULT_PANEL_EFFORT,
) -> dict[str, Any]:
    codex_version = command_version("codex")
    agy_version = command_version("agy")
    codex_path = shutil.which("codex")
    agy_path = shutil.which("agy")
    result: dict[str, Any] = {
        "codex": {
            "available": codex_version is not None,
            "version": codex_version,
            "path": str(Path(codex_path).resolve()) if codex_path else None,
            "reasoning_effort": effort,
        },
        "agy": {
            "available": agy_version is not None,
            "version": agy_version,
            "path": str(Path(agy_path).resolve()) if agy_path else None,
            "reasoning_effort": panel_effort,
        },
        "jsonschema": {
            "available": importlib.util.find_spec("jsonschema") is not None,
            "fallback": "built-in strict validator",
        },
    }
    if codex_version is None:
        raise InfraError("Codex CLI is not installed or not executable")
    if live_codex:
        executable = shutil.which("codex")
        assert executable is not None
        command = [
            executable,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "-c",
            f"model_reasoning_effort={effort}",
            "-m",
            model,
            "--sandbox",
            "read-only",
            "-",
        ]
        try:
            completed = subprocess.run(
                command,
                input="Respond with exactly: PONG",
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InfraError(f"Codex live preflight failed: {exc}") from exc
        if completed.returncode != 0 or "PONG" not in completed.stdout:
            raise InfraError(
                "Codex live preflight did not return PONG",
                details={"returncode": completed.returncode, "stderr": completed.stderr[-1000:]},
            )
        result["codex"]["live"] = True
    return result


def _codex_session_id(stdout: str) -> str | None:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                return thread_id
    return None


def _codex_usage(stdout: str) -> dict[str, int] | None:
    """`codex exec --json`이 `turn.completed`에 실어 보내는 실측 사용량을 모은다.

    codex-cli 0.145.0에서 첫 호출과 `exec resume` 모두 같은 이벤트를 낸다.
    이벤트가 없으면 추정하지 않고 None을 돌려준다.
    """
    totals = dict.fromkeys(USAGE_FIELDS, 0)
    reported = False
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        reported = True
        for key in totals:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                totals[key] += value
    return totals if reported else None


def _sum_usages(usages: list[dict[str, int]]) -> dict[str, int] | None:
    """보고된 시도들의 사용량 합. 실패한 시도의 토큰도 여기에 포함된다."""
    if not usages:
        return None
    totals = dict.fromkeys(USAGE_FIELDS, 0)
    for usage in usages:
        for key in totals:
            totals[key] += int(usage.get(key, 0))
    return totals


def run_codex(
    *,
    bundle_dir: Path,
    prompt: str,
    schema_path: Path,
    schema_kind: str,
    model: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    retries: int = INFRA_RETRIES,
    effort: str = DEFAULT_REVIEW_EFFORT,
    persist_session: bool = False,
    session_id: str | None = None,
) -> ProviderResult:
    if session_id is not None and not persist_session:
        raise ValueError("session_id requires persist_session=True")
    discovered = shutil.which("codex")
    if discovered is None:
        raise InfraError("Codex CLI is not installed")
    executable = str(Path(discovered).resolve())
    version = command_version(executable)
    last_error: dict[str, Any] | None = None
    last_schema_error: SchemaError | None = None
    attempt_errors: list[dict[str, Any]] = []
    attempt_usages: list[dict[str, int]] = []
    active_session_id = session_id
    for attempt in range(1, retries + 2):
        with tempfile.NamedTemporaryFile(prefix="ensemble-codex-", suffix=".json", delete=False) as handle:
            output_path = Path(handle.name)
        try:
            resuming = active_session_id is not None
            if resuming:
                command = [
                    executable,
                    "exec",
                    "resume",
                    "--ignore-user-config",
                    "--skip-git-repo-check",
                    "-c",
                    f"model_reasoning_effort={effort}",
                    "-m",
                    model,
                    "--output-schema",
                    str(schema_path),
                    "--json",
                    "--output-last-message",
                    str(output_path),
                    active_session_id,
                    "-",
                ]
            else:
                command = [
                    executable,
                    "exec",
                    *([] if persist_session else ["--ephemeral"]),
                    "--ignore-user-config",
                    "--skip-git-repo-check",
                    "-c",
                    f"model_reasoning_effort={effort}",
                    "-C",
                    str(bundle_dir),
                    "-m",
                    model,
                    "--sandbox",
                    "read-only",
                    "--output-schema",
                    str(schema_path),
                    # 응답은 --output-last-message로 받으므로 --json을 켜도
                    # 파싱 경로는 그대로다. 사용량 이벤트만 추가로 얻는다.
                    "--json",
                    "--output-last-message",
                    str(output_path),
                    "-",
                ]
            try:
                completed = subprocess.run(
                    command,
                    cwd=bundle_dir,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                last_error = {"attempt": attempt, "kind": "timeout", "message": str(exc)}
                attempt_errors.append(last_error)
                continue
            except OSError as exc:
                last_error = {"attempt": attempt, "kind": "os", "message": str(exc)}
                attempt_errors.append(last_error)
                continue
            # 스키마 오류 등으로 재시도해도 이미 소모한 토큰은 사라지지 않으므로
            # 검증 전에 시도별 사용량을 먼저 모은다.
            attempt_usage = _codex_usage(completed.stdout)
            if attempt_usage is not None:
                attempt_usages.append(attempt_usage)
            if completed.returncode != 0:
                last_error = {
                    "attempt": attempt,
                    "kind": "exit",
                    "returncode": completed.returncode,
                    "stderr": completed.stderr[-2000:],
                }
                attempt_errors.append(last_error)
                continue
            reported_session_id = _codex_session_id(completed.stdout)
            if active_session_id is not None and reported_session_id not in (None, active_session_id):
                last_error = {
                    "attempt": attempt,
                    "kind": "session_id_mismatch",
                    "expected": active_session_id,
                    "actual": reported_session_id,
                }
                attempt_errors.append(last_error)
                continue
            if persist_session and active_session_id is None:
                active_session_id = reported_session_id
                if active_session_id is None:
                    last_error = {"attempt": attempt, "kind": "missing_session_id"}
                    attempt_errors.append(last_error)
                    continue
            raw = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else ""
            if not raw:
                last_error = {"attempt": attempt, "kind": "empty_output"}
                attempt_errors.append(last_error)
                continue
            try:
                payload = parse_json_output(raw)
                validate_against_schema(payload, schema_path, schema_kind)
            except SchemaError as exc:
                last_schema_error = exc
                last_error = {"attempt": attempt, "kind": "schema", "message": exc.message}
                attempt_errors.append(last_error)
                continue
            return ProviderResult(
                payload=payload,
                stdout=completed.stdout,
                stderr=completed.stderr,
                attempts=attempt,
                executable=executable,
                version=version,
                model=model,
                attempt_errors=tuple(attempt_errors),
                reasoning_effort=effort,
                session_id=active_session_id,
                session_resumed=resuming,
                usage=_sum_usages(attempt_usages),
                attempts_reported=len(attempt_usages),
            )
        finally:
            output_path.unlink(missing_ok=True)
    if last_error and last_error.get("kind") == "schema" and last_schema_error is not None:
        raise SchemaError(
            "Codex output failed schema validation after retries",
            details={
                "attempts": retries + 1,
                "last_error": last_schema_error.as_dict(),
                "attempt_errors": attempt_errors,
                "command_path": executable,
                "cli_version": version,
                "model": model,
                "reasoning_effort": effort,
                "provider": "codex",
                "session_id": active_session_id,
                "persist_session": persist_session,
            },
        )
    raise InfraError(
        "Codex failed after retries",
        details={
            "attempts": retries + 1,
            "last_error": last_error,
            "attempt_errors": attempt_errors,
            "command_path": executable,
            "cli_version": version,
            "model": model,
            "reasoning_effort": effort,
            "provider": "codex",
            "session_id": active_session_id,
            "persist_session": persist_session,
        },
    )


def _parse_agy_output(raw: str) -> dict[str, Any]:
    stripped = raw.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[0].strip() in {"```", "```json", "```JSON"}:
            stripped = "\n".join(lines[1:-1]).strip()
    return parse_json_output(stripped)


def run_agy(
    *,
    bundle_dir: Path,
    prompt: str,
    schema_path: Path,
    schema_kind: str,
    model: str,
    effort: str = DEFAULT_PANEL_EFFORT,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    retries: int = INFRA_RETRIES,
) -> ProviderResult:
    discovered = shutil.which("agy")
    if discovered is None:
        raise InfraError("Antigravity CLI (agy) is not installed")
    executable = str(Path(discovered).resolve())
    version = command_version(executable)
    schema = schema_path.read_text(encoding="utf-8")
    full_prompt = f"{prompt}\n\nReturn JSON matching this schema exactly:\n{schema}"
    last_error: dict[str, Any] | None = None
    attempt_errors: list[dict[str, Any]] = []
    for attempt in range(1, retries + 2):
        command = [
            executable,
            "--model",
            model,
            "--effort",
            effort,
            "--mode",
            "plan",
            "--sandbox",
            "--print-timeout",
            f"{timeout}s",
            "-p",
            full_prompt,
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=bundle_dir,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            last_error = {"attempt": attempt, "kind": "infra", "message": str(exc)}
            attempt_errors.append(last_error)
            continue
        if completed.returncode != 0:
            last_error = {
                "attempt": attempt,
                "kind": "exit",
                "returncode": completed.returncode,
                "stderr": completed.stderr[-2000:],
            }
            attempt_errors.append(last_error)
            continue
        raw = completed.stdout.strip()
        if not raw:
            last_error = {"attempt": attempt, "kind": "empty_output"}
            attempt_errors.append(last_error)
            continue
        try:
            payload = _parse_agy_output(raw)
            validate_against_schema(payload, schema_path, schema_kind)
        except SchemaError as exc:
            last_error = {"attempt": attempt, "kind": "schema", "message": str(exc)}
            attempt_errors.append(last_error)
            continue
        return ProviderResult(
            payload,
            completed.stdout,
            completed.stderr,
            attempt,
            executable,
            version,
            model,
            tuple(attempt_errors),
            reasoning_effort=effort,
            # agy 1.1.5에는 사용량을 보고하는 플래그가 없다. 추정하지 않고
            # 미보고로 남긴다.
            usage=None,
            attempts_reported=0,
        )
    if last_error and last_error.get("kind") == "schema":
        raise SchemaError(
            "Antigravity output failed schema validation after retries",
            details={
                "attempts": retries + 1,
                "last_error": last_error,
                "attempt_errors": attempt_errors,
                "command_path": executable,
                "cli_version": version,
                "model": model,
                "provider": "agy",
                "reasoning_effort": effort,
            },
        )
    raise InfraError(
        "Antigravity CLI failed after retries",
        details={
            "attempts": retries + 1,
            "last_error": last_error,
            "attempt_errors": attempt_errors,
            "command_path": executable,
            "cli_version": version,
            "model": model,
            "provider": "agy",
            "reasoning_effort": effort,
        },
    )

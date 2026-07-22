from __future__ import annotations

import json
import importlib.util
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DEFAULT_REVIEW_EFFORT, DEFAULT_TIMEOUT_SECONDS, INFRA_RETRIES
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
    # What the finished call actually ran with, not a setting. run_codex always
    # passes its own effort here; run_gemini leaves it None because the Gemini
    # CLI has no reasoning-effort concept, and recording a value there would be
    # a false record.
    reasoning_effort: str | None = None
    session_id: str | None = None
    session_resumed: bool = False


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
) -> dict[str, Any]:
    codex_version = command_version("codex")
    gemini_version = command_version("gemini")
    codex_path = shutil.which("codex")
    gemini_path = shutil.which("gemini")
    result: dict[str, Any] = {
        "codex": {
            "available": codex_version is not None,
            "version": codex_version,
            "path": str(Path(codex_path).resolve()) if codex_path else None,
            "reasoning_effort": effort,
        },
        "gemini": {
            "available": gemini_version is not None,
            "version": gemini_version,
            "path": str(Path(gemini_path).resolve()) if gemini_path else None,
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
                    *(["--json"] if persist_session else []),
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


def run_gemini(
    *,
    bundle_dir: Path,
    prompt: str,
    schema_path: Path,
    schema_kind: str,
    model: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    retries: int = INFRA_RETRIES,
) -> ProviderResult:
    discovered = shutil.which("gemini")
    if discovered is None:
        raise InfraError("Gemini CLI is not installed")
    executable = str(Path(discovered).resolve())
    version = command_version(executable)
    schema = schema_path.read_text(encoding="utf-8")
    full_prompt = f"{prompt}\n\nReturn JSON matching this schema exactly:\n{schema}"
    last_error: dict[str, Any] | None = None
    last_schema_error: SchemaError | None = None
    attempt_errors: list[dict[str, Any]] = []
    for attempt in range(1, retries + 2):
        command = [
            executable,
            "--approval-mode",
            "plan",
            "--skip-trust",
            "--allowed-mcp-server-names",
            "",
            "-e",
            "",
            "-m",
            model,
            "-o",
            "json",
            "-p",
            "-",
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=bundle_dir,
                input=full_prompt,
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
            outer = json.loads(raw)
            candidate = outer.get("response", outer) if isinstance(outer, dict) else outer
            payload = candidate if isinstance(candidate, dict) else parse_json_output(str(candidate))
            validate_against_schema(payload, schema_path, schema_kind)
        except (json.JSONDecodeError, SchemaError) as exc:
            last_error = {"attempt": attempt, "kind": "schema", "message": str(exc)}
            if isinstance(exc, SchemaError):
                last_schema_error = exc
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
        )
    if last_error and last_error.get("kind") == "schema":
        raise SchemaError(
            "Gemini output failed schema validation after retries",
            details={
                "attempts": retries + 1,
                "last_error": last_error,
                "attempt_errors": attempt_errors,
                "command_path": executable,
                "cli_version": version,
                "model": model,
                "provider": "gemini",
            },
        )
    raise InfraError(
        "Gemini failed after retries",
        details={
            "attempts": retries + 1,
            "last_error": last_error,
            "attempt_errors": attempt_errors,
            "command_path": executable,
            "cli_version": version,
            "model": model,
            "provider": "gemini",
        },
    )

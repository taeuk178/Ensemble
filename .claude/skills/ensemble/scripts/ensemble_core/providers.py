from __future__ import annotations

import json
import importlib.util
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DEFAULT_TIMEOUT_SECONDS, INFRA_RETRIES
from .errors import InfraError, SchemaError
from .validation import parse_json_output, validate_against_schema


@dataclass(frozen=True)
class ProviderResult:
    payload: dict[str, Any]
    stdout: str
    stderr: str
    attempts: int


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


def preflight(*, live_codex: bool, model: str, timeout: int = 60) -> dict[str, Any]:
    codex_version = command_version("codex")
    gemini_version = command_version("gemini")
    result: dict[str, Any] = {
        "codex": {"available": codex_version is not None, "version": codex_version},
        "gemini": {"available": gemini_version is not None, "version": gemini_version},
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


def run_codex(
    *,
    bundle_dir: Path,
    prompt: str,
    schema_path: Path,
    schema_kind: str,
    model: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    retries: int = INFRA_RETRIES,
) -> ProviderResult:
    executable = shutil.which("codex")
    if executable is None:
        raise InfraError("Codex CLI is not installed")
    last_error: dict[str, Any] | None = None
    last_schema_error: SchemaError | None = None
    for attempt in range(1, retries + 2):
        with tempfile.NamedTemporaryFile(prefix="ensemble-codex-", suffix=".json", delete=False) as handle:
            output_path = Path(handle.name)
        try:
            command = [
                executable,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--skip-git-repo-check",
                "-C",
                str(bundle_dir),
                "-m",
                model,
                "--sandbox",
                "read-only",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-",
            ]
            try:
                completed = subprocess.run(
                    command,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                last_error = {"attempt": attempt, "kind": "timeout", "message": str(exc)}
                continue
            except OSError as exc:
                last_error = {"attempt": attempt, "kind": "os", "message": str(exc)}
                continue
            if completed.returncode != 0:
                last_error = {
                    "attempt": attempt,
                    "kind": "exit",
                    "returncode": completed.returncode,
                    "stderr": completed.stderr[-2000:],
                }
                continue
            raw = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else ""
            if not raw:
                last_error = {"attempt": attempt, "kind": "empty_output"}
                continue
            try:
                payload = parse_json_output(raw)
                validate_against_schema(payload, schema_path, schema_kind)
            except SchemaError as exc:
                last_schema_error = exc
                last_error = {"attempt": attempt, "kind": "schema", "message": exc.message}
                continue
            return ProviderResult(payload, completed.stdout, completed.stderr, attempt)
        finally:
            output_path.unlink(missing_ok=True)
    if last_error and last_error.get("kind") == "schema" and last_schema_error is not None:
        raise SchemaError(
            "Codex output failed schema validation after retries",
            details={"attempts": retries + 1, "last_error": last_schema_error.as_dict()},
        )
    raise InfraError("Codex failed after retries", details=last_error)


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
    executable = shutil.which("gemini")
    if executable is None:
        raise InfraError("Gemini CLI is not installed")
    schema = schema_path.read_text(encoding="utf-8")
    full_prompt = f"{prompt}\n\nReturn JSON matching this schema exactly:\n{schema}"
    last_error: dict[str, Any] | None = None
    last_schema_error: SchemaError | None = None
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
            continue
        if completed.returncode != 0:
            last_error = {
                "attempt": attempt,
                "kind": "exit",
                "returncode": completed.returncode,
                "stderr": completed.stderr[-2000:],
            }
            continue
        raw = completed.stdout.strip()
        if not raw:
            last_error = {"attempt": attempt, "kind": "empty_output"}
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
            continue
        return ProviderResult(payload, completed.stdout, completed.stderr, attempt)
    if last_error and last_error.get("kind") == "schema":
        raise SchemaError(
            "Gemini output failed schema validation after retries",
            details={"attempts": retries + 1, "last_error": last_error},
        )
    raise InfraError("Gemini failed after retries", details=last_error)

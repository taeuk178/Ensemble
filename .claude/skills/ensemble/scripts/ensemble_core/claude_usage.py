"""작성자(Claude)의 실측 토큰을 세션 기록에서 모은다.

Codex와 Agy는 CLI라서 `run_codex`/`run_agy`가 호출마다 사용량을 받아 온다.
작성자는 CLI가 아니라 스킬을 실행하는 주체이므로 그런 지점이 없다. 대신
Claude Code가 남기는 세션 기록(JSONL)에 API 응답이 보고한 토큰이 그대로
들어 있다. 프롬프트 길이로 추정하지 않는다는 원칙은 그대로다.

**귀속의 한계.** 세션 기록은 실행이 아니라 세션 단위다. 어느 메시지가 어느
실행을 위한 것인지 표시되지 않으므로, 실행의 시작~종료 시각 창에 들어오는
메시지를 모두 더한다. 같은 창에 실행과 무관한 작업이 섞여 있으면 그만큼
과계산된다. 따라서 이 값은 **상한값**이다 — 미보고 호출 때문에 하한값인
Codex 집계와 반대 방향의 오차를 가진다. 두 사실 모두 집계에 새겨 둔다.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from . import config
from .config import USAGE_FIELDS
from .errors import StateError
from .io_utils import atomic_write_json, read_json
from . import layout


# 세션 기록의 사용량 필드 → manifest의 제공자 공통 필드.
FIELD_MAP = {
    "input_tokens": "input_tokens",
    "cache_read_input_tokens": "cached_input_tokens",
    "cache_creation_input_tokens": "cache_write_input_tokens",
    "output_tokens": "output_tokens",
}

# Claude API는 추론 토큰을 따로 보고하지 않는다. 0을 실측값으로 읽지 않도록
# 집계에 미보고 필드로 명시한다.
UNREPORTED_FIELDS = ("reasoning_output_tokens",)

# 실제 API 호출이 아니라 클라이언트가 만들어 넣은 항목.
SYNTHETIC_MODEL = "<synthetic>"


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def iter_usage_entries(transcript_root: Path) -> Iterator[dict[str, Any]]:
    """세션 기록에서 사용량이 붙은 assistant 메시지를 하나씩 내놓는다.

    한 메시지가 여러 줄에 걸쳐 기록될 수 있고 그 줄들은 같은 사용량을 싣고
    있다. 중복 제거는 호출자가 `message.id`로 한다.
    """
    if not transcript_root.is_dir():
        return
    for path in sorted(transcript_root.glob("*.jsonl")):
        try:
            handle = path.open(encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "assistant":
                    continue
                message = entry.get("message")
                if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                    continue
                yield entry


def _within_project(cwd: Any, project_root: Path) -> bool:
    if not isinstance(cwd, str) or not cwd:
        return False
    root = str(project_root)
    return cwd == root or cwd.startswith(root + "/")


def collect_usage(
    *,
    window_start: datetime,
    window_end: datetime,
    transcript_root: Path | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """시간 창에 들어오는 작성자 메시지의 사용량을 모아 집계한다.

    경로는 호출 시점에 config에서 읽는다. 그래야 테스트가 사용자 홈의 실제
    세션 기록을 건드리지 않고 합성 기록으로 검증할 수 있다.
    """
    transcript_root = transcript_root or config.CLAUDE_TRANSCRIPT_ROOT
    project_root = project_root or config.PROJECT_ROOT
    totals = dict.fromkeys(USAGE_FIELDS, 0)
    per_model: dict[str, dict[str, int]] = {}
    sessions: set[str] = set()
    seen: set[str] = set()
    messages = 0
    for entry in iter_usage_entries(transcript_root):
        message = entry["message"]
        message_id = message.get("id")
        if isinstance(message_id, str) and message_id:
            if message_id in seen:
                continue
            seen.add(message_id)
        model = message.get("model")
        if model == SYNTHETIC_MODEL:
            continue
        if not _within_project(entry.get("cwd"), project_root):
            continue
        recorded_at = parse_timestamp(entry.get("timestamp"))
        if recorded_at is None or not (window_start <= recorded_at <= window_end):
            continue
        usage = message["usage"]
        bucket = per_model.setdefault(str(model or "unknown"), {"messages": 0})
        bucket["messages"] += 1
        messages += 1
        for source, target in FIELD_MAP.items():
            value = usage.get(source)
            amount = int(value) if isinstance(value, int) and not isinstance(value, bool) else 0
            totals[target] += amount
            bucket[target] = bucket.get(target, 0) + amount
        session_id = entry.get("sessionId") or entry.get("session_id")
        if isinstance(session_id, str) and session_id:
            sessions.add(session_id)
    return {
        **totals,
        "messages_counted": messages,
        # 이 창에 실행과 무관한 작업이 섞여 있으면 그만큼 과계산된다.
        # Codex 집계가 하한값인 것과 반대 방향의 오차다.
        "attribution": "session_time_window",
        "upper_bound": True,
        "unreported_fields": list(UNREPORTED_FIELDS),
        "window": {
            "start": window_start.isoformat().replace("+00:00", "Z"),
            "end": window_end.isoformat().replace("+00:00", "Z"),
        },
        "transcript_root": str(transcript_root),
        "transcripts_found": transcript_root.is_dir(),
        "sessions": sorted(sessions),
        "models": {name: dict(sorted(values.items())) for name, values in sorted(per_model.items())},
    }


def run_window(manifest: dict[str, Any]) -> tuple[datetime, datetime] | None:
    """실행이 살아 있던 구간. 끝나지 않은 실행은 마지막 기록 시각까지 본다."""
    start = parse_timestamp(manifest.get("started_at"))
    if start is None:
        return None
    end = parse_timestamp(manifest.get("finished_at"))
    if end is not None:
        return start, end
    candidates = [start]
    for key, field in (
        ("state_history", "recorded_at"),
        ("provider_calls", "recorded_at"),
        ("retry_events", "recorded_at"),
        ("user_decisions", "recorded_at"),
    ):
        for item in manifest.get(key) or []:
            if not isinstance(item, dict):
                continue
            parsed = parse_timestamp(item.get(field))
            if parsed is not None:
                candidates.append(parsed)
    return start, max(candidates)


def record_claude_usage(
    run_dir: Path,
    *,
    transcript_root: Path | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """실행의 manifest에 작성자 사용량을 기록한다.

    Codex/Agy 집계는 호출마다 더해 나가지만 이 값은 창 전체를 다시 계산해
    **대체**한다. 여러 번 실행해도 값이 부풀지 않는다.
    """
    manifest = read_json(layout.manifest(run_dir))
    window = run_window(manifest)
    if window is None:
        raise StateError("manifest에 started_at이 없어 사용량 구간을 정할 수 없습니다.")
    usage = collect_usage(
        window_start=window[0],
        window_end=window[1],
        transcript_root=transcript_root,
        project_root=project_root,
    )
    manifest.setdefault("usage", {})["claude"] = usage
    atomic_write_json(layout.manifest(run_dir), manifest)
    return usage

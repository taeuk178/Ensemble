"""2층 — 검토 루프가 문서를 실제로 개선했는지 심판 모델로 비교한다.

첫 초안(`draft-00.md`)과 finalize가 고른 마지막 초안을 루프 밖 모델이
어느 쪽이 나중인지 모른 채 비교한다. `final.md`는 쓰지 않는다 — 보고 단계가
붙인 상태 헤더와 리뷰 이력 부록이 블라인드를 깨고, 루프의 산출물도 아니다.

평가는 실행 상태를 바꾸지 않는다. 심판 호출은 실행의 `manifest.json`이 아니라
평가 결과 파일에만 기록하고, 심판 호출이 실패해도 실행을 종료시키지 않는다.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .bundle import isolated_bundle
from .config import JUDGE_AXES, REFERENCE_ROOT
from .environment import ensemble_source_hash, git_commit
from .errors import InfraError, StateError
from .io_utils import atomic_write_json, read_json, sha256_text, utc_now
from .providers import ProviderResult, run_agy
from .state_machine import accumulate_usage
from . import layout


SCHEMA_VERSION = 1

JUDGE_PROMPT_PATH = REFERENCE_ROOT / "judge-prompt.md"
JUDGE_SCHEMA_PATH = REFERENCE_ROOT / "judge.schema.json"
JUDGE_EXPECTATIONS_PROMPT_PATH = REFERENCE_ROOT / "judge-expectations-prompt.md"
JUDGE_EXPECTATIONS_SCHEMA_PATH = REFERENCE_ROOT / "judge-expectations.schema.json"

# 호출 1은 첫 초안을 앞에, 호출 2는 순서를 바꾼다. 위치 편향을 걷어내기 위한
# 것이지 심판 노이즈 자체를 없애지는 못한다.
CALL_ORDERS = ("DRAFT_FIRST", "FINAL_FIRST")


def _file_sha256(path: Path) -> str:
    return sha256_text(path.read_text(encoding="utf-8"))


def select_final_draft(run_dir: Path, manifest: dict[str, Any]) -> Path | None:
    """finalize와 같은 규칙으로 최종 문서에 쓰인 초안을 고른다."""
    current_round = int(manifest.get("current_round", 0))
    candidate = layout.draft(run_dir, current_round)
    if candidate.exists():
        return candidate
    drafts = layout.iter_drafts(run_dir)
    return drafts[-1] if drafts else None


def _next_raw_index(run_dir: Path) -> int:
    """심판 원문은 덮어쓰지 않는다. 재평가하면 번호가 이어서 붙는다."""
    raw_dir = layout.judge_raw_dir(run_dir)
    if not raw_dir.exists():
        return 1
    used = []
    for path in [*raw_dir.glob("call-*.json"), *raw_dir.glob("failure-*.json")]:
        try:
            used.append(int(path.stem.split("-", 1)[1]))
        except ValueError:
            continue
    return max(used, default=0) + 1


def map_verdict(order: str, winner: str) -> str:
    """심판이 고른 문서 번호를 draft/final로 되돌린다.

    이 매핑이 틀리면 결과가 정반대로 뒤집힌다.
    """
    if winner == "TIE":
        return "TIE"
    if order == "DRAFT_FIRST":
        return {"DOC1": "DRAFT", "DOC2": "FINAL"}[winner]
    if order == "FINAL_FIRST":
        return {"DOC1": "FINAL", "DOC2": "DRAFT"}[winner]
    raise StateError(f"알 수 없는 문서 제시 순서입니다: {order}")


def compose_pair(first: str, second: str) -> str:
    """순서를 바꾼 두 호출의 판정을 합성한다. 갈리면 불안정으로 본다."""
    if first != second:
        return "UNSTABLE"
    return {"FINAL": "FINAL_BETTER", "DRAFT": "DRAFT_BETTER", "TIE": "TIE"}[first]


def compose_repetitions(pair_results: list[str]) -> str:
    """반복 측정 결과를 하나로 모은다. 하나라도 다르면 불안정이다."""
    unique = set(pair_results)
    if len(unique) == 1:
        return pair_results[0]
    return "UNSTABLE"


def _judge_once(
    run_dir: Path,
    *,
    documents: dict[str, str],
    model: str,
    effort: str,
    timeout: int,
) -> ProviderResult:
    prompt = JUDGE_PROMPT_PATH.read_text(encoding="utf-8")
    with isolated_bundle(run_dir, mode="judge", inline_documents=documents) as bundle_dir:
        return run_agy(
            bundle_dir=bundle_dir,
            prompt=prompt,
            schema_path=JUDGE_SCHEMA_PATH,
            schema_kind="judge",
            model=model,
            effort=effort,
            timeout=timeout,
        )


def run_quality_eval(
    run_dir: Path,
    *,
    model: str,
    effort: str,
    timeout: int,
    repetitions: int = 1,
) -> dict[str, Any]:
    if repetitions < 1:
        raise StateError("반복 횟수는 1 이상이어야 합니다.")
    manifest = read_json(layout.manifest(run_dir))
    base = {
        "schema_version": SCHEMA_VERSION,
        "run_id": manifest.get("run_id"),
        "evaluated_at": utc_now(),
        "evaluator_git_commit": git_commit(),
        "evaluator_source_hash": ensemble_source_hash(),
        "judge_provider": "agy",
        "judge_model": model,
        "judge_cli_version": None,
        "judge_prompt_sha256": _file_sha256(JUDGE_PROMPT_PATH),
        "judge_schema_sha256": _file_sha256(JUDGE_SCHEMA_PATH),
        # escalation이 있었던 실행은 패널과 심판이 같은 모델이라 순환이 있다.
        # 보고할 때 걸러낼 수 있도록 표시만 남긴다.
        "panel_used_in_run": int(manifest.get("panel_call_count", 0)) > 0,
        "calls": [],
        "composite": {},
        "usage_total": None,
    }

    draft_path = layout.draft(run_dir, 0)
    final_path = select_final_draft(run_dir, manifest)
    if not layout.final(run_dir).exists():
        return {**base, "verdict": "SKIP", "reason": "실행이 끝나지 않아 final.md가 없습니다."}
    if not draft_path.exists():
        return {**base, "verdict": "SKIP", "reason": "첫 초안(draft-00.md)이 없습니다."}
    if final_path is None:
        return {**base, "verdict": "SKIP", "reason": "비교할 마지막 초안이 없습니다."}

    draft_text = draft_path.read_text(encoding="utf-8")
    final_text = final_path.read_text(encoding="utf-8")
    document_info = {
        "draft_doc": draft_path.relative_to(run_dir).as_posix(),
        "final_doc": final_path.relative_to(run_dir).as_posix(),
        "draft_sha256": sha256_text(draft_text),
        "final_sha256": sha256_text(final_text),
    }
    if document_info["draft_sha256"] == document_info["final_sha256"]:
        # 초안이 하나뿐이거나 수정 없이 승인된 실행. 비교할 것이 없으므로
        # 심판을 부르지 않는다.
        result = {
            **base,
            **document_info,
            "content_identical": True,
            "verdict": "IDENTICAL",
            "reason": "첫 초안과 마지막 초안의 내용이 같습니다.",
        }
        _write_judgment(run_dir, result)
        return result

    calls: list[dict[str, Any]] = []
    raw_index = _next_raw_index(run_dir)
    cli_version: str | None = None
    usage_holder: dict[str, Any] = {}
    for _ in range(repetitions):
        for order in CALL_ORDERS:
            documents = (
                {"document-1.md": draft_text, "document-2.md": final_text}
                if order == "DRAFT_FIRST"
                else {"document-1.md": final_text, "document-2.md": draft_text}
            )
            try:
                provider_result = _judge_once(
                    run_dir, documents=documents, model=model, effort=effort, timeout=timeout
                )
            except InfraError as exc:
                details = exc.details if isinstance(exc.details, dict) else {}
                attempts = int(details.get("attempts") or 1)
                failure_path = layout.judge_failure(run_dir, raw_index)
                atomic_write_json(
                    failure_path,
                    {
                        "recorded_at": utc_now(),
                        "provider": details.get("provider", "agy"),
                        "error": exc.message,
                        "details": details,
                    },
                    overwrite=False,
                )
                accumulate_usage(
                    usage_holder,
                    "agy",
                    None,
                    attempts=attempts,
                    attempts_reported=0,
                )
                result = {
                    **base,
                    **document_info,
                    "content_identical": False,
                    "verdict": "INFRA_ERROR",
                    "reason": exc.message,
                    "calls": calls,
                    "failure_record": failure_path.relative_to(run_dir).as_posix(),
                    "usage_total": usage_holder.get("usage", {}).get("agy"),
                }
                _write_judgment(run_dir, result)
                return result
            cli_version = provider_result.version
            raw_path = layout.judge_raw(run_dir, raw_index)
            atomic_write_json(raw_path, provider_result.payload, overwrite=False)
            accumulate_usage(
                usage_holder,
                "agy",
                provider_result.usage,
                attempts=provider_result.attempts,
                attempts_reported=provider_result.attempts_reported,
            )
            calls.append(
                {
                    "order": order,
                    "raw_path": raw_path.relative_to(run_dir).as_posix(),
                    "verdicts": {
                        axis: map_verdict(order, provider_result.payload[axis]["winner"])
                        for axis in JUDGE_AXES
                    },
                    "reasons": {
                        axis: provider_result.payload[axis]["reason"] for axis in JUDGE_AXES
                    },
                    "usage": provider_result.usage,
                }
            )
            raw_index += 1

    composite: dict[str, str] = {}
    distribution: dict[str, dict[str, int]] = {}
    for axis in JUDGE_AXES:
        pair_results = [
            compose_pair(calls[index]["verdicts"][axis], calls[index + 1]["verdicts"][axis])
            for index in range(0, len(calls), 2)
        ]
        composite[axis] = compose_repetitions(pair_results)
        distribution[axis] = dict(sorted(Counter(pair_results).items()))

    result = {
        **base,
        **document_info,
        "judge_cli_version": cli_version,
        "content_identical": False,
        "verdict": "JUDGED",
        "repetitions": repetitions,
        "calls": calls,
        "composite": composite,
        "composite_distribution": distribution,
        "usage_total": usage_holder.get("usage", {}).get("agy"),
    }
    _write_judgment(run_dir, result)
    return result


def _write_judgment(run_dir: Path, result: dict[str, Any]) -> None:
    atomic_write_json(layout.quality_judgment(run_dir), result)


def judge_expectations(
    run_dir: Path,
    *,
    document_path: Path,
    expectations: dict[str, Any],
    model: str,
    effort: str,
    timeout: int,
) -> tuple[dict[str, Any], ProviderResult]:
    """3층 품질 케이스의 정답지 대비 절대 판정.

    비교가 아니라 절대 채점이므로 2층의 비교 스키마와 분리한다. 정답지를
    비교 입력에 섞으면 블라인드가 깨지기 때문이다.
    """
    prompt = JUDGE_EXPECTATIONS_PROMPT_PATH.read_text(encoding="utf-8")
    documents = {"document.md": document_path.read_text(encoding="utf-8")}
    with isolated_bundle(
        run_dir,
        mode="judge-expectations",
        inline_documents=documents,
        expectations=expectations,
    ) as bundle_dir:
        result = run_agy(
            bundle_dir=bundle_dir,
            prompt=prompt,
            schema_path=JUDGE_EXPECTATIONS_SCHEMA_PATH,
            schema_kind="judge-expectations",
            model=model,
            effort=effort,
            timeout=timeout,
        )
    raw_path = layout.judge_raw(run_dir, _next_raw_index(run_dir))
    atomic_write_json(raw_path, result.payload, overwrite=False)
    return result.payload, result

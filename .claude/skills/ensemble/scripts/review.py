#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ensemble_core.config import (
    DEFAULT_MAX_FINAL_BLIND_ATTEMPTS,
    DEFAULT_MAX_PANEL_CALLS,
    DEFAULT_MAX_ROUNDS,
    DEFAULT_MAX_TOTAL_PROVIDER_CALLS,
    DEFAULT_PANEL_EFFORT,
    DEFAULT_PANEL_MODEL,
    DEFAULT_REVIEW_EFFORT,
    DEFAULT_REVIEW_MODEL,
    DEFAULT_TIMEOUT_SECONDS,
)
from ensemble_core.audit import apply_audit, run_issue_audit
from ensemble_core.claude_usage import record_claude_usage
from ensemble_core.convergence import refresh_author_dispositions, reproducibility_metrics
from ensemble_core.errors import EnsembleError, InfraError, InputError, SchemaError, StateError
from ensemble_core.eval_bench import (
    collect,
    compare_scorecards,
    plan,
    validate_benchmark_block,
    write_scorecard,
)
from ensemble_core.eval_process import compare_runs, evaluate_run
from ensemble_core.eval_quality import run_quality_eval
from ensemble_core.history import write_timeline
from ensemble_core import layout
from ensemble_core.io_utils import (
    parse_answer_section,
    read_json,
    resolve_run,
    safe_source_file,
    utc_now,
)
from ensemble_core.isolated import run_final_blind, save_final_assessment
from ensemble_core.noise import measure_noise
from ensemble_core.panel import run_panel
from ensemble_core.providers import preflight
from ensemble_core.registry import accept_risk, load_registry, record_author_decision
from ensemble_core.report import finalize
from ensemble_core.state_machine import (
    assert_run_can_advance,
    assert_accept_risk_ready,
    assert_final_blind_budget,
    assert_final_blind_ready,
    assert_iterative_review_budget,
    assert_provider_call_budget,
    assert_source_unchanged,
    initialize_run,
    mark_terminal,
    record_provider_call,
    record_provider_failure,
    record_retry_event,
    record_repair_plan,
    resolve_user_decision,
    transition_state,
)
from ensemble_core.workflow import (
    current_draft,
    ingest_review,
    promote_final_findings,
    run_proposal,
    run_review,
    save_artifact,
    save_proposal,
)


# 평가 명령은 실행 상태를 바꾸지 않는다. 실행 산출물을 쓰지 않고, 평가 중
# 오류가 나도 대상 실행을 INFRA_ERROR 등으로 종료 처리하지 않는다.
EVAL_COMMANDS = {"eval-run", "eval-quality", "eval-bench", "eval-compare"}

EXIT_CODES = {
    "INPUT_ERROR": 2,
    "SCHEMA_ERROR": 3,
    "SEMANTIC_VALIDATION_ERROR": 4,
    "INFRA_ERROR": 5,
    "STATE_ERROR": 6,
    "SECURITY_ERROR": 7,
}


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def enforce_run_budget(run_dir: Path, check: Any, **kwargs: Any) -> dict[str, Any]:
    """한도 초과를 단순 명령 오류가 아니라 재현 가능한 종료 상태로 기록한다."""
    try:
        return check(run_dir, **kwargs)
    except StateError as exc:
        if isinstance(exc.details, dict) and exc.details.get("limit_kind"):
            mark_terminal(
                run_dir,
                "ITERATION_LIMIT_REACHED",
                f"{exc.details['limit_kind']} limit reached",
            )
        raise


def load_payload(path: str) -> dict[str, Any]:
    value = read_json(safe_source_file(path))
    if not isinstance(value, dict):
        raise InputError("JSON input must be an object")
    return value


def panel_model_record(manifest: dict[str, Any]) -> dict[str, Any]:
    models = manifest.setdefault("models", {})
    record = models.get("agy") or models.get("gemini")
    if not isinstance(record, dict):
        raise InputError("Agy panel model configuration is missing")
    return record


def manifest_models(run_dir: Path, args: argparse.Namespace) -> tuple[str, str]:
    manifest = read_json(layout.manifest(run_dir))
    review_model = getattr(args, "model", None) or manifest["models"]["codex"]["requested"]
    panel_model = getattr(args, "panel_model", None) or panel_model_record(manifest)["requested"]
    return review_model, panel_model


def manifest_panel_effort(run_dir: Path, args: argparse.Namespace) -> str:
    manifest = read_json(layout.manifest(run_dir))
    return (
        getattr(args, "panel_effort", None)
        or panel_model_record(manifest).get("requested_reasoning_effort")
        or DEFAULT_PANEL_EFFORT
    )


def command_init(args: argparse.Namespace) -> dict[str, Any]:
    positional = " ".join(args.request).strip()
    sources = sum(bool(value) for value in (args.from_path, args.request_file, positional, args.stdin))
    if sources == 0:
        raise InputError("무엇을 만들 건가요?", details={"needs_user_input": True})
    if sources > 1:
        raise InputError("--from, --request-file, --stdin, positional 요청은 동시에 사용할 수 없습니다.")
    if args.from_path:
        source = safe_source_file(args.from_path)
        request = parse_answer_section(source.read_text(encoding="utf-8"))
    elif args.request_file:
        request = safe_source_file(args.request_file).read_text(encoding="utf-8")
    elif args.stdin:
        request = sys.stdin.read()
    else:
        request = positional
    benchmark = validate_benchmark_block(load_payload(args.benchmark_file)) if args.benchmark_file else None
    run_dir = initialize_run(
        request,
        phase=args.phase,
        review_model=args.model,
        panel_model=args.panel_model,
        panel_effort=args.panel_effort,
        max_rounds=args.max_rounds,
        max_final_blind_attempts=args.max_final_blind_attempts,
        max_total_provider_calls=args.max_total_provider_calls,
        max_panel_calls=args.max_panel_calls,
        reset_review_session_after_promotion=not args.keep_review_session_after_promotion,
        allow_reuse=args.allow_reuse,
        allow_sensitive=args.allow_sensitive,
        label=args.label,
        author_model=args.author_model,
        benchmark=benchmark,
    )
    manifest = read_json(layout.manifest(run_dir))
    environment = manifest.get("environment", {})
    for provider in ("codex", "agy"):
        info = environment.get(provider, {})
        manifest["models"][provider]["cli_version"] = info.get("version")
        manifest["models"][provider]["command_path"] = info.get("path")
    if manifest["models"]["agy"]["cli_version"] is None:
        manifest["warnings"].append(
            "Antigravity CLI (agy) unavailable; panel escalation will require user decision"
        )
    from ensemble_core.io_utils import atomic_write_json

    atomic_write_json(layout.manifest(run_dir), manifest)
    return {
        "run_id": manifest["run_id"],
        "run_dir": str(run_dir),
        "request": str(layout.request(run_dir)),
        "rubric": str(layout.rubric(run_dir)),
        "notice": "request.md, rubric.md, draft는 OpenAI에 전송될 수 있고 패널 사용 시 쟁점이 Google에 전송될 수 있습니다.",
        "warnings": manifest["warnings"],
    }


def command_preflight(args: argparse.Namespace) -> dict[str, Any]:
    if args.run:
        run_dir = resolve_run(args.run)
        review_model, panel_model = manifest_models(run_dir, args)
        panel_effort = manifest_panel_effort(run_dir, args)
    else:
        run_dir = None
        review_model = args.model or DEFAULT_REVIEW_MODEL
        panel_model = args.panel_model or DEFAULT_PANEL_MODEL
        panel_effort = args.panel_effort or DEFAULT_PANEL_EFFORT
    result = preflight(
        live_codex=args.live,
        live_agy=args.live_agy,
        model=review_model,
        panel_model=panel_model,
        timeout=args.timeout,
        effort=DEFAULT_REVIEW_EFFORT,
        panel_effort=panel_effort,
    )
    if run_dir:
        manifest = read_json(layout.manifest(run_dir))
        manifest["models"]["codex"]["cli_version"] = result["codex"]["version"]
        manifest["models"]["codex"]["command_path"] = result["codex"]["path"]
        manifest["models"]["codex"]["requested_reasoning_effort"] = result["codex"]["reasoning_effort"]
        agy_model = manifest["models"].setdefault("agy", dict(panel_model_record(manifest)))
        agy_model["cli_version"] = result["agy"]["version"]
        agy_model["command_path"] = result["agy"]["path"]
        agy_model["requested_reasoning_effort"] = panel_effort
        from ensemble_core.io_utils import atomic_write_json

        atomic_write_json(layout.manifest(run_dir), manifest)
    return result


def command_save(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    assert_run_can_advance(run_dir, "save")
    return save_artifact(
        run_dir,
        kind=args.kind,
        source=safe_source_file(args.source),
        round_number=args.round,
    )


def command_propose(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    assert_run_can_advance(run_dir, "propose")
    assert_source_unchanged(run_dir)
    if args.input:
        path = save_proposal(run_dir, load_payload(args.input))
        return {"proposal": str(path), "source": "ingested"}
    enforce_run_budget(run_dir, assert_provider_call_budget)
    review_model, _ = manifest_models(run_dir, args)
    result, path = run_proposal(run_dir, model=review_model, timeout=args.timeout)
    record_provider_call(run_dir, provider="codex", operation="proposal", result=result)
    return {"proposal": str(path), "source": "codex", "attempts": result.attempts}


def command_review(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    assert_run_can_advance(run_dir, "review")
    assert_source_unchanged(run_dir)
    manifest = read_json(layout.manifest(run_dir))
    if manifest.get("phase") == "1A" and args.round != 1:
        raise InputError("Phase 1A supports exactly one normal review round")
    enforce_run_budget(run_dir, assert_iterative_review_budget)
    if args.input:
        return ingest_review(
            run_dir,
            review=load_payload(args.input),
            review_round=args.round,
            draft_round=args.draft_round,
            allow_rebuttal_similarity=args.allow_rebuttal_similarity,
        )
    review_model, _ = manifest_models(run_dir, args)
    result, applied = run_review(
        run_dir,
        review_round=args.round,
        draft_round=args.draft_round,
        model=review_model,
        timeout=args.timeout,
    )
    applied["provider_attempts"] = result.attempts
    return applied


def command_decision(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    payload = load_payload(args.input)
    required = {
        "issue_id",
        "round",
        "disposition",
        "author_severity",
        "claim",
        "evidence_ref",
        "requested_disposition",
        "argument",
        "action",
    }
    if set(payload) != required:
        raise InputError(
            "Decision JSON fields are invalid",
            details={"missing": sorted(required - set(payload)), "unknown": sorted(set(payload) - required)},
        )
    record_author_decision(
        run_dir,
        issue_id=payload["issue_id"],
        round_number=int(payload["round"]),
        disposition=payload["disposition"],
        author_severity=int(payload["author_severity"]),
        claim=payload["claim"],
        evidence_ref=payload["evidence_ref"],
        requested_disposition=payload["requested_disposition"],
        argument=payload["argument"],
        action=payload["action"],
    )
    refresh_author_dispositions(run_dir, int(payload["round"]))
    if payload["disposition"] == "DEFER":
        manifest = read_json(layout.manifest(run_dir))
        transition_state(
            manifest,
            "USER_DECISION_REQUIRED",
            reason=f"{payload['issue_id']} deferred to user",
        )
        pending = set(manifest.get("pending_user_issue_ids", []))
        pending.add(payload["issue_id"])
        manifest["pending_user_issue_ids"] = sorted(pending)
        from ensemble_core.io_utils import atomic_write_json

        atomic_write_json(layout.manifest(run_dir), manifest)
    manifest = read_json(layout.manifest(run_dir))
    return {
        "recorded": payload["issue_id"],
        "disposition": payload["disposition"],
        "repair_plan_required_issue_ids": manifest.get(
            "repair_plan_required_issue_ids", []
        ),
    }


def command_repair_plan(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    assert_run_can_advance(run_dir, "repair-plan")
    assert_source_unchanged(run_dir)
    payload = load_payload(args.input)
    return record_repair_plan(
        run_dir,
        issue_id=args.issue,
        round_number=args.round,
        plan=payload,
    )


def command_accept_risk(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    manifest = assert_accept_risk_ready(run_dir, args.issue)
    pending = set(str(value) for value in manifest.get("pending_user_issue_ids", []))
    note = safe_source_file(args.note_file).read_text(encoding="utf-8").strip()
    if not note:
        raise InputError("Acceptance note cannot be empty")
    issue = accept_risk(
        run_dir,
        issue_id=args.issue,
        note=note,
        round_number=args.round,
    )
    pending.discard(args.issue)
    manifest["pending_user_issue_ids"] = sorted(pending)
    manifest.setdefault("user_decisions", []).append(
        {
            "recorded_at": utc_now(),
            "from_state": "USER_DECISION_REQUIRED",
            "action": "ACCEPT_RISK",
            "note": note,
            "pending_issue_ids": [args.issue],
            "authoritative_decision_ids": [],
        }
    )
    if not pending and manifest.get("state") == "USER_DECISION_REQUIRED":
        transition_state(manifest, "DRAFT_READY", reason="accepted risk resolved pending decision")
    from ensemble_core.io_utils import atomic_write_json

    atomic_write_json(layout.manifest(run_dir), manifest)
    return {"issue_id": args.issue, "status": issue["status"], "gating": issue["gating"]}


def command_final_blind(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    assert_run_can_advance(run_dir, "final-blind")
    assert_final_blind_ready(run_dir)
    assert_source_unchanged(run_dir)
    enforce_run_budget(run_dir, assert_final_blind_budget)
    _, draft_path = current_draft(run_dir)
    if args.input:
        reconciliation = save_final_assessment(
            run_dir, draft_path=draft_path, raw_review=load_payload(args.input)
        )
        reconciliation["source"] = "ingested"
        return reconciliation
    enforce_run_budget(run_dir, assert_provider_call_budget)
    review_model, _ = manifest_models(run_dir, args)
    result, reconciliation = run_final_blind(
        run_dir,
        draft_path=draft_path,
        model=review_model,
        timeout=args.timeout,
    )
    reconciliation["provider_attempts"] = result.attempts
    reconciliation["source"] = "codex"
    return reconciliation


def command_promote_final(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    assert_run_can_advance(run_dir, "promote-final")
    enforce_run_budget(run_dir, assert_iterative_review_budget)
    enforce_run_budget(run_dir, assert_final_blind_budget)
    return promote_final_findings(run_dir)


def command_panel(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    manifest = read_json(layout.manifest(run_dir))
    if manifest.get("phase") != "3":
        raise InputError("Panel evaluation is available only in phase 3")
    if manifest.get("state") != "ESCALATION_REQUIRED":
        raise InputError("Panel evaluation requires ESCALATION_REQUIRED state")
    assert_source_unchanged(run_dir)
    enforce_run_budget(run_dir, assert_provider_call_budget, needed=4)
    review_model, panel_model = manifest_models(run_dir, args)
    panel_effort = manifest_panel_effort(run_dir, args)
    return run_panel(
        run_dir,
        issue_id=args.issue,
        review_model=review_model,
        panel_model=panel_model,
        panel_effort=panel_effort,
        timeout=args.timeout,
    )


def command_finalize(args: argparse.Namespace) -> dict[str, Any]:
    return finalize(resolve_run(args.run), status=args.status)


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    manifest = read_json(layout.manifest(run_dir))
    registry = load_registry(run_dir)
    convergence = read_json(layout.convergence(run_dir))
    return {
        "run_dir": str(run_dir),
        "state": manifest["state"],
        "current_round": manifest["current_round"],
        "last_review_verdict": manifest.get("last_review_verdict"),
        "open_issues": {
            issue_id: {"status": issue.get("status"), "gating": issue.get("gating")}
            for issue_id, issue in registry.items()
            if issue.get("status") in {"OPEN", "UNVERIFIED", "BILATERAL_DEADLOCK", "PANEL_DISSENT"}
        },
        "accepted_risks": [issue_id for issue_id, issue in registry.items() if issue.get("status") == "ACCEPTED_RISK"],
        "issue_set_stalled_rounds": [record["round"] for record in convergence["rounds"] if record.get("issue_set_stalled")],
        "warnings": manifest.get("warnings", []),
        "escalation_signals": manifest.get("escalation_signals", []),
        "pending_user_issue_ids": manifest.get("pending_user_issue_ids", []),
        "pending_panel_issue_ids": manifest.get("pending_panel_issue_ids", []),
        "repair_plan_required_issue_ids": manifest.get(
            "repair_plan_required_issue_ids", []
        ),
        "timeline": str(layout.timeline(run_dir)),
    }


def command_timeline(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    return {"timeline": str(write_timeline(run_dir))}


def command_collect_claude_usage(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    usage = record_claude_usage(run_dir)
    return {"run_dir": str(run_dir), "usage": usage}


def command_resolve_user_decision(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    if args.decision_file:
        if args.action or args.note_file:
            raise InputError("--decision-file은 --action/--note-file과 함께 사용할 수 없습니다.")
        payload = load_payload(args.decision_file)
        required = {"action", "audit_note", "authoritative_decisions"}
        if set(payload) != required:
            raise InputError(
                "사용자 결정 JSON 필드가 올바르지 않습니다.",
                details={
                    "missing": sorted(required - set(payload)),
                    "unknown": sorted(set(payload) - required),
                },
            )
        action = payload["action"]
        note = payload["audit_note"]
        authoritative = payload["authoritative_decisions"]
        if action not in {"REVISE", "CONTINUE"}:
            raise InputError("사용자 결정 action은 REVISE 또는 CONTINUE여야 합니다.")
        if not isinstance(note, str) or not note.strip():
            raise InputError("사용자 결정 audit_note가 비어 있습니다.")
        if not isinstance(authoritative, list):
            raise InputError("authoritative_decisions는 배열이어야 합니다.")
        return resolve_user_decision(
            run_dir,
            action=action,
            note=note,
            authoritative_decisions=authoritative,
        )
    if not args.action or not args.note_file:
        raise InputError(
            "레거시 방식은 --action과 --note-file을 함께 지정해야 합니다. "
            "권위 결정을 전달하려면 --decision-file을 사용하세요."
        )
    note = safe_source_file(args.note_file).read_text(encoding="utf-8").strip()
    return resolve_user_decision(run_dir, action=args.action, note=note)


def command_fixture_metrics(args: argparse.Namespace) -> dict[str, Any]:
    payload = read_json(safe_source_file(args.input))
    if not isinstance(payload, list):
        raise InputError("Fixture metrics input must be a JSON array")
    return reproducibility_metrics(payload)


def command_measure_noise(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    assert_source_unchanged(run_dir)
    enforce_run_budget(
        run_dir,
        assert_provider_call_budget,
        needed=args.repetitions,
    )
    review_model, _ = manifest_models(run_dir, args)
    return measure_noise(
        run_dir,
        repetitions=args.repetitions,
        model=review_model,
        timeout=args.timeout,
    )


def command_issue_audit(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    if read_json(layout.manifest(run_dir)).get("phase") != "3":
        raise InputError("ISSUE_AUDIT is available only in phase 3")
    assert_source_unchanged(run_dir)
    if args.input:
        return apply_audit(run_dir, round_number=args.round, payload=load_payload(args.input))
    enforce_run_budget(run_dir, assert_provider_call_budget)
    review_model, _ = manifest_models(run_dir, args)
    result, applied = run_issue_audit(
        run_dir,
        round_number=args.round,
        model=review_model,
        timeout=args.timeout,
    )
    applied["provider_attempts"] = result.attempts
    return applied


def command_eval_run(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    if args.compare:
        return compare_runs([run_dir, *(resolve_run(value) for value in args.compare)])
    result = evaluate_run(run_dir)
    if getattr(args, "raw", False):
        return result
    return {
        "run_id": result.get("run_id"),
        "evaluated_at": result.get("evaluated_at"),
        "summary": result.get("display"),
        "artifacts": {
            "summary": str(layout.process_summary(run_dir)),
            "metrics": str(layout.process_metrics(run_dir)),
        },
        "warnings": result.get("warnings", []),
    }


def command_eval_quality(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    return run_quality_eval(
        run_dir,
        model=args.model or DEFAULT_PANEL_MODEL,
        effort=args.effort or DEFAULT_PANEL_EFFORT,
        timeout=args.timeout,
        repetitions=args.repetitions,
    )


def command_eval_bench(args: argparse.Namespace) -> dict[str, Any]:
    if not args.collect:
        return plan(args.suite, case_id=args.case, repeat=args.repeat)
    if not args.benchmark_run_id:
        raise InputError("--collect에는 --benchmark-run-id가 필요합니다.")
    scorecard = collect(
        suite=args.suite,
        benchmark_run_id=args.benchmark_run_id,
        case_id=args.case,
        judge_model=args.judge_model or DEFAULT_PANEL_MODEL,
        judge_effort=args.judge_effort or DEFAULT_PANEL_EFFORT,
        timeout=args.timeout,
        skip_expectation_judge=args.skip_expectation_judge,
        force_judge=args.force_judge,
    )
    path = write_scorecard(scorecard)
    return {"scorecard": str(path), **scorecard}


def command_eval_compare(args: argparse.Namespace) -> dict[str, Any]:
    return compare_scorecards(args.base, args.head, allow_model_mismatch=args.allow_model_mismatch)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Claude와 GPT로 구현 명세를 만들고 검토합니다.")
    subparsers = parser.add_subparsers(title="명령", dest="command", required=True)

    init = subparsers.add_parser("init", help="새 실행 시작")
    init.add_argument("request", nargs="*")
    init.add_argument("--from", dest="from_path")
    init.add_argument("--request-file")
    init.add_argument("--stdin", action="store_true")
    init.add_argument("--phase", choices=["1A", "1B", "2", "3"], default="2")
    init.add_argument("--model", default=DEFAULT_REVIEW_MODEL)
    init.add_argument("--panel-model", default=DEFAULT_PANEL_MODEL)
    init.add_argument(
        "--panel-effort",
        choices=["low", "medium", "high"],
        default=DEFAULT_PANEL_EFFORT,
    )
    init.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    init.add_argument(
        "--max-final-blind-attempts",
        type=int,
        default=DEFAULT_MAX_FINAL_BLIND_ATTEMPTS,
    )
    init.add_argument(
        "--max-total-provider-calls",
        type=int,
        default=DEFAULT_MAX_TOTAL_PROVIDER_CALLS,
    )
    init.add_argument("--max-panel-calls", type=int, default=DEFAULT_MAX_PANEL_CALLS)
    init.add_argument(
        "--keep-review-session-after-promotion",
        action="store_true",
        help="최종 독립 검토 이슈 승격 후에도 기존 Codex 검토 세션을 재사용",
    )
    init.add_argument("--allow-reuse", action="store_true")
    init.add_argument("--allow-sensitive", action="store_true")
    init.add_argument("--label", help="사람이 읽는 표식. 실행 식별에는 쓰지 않습니다.")
    init.add_argument("--author-model", help="작성자 모델 선언값. CLI가 검증하지 않습니다.")
    init.add_argument("--benchmark-file", help="벤치마크 실행 식별 블록이 담긴 JSON 파일")
    init.set_defaults(func=command_init)

    check = subparsers.add_parser("preflight", help="실행 환경 확인")
    check.add_argument("--run")
    check.add_argument("--model")
    check.add_argument("--panel-model")
    check.add_argument("--panel-effort", choices=["low", "medium", "high"])
    check.add_argument("--live", action="store_true")
    check.add_argument("--live-agy", action="store_true")
    check.add_argument("--timeout", type=int, default=60)
    check.set_defaults(func=command_preflight)

    save = subparsers.add_parser("save", help="요청, 완료 기준, 제안 또는 초안 저장")
    save.add_argument("--run", required=True)
    save.add_argument("--kind", choices=["request", "rubric", "claude-proposal", "draft"], required=True)
    save.add_argument("--source", required=True)
    save.add_argument("--round", type=int)
    save.set_defaults(func=command_save)

    propose = subparsers.add_parser("propose", help="GPT의 독립 제안 받기")
    propose.add_argument("--run", required=True)
    propose.add_argument("--input")
    propose.add_argument("--model")
    propose.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    propose.set_defaults(func=command_propose)

    review = subparsers.add_parser("review", help="현재 초안 검토")
    review.add_argument("--run", required=True)
    review.add_argument("--round", type=int, required=True)
    review.add_argument("--draft-round", type=int)
    review.add_argument("--input")
    review.add_argument("--model")
    review.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    review.add_argument("--allow-rebuttal-similarity", action="store_true")
    review.set_defaults(func=command_review)

    decision = subparsers.add_parser("decision", help="이슈에 대한 작성자 판단 기록")
    decision.add_argument("--run", required=True)
    decision.add_argument("--input", required=True)
    decision.set_defaults(func=command_decision)

    repair = subparsers.add_parser("repair-plan", help="반복 실패 이슈의 근본 수정 계획 기록")
    repair.add_argument("--run", required=True)
    repair.add_argument("--issue", required=True)
    repair.add_argument("--round", type=int, required=True)
    repair.add_argument("--input", required=True)
    repair.set_defaults(func=command_repair_plan)

    risk = subparsers.add_parser("accept-risk", help="사용자가 수용한 위험 기록")
    risk.add_argument("--run", required=True)
    risk.add_argument("--issue", required=True)
    risk.add_argument("--round", type=int, required=True)
    risk.add_argument("--note-file", required=True)
    risk.set_defaults(func=command_accept_risk)

    resolve = subparsers.add_parser("resolve-user-decision", help="사용자 선택을 기록하고 재개")
    resolve.add_argument("--run", required=True)
    resolve.add_argument("--action", choices=["REVISE", "CONTINUE"])
    resolve.add_argument("--note-file")
    resolve.add_argument(
        "--decision-file",
        help="action, audit_note, authoritative_decisions를 담은 구조화 JSON",
    )
    resolve.set_defaults(func=command_resolve_user_decision)

    final_blind = subparsers.add_parser("final-blind", help="이력을 숨긴 최종 독립 검토")
    final_blind.add_argument("--run", required=True)
    final_blind.add_argument("--input")
    final_blind.add_argument("--model")
    final_blind.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    final_blind.set_defaults(func=command_final_blind)

    promote = subparsers.add_parser("promote-final", help="최종 검토에서 발견한 새 이슈 등록")
    promote.add_argument("--run", required=True)
    promote.set_defaults(func=command_promote_final)

    panel = subparsers.add_parser("panel", help="합의하지 못한 이슈 추가 판단")
    panel.add_argument("--run", required=True)
    panel.add_argument("--issue", required=True)
    panel.add_argument("--model")
    panel.add_argument("--panel-model")
    panel.add_argument("--panel-effort", choices=["low", "medium", "high"])
    panel.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    panel.set_defaults(func=command_panel)

    finish = subparsers.add_parser("finalize", help="최종 문서 생성 및 실행 종료")
    finish.add_argument("--run", required=True)
    finish.add_argument(
        "--status",
        default="auto",
        choices=[
            "auto",
            "CONVERGED",
            "STABLE_DISSENT",
            "USER_DECISION_REQUIRED",
            "CANCELLED",
            "OSCILLATING",
            "PROTOTYPE_INCOMPLETE",
            "ITERATION_LIMIT_REACHED",
            "INFRA_ERROR",
            "RUN_TAINTED",
        ],
    )
    finish.set_defaults(func=command_finalize)

    status = subparsers.add_parser("status", help="현재 상태 확인")
    status.add_argument("--run", required=True)
    status.set_defaults(func=command_status)

    timeline = subparsers.add_parser("timeline", help="작업 기록 갱신")
    timeline.add_argument("--run", required=True)
    timeline.set_defaults(func=command_timeline)

    claude_usage = subparsers.add_parser(
        "collect-claude-usage",
        help="작성자 토큰 사용량을 세션 기록에서 수집 (finalize가 자동 호출)",
    )
    claude_usage.add_argument("--run", required=True)
    claude_usage.set_defaults(func=command_collect_claude_usage)

    metrics = subparsers.add_parser("fixture-metrics", help="고정 예제의 판정 지표 계산")
    metrics.add_argument("--input", required=True)
    metrics.set_defaults(func=command_fixture_metrics)

    noise = subparsers.add_parser("measure-noise", help="반복 검토의 판정 흔들림 측정")
    noise.add_argument("--run", required=True)
    noise.add_argument("--repetitions", type=int, default=3)
    noise.add_argument("--model")
    noise.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    noise.set_defaults(func=command_measure_noise)

    audit = subparsers.add_parser("issue-audit", help="새 이슈의 중복·회귀 여부 확인")
    audit.add_argument("--run", required=True)
    audit.add_argument("--round", type=int, required=True)
    audit.add_argument("--input")
    audit.add_argument("--model")
    audit.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    audit.set_defaults(func=command_issue_audit)

    eval_run = subparsers.add_parser("eval-run", help="완료된 실행의 프로세스 지표 계산 (1층, 비용 없음)")
    eval_run.add_argument("--run", required=True)
    eval_run.add_argument("--compare", nargs="+", metavar="RUN")
    eval_run.add_argument("--raw", action="store_true", help="표시용 요약 대신 전체 원시 지표 출력")
    eval_run.set_defaults(func=command_eval_run)

    eval_quality = subparsers.add_parser(
        "eval-quality", help="첫 초안과 마지막 초안을 심판이 블라인드 비교 (2층)"
    )
    eval_quality.add_argument("--run", required=True)
    eval_quality.add_argument("--repetitions", type=int, default=1)
    eval_quality.add_argument("--model")
    eval_quality.add_argument("--effort", choices=["low", "medium", "high"])
    eval_quality.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    eval_quality.set_defaults(func=command_eval_quality)

    eval_bench = subparsers.add_parser("eval-bench", help="고정 케이스 세트로 코드 버전 평가 (3층)")
    eval_bench.add_argument("--suite", choices=["smoke", "full"], required=True)
    eval_bench.add_argument("--case")
    eval_bench.add_argument("--collect", action="store_true")
    eval_bench.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="작성자 실행 반복 횟수(기본 1; 회귀 신호 재확인 시 늘림)",
    )
    eval_bench.add_argument("--benchmark-run-id")
    eval_bench.add_argument("--judge-model")
    eval_bench.add_argument("--judge-effort", choices=["low", "medium", "high"])
    eval_bench.add_argument("--skip-expectation-judge", action="store_true")
    eval_bench.add_argument(
        "--force-judge",
        action="store_true",
        help="상태 사전 채점 실패 또는 오염 실행이어도 심판 호출",
    )
    eval_bench.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    eval_bench.set_defaults(func=command_eval_bench)

    eval_compare = subparsers.add_parser("eval-compare", help="두 커밋의 점수표 비교")
    eval_compare.add_argument("--base", required=True)
    eval_compare.add_argument("--head", required=True)
    eval_compare.add_argument("--allow-model-mismatch", action="store_true")
    eval_compare.set_defaults(func=command_eval_compare)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = args.func(args)
        run_value = getattr(args, "run", None)
        if args.command == "init":
            write_timeline(Path(result["run_dir"]))
        elif run_value and args.command not in {"status", "timeline", "fixture-metrics"} | EVAL_COMMANDS:
            write_timeline(resolve_run(run_value))
        emit(result)
        return 0
    except EnsembleError as exc:
        run_value = getattr(args, "run", None)
        if run_value and args.command not in EVAL_COMMANDS:
            try:
                run_dir = resolve_run(run_value)
                manifest = read_json(layout.manifest(run_dir))
                if isinstance(exc.details, dict) and isinstance(exc.details.get("attempts"), int):
                    retry_key = "schema" if isinstance(exc, SchemaError) else "infra"
                    retry_count = max(int(exc.details["attempts"]) - 1, 0)
                    manifest["retries"][retry_key] += retry_count
                    from ensemble_core.io_utils import atomic_write_json

                    atomic_write_json(layout.manifest(run_dir), manifest)
                    record_retry_event(
                        run_dir,
                        retry_type=retry_key,
                        operation=str(getattr(args, "command", "unknown")),
                        round_number=getattr(args, "round", None),
                        attempt=int(exc.details["attempts"]),
                        error=exc.message,
                    )
                    record_provider_failure(
                        run_dir,
                        operation=str(getattr(args, "command", "unknown")),
                        round_number=getattr(args, "round", None),
                        details=exc.details,
                        error=exc.message,
                    )
                if isinstance(exc, InfraError):
                    current = read_json(layout.manifest(run_dir))
                    if current.get("state") != "USER_DECISION_REQUIRED":
                        mark_terminal(run_dir, "INFRA_ERROR", exc.message)
                write_timeline(run_dir)
            except EnsembleError:
                pass
        emit(exc.as_dict())
        return EXIT_CODES.get(exc.code, 1)


if __name__ == "__main__":
    raise SystemExit(main())

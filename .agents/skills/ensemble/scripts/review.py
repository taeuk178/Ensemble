#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ensemble_core.config import (
    DEFAULT_MAX_PANEL_CALLS,
    DEFAULT_MAX_ROUNDS,
    DEFAULT_PANEL_MODEL,
    DEFAULT_REVIEW_EFFORT,
    DEFAULT_REVIEW_MODEL,
    DEFAULT_TIMEOUT_SECONDS,
)
from ensemble_core.audit import apply_audit, run_issue_audit
from ensemble_core.convergence import refresh_author_dispositions, reproducibility_metrics
from ensemble_core.errors import EnsembleError, InfraError, InputError, SchemaError
from ensemble_core.history import write_timeline
from ensemble_core.io_utils import (
    detect_sensitive_text,
    parse_answer_section,
    read_json,
    resolve_run,
    safe_source_file,
)
from ensemble_core.isolated import run_final_blind, save_final_assessment
from ensemble_core.noise import measure_noise
from ensemble_core.panel import run_panel
from ensemble_core.providers import preflight
from ensemble_core.registry import accept_risk, load_registry, record_author_decision
from ensemble_core.report import finalize
from ensemble_core.state_machine import (
    assert_run_can_advance,
    assert_source_unchanged,
    initialize_run,
    mark_terminal,
    record_provider_call,
    record_provider_failure,
    record_retry_event,
    resolve_user_decision,
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


def load_payload(path: str) -> dict[str, Any]:
    value = read_json(safe_source_file(path))
    if not isinstance(value, dict):
        raise InputError("JSON input must be an object")
    return value


def manifest_models(run_dir: Path, args: argparse.Namespace) -> tuple[str, str]:
    manifest = read_json(run_dir / "manifest.json")
    review_model = getattr(args, "model", None) or manifest["models"]["codex"]["requested"]
    panel_model = getattr(args, "panel_model", None) or manifest["models"]["gemini"]["requested"]
    return review_model, panel_model


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
    sensitive = detect_sensitive_text(request)
    if sensitive and not args.allow_sensitive:
        raise InputError(
            "외부 모델에 보내면 안 될 수 있는 민감정보 패턴이 감지됐습니다.",
            details={"patterns": sensitive, "override": "--allow-sensitive"},
        )
    run_dir = initialize_run(
        request,
        phase=args.phase,
        review_model=args.model,
        panel_model=args.panel_model,
        max_rounds=args.max_rounds,
        max_panel_calls=args.max_panel_calls,
        allow_reuse=args.allow_reuse,
    )
    manifest = read_json(run_dir / "manifest.json")
    environment = manifest.get("environment", {})
    for provider in ("codex", "gemini"):
        info = environment.get(provider, {})
        manifest["models"][provider]["cli_version"] = info.get("version")
        manifest["models"][provider]["command_path"] = info.get("path")
    if manifest["models"]["gemini"]["cli_version"] is None:
        manifest["warnings"].append(
            "Gemini CLI unavailable; panel escalation will require user decision"
        )
    from ensemble_core.io_utils import atomic_write_json

    atomic_write_json(run_dir / "manifest.json", manifest)
    return {
        "run_id": manifest["run_id"],
        "run_dir": str(run_dir),
        "request": str(run_dir / "request.md"),
        "rubric": str(run_dir / "rubric.md"),
        "notice": "request.md, rubric.md, draft는 OpenAI에 전송될 수 있고 패널 사용 시 쟁점이 Google에 전송될 수 있습니다.",
        "warnings": manifest["warnings"],
    }


def command_preflight(args: argparse.Namespace) -> dict[str, Any]:
    if args.run:
        run_dir = resolve_run(args.run)
        review_model, _ = manifest_models(run_dir, args)
    else:
        run_dir = None
        review_model = args.model or DEFAULT_REVIEW_MODEL
    result = preflight(
        live_codex=args.live,
        model=review_model,
        timeout=args.timeout,
        effort=DEFAULT_REVIEW_EFFORT,
    )
    if run_dir:
        manifest = read_json(run_dir / "manifest.json")
        manifest["models"]["codex"]["cli_version"] = result["codex"]["version"]
        manifest["models"]["codex"]["command_path"] = result["codex"]["path"]
        manifest["models"]["codex"]["requested_reasoning_effort"] = result["codex"]["reasoning_effort"]
        manifest["models"]["gemini"]["cli_version"] = result["gemini"]["version"]
        manifest["models"]["gemini"]["command_path"] = result["gemini"]["path"]
        from ensemble_core.io_utils import atomic_write_json

        atomic_write_json(run_dir / "manifest.json", manifest)
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
    review_model, _ = manifest_models(run_dir, args)
    result, path = run_proposal(run_dir, model=review_model, timeout=args.timeout)
    record_provider_call(run_dir, provider="codex", operation="proposal", result=result)
    return {"proposal": str(path), "source": "codex", "attempts": result.attempts}


def command_review(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    assert_run_can_advance(run_dir, "review")
    assert_source_unchanged(run_dir)
    manifest = read_json(run_dir / "manifest.json")
    if manifest.get("phase") == "1A" and args.round != 1:
        raise InputError("Phase 1A supports exactly one normal review round")
    if args.round > int(manifest["limits"]["review_rounds"]):
        mark_terminal(run_dir, "ITERATION_LIMIT_REACHED", "Review round limit reached")
        raise InputError("Review round exceeds configured limit")
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
        manifest = read_json(run_dir / "manifest.json")
        manifest["state"] = "USER_DECISION_REQUIRED"
        pending = set(manifest.get("pending_user_issue_ids", []))
        pending.add(payload["issue_id"])
        manifest["pending_user_issue_ids"] = sorted(pending)
        from ensemble_core.io_utils import atomic_write_json

        atomic_write_json(run_dir / "manifest.json", manifest)
    return {"recorded": payload["issue_id"], "disposition": payload["disposition"]}


def command_accept_risk(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    note = safe_source_file(args.note_file).read_text(encoding="utf-8").strip()
    if not note:
        raise InputError("Acceptance note cannot be empty")
    issue = accept_risk(
        run_dir,
        issue_id=args.issue,
        note=note,
        round_number=args.round,
    )
    manifest = read_json(run_dir / "manifest.json")
    pending = set(manifest.get("pending_user_issue_ids", []))
    pending.discard(args.issue)
    manifest["pending_user_issue_ids"] = sorted(pending)
    if not pending and manifest.get("state") == "USER_DECISION_REQUIRED":
        manifest["state"] = "DRAFT_READY"
    from ensemble_core.io_utils import atomic_write_json

    atomic_write_json(run_dir / "manifest.json", manifest)
    return {"issue_id": args.issue, "status": issue["status"], "gating": issue["gating"]}


def command_final_blind(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    assert_run_can_advance(run_dir, "final-blind")
    assert_source_unchanged(run_dir)
    _, draft_path = current_draft(run_dir)
    if args.input:
        reconciliation = save_final_assessment(
            run_dir, draft_path=draft_path, raw_review=load_payload(args.input)
        )
        reconciliation["source"] = "ingested"
        return reconciliation
    review_model, _ = manifest_models(run_dir, args)
    result, reconciliation = run_final_blind(
        run_dir,
        draft_path=draft_path,
        model=review_model,
        timeout=args.timeout,
    )
    record_provider_call(
        run_dir,
        provider="codex",
        operation="final-blind",
        round_number=int(read_json(run_dir / "manifest.json").get("current_round", 0)),
        result=result,
    )
    reconciliation["provider_attempts"] = result.attempts
    reconciliation["source"] = "codex"
    return reconciliation


def command_promote_final(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    assert_run_can_advance(run_dir, "promote-final")
    return promote_final_findings(run_dir)


def command_panel(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    manifest = read_json(run_dir / "manifest.json")
    if manifest.get("phase") != "3":
        raise InputError("Panel evaluation is available only in phase 3")
    if manifest.get("state") != "ESCALATION_REQUIRED":
        raise InputError("Panel evaluation requires ESCALATION_REQUIRED state")
    assert_source_unchanged(run_dir)
    review_model, panel_model = manifest_models(run_dir, args)
    return run_panel(
        run_dir,
        issue_id=args.issue,
        review_model=review_model,
        panel_model=panel_model,
        timeout=args.timeout,
    )


def command_finalize(args: argparse.Namespace) -> dict[str, Any]:
    return finalize(resolve_run(args.run), status=args.status)


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    manifest = read_json(run_dir / "manifest.json")
    registry = load_registry(run_dir)
    convergence = read_json(run_dir / "convergence.json")
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
        "pending_panel_issue_ids": manifest.get("pending_panel_issue_ids", []),
        "timeline": str(run_dir / "timeline.md"),
    }


def command_timeline(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    return {"timeline": str(write_timeline(run_dir))}


def command_resolve_user_decision(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
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
    review_model, _ = manifest_models(run_dir, args)
    return measure_noise(
        run_dir,
        repetitions=args.repetitions,
        model=review_model,
        timeout=args.timeout,
    )


def command_issue_audit(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = resolve_run(args.run)
    if read_json(run_dir / "manifest.json").get("phase") != "3":
        raise InputError("ISSUE_AUDIT is available only in phase 3")
    assert_source_unchanged(run_dir)
    if args.input:
        return apply_audit(run_dir, round_number=args.round, payload=load_payload(args.input))
    review_model, _ = manifest_models(run_dir, args)
    result, applied = run_issue_audit(
        run_dir,
        round_number=args.round,
        model=review_model,
        timeout=args.timeout,
    )
    record_provider_call(
        run_dir,
        provider="codex",
        operation="issue-audit",
        round_number=args.round,
        result=result,
    )
    applied["provider_attempts"] = result.attempts
    return applied


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="작성자 모델과 GPT로 구현 명세를 만들고 검토합니다.")
    subparsers = parser.add_subparsers(title="명령", dest="command", required=True)

    init = subparsers.add_parser("init", help="새 실행 시작")
    init.add_argument("request", nargs="*")
    init.add_argument("--from", dest="from_path")
    init.add_argument("--request-file")
    init.add_argument("--stdin", action="store_true")
    init.add_argument("--phase", choices=["1A", "1B", "2", "3"], default="2")
    init.add_argument("--model", default=DEFAULT_REVIEW_MODEL)
    init.add_argument("--panel-model", default=DEFAULT_PANEL_MODEL)
    init.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    init.add_argument("--max-panel-calls", type=int, default=DEFAULT_MAX_PANEL_CALLS)
    init.add_argument("--allow-reuse", action="store_true")
    init.add_argument("--allow-sensitive", action="store_true")
    init.set_defaults(func=command_init)

    check = subparsers.add_parser("preflight", help="실행 환경 확인")
    check.add_argument("--run")
    check.add_argument("--model")
    check.add_argument("--live", action="store_true")
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

    risk = subparsers.add_parser("accept-risk", help="사용자가 수용한 위험 기록")
    risk.add_argument("--run", required=True)
    risk.add_argument("--issue", required=True)
    risk.add_argument("--round", type=int, required=True)
    risk.add_argument("--note-file", required=True)
    risk.set_defaults(func=command_accept_risk)

    resolve = subparsers.add_parser("resolve-user-decision", help="사용자 선택을 기록하고 재개")
    resolve.add_argument("--run", required=True)
    resolve.add_argument("--action", choices=["REVISE", "CONTINUE"], required=True)
    resolve.add_argument("--note-file", required=True)
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
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = args.func(args)
        run_value = getattr(args, "run", None)
        if args.command == "init":
            write_timeline(Path(result["run_dir"]))
        elif run_value and args.command not in {"status", "timeline", "fixture-metrics"}:
            write_timeline(resolve_run(run_value))
        emit(result)
        return 0
    except EnsembleError as exc:
        run_value = getattr(args, "run", None)
        if run_value:
            try:
                run_dir = resolve_run(run_value)
                manifest = read_json(run_dir / "manifest.json")
                if isinstance(exc.details, dict) and isinstance(exc.details.get("attempts"), int):
                    retry_key = "schema" if isinstance(exc, SchemaError) else "infra"
                    retry_count = max(int(exc.details["attempts"]) - 1, 0)
                    manifest["retries"][retry_key] += retry_count
                    from ensemble_core.io_utils import atomic_write_json

                    atomic_write_json(run_dir / "manifest.json", manifest)
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
                    current = read_json(run_dir / "manifest.json")
                    if current.get("state") != "USER_DECISION_REQUIRED":
                        mark_terminal(run_dir, "INFRA_ERROR", exc.message)
                write_timeline(run_dir)
            except EnsembleError:
                pass
        emit(exc.as_dict())
        return EXIT_CODES.get(exc.code, 1)


if __name__ == "__main__":
    raise SystemExit(main())

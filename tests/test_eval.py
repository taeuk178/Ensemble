"""평가 계층(0~3단계) 테스트.

`ensemble/runs/`는 Git에 없으므로 합성 산출물로 검증한다. 심판 호출은
기존 provider 테스트처럼 모의로 대체한다.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_ROOT = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "ensemble" / "scripts"
sys.path.insert(0, str(SCRIPT_ROOT))

from ensemble_core import layout
from ensemble_core.errors import InputError, StateError
from ensemble_core.eval_bench import (
    collect_benchmark_runs,
    evaluate_init_block,
    grade_case,
    iter_cases,
    load_case,
    observe_run,
    suite_hash,
    validate_benchmark_block,
    validate_expected,
)
from ensemble_core.eval_process import compare_runs, compute_process_metrics, evaluate_run
from ensemble_core.eval_quality import compose_pair, compose_repetitions, map_verdict, run_quality_eval
from ensemble_core.io_utils import atomic_write_json, atomic_write_text, read_json
from ensemble_core.providers import ProviderResult, _codex_usage
from ensemble_core.state_machine import accumulate_usage, initialize_run


# --- 합성 산출물 ------------------------------------------------------

def manifest(
    *,
    state: str = "CONVERGED",
    finished: str | None = "2026-07-23T00:10:00Z",
    usage: dict | None = None,
    review_rounds: int = 2,
) -> dict:
    return {
        "run_id": "20260723T000000Z-spec-test",
        "state": state,
        "request_hash": "a" * 64,
        "started_at": "2026-07-23T00:00:00Z",
        "finished_at": finished,
        "current_round": review_rounds - 1,
        "panel_call_count": 0,
        "retries": {"schema": 0, "semantic": 1, "infra": 0},
        "usage": usage if usage is not None else {},
        "escalation_signals": [],
        "user_decisions": [],
        "review_history": [
            {"review_round": index + 1, "draft_round": index, "verdict": "APPROVED"}
            for index in range(review_rounds)
        ],
        "provider_calls": [
            {"operation": "review", "outcome": "SUCCESS", "session_resumed": False},
            {"operation": "review", "outcome": "SUCCESS", "session_resumed": True},
            {"operation": "final-blind", "outcome": "SUCCESS", "session_resumed": False},
        ],
        "environment": {"git_commit": "deadbeef", "ensemble_source_hash": "cafe"},
    }


def convergence(rounds: list[dict] | None = None) -> dict:
    return {
        "rounds": rounds
        if rounds is not None
        else [
            {
                "round": 1,
                "new_issue_count": 2,
                "open_backlog": 2,
                "author_dispositions": {"R1-I1": "ACCEPT", "R1-I2": "REJECT"},
                "resolution_basis_counts": {},
                "resolved_without_relevant_edit": 0,
                "regression_count": 0,
                "reviewer_storm": False,
                "stalled_streak": 0,
                "severity_distribution": {"4": 2},
            },
            {
                "round": 2,
                "new_issue_count": 0,
                "open_backlog": 0,
                "author_dispositions": {},
                "resolution_basis_counts": {"EDIT": 2},
                "resolved_without_relevant_edit": 1,
                "regression_count": 0,
                "reviewer_storm": False,
                "stalled_streak": 1,
                "severity_distribution": {},
            },
        ],
        "events": [],
    }


def registry(*, iterative: int = 1, promoted: int = 1, unknown: int = 0) -> dict:
    issues: dict[str, dict] = {}
    for index in range(iterative):
        issues[f"R1-I{index + 1}"] = {"first_seen_source": "04-reviews/iterative/r01.json"}
    for index in range(promoted):
        issues[f"R2-I{index + 1}"] = {"first_seen_source": "04-reviews/promoted/r02.json"}
    for index in range(unknown):
        issues[f"R9-I{index + 1}"] = {"first_seen_source": None}
    return issues


class UsageCollectionTests(unittest.TestCase):
    """0단계 — 실측 토큰만 기록하고, 미보고는 하한값임을 드러낸다."""

    def test_codex_usage_keeps_every_reported_field(self) -> None:
        event = {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "cache_write_input_tokens": 10,
                "output_tokens": 20,
                "reasoning_output_tokens": 5,
            },
        }
        stdout = "\n".join(['{"type":"thread.started","thread_id":"t"}', json.dumps(event)])
        self.assertEqual(
            _codex_usage(stdout),
            {
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "cache_write_input_tokens": 10,
                "output_tokens": 20,
                "reasoning_output_tokens": 5,
            },
        )

    def test_codex_usage_is_none_without_events(self) -> None:
        self.assertIsNone(_codex_usage("not json\n{\"type\":\"other\"}"))

    def test_partial_reporting_marks_attempts_unreported(self) -> None:
        holder: dict = {}
        # 3번 시도해 2번만 사용량을 보고한 논리 호출.
        accumulate_usage(
            holder,
            "codex",
            {"input_tokens": 30, "output_tokens": 6},
            attempts=3,
            attempts_reported=2,
        )
        totals = holder["usage"]["codex"]
        self.assertEqual(totals["input_tokens"], 30)
        self.assertEqual(totals["calls_reported"], 1)
        self.assertEqual(totals["calls_unreported"], 0)
        self.assertEqual(totals["attempts_reported"], 2)
        self.assertEqual(totals["attempts_unreported"], 1)

    def test_unreported_call_counts_only_as_unreported(self) -> None:
        holder: dict = {}
        accumulate_usage(holder, "agy", None, attempts=1, attempts_reported=0)
        totals = holder["usage"]["agy"]
        self.assertEqual(totals["calls_unreported"], 1)
        self.assertEqual(totals["calls_reported"], 0)
        self.assertEqual(totals["input_tokens"], 0)


class ProcessMetricsTests(unittest.TestCase):
    """1층 — 결정적 계산. 원천이 없으면 그 지표만 null이 된다."""

    def test_full_run_metrics(self) -> None:
        blinds = [("draft-01.json", {"blocking_issues": [{}, {}]})]
        reconciliations = [
            (
                "draft-01.json",
                {"accepted_findings": [{}], "unaccepted_blocking_findings": [], "passed": True},
            )
        ]
        result = compute_process_metrics(
            manifest(), convergence(), registry(), blinds, reconciliations
        )
        self.assertEqual(result["convergence"]["review_rounds"], 2)
        self.assertEqual(result["convergence"]["draft_rounds"], 2)
        self.assertTrue(result["convergence"]["terminated_cleanly"])
        self.assertEqual(result["convergence"]["new_issues_by_round"], [2, 0])
        self.assertEqual(result["convergence"]["rounds_to_zero_backlog"], 2)
        self.assertEqual(result["issues"]["total_issues"], 2)
        self.assertEqual(result["issues"]["dispositions"], {"ACCEPT": 1, "REJECT": 1})
        self.assertEqual(result["issues"]["acceptance_rate"], 0.5)
        self.assertEqual(result["friction"]["session_reuse_rate"], 0.5)
        self.assertEqual(result["resources"]["wall_clock_seconds"], 600.0)

    def test_leakage_rate_splits_attempt_counts(self) -> None:
        blinds = [
            ("draft-01.json", {"blocking_issues": [{}, {}, {}]}),
            ("draft-01-attempt-2.json", {"blocking_issues": [{}]}),
        ]
        reconciliations = [
            (
                "draft-01.json",
                {
                    "accepted_findings": [{}],
                    "unaccepted_blocking_findings": [{}, {}],
                    "passed": False,
                },
            ),
            (
                "draft-01-attempt-2.json",
                {"accepted_findings": [], "unaccepted_blocking_findings": [], "passed": True},
            ),
        ]
        result = compute_process_metrics(
            manifest(), convergence(), registry(iterative=1, promoted=1), blinds, reconciliations
        )
        leakage = result["leakage"]
        self.assertEqual(leakage["leakage_rate_lower_bound"], 0.5)
        self.assertEqual(leakage["final_blind_attempts"], 2)
        self.assertIs(leakage["final_blind_first_pass"], False)
        self.assertEqual(
            [(item["raw_findings"], item["accepted_risk_matches"], item["unaccepted_findings"]) for item in leakage["attempts"]],
            [(3, 1, 2), (1, 0, 0)],
        )

    def test_unpromoted_findings_are_reported_as_lower_bound(self) -> None:
        blinds = [("draft-02.json", {"blocking_issues": [{}, {}]})]
        reconciliations = [
            (
                "draft-02.json",
                {"accepted_findings": [], "unaccepted_blocking_findings": [{}, {}], "passed": False},
            )
        ]
        result = compute_process_metrics(
            manifest(state="ITERATION_LIMIT_REACHED"),
            convergence(),
            registry(iterative=2, promoted=0),
            blinds,
            reconciliations,
        )
        self.assertEqual(result["leakage"]["unique_promoted_final_blind_blockers"], 0)
        self.assertEqual(result["leakage"]["leakage_rate_lower_bound"], 0.0)
        self.assertEqual(result["leakage"]["unpromoted_unaccepted_last_attempt"], 2)
        self.assertTrue(any("승격되지 않은" in warning for warning in result["warnings"]))

    def test_unknown_origin_issues_are_excluded_from_the_rate(self) -> None:
        result = compute_process_metrics(
            manifest(), convergence(), registry(iterative=0, promoted=0, unknown=3), [], []
        )
        self.assertEqual(result["leakage"]["unknown_origin_blockers"], 3)
        self.assertIsNone(result["leakage"]["leakage_rate_lower_bound"])

    def test_unfinished_run_has_no_wall_clock(self) -> None:
        result = compute_process_metrics(
            manifest(state="USER_DECISION_REQUIRED", finished=None), convergence(), {}, [], []
        )
        self.assertIsNone(result["resources"]["wall_clock_seconds"])
        self.assertFalse(result["convergence"]["terminated_cleanly"])

    def test_missing_usage_is_null_not_an_error(self) -> None:
        result = compute_process_metrics(manifest(usage={}), convergence(), {}, [], [])
        self.assertIsNone(result["resources"]["usage"])
        self.assertIsNone(result["resources"]["usage_incomplete"])
        self.assertTrue(any("manifest.usage" in warning for warning in result["warnings"]))

    def test_unreported_calls_mark_usage_incomplete(self) -> None:
        usage = {
            "codex": {"input_tokens": 10, "calls_reported": 1, "calls_unreported": 1, "attempts_unreported": 1}
        }
        result = compute_process_metrics(manifest(usage=usage), convergence(), {}, [], [])
        self.assertTrue(result["resources"]["usage_incomplete"])
        self.assertEqual(result["resources"]["usage_unreported_calls"], 1)

    def test_missing_convergence_leaves_other_metrics_intact(self) -> None:
        result = compute_process_metrics(manifest(), {"rounds": [], "events": []}, registry(), [], [])
        self.assertEqual(result["convergence"]["new_issues_by_round"], [])
        self.assertIsNone(result["convergence"]["rounds_to_zero_backlog"])
        self.assertEqual(result["issues"]["total_issues"], 2)
        self.assertEqual(result["convergence"]["review_rounds"], 2)


class AttemptOrderingTests(unittest.TestCase):
    def test_attempt_suffix_sorts_after_the_original(self) -> None:
        names = ["draft-00-attempt-2.json", "draft-00.json", "draft-10.json", "draft-02.json"]
        ordered = sorted((Path(name) for name in names), key=layout.attempt_of)
        self.assertEqual(
            [path.name for path in ordered],
            ["draft-00.json", "draft-00-attempt-2.json", "draft-02.json", "draft-10.json"],
        )


class EvalRunCase(unittest.TestCase):
    """실제 실행 폴더를 만들어 파일 입출력 경로까지 확인한다."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runs = self.root / "runs"
        self.patches = [
            patch("ensemble_core.state_machine.RUNS_ROOT", self.runs),
            patch("ensemble_core.io_utils.RUNS_ROOT", self.runs),
            patch("ensemble_core.eval_bench.RUNS_ROOT", self.runs),
        ]
        for item in self.patches:
            item.start()
        self.run = initialize_run("평가 대상 문서", allow_reuse=True)

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def write_draft(self, round_number: int, body: str) -> Path:
        path = layout.draft(self.run, round_number)
        atomic_write_text(path, body, overwrite=False)
        return path


class EvalRunIoTests(EvalRunCase):
    def test_evaluation_does_not_touch_the_run_manifest(self) -> None:
        before = layout.manifest(self.run).read_text(encoding="utf-8")
        evaluate_run(self.run)
        self.assertEqual(layout.manifest(self.run).read_text(encoding="utf-8"), before)
        self.assertTrue(layout.process_metrics(self.run).exists())

    def test_previous_result_is_archived_not_overwritten(self) -> None:
        evaluate_run(self.run)
        stale = read_json(layout.process_metrics(self.run))
        stale["evaluated_at"] = "2026-07-01T00:00:00Z"
        atomic_write_json(layout.process_metrics(self.run), stale)
        evaluate_run(self.run)
        archive = layout.process_metrics_archive(self.run, "20260701T000000Z")
        self.assertTrue(archive.exists())
        self.assertEqual(read_json(archive)["evaluated_at"], "2026-07-01T00:00:00Z")
        self.assertNotEqual(
            read_json(layout.process_metrics(self.run))["evaluated_at"], "2026-07-01T00:00:00Z"
        )

    def test_compare_reports_differing_request_hashes(self) -> None:
        other = initialize_run("다른 요청", allow_reuse=True)
        result = compare_runs([self.run, other])
        self.assertFalse(result["same_request"])
        self.assertEqual(len({row["request_hash"] for row in result["runs"]}), 2)
        self.assertIn("convergence.review_rounds", result["metrics"])


# --- 2층 -------------------------------------------------------------

def judge_payload(winner: str) -> dict:
    return {
        axis: {"winner": winner, "reason": "근거"}
        for axis in (
            "testable_criteria",
            "internal_consistency",
            "requirement_coverage",
            "over_specification",
            "overall",
        )
    }


class JudgeMappingTests(unittest.TestCase):
    """DOC1/DOC2 ↔ draft/final 매핑이 틀리면 결과가 정반대로 뒤집힌다."""

    def test_order_mapping_is_inverted_on_the_swapped_call(self) -> None:
        self.assertEqual(map_verdict("DRAFT_FIRST", "DOC1"), "DRAFT")
        self.assertEqual(map_verdict("DRAFT_FIRST", "DOC2"), "FINAL")
        self.assertEqual(map_verdict("FINAL_FIRST", "DOC1"), "FINAL")
        self.assertEqual(map_verdict("FINAL_FIRST", "DOC2"), "DRAFT")
        self.assertEqual(map_verdict("FINAL_FIRST", "TIE"), "TIE")

    def test_composition_rules(self) -> None:
        self.assertEqual(compose_pair("FINAL", "FINAL"), "FINAL_BETTER")
        self.assertEqual(compose_pair("DRAFT", "DRAFT"), "DRAFT_BETTER")
        self.assertEqual(compose_pair("TIE", "TIE"), "TIE")
        self.assertEqual(compose_pair("FINAL", "DRAFT"), "UNSTABLE")
        self.assertEqual(compose_pair("FINAL", "TIE"), "UNSTABLE")

    def test_repetitions_must_all_agree(self) -> None:
        self.assertEqual(compose_repetitions(["FINAL_BETTER"] * 3), "FINAL_BETTER")
        self.assertEqual(compose_repetitions(["FINAL_BETTER", "TIE", "FINAL_BETTER"]), "UNSTABLE")


class QualityEvalTests(EvalRunCase):
    def finish_run(self, *, drafts: list[str]) -> None:
        for index, body in enumerate(drafts):
            self.write_draft(index, body)
        atomic_write_text(layout.final(self.run), "<!-- ensemble-status: CONVERGED -->\n" + drafts[-1])
        current = read_json(layout.manifest(self.run))
        current["current_round"] = len(drafts) - 1
        atomic_write_json(layout.manifest(self.run), current)

    def judge(self, payloads: list[dict]):
        """호출 순서대로 정해진 응답을 돌려주는 모의 심판."""
        self.captured_bundles: list[set[str]] = []
        calls = iter(payloads)

        def fake(*, bundle_dir, **kwargs):
            self.captured_bundles.append({path.name for path in Path(bundle_dir).iterdir()})
            return ProviderResult(
                payload=next(calls),
                stdout="",
                stderr="",
                attempts=1,
                executable="/fake/agy",
                version="1.1.5",
                model=kwargs["model"],
                usage=None,
                attempts_reported=0,
            )

        return patch("ensemble_core.eval_quality.run_agy", side_effect=fake)

    def test_identical_drafts_skip_the_judge(self) -> None:
        self.finish_run(drafts=["# 스펙\n\n내용\n"])
        with self.judge([]) as mocked:
            result = run_quality_eval(self.run, model="m", effort="high", timeout=10)
        self.assertEqual(result["verdict"], "IDENTICAL")
        self.assertTrue(result["content_identical"])
        mocked.assert_not_called()

    def test_unfinished_run_is_skipped(self) -> None:
        self.write_draft(0, "# 스펙\n")
        with self.judge([]) as mocked:
            result = run_quality_eval(self.run, model="m", effort="high", timeout=10)
        self.assertEqual(result["verdict"], "SKIP")
        mocked.assert_not_called()

    def test_final_draft_selection_falls_back_to_the_highest_number(self) -> None:
        self.finish_run(drafts=["# 초안 0\n", "# 초안 1\n", "# 초안 2\n"])
        current = read_json(layout.manifest(self.run))
        current["current_round"] = 9  # 존재하지 않는 초안 번호
        atomic_write_json(layout.manifest(self.run), current)
        with self.judge([judge_payload("DOC2"), judge_payload("DOC1")]):
            result = run_quality_eval(self.run, model="m", effort="high", timeout=10)
        self.assertEqual(result["final_doc"], "03-drafts/draft-02.md")

    def test_bundle_never_contains_the_delivered_final_document(self) -> None:
        self.finish_run(drafts=["# 초안 0\n", "# 초안 1\n"])
        with self.judge([judge_payload("DOC2"), judge_payload("DOC1")]):
            run_quality_eval(self.run, model="m", effort="high", timeout=10)
        for names in self.captured_bundles:
            self.assertEqual(names, {"request.md", "rubric.md", "document-1.md", "document-2.md"})

    def test_consistent_preference_for_the_later_draft(self) -> None:
        self.finish_run(drafts=["# 초안 0\n", "# 초안 1\n"])
        # 호출 1은 draft가 앞이므로 DOC2가 final, 호출 2는 순서가 바뀌어 DOC1이 final.
        with self.judge([judge_payload("DOC2"), judge_payload("DOC1")]):
            result = run_quality_eval(self.run, model="m", effort="high", timeout=10)
        self.assertEqual(result["composite"]["overall"], "FINAL_BETTER")
        self.assertEqual([call["order"] for call in result["calls"]], ["DRAFT_FIRST", "FINAL_FIRST"])

    def test_loop_made_the_document_worse(self) -> None:
        self.finish_run(drafts=["# 초안 0\n", "# 초안 1\n"])
        with self.judge([judge_payload("DOC1"), judge_payload("DOC2")]):
            result = run_quality_eval(self.run, model="m", effort="high", timeout=10)
        self.assertEqual(result["composite"]["overall"], "DRAFT_BETTER")

    def test_position_bias_is_reported_as_unstable(self) -> None:
        self.finish_run(drafts=["# 초안 0\n", "# 초안 1\n"])
        # 두 호출 모두 첫 번째 문서를 골랐다 = 내용이 아니라 위치를 본 것이다.
        with self.judge([judge_payload("DOC1"), judge_payload("DOC1")]):
            result = run_quality_eval(self.run, model="m", effort="high", timeout=10)
        self.assertEqual(result["composite"]["overall"], "UNSTABLE")

    def test_repetitions_record_the_distribution(self) -> None:
        self.finish_run(drafts=["# 초안 0\n", "# 초안 1\n"])
        payloads = [
            judge_payload("DOC2"), judge_payload("DOC1"),  # FINAL_BETTER
            judge_payload("DOC2"), judge_payload("DOC1"),  # FINAL_BETTER
            judge_payload("DOC1"), judge_payload("DOC1"),  # UNSTABLE
        ]
        with self.judge(payloads):
            result = run_quality_eval(self.run, model="m", effort="high", timeout=10, repetitions=3)
        self.assertEqual(len(result["calls"]), 6)
        self.assertEqual(
            result["composite_distribution"]["overall"], {"FINAL_BETTER": 2, "UNSTABLE": 1}
        )
        self.assertEqual(result["composite"]["overall"], "UNSTABLE")
        self.assertEqual(result["usage_total"]["calls_unreported"], 6)

    def test_judge_raw_responses_are_never_overwritten(self) -> None:
        self.finish_run(drafts=["# 초안 0\n", "# 초안 1\n"])
        with self.judge([judge_payload("DOC2"), judge_payload("DOC1")]):
            run_quality_eval(self.run, model="m", effort="high", timeout=10)
        with self.judge([judge_payload("DOC2"), judge_payload("DOC1")]):
            run_quality_eval(self.run, model="m", effort="high", timeout=10)
        raw_files = sorted(path.name for path in layout.judge_raw_dir(self.run).iterdir())
        self.assertEqual(raw_files, ["call-1.json", "call-2.json", "call-3.json", "call-4.json"])


# --- 3층 -------------------------------------------------------------

def expected_case(**overrides) -> dict:
    payload = {
        "schema_version": 1,
        "review_required": "검토 필요",
        "reviewed_by_user": True,
        "case_type": "state_behavior",
        "suites": ["smoke"],
        "tags": [],
        "difficulty": "easy",
        "expected_terminal_states": ["CONVERGED"],
        "forbidden_states": ["ITERATION_LIMIT_REACHED"],
        "expect_user_decision": False,
        "expect_escalation": False,
    }
    payload.update(overrides)
    return payload


def case(**overrides) -> dict:
    return {
        "case_id": "case-1",
        "request": "요청",
        "expected": expected_case(**overrides),
        "case_revision_hash": "h" * 64,
    }


def observation(**overrides) -> dict:
    payload = {
        "run_dir": "/runs/x",
        "terminal_state": "CONVERGED",
        "init_blocked": False,
        "user_decision_reached": False,
        "escalation_reached": False,
        "states_seen": ["APPROVED", "CONVERGED"],
        "tainted": False,
    }
    payload.update(overrides)
    return payload


class ExpectedSchemaTests(unittest.TestCase):
    def test_valid_case_passes(self) -> None:
        validate_expected(expected_case(), case_id="case-1")

    def test_unknown_state_is_rejected(self) -> None:
        with self.assertRaises(InputError):
            validate_expected(
                expected_case(expected_terminal_states=["CONVERGD"]), case_id="case-1"
            )

    def test_init_block_must_not_expect_a_terminal_state(self) -> None:
        with self.assertRaises(InputError):
            validate_expected(
                expected_case(case_type="init_block", expected_terminal_states=["CONVERGED"]),
                case_id="case-1",
            )

    def test_quality_case_requires_expectations(self) -> None:
        with self.assertRaises(InputError):
            validate_expected(expected_case(case_type="quality"), case_id="case-1")

    def test_quality_expectations_must_have_content(self) -> None:
        with self.assertRaises(InputError):
            validate_expected(
                expected_case(
                    case_type="quality",
                    quality_expectations={"must_cover": [], "must_not_assert": []},
                ),
                case_id="case-1",
            )

    def test_unknown_field_is_rejected(self) -> None:
        with self.assertRaises(InputError):
            validate_expected(expected_case(nonsense=1), case_id="case-1")

    def test_state_cannot_be_expected_and_forbidden(self) -> None:
        with self.assertRaises(InputError):
            validate_expected(
                expected_case(forbidden_states=["CONVERGED"]), case_id="case-1"
            )


class GradingTests(unittest.TestCase):
    def test_matching_behaviour_passes(self) -> None:
        self.assertEqual(grade_case(case(), observation())["verdict"], "PASS")

    def test_wrong_terminal_state_fails(self) -> None:
        result = grade_case(case(), observation(terminal_state="ITERATION_LIMIT_REACHED"))
        self.assertEqual(result["verdict"], "FAIL")

    def test_forbidden_state_visited_fails(self) -> None:
        result = grade_case(
            case(expected_terminal_states=["STABLE_DISSENT"]),
            observation(
                terminal_state="STABLE_DISSENT",
                states_seen=["ITERATION_LIMIT_REACHED", "STABLE_DISSENT"],
            ),
        )
        self.assertEqual(result["verdict"], "FAIL")
        self.assertTrue(any("금지된 상태" in reason for reason in result["reasons"]))

    def test_unexpected_user_decision_fails(self) -> None:
        result = grade_case(case(), observation(user_decision_reached=True))
        self.assertEqual(result["verdict"], "FAIL")

    def test_expected_user_decision_passes(self) -> None:
        result = grade_case(
            case(expected_terminal_states=["USER_DECISION_REQUIRED"], expect_user_decision=True),
            observation(terminal_state="USER_DECISION_REQUIRED", user_decision_reached=True),
        )
        self.assertEqual(result["verdict"], "PASS")

    def test_unreviewed_answer_key_is_excluded(self) -> None:
        result = grade_case(case(reviewed_by_user=False), observation())
        self.assertEqual(result["verdict"], "UNREVIEWED")

    def test_only_infrastructure_failure_is_skipped(self) -> None:
        skipped = grade_case(case(), observation(terminal_state="INFRA_ERROR"))
        self.assertEqual(skipped["verdict"], "SKIP")
        # Ensemble 코드가 낸 오류는 SKIP으로 빼지 않는다. 빼면 회귀가 숨는다.
        failed = grade_case(case(), observation(terminal_state="OSCILLATING"))
        self.assertEqual(failed["verdict"], "FAIL")

    def test_tainted_run_is_skipped(self) -> None:
        result = grade_case(case(), observation(tainted=True))
        self.assertEqual(result["verdict"], "SKIP")

    def test_quality_case_fails_on_missing_coverage(self) -> None:
        quality = case(
            case_type="quality",
            quality_expectations={"must_cover": ["오류 흐름"], "must_not_assert": []},
        )
        result = grade_case(
            quality,
            observation(),
            {"must_cover_missing": [{"item": "오류 흐름", "reason": "없음"}], "must_not_assert_violations": []},
        )
        self.assertEqual(result["verdict"], "FAIL")

    def test_missing_findings_do_not_hide_a_failed_state_check(self) -> None:
        quality = case(
            case_type="quality",
            quality_expectations={"must_cover": ["오류 흐름"], "must_not_assert": []},
        )
        # 심판을 부르지 못했더라도 상태 검사가 이미 실패했으면 FAIL이다.
        failed = grade_case(quality, observation(terminal_state="ITERATION_LIMIT_REACHED"), None)
        self.assertEqual(failed["verdict"], "FAIL")
        # 상태 검사가 통과했는데 품질 축만 모르면 판정을 보류한다.
        unknown = grade_case(quality, observation(), None)
        self.assertEqual(unknown["verdict"], "SKIP")

    def test_quality_case_passes_with_clean_findings(self) -> None:
        quality = case(
            case_type="quality",
            quality_expectations={"must_cover": ["오류 흐름"], "must_not_assert": []},
        )
        result = grade_case(
            quality, observation(), {"must_cover_missing": [], "must_not_assert_violations": []}
        )
        self.assertEqual(result["verdict"], "PASS")

    def test_init_block_fails_when_a_run_was_created(self) -> None:
        blocked = case(case_type="init_block", expected_terminal_states=[], forbidden_states=[])
        self.assertEqual(
            grade_case(blocked, observation(init_blocked=True, run_dir=None))["verdict"], "PASS"
        )
        self.assertEqual(
            grade_case(blocked, observation(init_blocked=False))["verdict"], "FAIL"
        )


class BenchmarkBlockTests(unittest.TestCase):
    def valid(self) -> dict:
        return {
            "benchmark_run_id": "bench-1",
            "case_id": "case-1",
            "case_revision_hash": "h" * 64,
            "suite": "smoke",
            "repeat_index": 1,
        }

    def test_valid_block(self) -> None:
        self.assertEqual(validate_benchmark_block(self.valid())["case_id"], "case-1")

    def test_missing_field_is_rejected(self) -> None:
        payload = self.valid()
        del payload["case_id"]
        with self.assertRaises(InputError):
            validate_benchmark_block(payload)

    def test_repeat_index_must_be_positive(self) -> None:
        payload = self.valid()
        payload["repeat_index"] = 0
        with self.assertRaises(InputError):
            validate_benchmark_block(payload)


class CaseCollectionTests(EvalRunCase):
    def start_benchmark_run(self, *, case_id: str, revision: str, benchmark_run_id: str) -> Path:
        return initialize_run(
            f"벤치마크 요청 {case_id} {revision}",
            allow_reuse=True,
            benchmark={
                "benchmark_run_id": benchmark_run_id,
                "case_id": case_id,
                "case_revision_hash": revision,
                "suite": "smoke",
                "repeat_index": 1,
            },
        )

    def test_only_the_named_benchmark_traversal_is_collected(self) -> None:
        self.start_benchmark_run(case_id="case-1", revision="r1", benchmark_run_id="bench-A")
        self.start_benchmark_run(case_id="case-1", revision="r1", benchmark_run_id="bench-B")
        collected = collect_benchmark_runs("bench-A")
        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0][1]["benchmark"]["benchmark_run_id"], "bench-A")

    def test_runs_without_a_benchmark_block_are_ignored(self) -> None:
        self.assertEqual(collect_benchmark_runs("bench-A"), [])

    def test_observation_recovers_reached_states_from_the_manifest(self) -> None:
        current = read_json(layout.manifest(self.run))
        current["state"] = "CONVERGED"
        current["review_history"] = [{"review_round": 1, "draft_round": 0, "verdict": "NEEDS_REVISION"}]
        current["user_decisions"] = [{"from_state": "USER_DECISION_REQUIRED", "action": "CONTINUE"}]
        atomic_write_json(layout.manifest(self.run), current)
        result = observe_run(self.run, read_json(layout.manifest(self.run)))
        self.assertTrue(result["user_decision_reached"])
        self.assertIn("NEEDS_REVISION", result["states_seen"])
        self.assertEqual(result["terminal_state"], "CONVERGED")


class CollectScorecardTests(EvalRunCase):
    """수집 → 채점 → 점수표 작성까지의 조립 경로."""

    def setUp(self) -> None:
        super().setUp()
        self.cases_root = self.root / "cases"
        self.results_root = self.root / "results"
        self.extra = [
            patch("ensemble_core.config.EVAL_CASES_ROOT", self.cases_root),
            patch("ensemble_core.config.EVAL_RESULTS_ROOT", self.results_root),
        ]
        for item in self.extra:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.extra):
            item.stop()
        super().tearDown()

    def write_case(self, case_id: str, expected: dict, request: str) -> dict:
        atomic_write_text(layout.case_request(case_id), request, overwrite=False)
        atomic_write_json(layout.case_expected(case_id), expected, overwrite=False)
        return load_case(case_id)

    def test_scorecard_grades_a_finished_behaviour_case(self) -> None:
        from ensemble_core.eval_bench import collect, write_scorecard

        loaded = self.write_case(
            "case-a",
            expected_case(reviewed_by_user=True),
            "위키에 읽음 표시를 추가한다.",
        )
        run_dir = initialize_run(
            "위키에 읽음 표시를 추가한다.",
            allow_reuse=True,
            benchmark={
                "benchmark_run_id": "bench-A",
                "case_id": "case-a",
                "case_revision_hash": loaded["case_revision_hash"],
                "suite": "smoke",
                "repeat_index": 1,
            },
        )
        current = read_json(layout.manifest(run_dir))
        current["state"] = "CONVERGED"
        current["finished_at"] = "2026-07-23T00:10:00Z"
        atomic_write_json(layout.manifest(run_dir), current)

        scorecard = collect(suite="smoke", benchmark_run_id="bench-A", timeout=10)
        self.assertEqual(scorecard["totals"]["pass"], 1)
        self.assertEqual(scorecard["cases"][0]["case_id"], "case-a")
        self.assertIsNotNone(scorecard["cases"][0]["process_metrics"])
        path = write_scorecard(scorecard)
        self.assertTrue(path.exists())

    def test_run_from_a_changed_case_revision_is_not_collected(self) -> None:
        from ensemble_core.eval_bench import collect

        self.write_case("case-a", expected_case(reviewed_by_user=True), "요청 원문")
        initialize_run(
            "요청 원문",
            allow_reuse=True,
            benchmark={
                "benchmark_run_id": "bench-A",
                "case_id": "case-a",
                "case_revision_hash": "낡은-해시",
                "suite": "smoke",
                "repeat_index": 1,
            },
        )
        scorecard = collect(suite="smoke", benchmark_run_id="bench-A", timeout=10)
        self.assertTrue(any("케이스 파일이 바뀐" in warning for warning in scorecard["warnings"]))
        self.assertEqual(scorecard["totals"]["pass"], 0)

    def test_unreviewed_case_is_excluded_from_the_totals(self) -> None:
        from ensemble_core.eval_bench import collect

        loaded = self.write_case("case-a", expected_case(reviewed_by_user=False), "요청 원문")
        run_dir = initialize_run(
            "요청 원문",
            allow_reuse=True,
            benchmark={
                "benchmark_run_id": "bench-A",
                "case_id": "case-a",
                "case_revision_hash": loaded["case_revision_hash"],
                "suite": "smoke",
                "repeat_index": 1,
            },
        )
        current = read_json(layout.manifest(run_dir))
        current["state"] = "CONVERGED"
        atomic_write_json(layout.manifest(run_dir), current)
        scorecard = collect(suite="smoke", benchmark_run_id="bench-A", timeout=10)
        self.assertEqual(scorecard["totals"]["unreviewed"], 1)
        self.assertEqual(scorecard["totals"]["pass"], 0)


class InitBlockCaseTests(EvalRunCase):
    def test_secret_bearing_request_is_blocked_before_a_run_exists(self) -> None:
        request = "토큰은 sk-EXAMPLENOTAREALKEY0000000000000000000000 입니다."
        result = evaluate_init_block({"case_id": "c", "request": request, "expected": {}})
        self.assertTrue(result["init_blocked"])
        self.assertIsNone(result["run_dir"])
        self.assertEqual(result["block_details"]["patterns"], ["openai_key"])

    def test_clean_request_creates_a_run_which_is_the_failure_evidence(self) -> None:
        result = evaluate_init_block({"case_id": "c", "request": "평범한 기능 요청", "expected": {}})
        self.assertFalse(result["init_blocked"])
        self.assertIsNotNone(result["run_dir"])


class ShippedCaseTests(unittest.TestCase):
    """저장소에 든 케이스 파일이 스키마를 지키는지 확인한다."""

    def test_every_case_file_validates(self) -> None:
        for directory in sorted(layout.cases_root().iterdir()):
            if directory.is_dir():
                load_case(directory.name)

    def test_smoke_is_a_subset_of_full(self) -> None:
        smoke = {item["case_id"] for item in iter_cases("smoke")}
        full = {item["case_id"] for item in iter_cases("full")}
        self.assertTrue(smoke)
        self.assertTrue(smoke <= full)

    def test_suite_hash_changes_when_a_case_changes(self) -> None:
        cases = iter_cases("smoke")
        baseline = suite_hash(cases)
        altered = [dict(cases[0], case_revision_hash="different"), *cases[1:]]
        self.assertNotEqual(baseline, suite_hash(altered))

    def test_every_case_carries_the_review_gate(self) -> None:
        """검토를 실제로 했는지는 사람만 안다. 테스트는 게이트가 빠지지
        않았는지와, 승인된 케이스에 검토 근거를 적은 notes.md가 있는지만 본다.
        """
        for directory in sorted(layout.cases_root().iterdir()):
            if not directory.is_dir():
                continue
            expected = load_case(directory.name)["expected"]
            self.assertIsInstance(expected["reviewed_by_user"], bool)
            self.assertTrue(expected["review_required"].strip())
            if expected["reviewed_by_user"]:
                self.assertTrue(
                    (directory / "notes.md").is_file(),
                    f"{directory.name}: 승인된 정답지에 검토 근거(notes.md)가 없습니다.",
                )


class ScorecardComparisonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.results = Path(self.temporary.name)
        self.patch = patch("ensemble_core.config.EVAL_RESULTS_ROOT", self.results)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.temporary.cleanup()

    def scorecard(self, sha: str, **overrides) -> None:
        payload = {
            "schema_version": 1,
            "git_commit": sha,
            "suite": "smoke",
            "suite_hash": "s" * 64,
            "tainted": False,
            "model_config": {"codex": "gpt", "agy": "gemini", "judge": "gemini"},
            "cases": [{"case_id": "case-1", "repeat_index": 1, "verdict": "PASS"}],
            "totals": {"pass": 1, "fail": 0, "skip": 0, "unreviewed": 0},
        }
        payload.update(overrides)
        atomic_write_json(layout.scorecard(sha), payload)

    def test_comparison_reports_a_regression_signal(self) -> None:
        from ensemble_core.eval_bench import compare_scorecards

        self.scorecard("base")
        self.scorecard("head", cases=[{"case_id": "case-1", "repeat_index": 1, "verdict": "FAIL"}])
        result = compare_scorecards("base", "head")
        self.assertEqual(result["regression_signals"], ["case-1"])

    def test_tainted_scorecard_is_refused(self) -> None:
        from ensemble_core.eval_bench import compare_scorecards

        self.scorecard("base", tainted=True)
        self.scorecard("head")
        with self.assertRaises(StateError):
            compare_scorecards("base", "head")

    def test_different_suite_hash_is_refused(self) -> None:
        from ensemble_core.eval_bench import compare_scorecards

        self.scorecard("base")
        self.scorecard("head", suite_hash="t" * 64)
        with self.assertRaises(StateError):
            compare_scorecards("base", "head")

    def test_model_mismatch_needs_an_explicit_flag(self) -> None:
        from ensemble_core.eval_bench import compare_scorecards

        self.scorecard("base")
        self.scorecard("head", model_config={"codex": "other", "agy": "gemini", "judge": "gemini"})
        with self.assertRaises(StateError):
            compare_scorecards("base", "head")
        result = compare_scorecards("base", "head", allow_model_mismatch=True)
        self.assertTrue(result["model_config_mismatch_allowed"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPT_ROOT = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "ensemble" / "scripts"
sys.path.insert(0, str(SCRIPT_ROOT))

from ensemble_core.bundle import isolated_bundle, prepare_review_session_bundle
from ensemble_core.audit import apply_audit
from ensemble_core.cards import build_feedback_cards, build_panel_card
from ensemble_core.convergence import refresh_author_dispositions, reproducibility_metrics
from ensemble_core.errors import InfraError, InputError, SchemaError, SecurityError, SemanticValidationError, StateError
from ensemble_core.hashing import canonical_issue_key, parse_sections, refs_changed, section_hashes
from ensemble_core.history import write_timeline
from ensemble_core import layout
from ensemble_core.io_utils import atomic_write_json, parse_answer_section, read_json, resolve_run
from ensemble_core.isolated import save_final_assessment
from ensemble_core.providers import ProviderResult
from ensemble_core.registry import accept_risk, load_registry, record_author_decision
from ensemble_core.report import finalize
from ensemble_core.state_machine import (
    assert_run_can_advance,
    assert_final_blind_budget,
    assert_source_unchanged,
    initialize_run,
    load_codex_review_session,
    record_codex_review_session,
    register_draft,
    resolve_user_decision,
    verified_request_hash,
)
from ensemble_core.validation import validate_review_schema
from ensemble_core.workflow import ingest_review, promote_final_findings, run_review


def issue(*, issue_id: str | None = None, severity: int = 4, response: str | None = None) -> dict[str, object]:
    return {
        "id": issue_id,
        "criterion_id": "AC-02",
        "location": "오류 흐름",
        "evidence_refs": ["오류 흐름"],
        "problem": "실패 경로가 정의되지 않았다.",
        "violation_evidence": "`오류 흐름` 절에 실패 상태가 없다.",
        "implementation_consequence": "구현자가 실패 상태를 임의로 결정해야 한다.",
        "required_change": "실패 상태와 복구 흐름을 추가한다.",
        "severity": severity,
        "confidence": 0.8,
        "basis": "DOCUMENT_INTERNAL",
        "verification_required": False,
        "response_to_rebuttal": response,
        "split_from": None,
        "merged_from": [],
    }


def review(
    *,
    blockers: list[dict[str, object]] | None = None,
    resolved: list[dict[str, object]] | None = None,
    verdict: str | None = None,
) -> dict[str, object]:
    blockers = blockers or []
    resolved = resolved or []
    return {
        "verdict": verdict or ("NEEDS_REVISION" if blockers else "APPROVED"),
        "summary": "구조화 리뷰",
        "blocking_issues": blockers,
        "resolved_issues": resolved,
        "questions_for_user": [],
        "nonblocking_risks": [],
    }


class RunCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runs = self.root / "runs"
        self.patches = [
            patch("ensemble_core.state_machine.RUNS_ROOT", self.runs),
            patch("ensemble_core.io_utils.RUNS_ROOT", self.runs),
            # 테스트가 사용자 홈의 실제 세션 기록을 읽지 않게 한다.
            patch("ensemble_core.config.CLAUDE_TRANSCRIPT_ROOT", self.root / "transcripts"),
        ]
        for item in self.patches:
            item.start()
        self.run = initialize_run("오류 처리 문서", allow_reuse=True)
        self.draft0 = self.root / "draft-0.md"
        self.draft0.write_text("# 스펙\n\n## 오류 흐름\n\n성공 상태만 정의한다.\n", encoding="utf-8")
        register_draft(self.run, self.draft0, 0)

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def first_issue(self) -> str:
        result = ingest_review(self.run, review=review(blockers=[issue()]), review_round=1)
        return result["stats"]["new_issue_ids"][0]


class InputAndHashingTests(unittest.TestCase):
    def test_answer_section_ignores_comments(self) -> None:
        value = "# Request\n\n### 답변\n\n<!-- 안내 -->\n제품을 만든다.\n"
        self.assertEqual(parse_answer_section(value), "제품을 만든다.")

    def test_empty_answer_is_rejected(self) -> None:
        with self.assertRaises(InputError):
            parse_answer_section("### 답변\n<!-- 안내 -->\n")

    def test_korean_headings_have_distinct_slugs(self) -> None:
        sections = parse_sections("# 문서\n\n## 인증 정책\nA\n\n## 오류 흐름\nB\n")
        slugs = {section.slug for section in sections}
        self.assertIn("인증-정책", slugs)
        self.assertIn("오류-흐름", slugs)

    def test_issue_key_distinguishes_consequence(self) -> None:
        markdown = "# 문서\n\n## 오류 흐름\n\n성공 상태만 정의한다.\n"
        left = issue()
        right = issue()
        right["implementation_consequence"] = "재시도 횟수를 임의로 결정해야 한다."
        self.assertNotEqual(
            canonical_issue_key(markdown, left, unmatched_salt="left"),
            canonical_issue_key(markdown, right, unmatched_salt="right"),
        )

    def test_number_only_reference_resolves_full_heading(self) -> None:
        previous = {"2-2-종료-상태": "old"}
        current = {"2-2-종료-상태": "new"}
        self.assertTrue(refs_changed(previous, current, ["§2.2"]))

    def test_user_original_is_preserved_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs = Path(temporary) / "runs"
            original = "  첫 줄 `$()`\n둘째 줄\n"
            with patch("ensemble_core.state_machine.RUNS_ROOT", runs), patch(
                "ensemble_core.io_utils.RUNS_ROOT", runs
            ):
                run = initialize_run(original, allow_reuse=True)
            self.assertEqual(layout.request_original(run).read_text(encoding="utf-8"), original)
            self.assertIn(original, layout.request(run).read_text(encoding="utf-8"))


class ValidationTests(unittest.TestCase):
    def test_structured_output_schemas_require_every_object_property(self) -> None:
        reference_root = (
            Path(__file__).resolve().parents[1]
            / ".claude"
            / "skills"
            / "ensemble"
            / "references"
        )

        def inspect(value: object, context: str) -> None:
            if isinstance(value, dict):
                if value.get("type") == "object" and "properties" in value:
                    self.assertFalse(value.get("additionalProperties", True), context)
                    self.assertEqual(set(value["properties"]), set(value.get("required", [])), context)
                for key, child in value.items():
                    inspect(child, f"{context}/{key}")
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    inspect(child, f"{context}/{index}")

        for name in ("proposal.schema.json", "review.schema.json", "audit.schema.json", "panel.schema.json"):
            inspect(json.loads((reference_root / name).read_text(encoding="utf-8")), name)

    def test_reviewer_cannot_output_gating(self) -> None:
        payload = review(blockers=[issue()])
        payload["blocking_issues"][0]["gating"] = False
        with self.assertRaises(SchemaError):
            validate_review_schema(payload)

    def test_approved_cannot_have_blockers(self) -> None:
        payload = review(blockers=[issue()], verdict="APPROVED")
        validate_review_schema(payload)
        from ensemble_core.validation import validate_review_semantics

        with self.assertRaises(SemanticValidationError):
            validate_review_semantics(
                payload,
                registry={},
                previous_hashes={},
                current_hashes={},
            )

    def test_blocking_severity_floor(self) -> None:
        payload = review(blockers=[issue(severity=2)])
        from ensemble_core.validation import validate_review_semantics

        with self.assertRaises(SemanticValidationError):
            validate_review_semantics(
                payload,
                registry={},
                previous_hashes={},
                current_hashes={},
            )


class RegistryAndWorkflowTests(RunCase):
    def _resolution(self, issue_id: str, basis: str = "REBUTTAL_ACCEPTED") -> dict[str, object]:
        return {
            "id": issue_id,
            "resolution_basis": basis,
            "resolution_reason": "이전 이슈가 해소됨",
            "evidence_refs": ["오류 흐름"],
            "superseded_by": None,
            "merged_into": None,
        }

    def test_new_issue_gets_wrapper_id_and_projection_hides_scores(self) -> None:
        issue_id = self.first_issue()
        self.assertEqual(issue_id, "R1-I1")
        projection = read_json(layout.reviewer_index(self.run))
        rendered = json.dumps(projection)
        self.assertNotIn("severity", rendered)
        self.assertNotIn("confidence", rendered)
        self.assertEqual(projection[0]["id"], issue_id)

    def test_silent_open_issue_omission_is_rejected(self) -> None:
        self.first_issue()
        draft1 = self.root / "draft-1.md"
        draft1.write_text(self.draft0.read_text(encoding="utf-8"), encoding="utf-8")
        register_draft(self.run, draft1, 1)
        with self.assertRaises(SemanticValidationError):
            ingest_review(self.run, review=review(), review_round=2)

    def test_edit_resolution_requires_relevant_hash_change(self) -> None:
        issue_id = self.first_issue()
        draft1 = self.root / "draft-1.md"
        draft1.write_text(self.draft0.read_text(encoding="utf-8"), encoding="utf-8")
        register_draft(self.run, draft1, 1)
        resolution = {
            "id": issue_id,
            "resolution_basis": "EDIT",
            "resolution_reason": "수정됨",
            "evidence_refs": ["drafts/round-1.md#오류-흐름"],
            "superseded_by": None,
            "merged_into": None,
        }
        with self.assertRaises(SemanticValidationError):
            ingest_review(self.run, review=review(resolved=[resolution]), review_round=2)

    def test_edit_resolution_and_metric(self) -> None:
        issue_id = self.first_issue()
        draft1 = self.root / "draft-1.md"
        draft1.write_text(
            "# 스펙\n\n## 오류 흐름\n\n실패 상태와 재시도 후 종료 상태를 정의한다.\n",
            encoding="utf-8",
        )
        register_draft(self.run, draft1, 1)
        resolution = {
            "id": issue_id,
            "resolution_basis": "EDIT",
            "resolution_reason": "복구 흐름이 추가됨",
            "evidence_refs": ["drafts/round-1.md#오류-흐름"],
            "superseded_by": None,
            "merged_into": None,
        }
        result = ingest_review(self.run, review=review(resolved=[resolution]), review_round=2)
        self.assertEqual(result["stats"]["resolved_issue_ids"], [issue_id])
        self.assertEqual(result["metrics"]["resolved_without_relevant_edit"], 0)

    def test_decision_card_has_no_scores(self) -> None:
        issue_id = self.first_issue()
        record_author_decision(
            self.run,
            issue_id=issue_id,
            round_number=1,
            disposition="REJECT",
            author_severity=2,
            claim="요청 범위 밖이다.",
            evidence_ref="request.md 사용자 원문",
            requested_disposition="DISMISS",
            argument="원문에 요구가 없다.",
            action="수정하지 않음",
        )
        card = build_feedback_cards(self.run, layout.draft(self.run, 0))
        self.assertNotIn("severity", card)
        self.assertNotIn("confidence", card)
        self.assertIn("요청 범위 밖이다", card)
        self.assertIn("## 오류 흐름", card)

    def test_reject_can_be_rereviewed_without_duplicate_draft(self) -> None:
        issue_id = self.first_issue()
        record_author_decision(
            self.run,
            issue_id=issue_id,
            round_number=1,
            disposition="REJECT",
            author_severity=2,
            claim="범위 밖",
            evidence_ref="request.md",
            requested_disposition="DISMISS",
            argument="이미 정의됨",
            action="수정하지 않음",
        )
        refresh_author_dispositions(self.run, 1)
        result = ingest_review(
            self.run,
            review=review(
                blockers=[issue(issue_id=issue_id, response="반박을 검토했으나 유지한다.")]
            ),
            review_round=2,
        )
        self.assertEqual(result["verdict"], "NEEDS_REVISION")
        manifest = read_json(layout.manifest(self.run))
        self.assertEqual(manifest["last_reviewed_draft_round"], 0)
        self.assertEqual(manifest["review_history"][-1]["draft_round"], 0)

    def test_stall_requires_two_unchanged_transitions(self) -> None:
        issue_id = self.first_issue()
        record_author_decision(
            self.run,
            issue_id=issue_id,
            round_number=1,
            disposition="REJECT",
            author_severity=2,
            claim="범위 밖",
            evidence_ref="request.md",
            requested_disposition="DISMISS",
            argument="근거",
            action="없음",
        )
        refresh_author_dispositions(self.run, 1)
        for draft_round, response in ((1, "첫 반론"), (2, "두 번째로 다른 반론")):
            source = self.root / f"draft-{draft_round}.md"
            source.write_text(self.draft0.read_text(encoding="utf-8"), encoding="utf-8")
            register_draft(self.run, source, draft_round)
            ingest_review(
                self.run,
                review=review(blockers=[issue(issue_id=issue_id, response=response)]),
                review_round=draft_round + 1,
            )
            record_author_decision(
                self.run,
                issue_id=issue_id,
                round_number=draft_round + 1,
                disposition="REJECT",
                author_severity=2,
                claim="범위 밖",
                evidence_ref="request.md",
                requested_disposition="DISMISS",
                argument="근거",
                action="없음",
            )
            refresh_author_dispositions(self.run, draft_round + 1)
            if draft_round == 1:
                resolve_user_decision(
                    self.run,
                    action="CONTINUE",
                    note="정체 신호 검증을 위해 사용자가 계속 진행을 승인함",
                )
        convergence = read_json(layout.convergence(self.run))
        self.assertFalse(convergence["rounds"][1]["issue_set_stalled"])
        self.assertTrue(convergence["rounds"][2]["issue_set_stalled"])

    def test_two_consecutive_rejects_request_escalation(self) -> None:
        issue_id = self.first_issue()
        record_author_decision(
            self.run,
            issue_id=issue_id,
            round_number=1,
            disposition="REJECT",
            author_severity=2,
            claim="범위 밖",
            evidence_ref="request.md",
            requested_disposition="DISMISS",
            argument="근거",
            action="없음",
        )
        refresh_author_dispositions(self.run, 1)
        draft1 = self.root / "deadlock-draft-1.md"
        draft1.write_text(self.draft0.read_text(encoding="utf-8"), encoding="utf-8")
        register_draft(self.run, draft1, 1)
        ingest_review(
            self.run,
            review=review(blockers=[issue(issue_id=issue_id, response="반박을 검토했으나 실패 흐름은 필요하다.")]),
            review_round=2,
        )
        record_author_decision(
            self.run,
            issue_id=issue_id,
            round_number=2,
            disposition="REJECT",
            author_severity=2,
            claim="범위 밖",
            evidence_ref="request.md",
            requested_disposition="DISMISS",
            argument="근거",
            action="없음",
        )
        refresh_author_dispositions(self.run, 2)
        manifest = read_json(layout.manifest(self.run))
        self.assertEqual(manifest["state"], "USER_DECISION_REQUIRED")
        self.assertEqual(manifest["escalation_signals"][0]["type"], "AUTHOR_DEADLOCK")
        with self.assertRaises(StateError):
            assert_run_can_advance(self.run, "review")
        resolved = resolve_user_decision(
            self.run,
            action="CONTINUE",
            note="사용자가 양측 근거를 확인하고 한 번 더 검토하기로 결정함",
        )
        self.assertEqual(resolved["from_state"], "USER_DECISION_REQUIRED")
        self.assertEqual(assert_run_can_advance(self.run, "review")["state"], "DRAFT_READY")

    def test_reviewer_storm_pauses_before_another_draft_or_review(self) -> None:
        previous_id = self.first_issue()
        record_author_decision(
            self.run,
            issue_id=previous_id,
            round_number=1,
            disposition="ACCEPT",
            author_severity=4,
            claim="수정 필요",
            evidence_ref="오류 흐름",
            requested_disposition="MODIFY",
            argument="타당함",
            action="수정",
        )
        refresh_author_dispositions(self.run, 1)
        for review_round in (2, 3):
            draft_round = review_round - 1
            source = self.root / f"storm-{draft_round}.md"
            source.write_text(
                f"# 스펙\n\n## 오류 흐름\n\n수정 {draft_round}\n",
                encoding="utf-8",
            )
            register_draft(self.run, source, draft_round)
            result = ingest_review(
                self.run,
                review=review(
                    blockers=[issue()],
                    resolved=[self._resolution(previous_id)],
                ),
                review_round=review_round,
            )
            previous_id = result["stats"]["new_issue_ids"][0]
            record_author_decision(
                self.run,
                issue_id=previous_id,
                round_number=review_round,
                disposition="ACCEPT",
                author_severity=4,
                claim="수정 필요",
                evidence_ref="오류 흐름",
                requested_disposition="MODIFY",
                argument="타당함",
                action="수정",
            )
            refresh_author_dispositions(self.run, review_round)
        manifest = read_json(layout.manifest(self.run))
        self.assertEqual(manifest["state"], "USER_DECISION_REQUIRED")
        self.assertEqual(manifest["escalation_signals"][0]["type"], "REVIEWER_STORM")
        with self.assertRaises(StateError):
            assert_run_can_advance(self.run, "review")

    def test_issue_audit_merges_duplicate(self) -> None:
        first = self.first_issue()
        registry = load_registry(self.run)
        second_issue = issue()
        registry["R1-I2"] = {
            **registry[first],
            "first_seen_round": 1,
            "latest_issue": second_issue,
        }
        from ensemble_core.io_utils import atomic_write_json

        atomic_write_json(layout.registry(self.run), registry)
        manifest = read_json(layout.manifest(self.run))
        manifest["phase"] = "3"
        manifest["state"] = "ESCALATION_REQUIRED"
        manifest["escalation_signals"] = [{"type": "REVIEWER_STORM", "round": 1}]
        atomic_write_json(layout.manifest(self.run), manifest)
        payload = {
            "issues": [
                {
                    "id": first,
                    "validity": "VALID_BLOCKER",
                    "origin": "PRE_EXISTING",
                    "relation": "UNIQUE",
                    "duplicate_of": None,
                    "reason": "유효",
                },
                {
                    "id": "R1-I2",
                    "validity": "VALID_BLOCKER",
                    "origin": "PRE_EXISTING",
                    "relation": "DUPLICATE",
                    "duplicate_of": first,
                    "reason": "중복",
                },
            ]
        }
        apply_audit(self.run, round_number=1, payload=payload)
        self.assertEqual(load_registry(self.run)["R1-I2"]["status"], "MERGED")
        manifest = read_json(layout.manifest(self.run))
        self.assertEqual(manifest["pending_panel_issue_ids"], [first])
        self.assertEqual(manifest["state"], "ESCALATION_REQUIRED")


class AcceptedRiskTests(RunCase):
    def test_acceptance_reconciles_final_blind(self) -> None:
        issue_id = self.first_issue()
        accepted = accept_risk(self.run, issue_id=issue_id, note="사용자 수용", round_number=0)
        self.assertEqual(accepted["status"], "ACCEPTED_RISK")
        result = save_final_assessment(
            self.run,
            draft_path=layout.draft(self.run, 0),
            raw_review=review(blockers=[issue()]),
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["accepted_findings"][0]["accepted_risk_id"], issue_id)

    def test_draft_change_invalidates_acceptance(self) -> None:
        issue_id = self.first_issue()
        accept_risk(self.run, issue_id=issue_id, note="사용자 수용", round_number=0)
        draft1 = self.root / "draft-1.md"
        draft1.write_text("# 스펙\n\n## 오류 흐름\n\n문서가 크게 변경됐다.\n", encoding="utf-8")
        result = register_draft(self.run, draft1, 1)
        self.assertEqual(result["invalidated_accepted_risks"], [issue_id])
        self.assertEqual(load_registry(self.run)[issue_id]["status"], "OPEN")

    def test_final_blind_raw_output_is_preserved(self) -> None:
        raw = review()
        result = save_final_assessment(
            self.run,
            draft_path=layout.draft(self.run, 0),
            raw_review=raw,
        )
        stored = read_json(Path(result["raw_review_path"]))
        self.assertEqual(stored, raw)
        self.assertTrue(result["passed"])

    def test_final_promotion_resets_reviewer_session_without_using_review_budget(self) -> None:
        ingest_review(self.run, review=review(), review_round=1)
        workspace = layout.review_sessions_dir(self.run) / "test"
        workspace.mkdir(parents=True)
        record_codex_review_session(
            self.run,
            session_id="session-before-promotion",
            review_round=1,
            workspace=workspace,
        )
        save_final_assessment(
            self.run,
            draft_path=layout.draft(self.run, 0),
            raw_review=review(blockers=[issue()]),
        )
        promote_final_findings(self.run)
        current = read_json(layout.manifest(self.run))
        self.assertEqual(current["counters"]["iterative_reviews"], 1)
        self.assertEqual(current["counters"]["promotions"], 1)
        self.assertIsNone(current["codex_review_session"])
        self.assertEqual(current["review_session_policy"]["reset_count"], 1)


class BundleAndReportTests(RunCase):
    def test_manifest_uses_agy_panel_provider(self) -> None:
        models = read_json(layout.manifest(self.run))["models"]
        self.assertIn("agy", models)
        self.assertNotIn("gemini", models)
        self.assertEqual(models["agy"]["requested"], "gemini-3.6-flash-high")
        self.assertEqual(models["agy"]["requested_reasoning_effort"], "high")

    def test_final_bundle_contains_only_blind_inputs(self) -> None:
        with isolated_bundle(
            self.run,
            mode="final",
            draft_path=layout.draft(self.run, 0),
        ) as bundle:
            self.assertEqual(
                {path.name for path in bundle.iterdir()},
                {"request.md", "rubric.md", "user-decisions.json", "draft.md"},
            )

    def test_auto_finalize_converges_after_clean_final(self) -> None:
        ingest_review(self.run, review=review(), review_round=1)
        save_final_assessment(
            self.run,
            draft_path=layout.draft(self.run, 0),
            raw_review=review(),
        )
        result = finalize(self.run, status="auto")
        self.assertEqual(result["status"], "CONVERGED")
        self.assertTrue(layout.final(self.run).exists())
        self.assertEqual(
            layout.final(self.run).read_text(encoding="utf-8").strip(),
            layout.draft(self.run, 0).read_text(encoding="utf-8").strip(),
        )
        self.assertNotIn("ensemble-status", layout.final(self.run).read_text(encoding="utf-8"))

    def test_authoritative_user_decisions_are_projected_for_reviewers(self) -> None:
        current = read_json(layout.manifest(self.run))
        current["state"] = "USER_DECISION_REQUIRED"
        atomic_write_json(layout.manifest(self.run), current)
        event = resolve_user_decision(
            self.run,
            action="REVISE",
            note="기본 경로를 확정",
            authoritative_decisions=[
                {"decision": "기본 입력은 현재 디렉터리의 .env이다.", "supersedes": []}
            ],
        )
        projection = read_json(layout.user_decisions(self.run))
        self.assertEqual(event["authoritative_decision_ids"], ["UD-001"])
        self.assertEqual(projection["decisions"][0]["source"], "USER")
        with isolated_bundle(
            self.run,
            mode="final",
            draft_path=layout.draft(self.run, 0),
        ) as bundle:
            self.assertEqual(
                read_json(bundle / "user-decisions.json")["decisions"][0]["decision_id"],
                "UD-001",
            )

    def test_benchmark_run_cannot_resume_after_user_decision_state(self) -> None:
        current = read_json(layout.manifest(self.run))
        current["state"] = "USER_DECISION_REQUIRED"
        current["benchmark"] = {
            "benchmark_run_id": "bench-1",
            "case_id": "case-1",
            "case_revision_hash": "h",
            "suite": "smoke",
            "repeat_index": 1,
        }
        atomic_write_json(layout.manifest(self.run), current)
        with self.assertRaises(StateError) as raised:
            resolve_user_decision(self.run, action="CONTINUE", note="계속")
        self.assertTrue(raised.exception.details["benchmark_case_completed"])

    def test_final_blind_attempt_budget_is_independent(self) -> None:
        for _ in range(3):
            save_final_assessment(
                self.run,
                draft_path=layout.draft(self.run, 0),
                raw_review=review(),
            )
        with self.assertRaises(StateError) as raised:
            assert_final_blind_budget(self.run)
        self.assertEqual(raised.exception.details["limit_kind"], "FINAL_BLIND_ATTEMPTS")

    def test_run_ids_are_unique(self) -> None:
        second = initialize_run("오류 처리 문서", allow_reuse=True)
        self.assertNotEqual(self.run.name, second.name)

    def test_request_becomes_immutable_after_draft(self) -> None:
        from ensemble_core.workflow import save_artifact
        structured = self.root / "request.md"
        structured.write_text(layout.request(self.run).read_text(encoding="utf-8"), encoding="utf-8")
        from ensemble_core.errors import StateError

        with self.assertRaises(StateError):
            save_artifact(self.run, kind="request", source=structured)

    def test_source_change_marks_run_tainted(self) -> None:
        with patch(
            "ensemble_core.state_machine.ensemble_source_hash",
            return_value="changed-source-hash",
        ):
            with self.assertRaises(StateError):
                assert_source_unchanged(self.run)
        self.assertEqual(read_json(layout.manifest(self.run))["state"], "RUN_TAINTED")

    def test_timeline_summarizes_review_and_draft_mapping(self) -> None:
        ingest_review(self.run, review=review(), review_round=1)
        path = write_timeline(self.run)
        rendered = path.read_text(encoding="utf-8")
        self.assertIn("리뷰 1 · 초안 0", rendered)
        self.assertIn("`APPROVED`", rendered)


class ReproducibilityTests(unittest.TestCase):
    def test_jaccard_metrics(self) -> None:
        result = reproducibility_metrics(
            [
                {"name": "a", "issue_keys": ["1", "2"], "verdict": "NEEDS_REVISION"},
                {"name": "b", "issue_keys": ["2", "3"], "verdict": "NEEDS_REVISION"},
            ]
        )
        self.assertAlmostEqual(result["mean_jaccard"], 1 / 3)
        self.assertEqual(result["verdict_agreement"], 1.0)


class ProviderCommandTests(unittest.TestCase):
    def test_legacy_gemini_manifest_record_remains_readable(self) -> None:
        from review import panel_model_record

        legacy = {"models": {"gemini": {"requested": "legacy-panel-model"}}}
        self.assertEqual(panel_model_record(legacy)["requested"], "legacy-panel-model")

    def test_agy_invocation_uses_flash_high_plan_sandbox_and_plain_stdout(self) -> None:
        from ensemble_core.providers import run_agy

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "issue.json").write_text('{"issue":"embedded"}\n', encoding="utf-8")
            schema = root / "panel.schema.json"
            schema.write_text(
                (
                    Path(__file__).resolve().parents[1]
                    / ".claude"
                    / "skills"
                    / "ensemble"
                    / "references"
                    / "panel.schema.json"
                ).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            payload = {
                "issue_id": "R1-I1",
                "severity": 4,
                "confidence": 0.9,
                "claim": "실패 흐름이 필요합니다.",
                "evidence_ref": "AC-02",
                "requested_disposition": "MODIFY",
            }
            captured: dict[str, object] = {}

            def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
                if "--version" in command:
                    return SimpleNamespace(returncode=0, stdout="1.1.5", stderr="")
                captured["command"] = command
                captured["cwd"] = kwargs.get("cwd")
                captured["input"] = kwargs.get("input")
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"```json\n{json.dumps(payload)}\n```\n",
                    stderr="",
                )

            with patch("ensemble_core.providers.shutil.which", return_value="/fake/agy"), patch(
                "ensemble_core.providers.subprocess.run", side_effect=fake_run
            ):
                result = run_agy(
                    bundle_dir=root,
                    prompt="PROMPT",
                    schema_path=schema,
                    schema_kind="panel",
                    model="gemini-3.6-flash-high",
                    effort="high",
                    retries=0,
                )

        command = captured["command"]
        self.assertEqual(command[0], "/fake/agy")
        self.assertEqual(command[command.index("--model") + 1], "gemini-3.6-flash-high")
        self.assertEqual(command[command.index("--effort") + 1], "high")
        self.assertEqual(command[command.index("--mode") + 1], "plan")
        self.assertIn("--sandbox", command)
        self.assertNotIn("--dangerously-skip-permissions", command)
        inline_prompt = command[command.index("-p") + 1]
        self.assertIn("Return JSON matching this schema exactly", inline_prompt)
        self.assertIn('{"issue":"embedded"}', inline_prompt)
        self.assertIn("Do not use tools or commands", inline_prompt)
        self.assertEqual(captured["cwd"], root)
        self.assertIsNone(captured["input"])
        self.assertEqual(result.payload, payload)
        self.assertEqual(result.reasoning_effort, "high")
        self.assertEqual([item["name"] for item in result.input_manifest], ["issue.json"])

    def test_agy_permission_denial_fails_without_repeating(self) -> None:
        from ensemble_core.providers import run_agy

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "schema.json"
            schema.write_text(
                json.dumps(
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["ok"],
                        "properties": {"ok": {"type": "boolean"}},
                    }
                ),
                encoding="utf-8",
            )
            calls = 0

            def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
                nonlocal calls
                if "--version" in command:
                    return SimpleNamespace(returncode=0, stdout="1.1.5", stderr="")
                calls += 1
                return SimpleNamespace(
                    returncode=0,
                    stdout="",
                    stderr=(
                        'jetski: no output produced — a tool required the "command" '
                        "permission that headless mode cannot prompt for, so it was auto-denied."
                    ),
                )

            with patch("ensemble_core.providers.shutil.which", return_value="/fake/agy"), patch(
                "ensemble_core.providers.subprocess.run", side_effect=fake_run
            ):
                with self.assertRaises(InfraError) as raised:
                    run_agy(
                        bundle_dir=root,
                        prompt="PROMPT",
                        schema_path=schema,
                        schema_kind="test",
                        model="m",
                        retries=2,
                    )
        self.assertEqual(calls, 1)
        self.assertEqual(raised.exception.details["last_error"]["kind"], "permission_denied")

    def test_codex_invocation_uses_isolation_flags_and_stdin(self) -> None:
        from ensemble_core.providers import run_codex

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "proposal.schema.json"
            schema.write_text(
                (Path(__file__).resolve().parents[1] / ".claude" / "skills" / "ensemble" / "references" / "proposal.schema.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            payload = {
                "goal": "목표",
                "sections": [],
                "requirements": [],
                "assumptions": [],
                "risks": [],
                "user_decisions": [],
            }
            captured: dict[str, object] = {}

            def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
                if "--version" in command:
                    return SimpleNamespace(returncode=0, stdout="codex-cli test", stderr="")
                captured["command"] = command
                captured["input"] = kwargs.get("input")
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text(json.dumps(payload), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("ensemble_core.providers.shutil.which", return_value="/fake/codex"), patch(
                "ensemble_core.providers.subprocess.run", side_effect=fake_run
            ):
                result = run_codex(
                    bundle_dir=root,
                    prompt="PROMPT",
                    schema_path=schema,
                    schema_kind="proposal",
                    model="test-model",
                    retries=0,
                )
            command = captured["command"]
            self.assertIn("--ephemeral", command)
            self.assertIn("--ignore-user-config", command)
            self.assertIn("--skip-git-repo-check", command)
            self.assertIn("read-only", command)
            self.assertEqual(captured["input"], "PROMPT")
            self.assertEqual(result.payload, payload)
            self.assertIn("model_reasoning_effort=high", command)
            self.assertEqual(result.reasoning_effort, "high")

    def test_failed_codex_call_keeps_usage_from_every_attempt(self) -> None:
        from ensemble_core.providers import run_codex

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "schema.json"
            schema.write_text(
                json.dumps(
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["ok"],
                        "properties": {"ok": {"type": "boolean"}},
                    }
                ),
                encoding="utf-8",
            )
            attempt = 0

            def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
                nonlocal attempt
                if "--version" in command:
                    return SimpleNamespace(returncode=0, stdout="codex-cli test", stderr="")
                attempt += 1
                event = {
                    "type": "turn.completed",
                    "usage": {"input_tokens": attempt * 10, "output_tokens": attempt},
                }
                return SimpleNamespace(
                    returncode=1,
                    stdout=json.dumps(event),
                    stderr="failed",
                )

            with patch("ensemble_core.providers.shutil.which", return_value="/fake/codex"), patch(
                "ensemble_core.providers.subprocess.run", side_effect=fake_run
            ):
                with self.assertRaises(InfraError) as raised:
                    run_codex(
                        bundle_dir=root,
                        prompt="PROMPT",
                        schema_path=schema,
                        schema_kind="test",
                        model="m",
                        retries=1,
                    )
        self.assertEqual(raised.exception.details["usage"]["input_tokens"], 30)
        self.assertEqual(raised.exception.details["usage"]["output_tokens"], 3)
        self.assertEqual(raised.exception.details["attempts_reported"], 2)

    def test_codex_review_session_is_created_then_resumed(self) -> None:
        from ensemble_core.providers import run_codex

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema = root / "proposal.schema.json"
            schema.write_text(
                (Path(__file__).resolve().parents[1] / ".claude" / "skills" / "ensemble" / "references" / "proposal.schema.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            payload = {
                "goal": "목표",
                "sections": [],
                "requirements": [],
                "assumptions": [],
                "risks": [],
                "user_decisions": [],
            }
            commands: list[list[str]] = []
            cwds: list[object] = []

            def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
                if "--version" in command:
                    return SimpleNamespace(returncode=0, stdout="codex-cli test", stderr="")
                commands.append(command)
                cwds.append(kwargs.get("cwd"))
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text(json.dumps(payload), encoding="utf-8")
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"type": "thread.started", "thread_id": "session-123"}),
                    stderr="",
                )

            with patch("ensemble_core.providers.shutil.which", return_value="/fake/codex"), patch(
                "ensemble_core.providers.subprocess.run", side_effect=fake_run
            ):
                created = run_codex(
                    bundle_dir=root,
                    prompt="ROUND 1",
                    schema_path=schema,
                    schema_kind="proposal",
                    model="test-model",
                    retries=0,
                    persist_session=True,
                )
                resumed = run_codex(
                    bundle_dir=root,
                    prompt="ROUND 2",
                    schema_path=schema,
                    schema_kind="proposal",
                    model="test-model",
                    retries=0,
                    persist_session=True,
                    session_id=created.session_id,
                )

        self.assertNotIn("--ephemeral", commands[0])
        self.assertIn("--json", commands[0])
        self.assertNotIn("resume", commands[0])
        self.assertEqual(created.session_id, "session-123")
        self.assertFalse(created.session_resumed)
        self.assertIn("resume", commands[1])
        self.assertIn("session-123", commands[1])
        self.assertNotIn("-C", commands[1])
        self.assertNotIn("--sandbox", commands[1])
        self.assertTrue(resumed.session_resumed)
        # `codex exec resume` has no `-C`, so the bundle must be the process cwd.
        # Otherwise the resumed session reads whatever directory Ensemble runs from.
        self.assertEqual(cwds, [root, root])

    def test_provider_call_records_reasoning_effort_in_manifest(self) -> None:
        from ensemble_core.state_machine import record_provider_call

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            layout.manifest(run_dir).parent.mkdir(parents=True, exist_ok=True)
            layout.manifest(run_dir).write_text(
                json.dumps({"models": {"codex": {"requested": "test-model"}}}),
                encoding="utf-8",
            )
            result = SimpleNamespace(
                model="test-model",
                version="codex-cli test",
                executable="/fake/codex",
                attempts=1,
                attempt_errors=(),
                reasoning_effort="high",
            )
            record_provider_call(run_dir, provider="codex", operation="review", result=result)
            manifest = json.loads(layout.manifest(run_dir).read_text(encoding="utf-8"))

        self.assertEqual(manifest["models"]["codex"]["reasoning_effort"], "high")
        self.assertEqual(manifest["provider_calls"][0]["reasoning_effort"], "high")

    def test_panel_card_uses_agy_provider_key(self) -> None:
        assessment = {
            "severity": 4,
            "confidence": 0.9,
            "claim": "검토 필요",
            "evidence_ref": "AC-02",
            "requested_disposition": "MODIFY",
        }
        card = build_panel_card(
            "R1-I1",
            {"gpt": assessment, "agy": assessment},
            {
                "value": "REJECT",
                "claim": "반박",
                "evidence_ref": "AC-02",
                "requested_disposition": "DISMISS",
            },
        )
        self.assertIn("AGY 4", card)


class ReviewSessionTests(RunCase):
    def test_review_session_bundle_rejects_allowed_name_symlink(self) -> None:
        request_hash = verified_request_hash(self.run)
        workspace = layout.review_session(self.run, request_hash)
        workspace.mkdir(parents=True)
        outside = self.root / "outside.md"
        outside.write_text("보호할 내용", encoding="utf-8")
        (workspace / "request.md").symlink_to(outside)

        with self.assertRaises(SecurityError):
            prepare_review_session_bundle(
                self.run,
                draft_path=layout.draft(self.run, 0),
                request_hash=request_hash,
            )
        self.assertEqual(outside.read_text(encoding="utf-8"), "보호할 내용")

    def test_review_session_record_is_scoped_to_the_same_request_and_run(self) -> None:
        request_hash = verified_request_hash(self.run)
        workspace = prepare_review_session_bundle(
            self.run,
            draft_path=layout.draft(self.run, 0),
            request_hash=request_hash,
        )
        record_codex_review_session(
            self.run,
            session_id="session-123",
            review_round=1,
            workspace=workspace,
        )
        self.assertEqual(load_codex_review_session(self.run)["session_id"], "session-123")

        manifest = read_json(layout.manifest(self.run))
        manifest["codex_review_session"]["request_hash"] = "0" * 64
        atomic_write_json(layout.manifest(self.run), manifest)
        with self.assertRaises(StateError):
            load_codex_review_session(self.run)

    def test_review_rounds_resume_the_recorded_session(self) -> None:
        received_session_ids: list[str | None] = []

        def fake_codex(**kwargs: object) -> ProviderResult:
            prior_session_id = kwargs.get("session_id")
            received_session_ids.append(prior_session_id if isinstance(prior_session_id, str) else None)
            return ProviderResult(
                payload=review(),
                stdout="",
                stderr="",
                attempts=1,
                executable="/fake/codex",
                version="codex-cli test",
                model="test-model",
                reasoning_effort="high",
                session_id="session-123",
                session_resumed=prior_session_id is not None,
            )

        with patch("ensemble_core.workflow.run_codex", side_effect=fake_codex):
            first, _ = run_review(
                self.run,
                review_round=1,
                draft_round=0,
                model="test-model",
                timeout=30,
            )
            second, _ = run_review(
                self.run,
                review_round=2,
                draft_round=0,
                model="test-model",
                timeout=30,
            )

        self.assertEqual(received_session_ids, [None, "session-123"])
        self.assertFalse(first.session_resumed)
        self.assertTrue(second.session_resumed)
        manifest = read_json(layout.manifest(self.run))
        self.assertEqual(manifest["codex_review_session"]["last_review_round"], 2)
        self.assertEqual(manifest["codex_review_session"]["request_hash"], verified_request_hash(self.run))


class LayoutTests(RunCase):
    """실행 디렉토리 구조를 고정한다.

    경로를 옮길 때 코드가 조용히 다른 곳에 쓰는 것을 막는다. 여기서
    확인하는 것은 `layout`이 돌려주는 경로가 아니라 실제로 생긴 파일의
    위치다. 그래야 `layout`만 고치고 실제 쓰기를 빠뜨린 경우를 잡는다.
    """

    def relative(self, path: Path) -> str:
        return path.relative_to(self.run).as_posix()

    def test_init_places_input_and_state(self) -> None:
        self.assertEqual(self.relative(layout.request(self.run)), "01-input/request.md")
        self.assertEqual(self.relative(layout.request_original(self.run)), "01-input/request.original.txt")
        self.assertEqual(self.relative(layout.rubric(self.run)), "01-input/rubric.md")
        self.assertEqual(self.relative(layout.manifest(self.run)), "_state/manifest.json")
        self.assertEqual(self.relative(layout.registry(self.run)), "_state/issue-registry.json")
        self.assertEqual(self.relative(layout.reviewer_index(self.run)), "_state/reviewer-issue-index.json")
        self.assertEqual(self.relative(layout.convergence(self.run)), "_state/convergence.json")
        self.assertEqual(self.relative(layout.feedback_cards(self.run)), "_state/feedback-cards.md")
        for path in (
            layout.request(self.run),
            layout.rubric(self.run),
            layout.manifest(self.run),
            layout.feedback_cards(self.run),
        ):
            self.assertTrue(path.exists(), path)

    def test_human_facing_files_stay_at_the_root(self) -> None:
        ingest_review(self.run, review=review(), review_round=1)
        save_final_assessment(self.run, draft_path=layout.draft(self.run, 0), raw_review=review())
        finalize(self.run, status="auto")
        write_timeline(self.run)
        self.assertEqual(self.relative(layout.final(self.run)), "final.md")
        self.assertEqual(self.relative(layout.timeline(self.run)), "timeline.md")
        self.assertEqual(self.relative(layout.decisions(self.run)), "decisions.md")
        for path in (layout.final(self.run), layout.timeline(self.run), layout.decisions(self.run)):
            self.assertTrue(path.exists(), path)

    def test_draft_and_hashes_use_padded_numbers(self) -> None:
        self.assertEqual(self.relative(layout.draft(self.run, 0)), "03-drafts/draft-00.md")
        self.assertEqual(self.relative(layout.hashes(self.run, 0)), "_state/hashes/draft-00.json")
        self.assertTrue(layout.draft(self.run, 0).exists())
        self.assertTrue(layout.hashes(self.run, 0).exists())

    def test_review_kinds_go_to_separate_folders(self) -> None:
        ingest_review(self.run, review=review(blockers=[issue()]), review_round=1)
        self.assertEqual(self.relative(layout.review(self.run, 1)), "04-reviews/iterative/r01.json")
        self.assertTrue(layout.review(self.run, 1).exists())

        save_final_assessment(self.run, draft_path=layout.draft(self.run, 0), raw_review=review())
        self.assertEqual(self.relative(layout.blind(self.run, 0)), "04-reviews/blind/draft-00.json")
        self.assertEqual(
            self.relative(layout.reconciliation(self.run, 0)),
            "04-reviews/reconciliation/draft-00.json",
        )
        self.assertTrue(layout.blind(self.run, 0).exists())
        self.assertTrue(layout.reconciliation(self.run, 0).exists())
        self.assertTrue(layout.final_reconciliation(self.run).exists())
        self.assertEqual(
            self.relative(layout.final_reconciliation(self.run)),
            "_state/final-reconciliation.json",
        )

        self.assertEqual(self.relative(layout.promoted(self.run, 2)), "04-reviews/promoted/r02.json")
        self.assertEqual(self.relative(layout.issue_audit(self.run, 2)), "04-reviews/audit/r02.json")
        self.assertEqual(self.relative(layout.panel_issue(self.run, "R1-I1")), "04-reviews/panel/R1-I1")

    def test_issue_audit_is_not_picked_up_as_an_iterative_review(self) -> None:
        ingest_review(self.run, review=review(), review_round=1)
        atomic_write_json(layout.issue_audit(self.run, 1), {"merged": []})
        self.assertEqual(layout.iter_reviews(self.run), [layout.review(self.run, 1)])

    def test_internal_artifacts_are_separated_from_state(self) -> None:
        self.assertEqual(self.relative(layout.bundles_dir(self.run)), "_internal/bundles")
        self.assertEqual(self.relative(layout.review_sessions_dir(self.run)), "_internal/review-sessions")
        self.assertEqual(self.relative(layout.noise_dir(self.run)), "_internal/noise")

    def test_rounds_sort_by_number_not_by_name(self) -> None:
        for number in (1, 2, 10):
            source = self.root / f"draft-{number}.md"
            source.write_text(f"# 스펙 {number}\n\n## 오류 흐름\n\n내용\n", encoding="utf-8")
            register_draft(self.run, source, number)
        self.assertEqual([layout.round_of(path) for path in layout.iter_drafts(self.run)], [0, 1, 2, 10])

    def test_new_runs_record_the_layout_version(self) -> None:
        self.assertEqual(read_json(layout.manifest(self.run))["layout_version"], layout.LAYOUT_VERSION)

    def test_readme_is_written_next_to_the_timeline(self) -> None:
        ingest_review(self.run, review=review(blockers=[issue()]), review_round=1)
        write_timeline(self.run)
        readme = layout.readme(self.run)
        self.assertEqual(self.relative(readme), "README.md")
        text = readme.read_text(encoding="utf-8")
        self.assertIn(self.run.name, text)
        self.assertIn("미해결 이슈 1건", text)
        self.assertIn("| 1 | 초안 0 | `NEEDS_REVISION` |", text)

    def test_readme_reports_rounds_that_left_no_iterative_file(self) -> None:
        """승격이 쓴 회차는 `iterative/`에 파일이 없어도 경과에 남아야 한다."""
        ingest_review(self.run, review=review(), review_round=1)
        manifest = read_json(layout.manifest(self.run))
        manifest["review_history"].append(
            {"review_round": 2, "draft_round": 0, "verdict": "NEEDS_REVISION"}
        )
        atomic_write_json(layout.manifest(self.run), manifest)
        write_timeline(self.run)
        self.assertFalse(layout.review(self.run, 2).exists())
        self.assertIn("| 2 | 초안 0 | `NEEDS_REVISION` |", layout.readme(self.run).read_text(encoding="utf-8"))


class LegacyRunTests(unittest.TestCase):
    def test_v1_run_is_rejected_with_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs = Path(temporary) / "runs"
            legacy = runs / "20260101T000000Z-spec-abc12345"
            legacy.mkdir(parents=True)
            (legacy / "manifest.json").write_text(
                json.dumps({"state": "USER_DECISION_REQUIRED"}), encoding="utf-8"
            )
            (legacy / "final.md").write_text("# 최종\n", encoding="utf-8")
            with patch("ensemble_core.io_utils.RUNS_ROOT", runs):
                with self.assertRaises(StateError) as caught:
                    resolve_run(legacy)
        details = caught.exception.details
        self.assertEqual(details["layout_version"], 1)
        self.assertEqual(details["last_state"], "USER_DECISION_REQUIRED")
        self.assertTrue(details["final"].endswith("final.md"))
        self.assertIsNone(details["timeline"])

    def test_v1_state_files_are_not_used_to_serve_commands(self) -> None:
        """안내만 하고 v1 상태를 읽어 명령을 처리하지는 않는다."""
        with tempfile.TemporaryDirectory() as temporary:
            runs = Path(temporary) / "runs"
            legacy = runs / "20260101T000000Z-spec-abc12345"
            legacy.mkdir(parents=True)
            (legacy / "manifest.json").write_text(json.dumps({"state": "APPROVED"}), encoding="utf-8")
            (legacy / "issue-registry.json").write_text("{}", encoding="utf-8")
            with patch("ensemble_core.io_utils.RUNS_ROOT", runs):
                with self.assertRaises(StateError):
                    resolve_run(legacy)


if __name__ == "__main__":
    unittest.main()

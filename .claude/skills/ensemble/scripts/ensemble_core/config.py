from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[5]
SKILL_ROOT = PROJECT_ROOT / ".claude" / "skills" / "ensemble"
REFERENCE_ROOT = SKILL_ROOT / "references"
RUNS_ROOT = PROJECT_ROOT / "ensemble" / "runs"

DEFAULT_REVIEW_MODEL = os.environ.get("CODEX_REVIEW_MODEL", "gpt-5.6-sol")
DEFAULT_PANEL_MODEL = os.environ.get("GEMINI_PANEL_MODEL", "gemini-2.5-pro")
DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_MAX_ROUNDS = 8
DEFAULT_MAX_PANEL_CALLS = 3
INFRA_RETRIES = 2
SEMANTIC_RETRIES = 2

ISSUE_ID_PATTERN = r"^R\d+-I\d+$"
CRITERIA: dict[str, str] = {
    "AC-01": "사용자 원문의 필수 기능이 모두 문서에 명시되어야 한다",
    "AC-02": "각 주요 흐름에 성공 상태와 실패 상태가 정의되어야 한다",
    "AC-03": "구현 완료 여부를 판정할 관찰 가능한 조건이 있어야 한다",
    "AC-04": "같은 개념이 문서 전체에서 같은 용어로 지칭되어야 한다",
    "AC-05": "명시된 제약 안에서 기술적으로 구현 가능해야 한다",
    "AC-06": "개인정보·인증·권한 처리 방식이 정의되어야 한다",
    "AC-07": "사실 주장과 가정이 구분되어 표시되어야 한다",
    "AC-08": "이전 라운드에서 충족된 기준이 회귀하지 않아야 한다",
    "AC-09": "원문에 없는 요구가 확정된 사실로 추가되지 않아야 한다",
}

OPEN_STATUSES = {"OPEN", "UNVERIFIED", "BILATERAL_DEADLOCK", "PANEL_DISSENT"}
CLOSED_STATUSES = {"RESOLVED", "SUPERSEDED", "MERGED"}
ALL_ISSUE_STATUSES = OPEN_STATUSES | CLOSED_STATUSES | {"ACCEPTED_RISK"}
TERMINAL_STATES = {
    "CONVERGED",
    "STABLE_DISSENT",
    "USER_DECISION_REQUIRED",
    "CANCELLED",
    "OSCILLATING",
    "PROTOTYPE_INCOMPLETE",
    "ITERATION_LIMIT_REACHED",
    "INFRA_ERROR",
}

REVIEW_BUNDLE_ALLOWLIST = {
    "request.md",
    "rubric.md",
    "draft.md",
    "reviewer-issue-index.json",
    "feedback-cards.md",
}
FINAL_BUNDLE_ALLOWLIST = {"request.md", "rubric.md", "draft.md"}
PROPOSAL_BUNDLE_ALLOWLIST = {"request.md", "rubric.md"}
PANEL_BUNDLE_ALLOWLIST = {"request.md", "rubric.md", "draft.md", "issue.json"}
AUDIT_BUNDLE_ALLOWLIST = {
    "request.md",
    "rubric.md",
    "draft.md",
    "previous-draft.md",
    "new-issues.json",
}

FORBIDDEN_REVIEW_FILES = {
    "issue-registry.json",
    "decisions.md",
    "convergence.json",
    "manifest.json",
}

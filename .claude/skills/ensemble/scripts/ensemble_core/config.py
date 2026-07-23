from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[5]
SKILL_ROOT = PROJECT_ROOT / ".claude" / "skills" / "ensemble"
REFERENCE_ROOT = SKILL_ROOT / "references"
RUNS_ROOT = PROJECT_ROOT / "ensemble" / "runs"
EVAL_ROOT = PROJECT_ROOT / "eval"
EVAL_CASES_ROOT = EVAL_ROOT / "cases"
EVAL_RESULTS_ROOT = EVAL_ROOT / "results"


def _claude_transcript_root() -> Path:
    """Claude Code가 이 프로젝트의 세션 기록을 쌓는 폴더.

    작성자(Claude)는 CLI가 아니라 스킬을 실행하는 주체라서 run_codex 같은
    수집 지점이 없다. API 응답이 보고한 실측 토큰은 이 세션 기록에만 남는다.
    폴더 이름은 프로젝트 절대 경로의 구분자를 `-`로 바꾼 것이다.
    """
    override = os.environ.get("ENSEMBLE_CLAUDE_TRANSCRIPT_DIR")
    if override:
        return Path(override)
    config_home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    return config_home / "projects" / str(PROJECT_ROOT).replace("/", "-")


CLAUDE_TRANSCRIPT_ROOT = _claude_transcript_root()

DEFAULT_REVIEW_MODEL = os.environ.get("CODEX_REVIEW_MODEL", "gpt-5.6-sol")
DEFAULT_PANEL_MODEL = os.environ.get("ANTIGRAVITY_PANEL_MODEL", "gemini-3.6-flash-high")
# Codex runs with --ignore-user-config for reproducibility, so the user's
# model_reasoning_effort never applies. Without an explicit value the CLI falls
# back to "none", which is below this model's own default. Set it here instead.
DEFAULT_REVIEW_EFFORT = os.environ.get("CODEX_REVIEW_EFFORT", "high")
DEFAULT_PANEL_EFFORT = os.environ.get("ANTIGRAVITY_PANEL_EFFORT", "high")
DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_MAX_ROUNDS = 5
DEFAULT_MAX_FINAL_BLIND_ATTEMPTS = 2
DEFAULT_MAX_TOTAL_PROVIDER_CALLS = 16
DEFAULT_MAX_PANEL_CALLS = 3
DEFAULT_REVIEW_SESSION_MAX_ROUNDS = 3
AGY_INLINE_MAX_BYTES = 256 * 1024
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
    "RUN_TAINTED",
}

PAUSED_STATES = {"USER_DECISION_REQUIRED", "ESCALATION_REQUIRED"}

REVIEW_BUNDLE_ALLOWLIST = {
    "request.md",
    "rubric.md",
    "user-decisions.json",
    "draft.md",
    "reviewer-issue-index.json",
    "feedback-cards.md",
}
FINAL_BUNDLE_ALLOWLIST = {"request.md", "rubric.md", "user-decisions.json", "draft.md"}
PROPOSAL_BUNDLE_ALLOWLIST = {"request.md", "rubric.md"}
PANEL_BUNDLE_ALLOWLIST = {
    "request.md",
    "rubric.md",
    "user-decisions.json",
    "draft.md",
    "issue.json",
}
AUDIT_BUNDLE_ALLOWLIST = {
    "request.md",
    "rubric.md",
    "user-decisions.json",
    "draft.md",
    "previous-draft.md",
    "new-issues.json",
}

# codex-cli 0.145.0의 `turn.completed` 이벤트가 보고하는 사용량 필드.
# 지금 안 쓰는 값이라도 버리면 과거 실행에서 복구할 수 없으므로 모두 남긴다.
USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

# 심판 입력에는 어느 쪽이 최종본인지 알려 줄 파일을 넣지 않는다.
JUDGE_BUNDLE_ALLOWLIST = {
    "request.md",
    "rubric.md",
    "user-decisions.json",
    "document-1.md",
    "document-2.md",
}
JUDGE_EXPECTATIONS_BUNDLE_ALLOWLIST = {
    "request.md",
    "rubric.md",
    "user-decisions.json",
    "document.md",
    "expectations.json",
}

# 비교 채점 축. 점수 척도 대신 축별 승자만 고른다.
JUDGE_AXES = (
    "testable_criteria",
    "internal_consistency",
    "requirement_coverage",
    "over_specification",
    "overall",
)

FORBIDDEN_REVIEW_FILES = {
    "issue-registry.json",
    "decisions.md",
    "convergence.json",
    "manifest.json",
}

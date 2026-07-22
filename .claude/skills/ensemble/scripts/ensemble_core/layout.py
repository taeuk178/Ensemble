"""실행 디렉토리 안의 모든 경로를 여기서만 만든다.

다른 모듈은 `run_dir`에 이름을 직접 붙이지 않는다. 레이아웃을 바꿀 때
고칠 곳이 이 파일 하나가 되도록 한다. 검토자에게 넘기는 입력 묶음
(`bundle_dir`) 안의 파일명은 프롬프트와의 계약이므로 여기서 다루지 않는다.
"""

from __future__ import annotations

import re
from pathlib import Path


# 새 실행에 기록하는 레이아웃 버전. 구조를 바꿀 때 올린다.
LAYOUT_VERSION = 2

# init이 미리 만들어 두는 하위 디렉토리.
RUN_SUBDIRS = (
    "01-input",
    "02-proposals",
    "03-drafts",
    "04-reviews/iterative",
    "04-reviews/blind",
    "04-reviews/promoted",
    "04-reviews/audit",
    "04-reviews/reconciliation",
    "04-reviews/panel",
    "_state/hashes",
    "_internal/bundles",
)


def round_of(path: Path) -> int:
    """회차 번호를 파일명에서 읽는다.

    파일명 형식도 레이아웃의 일부이므로 파싱을 여기 모아 둔다. 재시도
    접미사가 붙는 독립 검토 파일에는 쓰지 않는다. 끝의 숫자가 회차가
    아니라 시도 번호이기 때문이다.
    """
    match = re.search(r"(\d+)$", path.stem)
    if match is None:
        raise ValueError(f"회차 번호가 없는 파일명입니다: {path.name}")
    return int(match.group(1))


def _by_round(paths: list[Path]) -> list[Path]:
    """사전순으로 정렬하면 round-10이 round-2보다 앞에 온다."""
    return sorted(paths, key=round_of)


# --- 입력 -------------------------------------------------------------

def request(run_dir: Path) -> Path:
    return run_dir / "01-input" / "request.md"


def request_original(run_dir: Path) -> Path:
    return run_dir / "01-input" / "request.original.txt"


def rubric(run_dir: Path) -> Path:
    return run_dir / "01-input" / "rubric.md"


# --- 제안 -------------------------------------------------------------

def proposal(run_dir: Path, name: str) -> Path:
    return run_dir / "02-proposals" / name


# --- 초안 -------------------------------------------------------------

def draft(run_dir: Path, round_number: int) -> Path:
    return run_dir / "03-drafts" / f"draft-{round_number:02d}.md"


def iter_drafts(run_dir: Path) -> list[Path]:
    return _by_round(list((run_dir / "03-drafts").glob("draft-*.md")))


# --- 검토 -------------------------------------------------------------

def review(run_dir: Path, review_round: int) -> Path:
    return run_dir / "04-reviews" / "iterative" / f"r{review_round:02d}.json"


def iter_reviews(run_dir: Path) -> list[Path]:
    return _by_round(list((run_dir / "04-reviews" / "iterative").glob("r*.json")))


def blind(run_dir: Path, draft_round: int, suffix: str = "") -> Path:
    return run_dir / "04-reviews" / "blind" / f"draft-{draft_round:02d}{suffix}.json"


def iter_blind_attempts(run_dir: Path, draft_round: int) -> list[Path]:
    """같은 초안을 두 번 이상 독립 검토했을 때 쌓인 파일들."""
    return sorted((run_dir / "04-reviews" / "blind").glob(f"draft-{draft_round:02d}*.json"))


def iter_blinds(run_dir: Path) -> list[Path]:
    return sorted((run_dir / "04-reviews" / "blind").glob("draft-*.json"))


def promoted(run_dir: Path, review_round: int) -> Path:
    return run_dir / "04-reviews" / "promoted" / f"r{review_round:02d}.json"


def iter_promoted(run_dir: Path) -> list[Path]:
    return _by_round(list((run_dir / "04-reviews" / "promoted").glob("r*.json")))


def reconciliation(run_dir: Path, draft_round: int, suffix: str = "") -> Path:
    return run_dir / "04-reviews" / "reconciliation" / f"draft-{draft_round:02d}{suffix}.json"


def issue_audit(run_dir: Path, review_round: int) -> Path:
    return run_dir / "04-reviews" / "audit" / f"r{review_round:02d}.json"


def panel_issue(run_dir: Path, issue_id: str) -> Path:
    return run_dir / "04-reviews" / "panel" / issue_id


# --- 실행 상태 --------------------------------------------------------

def manifest(run_dir: Path) -> Path:
    return run_dir / "_state" / "manifest.json"


def registry(run_dir: Path) -> Path:
    return run_dir / "_state" / "issue-registry.json"


def reviewer_index(run_dir: Path) -> Path:
    return run_dir / "_state" / "reviewer-issue-index.json"


def convergence(run_dir: Path) -> Path:
    return run_dir / "_state" / "convergence.json"


def feedback_cards(run_dir: Path) -> Path:
    return run_dir / "_state" / "feedback-cards.md"


def final_reconciliation(run_dir: Path) -> Path:
    """최신 종료 판정용 사본. 회차별 원본은 `reconciliation()`에 있다."""
    return run_dir / "_state" / "final-reconciliation.json"


def hashes(run_dir: Path, round_number: int) -> Path:
    return hashes_dir(run_dir) / f"draft-{round_number:02d}.json"


def hashes_dir(run_dir: Path) -> Path:
    return run_dir / "_state" / "hashes"


def iter_hashes(run_dir: Path) -> list[Path]:
    return _by_round(list(hashes_dir(run_dir).glob("draft-*.json")))


# --- 사람이 읽는 결과 -------------------------------------------------

def final(run_dir: Path) -> Path:
    return run_dir / "final.md"


def timeline(run_dir: Path) -> Path:
    return run_dir / "timeline.md"


def decisions(run_dir: Path) -> Path:
    return run_dir / "decisions.md"


# --- 내부 산출물 ------------------------------------------------------

def bundles_dir(run_dir: Path) -> Path:
    return run_dir / "_internal" / "bundles"


def review_sessions_dir(run_dir: Path) -> Path:
    return run_dir / "_internal" / "review-sessions"


def review_session(run_dir: Path, request_hash: str) -> Path:
    return review_sessions_dir(run_dir) / request_hash


def noise_dir(run_dir: Path) -> Path:
    return run_dir / "_internal" / "noise"

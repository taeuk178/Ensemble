# Ensemble

Claude Code를 작성자·오케스트레이터로, Codex CLI의 GPT를 외부 리뷰어로 사용하는 문서 스펙 앙상블입니다. 실행 상태와 이슈 ID, 스키마 검증, 격리 bundle, 해시 및 종료 판정은 Python 래퍼가 결정적으로 관리합니다.

설계 근거와 전체 규범은 [`handoff.md`](handoff.md)에 있습니다.

## 구성

- `.claude/skills/ensemble/SKILL.md`: Claude Code `/ensemble` 워크플로
- `.claude/skills/ensemble/scripts/review.py`: 결정적 CLI 진입점
- `.claude/skills/ensemble/scripts/ensemble_core/`: provider·registry·validation·bundle·state machine 등 분리 모듈
- `.claude/skills/ensemble/references/`: 프롬프트와 JSON Schema
- `.claude/skills/ensemble/fixtures/`: 재현성 측정용 고정 fixture
- `ensemble/runs/`: 실행 산출물. Git에서 제외됨

Python 3.10 이상과 로그인된 Codex CLI가 필요합니다. `jsonschema`가 설치되어 있으면 표준 JSON Schema 검증을 추가로 사용하며, 설치되어 있지 않아도 내장된 엄격 검증기가 동일한 핵심 제약을 검사합니다.

```bash
python3 -m pip install -r .claude/skills/ensemble/requirements.txt
```

## 빠른 확인

```bash
python3 .claude/skills/ensemble/scripts/review.py preflight
python3 -m unittest discover -s tests -v
```

실제 모델까지 확인하려면 비용이 발생할 수 있는 live preflight를 명시적으로 실행합니다.

```bash
python3 .claude/skills/ensemble/scripts/review.py preflight --live
```

## 기본 흐름

Claude Code에서 `/ensemble`을 실행하는 것이 기본 사용법입니다. 래퍼를 직접 사용할 때는 사용자 원문을 셸 인자로 보간하지 말고 파일로 전달하는 방식을 권장합니다.

```bash
python3 .claude/skills/ensemble/scripts/review.py init --request-file /tmp/request.txt
```

명령이 반환한 `run_dir`를 사용해 진행합니다.

```bash
python3 .claude/skills/ensemble/scripts/review.py save --run <run_dir> --kind claude-proposal --source /tmp/claude-proposal.md
python3 .claude/skills/ensemble/scripts/review.py propose --run <run_dir>
python3 .claude/skills/ensemble/scripts/review.py save --run <run_dir> --kind draft --round 0 --source /tmp/draft.md
python3 .claude/skills/ensemble/scripts/review.py review --run <run_dir> --round 1
python3 .claude/skills/ensemble/scripts/review.py final-blind --run <run_dir>
python3 .claude/skills/ensemble/scripts/review.py finalize --run <run_dir> --status auto
```

모델을 호출하지 않고 저장된 JSON을 검증·반영하려면 `propose`, `review`, `final-blind`, `issue-audit`에 `--input <json>`을 지정할 수 있습니다. 이 경로는 테스트와 재현에 사용합니다.

## 주요 안전 규칙

- 일반 리뷰 bundle에는 `issue-registry.json`, `decisions.md`, 점수 이력이 들어가지 않습니다.
- FINAL_BLIND는 request·rubric·draft만 받으며, 수용 위험은 평가 후 래퍼가 별도로 대조합니다.
- 리뷰 파일과 draft 스냅샷은 덮어쓰지 않습니다.
- 반복 상한은 승인 상태가 아니라 `ITERATION_LIMIT_REACHED`입니다.
- Gemini가 없으면 교착을 임의 판정하지 않고 `USER_DECISION_REQUIRED`로 이관합니다.
- `.env`, 토큰, 개인 키로 보이는 패턴이 요청에 있으면 초기화를 차단합니다.

## 관측 및 패널

```bash
python3 .claude/skills/ensemble/scripts/review.py measure-noise --run <run_dir> --repetitions 3
python3 .claude/skills/ensemble/scripts/review.py issue-audit --run <run_dir> --round <N>
python3 .claude/skills/ensemble/scripts/review.py panel --run <run_dir> --issue <R1-I1>
```

수렴 지표는 보고·에스컬레이션 신호일 뿐 성공 종료를 만들지 않습니다.

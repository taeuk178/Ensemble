# Ensemble

사용자의 요청을 구현 가능한 명세로 다듬는 도구입니다. Claude가 초안을 만들고 GPT가 독립적으로 검토합니다. Python 스크립트는 파일 저장, 검토 순서, 이슈 추적, 종료 조건을 일관되게 관리합니다.

자세한 설계와 판단 기준은 [`handoff.md`](handoff.md)에 있습니다.

## 구성

- `.claude/skills/ensemble/SKILL.md`: Claude Code에서 실행하는 `/ensemble` 절차
- `.claude/skills/ensemble/scripts/review.py`: 전체 작업을 실행하는 CLI
- `.claude/skills/ensemble/scripts/ensemble_core/`: 모델 호출, 검증, 이슈 관리, 상태 관리 모듈
- `.claude/skills/ensemble/references/`: 모델 프롬프트와 JSON Schema
- `.claude/skills/ensemble/fixtures/`: 결과 일관성을 확인하는 고정 예제
- `ensemble/runs/`: 실행별 결과물. Git에는 포함하지 않음

각 실행 폴더의 `timeline.md`에서 제안, 검토한 초안, 이슈별 판단, 사용자 결정, 최종 상태를 시간순으로 볼 수 있습니다. 원문은 `proposals/`, `reviews/`, `drafts/`, `issue-registry.json`, `decisions.md`에 보관됩니다.

기존 실행의 타임라인을 갱신하려면 다음 명령을 사용합니다.

```bash
python3 .claude/skills/ensemble/scripts/review.py timeline --run <run_dir>
```

## 준비

Python 3.10 이상과 로그인된 Codex CLI가 필요합니다. `jsonschema`는 선택 사항이며, 없어도 내장 검증기가 핵심 규칙을 검사합니다.

```bash
python3 -m pip install -r .claude/skills/ensemble/requirements.txt
python3 .claude/skills/ensemble/scripts/review.py preflight
python3 -m unittest discover -s tests -v
```

실제 모델 연결까지 확인하려면 아래 명령을 사용합니다. 모델 사용 비용이 발생할 수 있습니다.

```bash
python3 .claude/skills/ensemble/scripts/review.py preflight --live
```

## 기본 사용법

Claude Code에서 `/ensemble`을 실행하는 것이 가장 간단합니다. CLI를 직접 쓸 때는 사용자 요청을 명령문에 넣지 말고 파일로 전달합니다.

```bash
python3 .claude/skills/ensemble/scripts/review.py init --request-file /tmp/request.txt
```

반환된 `run_dir`를 이후 명령의 `--run`에 넣습니다.

```bash
python3 .claude/skills/ensemble/scripts/review.py save --run <run_dir> --kind claude-proposal --source /tmp/claude-proposal.md
python3 .claude/skills/ensemble/scripts/review.py propose --run <run_dir>
python3 .claude/skills/ensemble/scripts/review.py save --run <run_dir> --kind draft --round 0 --source /tmp/draft.md
python3 .claude/skills/ensemble/scripts/review.py review --run <run_dir> --round 1
python3 .claude/skills/ensemble/scripts/review.py final-blind --run <run_dir>
python3 .claude/skills/ensemble/scripts/review.py finalize --run <run_dir> --status auto
```

검토 번호와 초안 번호는 별개입니다. 문서를 고치지 않았다면 새 초안을 만들지 않고 다음 검토 번호로 최신 초안을 다시 검토합니다. 특정 초안을 지정할 때만 `review --draft-round <N>`을 사용합니다.

`USER_DECISION_REQUIRED` 또는 `ESCALATION_REQUIRED`가 나오면 작업이 멈추고 사용자 선택을 기다립니다. 선택 내용을 파일로 저장한 뒤 작업을 재개합니다.

```bash
python3 .claude/skills/ensemble/scripts/review.py resolve-user-decision \
  --run <run_dir> --action REVISE --note-file <사용자-결정.txt>
```

문서를 고치지 않고 검토를 이어가려면 `--action CONTINUE`를 사용합니다. 모든 결정은 `decisions.md`, `manifest.json`, `timeline.md`에 기록됩니다.

## 용어

| 표시 | 뜻 |
|---|---|
| 실행 (`run`) | 요청 하나를 시작해 최종 문서가 나올 때까지의 전체 작업 |
| 입력 묶음 (`bundle`) | 모델에 전달해도 되는 파일만 모은 폴더 |
| 완료 기준 (`rubric`) | 최종 명세가 충족해야 하는 확인 가능한 조건 |
| 고정 예제 (`fixture`) | 같은 입력에 비슷한 판정이 나오는지 확인하는 테스트 자료 |
| 최종 독립 검토 (`FINAL_BLIND`) | 이전 논의를 숨기고 최종 초안만 새로 검토하는 단계 |
| 진행을 막는 이슈 (`blocker`) | 해결하거나 사용자가 위험을 받아들여야 다음 단계로 갈 수 있는 문제 |
| 추가 판단 (`escalation`) | 작성자와 리뷰어가 합의하지 못했을 때 다른 평가를 받는 절차 |
| 실행 기록 (`manifest.json`) | 상태, 모델, 재시도, 시작·종료 정보를 담은 파일 |

코드와 JSON에는 호환성을 위해 영문 상태값을 그대로 사용합니다.

자주 보는 상태는 다음과 같습니다.

| 상태 | 의미 |
|---|---|
| `DRAFT_READY` | 초안을 만들거나 다시 검토할 수 있음 |
| `NEEDS_REVISION` | 고쳐야 할 이슈가 있음 |
| `APPROVED` | 일반 검토를 통과함. 최종 독립 검토는 아직 남아 있음 |
| `USER_DECISION_REQUIRED` | 사용자 선택이 있어야 계속할 수 있음 |
| `ESCALATION_REQUIRED` | 합의하지 못한 이슈에 추가 판단이 필요함 |
| `CONVERGED` | 완료 기준상 남은 진행 차단 이슈가 없음 |
| `ITERATION_LIMIT_REACHED` | 해결되지 않은 이슈가 있는 채 검토 한도에 도달함 |
| `RUN_TAINTED` | 실행 중 코드가 바뀌어 새 실행이 필요함 |

## 안전 규칙

- 일반 검토의 입력 묶음에는 전체 이슈 기록, 작성자 결정, 이전 점수를 넣지 않습니다.
- 최종 독립 검토에는 요청, 완료 기준, 최종 초안만 전달합니다.
- 검토 결과와 초안 사본은 덮어쓰지 않습니다.
- 최대 반복 횟수에 도달해도 승인으로 처리하지 않습니다.
- 추가 평가자를 사용할 수 없으면 임의로 결론 내리지 않고 사용자에게 선택을 요청합니다.
- 요청에 비밀정보로 보이는 내용이 있으면 실행을 막습니다.
- 실행 중 Ensemble 코드가 바뀌면 해당 실행을 중단하고 새 실행을 요구합니다.
- 호출한 모델, CLI 버전, 재시도 원인은 실행 기록에 남깁니다.

## 진단 명령

```bash
python3 .claude/skills/ensemble/scripts/review.py measure-noise --run <run_dir> --repetitions 3
python3 .claude/skills/ensemble/scripts/review.py issue-audit --run <run_dir> --round <N>
python3 .claude/skills/ensemble/scripts/review.py panel --run <run_dir> --issue <R1-I1>
```

이 명령들은 판정이 얼마나 안정적인지 확인하고, 추가 판단이 필요한지 알려줍니다. 자동 승인을 만들지는 않습니다.

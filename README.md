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

일반 검토는 같은 실행의 같은 요청 안에서 Codex 세션을 이어서 사용하되 한 세션은 최대 3회까지만 재사용합니다. 이후에는 현재 이슈 projection을 바탕으로 새 세션을 시작해 누적 문맥과 확증 편향을 줄입니다. 요청 원문이나 실행 ID가 다르면 세션 재사용을 거부합니다. 제안, 추가 판단, 이슈 점검, 최종 독립 검토는 새 세션에서 실행합니다.

`USER_DECISION_REQUIRED` 또는 `ESCALATION_REQUIRED`가 나오면 작업이 멈추고 사용자 선택을 기다립니다. 선택과 새로 확정한 요구를 구조화 JSON으로 저장한 뒤 작업을 재개합니다.

```bash
python3 .claude/skills/ensemble/scripts/review.py resolve-user-decision \
  --run <run_dir> --decision-file <사용자-결정.json>
```

결정 파일은 `action`, `audit_note`, `authoritative_decisions`를 담습니다. 각 권위 결정은 `decision`과 기존 결정을 대체할 때 쓰는 `supersedes` ID 배열을 가집니다. 문서를 고치지 않고 검토를 이어가려면 `action`을 `CONTINUE`로 둡니다. 감사 기록은 `decisions.md`, `manifest.json`, `timeline.md`에, 검토자가 읽는 권위 입력은 `01-input/user-decisions.json`에 남습니다. `user-decisions.json`을 직접 수정하면 무결성 검사에서 거부됩니다.

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
| 실행 기록 (`manifest.json`) | 상태, 모델, 검토 세션, 재시도, 시작·종료 정보를 담은 파일 |

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
- 일반 검토 세션은 요청 해시와 실행 ID가 모두 같은 경우에만 이어서 사용합니다.
- 기본 한도는 일반 검토 5회, 최종 독립 검토 2회입니다.
- 최종 독립 검토에는 요청, 완료 기준, 권위 있는 사용자 결정, 최종 초안만 전달합니다.
- 최종 독립 검토는 현재 초안이 일반 검토를 통과한 경우에만, 초안당 한 번 실행합니다.
- `final.md`에는 상태 헤더나 리뷰 이력 부록을 붙이지 않습니다. 상태와 이견은 manifest·registry·timeline에서 확인합니다.
- 검토 결과와 초안 사본은 덮어쓰지 않습니다.
- 일반 검토·최종 독립 검토·전체 provider 호출 한도를 따로 적용하며, 한도 도달을 승인으로 처리하지 않습니다.
- 최종 검토 이슈를 승격하면 다음 일반 검토는 새 Codex 세션에서 시작합니다.
- 추가 평가자를 사용할 수 없으면 임의로 결론 내리지 않고 사용자에게 선택을 요청합니다.
- 요청에 비밀정보로 보이는 내용이 있으면 실행을 막습니다.
- 실행 중 Ensemble 코드가 바뀌면 해당 실행을 중단하고 새 실행을 요구합니다.
- 호출한 모델, CLI 버전, 재시도 원인은 실행 기록에 남깁니다.

## 진단 명령

```bash
python3 .claude/skills/ensemble/scripts/review.py measure-noise --run <run_dir> --repetitions 3
python3 .claude/skills/ensemble/scripts/review.py issue-audit --run <run_dir> --round <N>
python3 .claude/skills/ensemble/scripts/review.py panel --run <run_dir> --issue <R1-I1>
python3 .claude/skills/ensemble/scripts/review.py preflight --live-agy
```

이 명령들은 판정이 얼마나 안정적인지 확인하고, 추가 판단이 필요한지 알려줍니다. 자동 승인을 만들지는 않습니다.

## 평가 명령

파이프라인이 좋은 명세를 만들어내는지를 측정합니다. 비용이 다른 세 계층으로 나뉘며, 각 명령은 자기보다 비싼 계층을 대신 호출하지 않습니다. 설계는 [evaluator_handoff.md](evaluator_handoff.md)에 있습니다.

```bash
# 1층 — 끝난 실행의 프로세스 지표. 모델 호출이 없어 비용이 0입니다.
python3 .claude/skills/ensemble/scripts/review.py eval-run --run <run_dir>
python3 .claude/skills/ensemble/scripts/review.py eval-run --run <run_dir> --raw
python3 .claude/skills/ensemble/scripts/review.py eval-run --run <run_dir> --compare <run_dir2> <run_dir3>

# 2층 — 첫 초안과 마지막 초안을 제3 모델이 블라인드 비교. 실행당 심판 2회입니다.
python3 .claude/skills/ensemble/scripts/review.py eval-quality --run <run_dir>

# 3층 — 고정 케이스 세트로 코드 버전을 평가하고 커밋 간에 비교합니다.
python3 .claude/skills/ensemble/scripts/review.py eval-bench --suite smoke
python3 .claude/skills/ensemble/scripts/review.py eval-bench --suite full --repeat 3
python3 .claude/skills/ensemble/scripts/review.py eval-bench --collect --suite smoke --benchmark-run-id <id>
python3 .claude/skills/ensemble/scripts/review.py eval-compare --base <sha> --head <sha>
```

- 평가는 실행 상태를 바꾸지 않습니다. 대상 실행의 `manifest.json`을 건드리지 않고, 평가 중 오류가 나도 실행을 종료 처리하지 않습니다. 결과는 `<run_dir>/eval/`에 남습니다.
- `eval-run`은 기본적으로 퍼센트·분자/분모·압축 토큰 단위를 사용한 표시용 요약을 출력하고 `process-summary.md`를 만듭니다. 전체 원시 지표가 필요할 때만 `--raw`를 사용합니다.
- 어떤 지표도 자동 게이트로 쓰지 않습니다. 실사용 기록으로 분별력이 확인된 뒤에만 게이트 승격을 논의합니다.
- 토큰은 실측값만 기록합니다. 프롬프트 길이로 추정하지 않고, 금액으로 환산하지 않습니다.
- 토큰은 세 주체를 모두 셉니다. **Codex와 Agy는 하한값**입니다 — 사용량을 보고하지 않은 호출이 있으면 그만큼 빠집니다. **작성자(Claude)는 상한값**입니다 — 세션 기록이 실행 단위가 아니라 세션 단위라서, 같은 시간대의 다른 작업이 섞일 수 있습니다. 오차의 방향이 반대이므로 한 숫자로 합치지 않고 제공자별로 표시합니다.

```bash
# 정지 상태로 멈춰 finalize를 거치지 않은 실행의 작성자 토큰 수집
python3 .claude/skills/ensemble/scripts/review.py collect-claude-usage --run <run_dir>
```

`finalize`가 종료 직후 자동으로 부르므로 보통은 직접 실행할 필요가 없습니다. 세션 기록이 없어도 종료를 막지 않고 경고만 남깁니다. 이 명령은 창 전체를 다시 계산해 **대체**하므로 여러 번 실행해도 값이 부풀지 않습니다.
- 3층 순회는 작성자 단계가 필요하므로 `/ensemble-eval` 스킬이 수행합니다. 케이스 정답지는 사용자가 검토해 `reviewed_by_user: true`로 바꾸기 전까지 `UNREVIEWED`로 표시되고 합계에서 빠집니다.
- 3층은 상태 사전 채점 실패나 tainted 수집분에 대해 비용이 드는 심판을 호출하지 않습니다. 꼭 필요한 진단에서만 `--force-judge`로 재정의합니다.
- Agy에는 파일·셸 권한을 열지 않습니다. 검증된 번들 내용을 프롬프트에 직접 싣고, 파일명·크기·해시만 호출 기록에 남깁니다.

### 심판 안정성 확인

심판 판정도 흔들립니다. `measure-noise`와 같은 방식으로 반복 측정해 축별 안정성을 확인한 뒤에 결과를 씁니다.

```bash
python3 .claude/skills/ensemble/scripts/review.py eval-quality --run <run_dir> --repetitions 3
```

출력의 `composite_distribution`에서 `UNSTABLE` 비율이 높은 축은 심판이나 프롬프트를 신뢰할 수 없다는 뜻입니다. 그 축은 보고에서 빼고 `references/judge-prompt.md`를 고칩니다.

# 1층 handoff — 프로세스 지표 (`eval-run`)

> 상위 문서: [evaluator_handoff.md](../evaluator_handoff.md)
>
> 목적: 완료된(또는 중단된) 실행 하나의 산출물에서 **결정적으로** 지표를 계산한다. 모델 호출이 없으므로 비용이 없고, 같은 입력이면 항상 같은 결과가 나온다.

## 1. 입력과 출력

```bash
python3 .claude/skills/ensemble/scripts/review.py eval-run --run <run_dir>
```

- 입력(레이아웃 v2, 반드시 `layout.py` 헬퍼로 접근): `_state/manifest.json`, `_state/convergence.json`, `_state/issue-registry.json`, `04-reviews/blind/draft-<NN>[-attempt-<M>].json`, `04-reviews/reconciliation/draft-<NN>[-attempt-<M>].json`
- 독립 검토 파일명 끝의 숫자는 회차가 아니라 **시도 번호**다. 사전순 정렬은 `draft-00-attempt-2.json`을 `draft-00.json`보다 앞에 두므로 쓰지 않는다. (초안 번호, 시도 번호) 전용 키로 정렬하는 헬퍼(`layout.attempt_of`)를 추가해 쓰고, 접미사 없는 원본 파일은 시도 1로 취급한다.
- 출력: stdout으로 JSON을 출력하고 `<run_dir>/eval/process-metrics.json`(`layout.process_metrics`)에 저장한다
- 이 명령은 실행 상태를 바꾸지 않는다. `assert_source_unchanged` 검사를 하지 않는다 — 평가는 실행이 끝난 뒤 다른 코드 버전에서 수행해도 유효하며, 대신 출력에 평가 시점의 코드 해시를 함께 기록해 구분한다

## 2. 지표 정의

각 지표는 이름, 계산 원천, 계산식으로 정의한다. 원천 필드가 없거나 실행이 미완료면 해당 지표는 `null`로 기록하고 실패하지 않는다.

### 2.1 수렴 효율

| 지표 | 원천 | 계산 |
|---|---|---|
| `review_rounds`, `iterative_reviews` | `manifest.counters.iterative_reviews` | 일반 검토 횟수. 승격이 사용한 시퀀스 번호는 제외 |
| `sequence_rounds` | `manifest.review_history` | 일반 검토와 승격을 모두 포함한 시퀀스 항목 수 |
| `promotions` | `manifest.counters.promotions` | FINAL_BLIND 발견 승격 횟수 |
| `review_session_resets` | `manifest.review_session_policy.reset_count` | 승격 후 검토자 세션을 초기화한 횟수 |
| `draft_rounds` | `manifest.review_history[].draft_round` | 최대값 + 1 |
| `final_state` | `manifest.state` | 그대로 |
| `terminated_cleanly` | `manifest.state` | `CONVERGED` 또는 `STABLE_DISSENT`면 true |
| `new_issues_by_round` | `convergence.rounds[].new_issue_count` | 라운드 순 배열. 감소 추세면 수렴 중 |
| `open_backlog_by_round` | `convergence.rounds[].open_backlog` | 라운드 순 배열 |
| `rounds_to_zero_backlog` | 위 배열 | `open_backlog`가 0이 된 첫 라운드. 없으면 `null` |

### 2.2 누출률 (핵심 지표)

일반 검토 루프가 승인한 초안을 최종 독립 검토가 다시 봤을 때 새 진행 차단 이슈가 나오면, 루프가 그만큼 놓친 것이다.

**blind 원문의 `blocking_issues`를 그대로 합산하면 과계산된다.** 거기에는 (1) 사용자가 이미 수용한 위험과의 일치, (2) 여러 시도에 반복 등장한 같은 발견, (3) 승격되어 registry에도 들어간 이슈가 섞여 중복·이중 계산이 생긴다. 그래서 비율의 분자·분모는 registry의 `first_seen_source`(각 이슈가 처음 등장한 검토 파일 경로, `registry.apply_review`가 기록)로 **고유 이슈 수**를 세고, 시도별 원 수치는 별도 배열로만 남긴다.

| 지표 | 원천 | 계산 |
|---|---|---|
| `final_blind_attempts` | `04-reviews/blind/` | 파일 수 |
| `attempts[]` | blind + reconciliation 시도 쌍 | 시도별 `{raw_findings, accepted_risk_matches, unaccepted_findings, passed}` — 각각 blind의 `blocking_issues` 수, reconciliation의 `accepted_findings` 수, `unaccepted_blocking_findings` 수, `passed` |
| `final_blind_first_pass` | 첫 번째 reconciliation | `passed` 그대로 |
| `unique_iterative_origin_blockers` | registry | `first_seen_source`가 `04-reviews/iterative/`인 이슈 수 |
| `unique_promoted_final_blind_blockers` | registry | `first_seen_source`가 `04-reviews/promoted/`인 이슈 수 (FINAL_BLIND 발견은 승격을 통해서만 registry에 들어간다) |
| `unpromoted_unaccepted_last_attempt` | 마지막 reconciliation | 승격되지 않고 남은 `unaccepted_blocking_findings` 수. 0이 아니면 `warnings`에 기록 |
| `leakage_rate_lower_bound` | 위 값 | `unique_promoted_final_blind_blockers / (unique_iterative_origin_blockers + unique_promoted_final_blind_blockers)`. 분모가 0이면 `null` |
| `unique_observed_final_blind_blockers` | 모든 reconciliation | `(criterion_id, consequence_fingerprint)`로 시도 간 중복 제거한 미수용 발견 수 |
| `unique_unpromoted_final_blind_blockers` | 위 값 + registry | 관측 고유 발견 수에서 승격 기원 고유 이슈 수를 뺀 값 |
| `leakage_rate_observed` | 위 값 | `unique_observed_final_blind_blockers / (unique_iterative_origin_blockers + unique_observed_final_blind_blockers)`. 반복 발견을 제거한 실측 보조값 |

하한값임을 이름에 드러낸다(`_lower_bound`). 알려진 한계 (출력 `warnings`에 함께 기록):

- 승격되지 않은 unaccepted 발견(예: 반복 한도 도달로 종료된 실행)은 registry에 없어 분자에서 빠진다. `unpromoted_unaccepted_last_attempt`가 그 보정 단서다.
- reconciliation의 `UNMATCHED:` 앵커는 시도별 salt가 섞여 있어 직접 중복 제거에 쓰지 않는다. 관측 보조값은 기준과 구현 결과가 같은 발견을 `(criterion_id, consequence_fingerprint)`로 묶는다. 표현이 크게 바뀐 같은 결함은 별개로 셀 수 있으므로 `leakage_rate_observed`도 추정치다.
- `first_seen_source`가 없는 이슈(구버전 실행)는 `unknown_origin_blockers`로 따로 세고 비율에서 제외한다.

2차 시도에서도 `unaccepted_findings`가 나오면 수정이 새 문제를 만들고 있다는 신호다.

### 2.3 이슈 처리 품질

| 지표 | 원천 | 계산 |
|---|---|---|
| `total_issues` | `issue-registry.json` | 키 수 |
| `dispositions` | `convergence.rounds[].author_dispositions` | ACCEPT/REJECT/DEFER 집계 |
| `acceptance_rate` | 위 값 | ACCEPT / 전체 판단 수 |
| `resolution_basis` | `convergence.rounds[].resolution_basis_counts` | 근거 유형별 합계 |
| `resolved_without_relevant_edit` | `convergence.rounds[]` | 합계. 문서 수정 없이 해소 처리된 이슈 수 — 높으면 해소 근거가 약하다는 신호 |
| `regression_count` | `convergence.rounds[]` | 합계. 해소된 이슈의 재발 수 |
| `reviewer_storm_rounds` | `convergence.rounds[].reviewer_storm` | true인 라운드 수 |
| `max_stalled_streak` | `convergence.rounds[].stalled_streak` | 최대값 |
| `severity_distribution` | `convergence.rounds[].severity_distribution` | 전 라운드 합산 |

### 2.4 개입과 마찰

| 지표 | 원천 | 계산 |
|---|---|---|
| `user_decisions` | `manifest.user_decisions` | 항목 수와 action 분포 |
| `escalations` | `manifest.escalation_signals`, panel 산출물 | 발생 수 |
| `retries` | `manifest.retries` | infra/schema/semantic 그대로 |
| `validation_retry_calls` | `manifest.provider_calls[].outcome` | `VALIDATION_RETRY` 수 / 전체 호출 수 |
| `provider_call_count` | `manifest.provider_calls` | operation별 집계 |
| `session_reuse_rate` | `manifest.provider_calls[].session_resumed` | review 호출 중 재사용 비율 |

### 2.5 소요 자원

| 지표 | 원천 | 계산 |
|---|---|---|
| `wall_clock_seconds` | `manifest.started_at`, `finished_at` | 차이. `finished_at` 없으면 `null` |
| `usage` | `manifest.usage` | 제공자별 토큰 합계 그대로. 비어 있으면 `null` |
| `usage_incomplete` | `manifest.usage.*.calls_unreported` | 하나라도 0이 아니면 true. 이때 토큰 합계는 하한값 |

`provider_calls[].recorded_at` 간격으로 호출별 소요 시간을 추정하지 않는다. 기록 시점은 호출 종료 시점이라 간격에는 Claude의 작성 시간이 섞여 있어 오해를 만든다.

## 3. 출력 스키마 (`process-metrics.json`)

```json
{
  "schema_version": 1,
  "run_id": "…",
  "request_hash": "manifest.request_hash — --compare에서 같은 요청인지 구분",
  "evaluated_at": "…",
  "run_git_commit": "manifest.environment.git_commit",
  "run_source_hash": "manifest.environment.ensemble_source_hash",
  "evaluator_git_commit": "평가 시점의 git HEAD",
  "evaluator_source_hash": "평가 시점의 ensemble_source_hash",
  "convergence": { "…": "§2.1" },
  "leakage": { "…": "§2.2" },
  "issues": { "…": "§2.3" },
  "friction": { "…": "§2.4" },
  "resources": { "…": "§2.5" },
  "warnings": ["누락된 원천 파일이나 null 처리된 지표의 사유"]
}
```

- `run_git_commit`과 `evaluator_git_commit`(또는 두 source hash)이 다르면 `warnings`에 기록한다. 지표 정의가 바뀐 코드로 옛 실행을 평가할 수 있기 때문이다.
- 기존 파일이 있으면 덮어쓰되, `evaluated_at`이 다른 이전 결과는 `process-metrics-<timestamp>.json`으로 옮겨 보존한다. 검토 결과를 덮어쓰지 않는 기존 규칙과 맞춘다.

## 4. 여러 실행의 비교

단일 실행 지표만으로는 좋고 나쁨을 말할 수 없다. 비교 명령을 함께 제공한다.

```bash
python3 .claude/skills/ensemble/scripts/review.py eval-run --run <dir> --compare <dir2> [<dir3> …]
```

- 각 실행의 `process-metrics.json`을 (없으면 계산해서) 표로 병렬 출력한다.
- 요청이 다른 실행끼리의 비교는 참고용이다. 같은 케이스를 반복한 실행(3층)끼리의 비교만 회귀 판단에 쓴다. 출력에 각 실행의 `request_hash`를 표시해 구분을 돕는다.

## 5. 구현 노트

- 구현 위치: `ensemble_core/eval_process.py`. `review.py`에 `eval-run` 하위 명령 등록.
- 파일 읽기는 기존 `io_utils`의 안전한 읽기 함수를 재사용한다.
- 모든 계산은 순수 함수로 작성한다: `compute_process_metrics(manifest, convergence, registry, final_blinds, reconciliations) -> dict`. 파일 I/O와 분리해야 테스트가 쉽다.
- `convergence.json`의 라운드 항목은 검토 라운드 기준이다. 초안 라운드와 혼동하지 않는다(기존 규칙: 검토 번호와 초안 번호는 별개).

## 6. 테스트 계획

`ensemble/runs/`는 Git에 없으므로 테스트 fixture는 `tests/data/`에 합성 산출물로 만든다.

구현 위치: `tests/test_eval.py`.

- [x] 정상 완료 실행(CONVERGED)의 합성 manifest·convergence로 전 지표 계산 검증
- [x] `USER_DECISION_REQUIRED`로 멈춘 실행: `finished_at` 없음 → `wall_clock_seconds: null`, 실패하지 않음
- [x] `usage`가 빈 구버전 manifest → `resources.usage: null`, `warnings`에 사유
- [x] `calls_unreported > 0` → `usage_incomplete: true`
- [x] 누출률: iterative 기원 1건 + promoted 기원 1건 → `leakage_rate_lower_bound: 0.5`. 시도별 `attempts[]`에 accepted/unaccepted가 분리되는지
- [x] 승격 없이 unaccepted 발견이 남은 채 종료된 실행 → 분자에 반영되지 않고 `unpromoted_unaccepted_last_attempt`와 `warnings`로 표시(하한값 규칙)
- [x] `draft-00.json`과 `draft-00-attempt-2.json`의 시도 순서 정렬이 (초안, 시도) 기준으로 되는지
- [x] 원천 파일 일부 누락(예: convergence.json 없음) → 해당 지표만 `null`, 나머지는 정상 계산
- [x] `--compare`에 request_hash가 다른 실행을 섞었을 때 표시 확인

## 7. 완료 기준

- [x] 기존 실행에서 `eval-run`이 오류 없이 지표를 출력한다 — 레이아웃 v2 실행 2건에서 확인. 나머지 2건은 v1이라 `resolve_run`이 설계대로 거부한다
- [x] 계산에 모델 호출이 없고, 같은 실행에 두 번 실행하면 `evaluated_at` 외에 같은 결과가 나온다
- [x] 위 테스트가 모두 통과한다
- [x] README 평가 명령 절에 `eval-run` 사용법이 추가된다

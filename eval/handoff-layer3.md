# 3층 handoff — 벤치마크 케이스 세트 (`eval-bench`)

> 상위 문서: [evaluator_handoff.md](../evaluator_handoff.md)
>
> 목적: 고정된 요청 세트로 파이프라인 전체를 실행해 **코드 버전 간** 회귀를 잡는다. `fixtures/`가 "결함을 심은 초안"으로 리뷰어 한 명을 시험한다면, 3층은 "고정된 요청"으로 파이프라인 전체를 시험한다. 결과는 커밋에 묶인 점수표로 남는다.

## 1. 케이스 구조

```text
eval/cases/<case-id>/
├── request.txt        # /ensemble에 넣을 요청 원문. 고정. 수정 시 케이스 ID를 새로 딴다
├── expected.json      # 기대 결과. 루프 밖 작성 원칙 적용
└── notes.md           # 선택. 케이스 의도와 이력
```

`expected.json`:

```json
{
  "schema_version": 1,
  "review_required": "이 정답지는 사용자가 검토해야 채점에 사용할 수 있습니다.",
  "reviewed_by_user": false,
  "case_type": "init_block | state_behavior | quality",
  "suites": ["smoke", "full"],
  "tags": ["ambiguous", "secret", "policy-decision", "…"],
  "difficulty": "easy | medium | hard",

  "expected_terminal_states": ["CONVERGED"],
  "forbidden_states": ["ITERATION_LIMIT_REACHED"],
  "expect_user_decision": false,
  "expect_escalation": false,

  "quality_expectations": {
    "must_cover": ["요구 키워드나 rubric 항목"],
    "must_not_assert": ["원문에 없는데 확정하면 안 되는 사항"]
  }
}
```

- `suites`는 이 케이스가 속한 세트다(스모크는 full의 부분집합). 세트 구성이 곧 `suite_hash`의 입력이 된다(§3.2).
- `expected_terminal_states`에는 종료 상태뿐 아니라 정지 상태(`ESCALATION_REQUIRED`)도 넣을 수 있다. 채점 대상은 "실행이 멈춘 지점의 상태"다.
- 유형별 필수 필드: `init_block`은 `expected_terminal_states`를 비워 둔다(run이 안 생기는 게 정답이므로). `state_behavior`는 `expected_terminal_states`가 필수. `quality`는 `expected_terminal_states`와 `quality_expectations`가 필수.

- `reviewed_by_user`가 false인 케이스는 실행은 되지만 점수표에 `UNREVIEWED`로 표시되고 합계에서 제외된다. `fixtures/expected-criteria.json`의 `review_required` 원칙과 동일하다.
- `request.txt`를 고치면 과거 점수와 비교할 수 없으므로 케이스 ID를 새로 만든다. 케이스는 불변이다.

## 2. 케이스 유형

유형마다 "실행이 어디까지 진행되는가"와 "무엇으로 채점하는가"가 다르므로, 공통 PASS 규칙 하나로 묶지 않고 유형별로 채점을 정의한다(§3.3).

### 2.1 `init_block` — 실행 생성 전 차단 검증

run이 만들어지지 않는 것이 정답이므로 종료 상태 검증은 적용되지 않는다.

- 예: 요청에 API 키 형태 문자열 포함 → `init`이 비밀정보 감지(`InputError` + 감지 패턴)로 실행을 막으면 PASS.
- run_id는 `null`이고, 모델 호출이 없어 결정적이다. `--collect` 시점에 요청 원문으로 재채점해도 같은 결과가 나온다.

### 2.2 `state_behavior` — 정지 상태 검증, 심판 불필요

파이프라인이 올바른 지점에서 멈추는지를 관측된 상태로 결정적으로 채점한다. 이 프로젝트 안전 규칙의 회귀 테스트다.

| 케이스 예 | 기대 |
|---|---|
| 상반된 요구 두 개를 함께 명시 | `expect_user_decision: true` 또는 `expect_escalation: true` |
| 정책 판단이 필요한 요청 (예: 개인정보 보존 기간) | `expect_user_decision: true` — 자동 진행하면 FAIL |
| 명확하고 좁은 요청 | `expected_terminal_states: ["CONVERGED"]`, 낮은 라운드 수 |

### 2.3 `quality` — 최종 문서 채점

정상 완료가 기대되는 요청. 종료 상태 검증에 더해 1층 지표와 2층 심판 결과를 수집한다. `quality_expectations`는 2층의 비교 심판에 추가 축으로 넣지 않고 **별도의 절대 판정**(judge-expectations 프롬프트·스키마, 2층 handoff §4)으로 채점한다 — 정답지가 비교 입력에 섞이면 블라인드가 깨지기 때문이다. `must_cover` 누락이나 `must_not_assert` 위반이 있으면 FAIL.

### 2.4 초기 세트 구성 권장안

스모크 3개 + 전체 10개 내외. 도메인은 사용자의 실사용 유형에 맞춘다(상위 문서 §7 미결정).

- 스모크: 비밀정보 차단 1, 명확한 소형 요청 1, 모호한 요청 1
- 전체: 위 3 + 난이도별 품질 케이스 4~5 + 정책 판단 1 + 상충 요구 1 + 범위 확대 유혹(요청 밖 기능을 넣고 싶어지는) 1

## 3. 실행 방식 — 자동화의 한계와 분담

파이프라인의 작성자 역할(요청 구조화, 초안 작성, 이슈 수용·반박)은 Claude Code 스킬이 수행하므로 **`review.py` 단독으로는 전체 파이프라인을 무인 실행할 수 없다.** 이를 숨기지 않고 실행 방식을 둘로 나눈다.

### 3.1 runner가 단독 실행하는 부분

```bash
python3 .claude/skills/ensemble/scripts/review.py eval-bench --suite smoke|full [--case <id>]
```

- `init_block` 케이스: `init`의 차단 여부만 확인하면 되므로 runner가 완전 자동으로 채점한다.
- 이미 실행이 끝난 케이스의 결과 수집: run 폴더를 찾아 1층·2층 평가를 적용하고 점수표를 갱신한다.

### 3.2 Claude Code가 수행하는 부분 (`/ensemble-eval` 스킬)

작성자 단계가 필요한 케이스는 새 스킬 `/ensemble-eval`이 케이스를 순회하며 기존 `/ensemble` 절차대로 실행한다.

- 각 케이스의 `request.txt`로 `init --request-file`을 호출하고, 이후 단계는 SKILL.md의 작업 순서를 그대로 따른다.
- `USER_DECISION_REQUIRED` 등 사용자 개입 지점에 도달하면 **그 상태 자체가 채점 대상이므로** 임의로 재개하지 않는다. 상태를 기록하고 다음 케이스로 넘어간다. 행동 케이스는 이 지점에서 채점이 끝난다.
- **실행 식별 계약**: 벤치마크 실행은 manifest에 `benchmark` 블록을 기록하고, runner는 label 같은 자유 문자열이 아니라 이 블록으로만 run을 수집한다. label 표식은 사람이 읽는 보조 수단이다.

  ```json
  "benchmark": {
    "benchmark_run_id": "벤치마크 1회 순회의 ID. 이전 순회나 반복 실행과의 혼동을 막는다",
    "case_id": "…",
    "case_revision_hash": "request.txt + expected.json 내용의 해시. 케이스 파일이 바뀐 run을 걸러낸다",
    "suite": "smoke | full",
    "suite_hash": "세트에 속한 (케이스 ID, 개정 해시) 목록의 해시",
    "repeat_index": 1
  }
  ```

  시작 시점의 `git_commit`과 `ensemble_source_hash`는 이미 manifest의 `environment` 스냅샷에 기록되므로 중복 저장하지 않는다.
- 같은 케이스를 반복 실행하면 요청 원문이 같으므로 `init`의 중복 요청 검사에 걸린다. 벤치마크 init은 `--allow-reuse`를 항상 켠다. (중복 요청 검색이 layout v1 경로만 보는 기존 버그가 있어 v2 경로도 함께 보도록 수정한다 — `io_utils.find_consumed_request_hash`.)
- 순회가 끝나면 `eval-bench --collect`를 호출해 점수표를 만든다.

### 3.3 채점 규칙

판정 조건은 케이스 유형별로 다르다.

| 유형 | PASS 조건 |
|---|---|
| `init_block` | `init`이 요청을 차단함(비밀정보 감지 등). run이 생겼으면 FAIL |
| `state_behavior` | 실행이 멈춘 상태가 `expected_terminal_states`에 있고, `forbidden_states`를 지나지 않았고, `expect_user_decision`·`expect_escalation` 기대가 관측과 일치 |
| `quality` | `state_behavior` 조건 + `must_cover` 누락 없음 + `must_not_assert` 위반 없음 |

공통 판정:

| 판정 | 조건 |
|---|---|
| `FAIL` | 위 조건 위반. **Ensemble 코드가 낸 오류(상태 기계 위반, 검증 실패로 인한 중단 등)도 FAIL이다** — SKIP으로 빼면 회귀가 숨는다 |
| `SKIP` | 외부 인프라 장애(제공자 CLI 실패, 네트워크 등)로 실행이 완료되지 못함(`INFRA_ERROR`). 점수 합계에서 제외하고 사유 기록 |
| `UNREVIEWED` | 정답지가 사용자 검토 전(`reviewed_by_user: false`). 합계 제외 |

- 실행 도중 `RUN_TAINTED`가 관측되면 해당 케이스는 SKIP하고 점수표에 `tainted: true`를 기록한다.
- `case_revision_hash`가 현재 케이스 파일과 다른 run은 수집하지 않고 사유를 기록한다(케이스 불변 원칙).
- 2층 합성 결과(`FINAL_BETTER` 등)는 PASS/FAIL에 넣지 않고 별도 열로 집계한다. 비교 판정은 관찰 지표이지 합격 기준이 아니다(관찰 우선 원칙).

## 4. 점수표 생성과 추이 비교

```bash
python3 .claude/skills/ensemble/scripts/review.py eval-bench --collect --suite full
python3 .claude/skills/ensemble/scripts/review.py eval-compare --base <sha> --head <sha>
```

- `--collect`는 `eval/results/<git-sha>/scorecard.json`을 만든다. 같은 sha에 이미 점수표가 있으면 `scorecard-<timestamp>.json`으로 보존 후 새로 쓴다. `eval/results/`는 Git에서 제외한다(상위 문서 §7 확정). 구현 시 `.gitignore`에 추가한다.
- 벤치마크 도중 코드가 바뀌면(작업 사본 dirty, run들의 `ensemble_source_hash` 불일치, `RUN_TAINTED` 관측 포함) 점수표에 `tainted: true`를 기록한다. `RUN_TAINTED`와 같은 철학이며, tainted 점수표는 `eval-compare`가 기본적으로 거부한다.
- `eval-compare`의 비교 가능 조건: (1) 두 점수표 모두 `tainted: false`, (2) `suite_hash`가 같음(같은 케이스 세트 버전), (3) `model_config`가 같음. 모델 구성이 다르면 기본 거부하고, 명시적 플래그로만 경고와 함께 비교를 허용한다 — 점수 차이가 코드 때문인지 모델 때문인지 구분할 수 없기 때문이다.
- 출력은 케이스별 판정, 1층 핵심 지표(라운드 수, 누출률, 재시도), 사용량 합계의 병렬 표다. 케이스당 1회 실행끼리의 판정 차이는 "회귀"가 아니라 **"회귀 신호"**로 표기한다. 확률적 흔들림과 회귀를 구분하려면 반복 실행(`--repeat`)이 필요하다.

## 5. 점수표 스키마 (`scorecard.json`)

```json
{
  "schema_version": 1,
  "git_commit": "…",
  "ensemble_source_hash": "…",
  "created_at": "…",
  "suite": "smoke | full",
  "suite_hash": "…",
  "benchmark_run_id": "…",
  "tainted": false,
  "model_config": {
    "codex": "manifest.models.codex.requested",
    "agy": "manifest.models.agy.requested",
    "declared_author_model": "호출자가 선언한 작성자 모델 | null",
    "author_verified": false,
    "judge": "2층 judge_model"
  },
  "cases": [
    {
      "case_id": "…",
      "case_type": "init_block | state_behavior | quality",
      "repeat_index": 1,
      "run_id": "… | null (init_block)",
      "verdict": "PASS | FAIL | SKIP | UNREVIEWED",
      "observed": {
        "terminal_state": "…",
        "init_blocked": false,
        "user_decision_reached": false,
        "escalation_reached": false
      },
      "process_metrics": {
        "review_rounds": 0,
        "leakage_rate": null,
        "retries": {},
        "wall_clock_seconds": null
      },
      "quality_judgment": {
        "overall": "FINAL_BETTER | … | null",
        "must_cover_missing": [],
        "must_not_assert_violations": []
      },
      "usage": {
        "run": { "…": "manifest.usage 합계" },
        "judge": { "…": "2층 usage_total" }
      }
    }
  ],
  "totals": {
    "pass": 0, "fail": 0, "skip": 0, "unreviewed": 0,
    "draft_better_count": 0,
    "usage": { "…": "전 케이스 제공자별 토큰 합계, calls_unreported 포함" }
  }
}
```

`totals.usage`가 이 벤치마크 1회의 실측 비용 기록이다. 상위 문서 원칙대로 금액 환산은 하지 않는다.

## 6. 비용 통제

- 스모크 세트는 행동 케이스 중심으로 구성해 파이프라인 완주가 1건 이하가 되게 한다. 코드 변경마다 부담 없이 돌리는 용도다.
- full 세트는 릴리스 전이나 파이프라인 로직 변경 시에만 돌린다. 1회 비용은 파이프라인 8~12회 실행과 같다.
- 케이스별 `limits`(검토 라운드 상한)를 파이프라인 기본값보다 낮게 걸어 폭주를 막는 옵션을 검토한다. 단, 상한 도달(`ITERATION_LIMIT_REACHED`)은 그 자체로 관측 결과이므로 상한을 너무 낮추면 벤치마크가 왜곡된다. 초기값은 기본 상한을 그대로 쓴다.
- 반복 실행은 `--repeat N`으로 지원하되 기본값은 1이다. 반복은 비용을 배수로 늘리므로, 모델 노이즈와 회귀를 구분해야 하는 시점(예: 단일 실행에서 회귀 신호가 나왔을 때)에만 늘린다.

## 7. 테스트 계획

구현 위치: `tests/test_eval.py`.

- [x] `expected.json` 스키마 검증기 (필수 필드, 상태값 오타, 유형별 필수 필드 규칙)
- [x] 채점 규칙 단위 테스트: 합성 manifest + expected 조합으로 유형별 PASS/FAIL과 SKIP/UNREVIEWED 각각
- [x] Ensemble 코드 오류로 끝난 실행이 SKIP이 아니라 FAIL로 채점되는지 (`INFRA_ERROR`만 SKIP)
- [x] `init_block` 케이스: 비밀정보 포함 요청으로 실제 `init` 경로 호출(모델 호출 없음) → 차단 확인
- [x] tainted 점수표와 `suite_hash`·`model_config`가 다른 점수표를 `eval-compare`가 거부하는지
- [x] 케이스-run 매칭(`benchmark` 블록) 검증: 이전 benchmark_run_id의 run과 개정 해시가 다른 run이 수집에서 제외되는지
- [x] 저장소에 든 케이스 파일이 스키마를 지키고, 정답지가 사용자 검토 없이 승인돼 있지 않은지

## 8. 완료 기준

- [x] 스모크 세트 3케이스가 정의됐다 (`smoke-01-secret-blocked`, `smoke-02-narrow-feature`, `smoke-03-ambiguous-policy`)
- [ ] **미완료** — 정답지를 사용자가 검토했다 (`reviewed_by_user: true`). 세 케이스 모두 `false`이며 각 `notes.md`에 검토가 필요한 지점을 적어 두었다
- [ ] **미완료** — `/ensemble-eval` 스킬로 스모크 세트를 1회 완주해 `scorecard.json`이 생성된다. 스킬과 수집 경로는 만들어졌고 실제 완주가 남았다
- [x] 서로 다른 두 커밋에서 만든 점수표를 `eval-compare`로 비교할 수 있다
- [ ] **미완료** — 점수표에 토큰 사용량 합계가 실측값으로 들어 있다. 수집·집계 경로는 만들었으나 토큰 수집 이후의 실행이 아직 없다
- [x] README에 벤치마크 실행 절차가 추가된다

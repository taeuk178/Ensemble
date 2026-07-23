# Evaluator 설계 명세 (전체 흐름)

> 목적: Ensemble 파이프라인의 실행 결과를 측정한다. LLM 벤치마크가 모델 버전별 성능을 추적하듯, Ensemble 코드 버전별로 "좋은 명세를 만들어내는가"를 추적한다.
>
> 세부 설계는 계층별 handoff에 있다.
> - [1층: 프로세스 지표](eval/handoff-layer1.md) — 기존 실행 산출물에서 결정적으로 계산. 비용 없음.
> - [2층: 결과물 품질 비교](eval/handoff-layer2.md) — draft-0 대 finalize가 선택한 마지막 초안을 제3 모델이 블라인드 비교. 저비용.
> - [3층: 벤치마크 케이스 세트](eval/handoff-layer3.md) — 고정 요청 세트로 회귀 벤치마크. 고비용.

본 문서는 세 계층이 공유하는 원칙, 디렉토리 구조, 토큰 사용량 수집 기반, 착수 순서를 정의한다.

## 문서에서 쓰는 용어

| 용어 | 뜻 |
|---|---|
| 평가 실행 (`evaluation`) | 완료된 실행(run) 하나 또는 케이스 세트에 대해 지표를 계산하는 작업 |
| 점수표 (`scorecard`) | 한 코드 버전에 대한 평가 결과를 모은 파일 |
| 심판 (`judge`) | 결과물 품질을 비교 채점하는 제3 모델. 작성자(Claude)·리뷰어(GPT)와 다른 모델 |
| 케이스 (`case`) | 벤치마크용으로 고정한 요청과 기대 결과의 묶음 |
| 누출률 (`leakage`) | 일반 검토 루프가 놓치고 최종 독립 검토(`FINAL_BLIND`)에서야 발견된 진행 차단 이슈의 비율 |
| 행동 케이스 (`behavior case`) | 최종 문서 품질이 아니라 파이프라인이 올바르게 멈추는지를 검증하는 케이스. 실행이 생기기 전에 막혀야 하는 `init_block`과 실행 후 정지 상태를 보는 `state_behavior`로 나뉜다 |
| 사용량 (`usage`) | 모델 호출별 입력·캐시·출력 토큰 수. CLI가 보고한 실측값만 기록 |

파일명, 명령, JSON 필드, 상태값은 기존 handoff.md와 같은 이유로 영문을 유지한다.

## 1. 핵심 원칙

- **정답지는 루프 밖에서 만든다.** 기대 결과(`expected-criteria.json`, 케이스 정답지, 심판 rubric)는 루프 참여자가 아닌 사람이 작성하거나 최소한 사용자가 검토한다. Claude가 문제와 정답을 모두 쓰면 평가는 "GPT가 Claude에 동의하는가"를 재는 것이 된다. 이 원칙은 기존 `fixtures/expected-criteria.json`의 `review_required` 규칙을 그대로 계승한다.
- **심판은 루프 밖 모델이다.** 작성자(Claude)나 리뷰어(GPT)가 자기 결과물을 채점하면 자기 동의 측정이 된다. 심판은 제3 제공자(현재 후보: panel과 같은 Antigravity `agy`)를 쓴다.
- **점수는 코드 버전과 모델 구성에 묶인다.** 모든 평가 결과에 `git_commit`과 `ensemble_source_hash`를 기록한다. 다른 커밋의 점수와 비교할 때만 개선·회귀를 말할 수 있다. `RUN_TAINTED`와 같은 철학이다. 모델 구성(리뷰어·패널·심판, 그리고 호출자가 선언한 작성자 모델)도 함께 기록한다 — 모델이 바뀌면 점수 변화가 코드 때문인지 모델 때문인지 구분할 수 없기 때문이다. 작성자(Claude) 모델은 CLI가 감지할 수 없으므로 선언값만 기록하고 검증하지 않는다는 한계를 명시한다. 모델 구성이 다른 점수표는 기본적으로 비교를 거부한다. 케이스당 1회 실행에서 나온 차이는 "회귀"가 아니라 "회귀 신호"로 표현한다.
- **평가는 관찰로 시작한다.** 어떤 지표도 처음부터 자동 게이트(예: 점수 미달 시 실행 차단)로 쓰지 않는다. 실사용 기록으로 지표의 분별력이 검증된 뒤에만 게이트 승격을 논의한다. 기존 안정성 지표와 같은 규칙이다.
- **심판도 노이즈가 있다.** 블라인드 비교는 문서 제시 순서를 바꿔 2회 호출하고, 두 호출이 갈리면 그 축은 불안정(`UNSTABLE`)으로 기록한다. 심판 판정의 재현성은 `measure-noise`와 같은 방식으로 반복 측정한다.
- **토큰은 실측만 기록한다.** CLI가 사용량을 보고하지 않으면 `null`로 남기고 호출 횟수만 센다. 프롬프트 길이로 추정하지 않는다. 금액 환산도 하지 않는다. 단가는 수시로 바뀌어 기록이 낡기 때문에 토큰 수만 남기고 환산은 보는 시점에 한다.
- **평가 입력에도 격리 규칙이 적용된다.** 심판 입력에는 리뷰 이력, 이슈 기록, 어느 문서가 최종본인지의 라벨을 넣지 않는다. 기존 `FINAL_BLIND` 번들 규칙과 같은 이유다.
- **Agy는 파일 도구를 쓰지 않는다.** headless 실행에서 권한 프롬프트를 낼 수 없으므로, allowlist로 만든 번들의 UTF-8 원문을 프롬프트에 직접 삽입한다. 셸 권한을 넓히지 않으며 호출 기록에는 파일명·바이트 수·SHA-256만 남긴다.
- **비용 계층을 섞지 않는다.** 1층은 언제든 무료로 실행한다. 2층은 실행당 심판 호출 몇 번, 3층은 파이프라인 전체 반복이다. 각 명령은 자기보다 비싼 계층을 암묵적으로 호출하지 않는다.

## 2. 전체 구조

```text
                       ┌────────────────────────────────────┐
                       │ 공통 기반: 토큰 사용량 수집(§4)          │
                       │ providers → provider_calls → usage │
                       └────────────────┬───────────────────┘
                                        │ 모든 계층이 사용
        ┌───────────────────────────────┼──────────────────────────────┐
        ▼                               ▼                              ▼
┌──────────────────┐          ┌──────────────────┐          ┌────────────────────┐
│ 1층 프로세스 지표    │          │ 2층 품질 비교        │          │ 3층 벤치마크 케이스   │
│ eval-run          │          │ eval-quality      │          │ eval-bench          │
│ 입력: 완료된 run    │          │ 입력: 완료된 run    │          │ 입력: eval/cases/    │
│ 비용: 0           │          │ 비용: 심판 2~4회     │          │ 비용: 파이프라인 N회  │
│ 결정적 계산         │          │ LLM 심판, 블라인드   │          │ 1층+2층 결과를 집계    │
└────────┬─────────┘          └────────┬─────────┘          └─────────┬──────────┘
         ▼                             ▼                              ▼
<run>/eval/                   <run>/eval/                   eval/results/<git-sha>/
process-metrics.json          quality-judgment.json         scorecard.json
```

- 1층과 2층은 **개별 실행**을 평가한다. 결과는 해당 실행 폴더의 `eval/` 하위에 저장한다.
- 3층은 **코드 버전**을 평가한다. 고정 케이스를 실행해 만들어진 run들에 1층·2층 평가를 적용하고, 그 결과를 `eval/results/<git-sha>/scorecard.json`으로 집계한다.
- 세 계층 모두 `review.py`의 하위 명령으로 추가한다. 구현은 `ensemble_core/`에 `eval_process.py`, `eval_quality.py`, `eval_bench.py`로 나눈다. 기존 `fixture-metrics`, `measure-noise`와 같은 배치 방식이다.
- **경로는 `layout.py`를 통해서만 접근한다.** 현재 실행 레이아웃은 v2다: `01-input/request.md`, `03-drafts/draft-00.md`, `_state/manifest.json`, `_state/convergence.json`, `04-reviews/blind/…`. 평가 코드는 경로 문자열을 직접 조합하지 않고 `layout.py`에 평가용 헬퍼(`eval_dir`, `process_metrics`, `quality_judgment`, `judge_raw_dir`)를 추가해 쓴다. 계층 handoff의 경로 표기도 이 레이아웃을 따른다.
- **평가는 실행 상태를 바꾸지 않는다.** 평가 명령은 `manifest.json`을 포함한 실행 산출물을 수정하지 않으며, 평가 중 오류가 나도 실행을 `INFRA_ERROR` 등으로 종료 처리하지 않는다. 심판 호출 기록과 사용량도 실행의 manifest가 아니라 평가 결과 파일에 남긴다.

## 3. 프로젝트 구조 변경

```text
<project>/
├── evaluator_handoff.md               # 본 문서
├── eval/
│   ├── handoff-layer1.md              # 1층 세부 설계
│   ├── handoff-layer2.md              # 2층 세부 설계
│   ├── handoff-layer3.md              # 3층 세부 설계
│   ├── cases/                         # 3층 케이스 세트 (Git에 포함)
│   │   └── <case-id>/
│   │       ├── request.txt
│   │       └── expected.json
│   └── results/                       # 점수표 (Git 제외, §7 확정 — .gitignore에 추가)
│       └── <git-sha>/
│           └── scorecard.json
├── .claude/skills/ensemble/
│   ├── scripts/
│   │   ├── review.py                  # eval-run, eval-quality, eval-bench 하위 명령 추가
│   │   └── ensemble_core/
│   │       ├── eval_process.py        # 1층
│   │       ├── eval_quality.py        # 2층
│   │       ├── eval_bench.py          # 3층
│   │       └── providers.py           # usage 수집 추가 (§4)
│   └── references/
│       ├── judge-prompt.md, judge.schema.json                            # 2층 비교 판정
│       └── judge-expectations-prompt.md, judge-expectations.schema.json  # 3층 정답지 절대 판정
└── ensemble/runs/<run-id>/
    └── eval/                          # 실행별 평가 결과 (runs와 함께 Git 제외)
        ├── process-metrics.json
        ├── quality-judgment.json
        └── judge-raw/                 # 심판 원문 응답
```

## 4. 공통 기반: 토큰 사용량 수집

세 계층 모두 비용을 보고해야 하므로, 계층 구현 전에 사용량 수집을 먼저 넣는다. `manifest.json`에는 이미 비어 있는 `usage: {}` 필드가 예약돼 있다(`state_machine.py`의 매니페스트 초기화 참조).

### 4.1 수집 지점

| 제공자 | 방법 | 상태 |
|---|---|---|
| Codex (`run_codex`) | `--json`을 모든 호출에 켜고 stdout의 `turn.completed` 이벤트에서 사용량을 파싱 | **확인 완료(§4.4)** |
| Antigravity (`run_agy`) | 보고 수단 없음 | **확인 완료(§4.4)** — `usage: null` 고정 |
| 작성자 Claude (`claude_usage`) | Claude Code 세션 기록(JSONL)의 `message.usage`를 실행 시간 창으로 귀속 | **확인 완료(§4.5)** — 상한값 |

작성자는 CLI가 아니라 스킬을 실행하는 주체라서 `run_codex` 같은 수집 지점이 없다. 대신 Claude Code가 남기는 세션 기록에 API 응답이 보고한 토큰이 그대로 들어 있다. 프롬프트 길이로 추정하지 않는다는 원칙은 그대로 지켜진다 — 원천이 실측값이다.

현재 `run_codex`는 세션을 유지하는 검토 호출에만 `--json`을 켠다. 일회성 호출(제안, 최종 독립 검토)은 `--json` 없이 실행돼 stdout에 이벤트가 없다. 모델 응답은 이미 `--output-last-message` 파일로 받으므로, 모든 호출에 `--json`을 켜도 응답 파싱 경로는 바뀌지 않는다. 기존 `_codex_session_id()`가 `thread.started` 이벤트를 파싱하는 것과 같은 방식으로 사용량 이벤트를 파싱하는 `_codex_usage()`를 추가한다.

### 4.2 데이터 흐름

```text
run_codex / run_agy
  → ProviderResult.usage  (신규 필드, 기본 None)
      {"input_tokens": int, "cached_input_tokens": int,
       "cache_write_input_tokens": int, "output_tokens": int,
       "reasoning_output_tokens": int}
    ProviderResult.attempts_reported  (사용량을 보고한 시도 수)
  → record_provider_call(..., usage=...)
      provider_calls[] 각 항목에 usage 필드 추가 (없으면 null)
  → manifest["usage"] 집계 갱신
      {
        "codex": {
          "input_tokens": 0, "cached_input_tokens": 0,
          "cache_write_input_tokens": 0, "output_tokens": 0,
          "reasoning_output_tokens": 0,
          "calls_reported": 0, "calls_unreported": 0,
          "attempts_reported": 0, "attempts_unreported": 0
        },
        "agy": { ... }
      }
```

CLI가 보고한 필드는 **모두 보존한다**. 지금 안 쓰는 필드라도 버리면 과거 실행에서 복구할 수 없다.

용어를 명확히 한다. **논리 호출**은 `run_codex`/`run_agy` 한 번의 호출이고, **시도**는 그 안의 재시도 루프 1회다. 하나의 논리 호출이 여러 시도를 소모할 수 있다.

- `ProviderResult.usage`는 **논리 호출 안에서 사용량을 보고한 모든 시도의 합산값**이다. 스키마 오류로 재시도해도 이미 소모한 토큰은 사라지지 않으므로, `run_codex`는 검증 전에 시도별 stdout에서 사용량을 먼저 모아 둔다. 어떤 시도도 보고하지 않았으면 `None`이다.
- `calls_reported` / `calls_unreported`는 **논리 호출 수** 기준이다(시도 수가 아니다). 성공한 논리 호출이라도 사용량 보고가 전혀 없으면 `calls_unreported`로 센다.
- `attempts_reported` / `attempts_unreported`는 **시도 수** 기준이다. 일부 시도만 보고된 논리 호출은 `calls_reported`로 세면서도 `attempts_unreported`가 올라가므로, 합산값이 하한인지 여부가 이 카운터로 드러난다.
- 논리 호출이 최종 실패해도 Codex의 각 시도 stdout에 `turn.completed`가 있으면 예외 details로 합산 사용량을 전달한다. 보고가 없었던 실패 시도만 `attempts_unreported`로 센다. Agy처럼 사용량 수단이 없는 제공자는 `calls_unreported`로 남으므로, 이 값이 0보다 크면 집계는 하한값이다.
- 기존 실행에는 사용량이 없다. 1층은 `usage`가 비어 있으면 `null`로 보고하고 실패하지 않는다.

작성자 사용량은 흐름이 다르다. 호출마다 더해 나가는 것이 아니라 **창 전체를 다시 계산해 대체한다.** 여러 번 수집해도 값이 부풀지 않는다.

```text
Claude Code 세션 기록 (~/.claude/projects/<프로젝트 경로>/…jsonl)
  → claude_usage.collect_usage(window_start, window_end)
      type=assistant이고 message.usage가 있는 항목만
      message.id로 중복 제거 (한 메시지가 여러 줄로 기록된다)
      cwd가 프로젝트 안이고 시간 창에 드는 것만
  → manifest["usage"]["claude"]  (덮어쓰기)
      { …공통 토큰 필드…, "messages_counted": int,
        "attribution": "session_time_window", "upper_bound": true,
        "unreported_fields": ["reasoning_output_tokens"],
        "window": {…}, "sessions": […], "models": {모델별 내역} }
```

필드 대응은 `input_tokens` → `input_tokens`, `cache_read_input_tokens` → `cached_input_tokens`, `cache_creation_input_tokens` → `cache_write_input_tokens`, `output_tokens` → `output_tokens`다. Claude API는 추론 토큰을 따로 보고하지 않으므로 `reasoning_output_tokens`는 `unreported_fields`에 넣어 0을 실측값으로 읽지 않게 한다.

### 4.2.1 오차의 방향

**제공자마다 오차의 방향이 반대다. 이것을 한 숫자로 합치지 않는다.**

| 제공자 | 방향 | 이유 |
|---|---|---|
| Codex | **하한값** | 사용량을 보고하지 않은 호출·시도가 있으면 그만큼 빠진다 |
| Agy | **하한값** | 보고 수단이 아예 없어 전부 `calls_unreported`다 |
| 작성자 Claude | **상한값** | 세션 기록은 실행이 아니라 세션 단위다. 같은 시간 창에 실행과 무관한 작업이 섞이면 과계산된다 |

1층은 `resources.usage_incomplete`(하한 신호)와 `resources.usage_upper_bound_providers`(상한 신호)를 따로 보고한다. 3층 점수표의 `totals.usage`도 제공자별 `upper_bound` 표시를 합산 과정에서 잃지 않는다.

작성자 귀속의 알려진 한계:

- 한 세션에서 실행과 다른 작업을 병행하면 그 토큰까지 포함된다. 실행 하나만 집중해서 돌린 창일수록 정확하다.
- 같은 시간대에 실행 두 개를 돌리면 양쪽에 이중 계산된다.
- 세션 기록이 없거나 지워졌으면 `transcripts_found: false`로 0을 보고한다. 실패로 처리하지 않는다.
- 끝나지 않은 실행은 `finished_at` 대신 manifest에 남은 마지막 기록 시각을 창의 끝으로 쓴다.

### 4.3 하위 호환

- `ProviderResult.usage`와 `provider_calls[].usage`는 선택 필드다. 스키마 버전을 올리지 않는다.
- `run_agy`의 `ProviderResult` 생성부는 위치 인자를 쓰고 있으므로, `usage`는 키워드 전용 필드로 추가해 순서 실수를 막는다.

### 4.4 구현 전 확인 항목 (2026-07-23 실측 완료)

- [x] `codex exec --json`(codex-cli 0.145.0): `turn.completed` 이벤트의 `usage` 객체로 보고된다. 실측 필드: `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`. 다섯 필드 모두 기록·집계한다.
- [x] 세션 재개(`exec resume --json`)에서도 같은 `turn.completed` 이벤트가 온다.
- [x] `agy` 1.1.5: 도움말에 사용량 출력 옵션이 없다. `run_agy`는 `usage=None` 고정. agy 업데이트로 보고 수단이 생기면 이 표를 갱신한다.

### 4.5 작성자 세션 기록 실측 (2026-07-23)

- [x] 위치: `~/.claude/projects/<프로젝트 절대 경로의 `/`를 `-`로 바꾼 이름>/*.jsonl`. `CLAUDE_CONFIG_DIR`과 `ENSEMBLE_CLAUDE_TRANSCRIPT_DIR`로 재정의할 수 있다.
- [x] `type: "assistant"` 항목의 `message.usage`에 `input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `output_tokens`가 있다. 추론 토큰은 따로 없다.
- [x] **한 메시지가 여러 줄로 기록되며 각 줄이 같은 사용량을 싣는다.** 실측 1,338건 중 641건이 중복이었고 중복끼리 값이 다른 경우는 0건이었다. `message.id`로 중복을 제거하지 않으면 두 배 가까이 과계산된다.
- [x] `model` 필드로 모델별 내역을 나눌 수 있다(실측에서 opus·sonnet·fable이 섞여 있었다). `<synthetic>` 모델은 실제 API 호출이 아니므로 제외한다.
- [x] `cwd`가 프로젝트 하위 폴더로 바뀔 수 있어(실행 폴더 등) 정확히 일치가 아니라 하위 포함으로 판단한다.
- [x] 수집 명령: `collect-claude-usage --run <run>`. `finalize`가 종료 직후 자동으로 부르며, 세션 기록이 없어도 종료를 막지 않고 경고만 남긴다. 정지 상태로 멈춘 실행은 명령을 직접 부른다.

## 5. 점수표 (`scorecard.json`) 개요

3층이 만드는 최종 산출물이며, 1층·2층 결과를 포함한다. 상세 스키마는 [3층 handoff](eval/handoff-layer3.md) §5.

```json
{
  "schema_version": 1,
  "git_commit": "…",
  "ensemble_source_hash": "…",
  "created_at": "…",
  "suite": "smoke | full",
  "suite_hash": "케이스 ID·개정 해시 목록의 해시",
  "benchmark_run_id": "…",
  "tainted": false,
  "model_config": { "…": "codex·agy·작성자(선언값)·심판 모델" },
  "cases": [
    {
      "case_id": "…",
      "case_type": "init_block | state_behavior | quality",
      "repeat_index": 1,
      "run_id": "… | null (init_block)",
      "verdict": "PASS | FAIL | SKIP | UNREVIEWED",
      "process_metrics": { "…": "1층 요약" },
      "quality_judgment": { "…": "2층 요약, 해당 시" },
      "usage": { "…": "제공자별 토큰 합계" }
    }
  ],
  "totals": { "…": "케이스 합계와 사용량 합계" }
}
```

## 6. 착수 순서

각 단계는 독립적으로 완결되며, 이전 단계가 끝나기 전에 다음 단계를 시작하지 않는다.

1. **0단계 — 토큰 수집 기반**: §4 구현. 완료 기준: 새 실행의 `manifest.json`에 제공자별 토큰 합계가 기록되고, 기존 테스트가 모두 통과한다.
2. **1단계 — 1층 `eval-run`**: 기존 실행 3건에서 지표가 계산된다. 비용이 없으므로 즉시 검증 가능.
3. **2단계 — 2층 `eval-quality`**: draft-0 대 마지막 초안 블라인드 비교. 완료된 실행 1건으로 검증.
4. **3단계 — 3층 케이스 세트와 `eval-bench`**: 케이스 정답지에 사용자 검토가 필요하므로 마지막. 스모크 세트부터 시작한다.

## 7. 미결정 사항

구현 전 사용자 결정이 필요한 항목이다. 결정되면 본 문서와 해당 계층 handoff를 갱신한다.

**결정 완료 (2026-07-23)**

| 항목 | 결정 | 비고 |
|---|---|---|
| 심판 모델 | panel과 같은 `agy` 재사용 | escalation이 발생했던 실행은 심판과 패널이 같은 모델이 되는 순환이 있으므로, 2층 결과의 `panel_used_in_run: true`로 표시해 보고 시 걸러낼 수 있게 한다 |
| `eval/results/` Git 포함 여부 | **제외** | 점수표는 로컬에만 남는다. `.gitignore`에 `eval/results/`를 추가한다. 커밋 간 비교는 로컬에 쌓인 점수표로 수행하며, 점수표를 지우면 해당 커밋의 기록도 사라진다는 한계를 감수한다 |
| 케이스 도메인 | **소프트웨어 기능 명세** | 기존 rubric(AC-01~09)이 상정한 산출물과 가장 가까워 채점 신호가 선명하다. 스모크 3케이스를 이 도메인으로 작성했다 |
| 심판 입력 전달 | **번들 내용을 프롬프트에 직접 삽입** | Agy headless의 command 권한 자동 거부를 피하고 신뢰할 수 없는 번들에 셸 권한을 주지 않는다 |
| 반복 실행 | **기본 1회, 필요 시 `--repeat N`** | 단일 차이는 회귀가 아니라 회귀 신호로 표현하고, 신호 재확인 시에만 비용을 늘린다 |
| 심판 호출 게이트 | **상태 사전 채점 통과 + non-tainted일 때만 호출** | 이미 실패한 실행과 비교 불가능한 실행에 비용을 쓰지 않는다. 진단용 `--force-judge`만 예외 |
| 3층 예산 | **기본 1회 + full 실행 전 확인** | full은 명시적으로 선택하고 예상 케이스 수를 알린다. 반복 비용은 `--repeat N`을 지정한 경우에만 늘어난다 |

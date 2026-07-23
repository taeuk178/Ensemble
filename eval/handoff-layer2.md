# 2층 handoff — 결과물 품질 비교 (`eval-quality`)

> 상위 문서: [evaluator_handoff.md](../evaluator_handoff.md)
>
> 목적: 앙상블의 핵심 가설인 **"검토 루프가 문서를 실제로 개선했는가"**를 측정한다. 모든 완료된 실행에는 비교 자료가 이미 들어 있다 — 검토 전 첫 초안(`03-drafts/draft-00.md`)과 최종화에 사용된 마지막 초안(`03-drafts/draft-NN.md`)이다. 루프 밖 심판 모델이 두 문서를 어느 쪽이 나중인지 모른 채 비교한다.
>
> `final.md`를 쓰지 않는 이유: `final.md`는 마지막 초안에 보고 단계(`finalize`)가 상태 헤더와 미해결 이견·수용 위험 부록을 덧붙인 **전달물**이다. 부록에는 리뷰 이력이 원문 그대로 들어 있어 블라인드를 깨고, 애초에 검토 루프가 만든 명세가 아니다. 루프의 산출물은 초안이므로 초안끼리 비교한다. 이렇게 하면 생성 표식을 제거하는 정규화 규칙 자체가 필요 없다. 전달물 전체의 품질이 궁금해지면 별도 지표로 다룬다.

## 1. 입력과 출력

```bash
python3 .claude/skills/ensemble/scripts/review.py eval-quality --run <run_dir> [--repetitions N]
```

- 비교 대상: `03-drafts/draft-00.md` 대 **finalize가 최종 문서로 고른 마지막 초안**. 선택 규칙은 `report.py`의 `finalize`와 같다 — `manifest.current_round`의 초안, 없으면 가장 높은 번호의 초안(`layout.iter_drafts` 마지막). 어느 초안을 골랐는지 결과에 기록한다.
- 전제 조건: 실행이 종료되어 `final.md`가 있어야 하고(루프가 끝났다는 증거), `draft-00.md`가 있어야 한다. 하나라도 없으면 `SKIP` 사유를 출력하고 심판을 호출하지 않는다.
- 퇴화 케이스: 두 초안의 내용 해시가 같으면(초안이 하나뿐인 실행 포함 — 수정 없이 승인) 심판 없이 `verdict: IDENTICAL`을 기록한다.
- 출력: `<run_dir>/eval/quality-judgment.json`(`layout.quality_judgment`). 심판 원문 응답은 `<run_dir>/eval/judge-raw/`(`layout.judge_raw_dir`)에 호출별로 보존하고 덮어쓰지 않는다.
- 평가는 실행 상태를 바꾸지 않는다. 심판 호출은 실행 manifest의 `provider_calls`에 기록하지 않고, 심판 호출 실패도 실행을 종료 상태로 만들지 않는다.
- 비용: 기본 2회 호출(순서 스왑). `--repetitions N`이면 스왑 쌍을 N번 반복해 2N회.

## 2. 심판 구성

- 제공자: `run_agy` 재사용 (제3 제공자, panel과 동일). 모델·effort는 `config.py`의 panel 설정을 기본값으로 쓰되 `--model`로 재정의할 수 있다. (상위 문서 §7에서 확정)
- escalation이 발생한 실행에서는 panel과 심판이 같은 모델이 되는 순환이 있다. 결과에 `judge_model`과 `panel_used_in_run: bool`을 함께 기록해 보고 시 걸러낼 수 있게 한다.
- 프롬프트와 스키마는 `references/judge-prompt.md`, `references/judge.schema.json`으로 추가한다. 기존 panel 프롬프트와 같은 검증 경로(`validate_against_schema`)를 쓴다.
- `agy` 1.1.5는 사용량을 보고하지 않는 것으로 확인됐다(상위 문서 §4.4). 심판 호출의 `usage`는 `null`로, 호출 수는 `calls_unreported`로 기록한다.

## 3. 블라인드 규칙

`FINAL_BLIND`와 같은 격리 원칙을 따른다.

- 심판 입력 번들: 해당 실행의 `request.md`, `rubric.md`, 그리고 두 문서를 `document-1.md`, `document-2.md`로 복사한 것. **그 외에는 아무것도 넣지 않는다.** 리뷰 이력, 이슈 기록, 초안 라운드 번호, 파일 수정 시각 등 어느 쪽이 최종본인지 추론할 단서를 제거한다.
- 번들 구성은 기존 `bundle.py`의 allowlist 방식을 재사용한다.
- 호출 1은 (문서1=draft-0, 문서2=final), 호출 2는 순서를 바꾼다. 매핑은 `quality-judgment.json`에만 기록한다.
- 비교 대상이 초안끼리이므로(`final.md` 미사용, §1) 생성 표식을 제거하는 정규화는 필요 없다. 초안은 어떤 가공도 없이 그대로 전달한다.
- 초안 **본문**에 남은 단서(예: "검토 반영" 같은 변경 이력 문구)는 제거하지 않는다 — 실제 산출물을 평가하는 것이므로 가공하면 측정 대상이 달라진다. 대신 알려진 한계로 §7에 기록한다.

## 4. 채점 축

축별로 승자만 고른다. 점수 척도(1~10 등)는 쓰지 않는다 — 척도는 심판 노이즈가 크고 실행 간 비교가 어렵다. 비교 판정(어느 쪽이 나은가)이 더 안정적이다.

| 축 | 질문 |
|---|---|
| `testable_criteria` | 완료 기준이 검증 가능한 형태로 서술된 쪽은? |
| `internal_consistency` | 용어·상태·흐름의 내부 모순이 적은 쪽은? |
| `requirement_coverage` | `request.md`의 요구를 더 빠짐없이 다룬 쪽은? |
| `over_specification` | 원문에 없는 요구를 확정 사실처럼 추가한 정도가 **적은** 쪽은? (handoff.md가 경계하는 과잉 명세) |
| `overall` | 구현자에게 건넬 명세로 더 나은 쪽은? |

각 축의 판정값은 `DOC1 | DOC2 | TIE`이며 근거 문장을 요구한다. `confidence` 필드는 두지 않는다 — 기존 원칙대로 확신도로 자동 판정하지 않으며, 수집하지 않는 값은 오용될 수 없다.

3층 품질 케이스의 `must_cover`·`must_not_assert` 채점은 **이 비교 스키마에 넣지 않는다.** 정답지를 비교 입력에 섞으면 블라인드가 깨지고, 두 판정의 성격도 다르다(비교 대 절대). 별도의 절대 판정 프롬프트·스키마(`judge-expectations-prompt.md`, `judge-expectations.schema.json`)로 분리하고, 최종 문서 하나만 입력으로 받아 3층에서만 호출한다(3층 handoff §2.3).

## 5. 판정 합성

순서 스왑 2회 호출의 결과를 축별로 합성한다.

| 호출 1 | 호출 2 (스왑) | 합성 결과 |
|---|---|---|
| final 승 | final 승 | `FINAL_BETTER` |
| draft 승 | draft 승 | `DRAFT_BETTER` (루프가 문서를 악화시킴 — 중요 신호) |
| TIE | TIE | `TIE` |
| 불일치 | — | `UNSTABLE` (순서 편향 또는 심판 노이즈) |

- `--repetitions N > 1`이면 합성 결과의 분포를 함께 기록한다. `UNSTABLE` 비율이 높은 축은 심판이나 프롬프트를 신뢰할 수 없다는 뜻이므로, 그 축의 결과를 보고에서 제외하고 프롬프트를 개선한다.
- `DRAFT_BETTER`가 나온 실행은 앙상블 관점에서 가장 중요한 표본이다. 3층 점수표에서 별도 집계한다.

## 6. 출력 스키마 (`quality-judgment.json`)

```json
{
  "schema_version": 1,
  "run_id": "…",
  "evaluated_at": "…",
  "evaluator_git_commit": "평가 시점의 git HEAD",
  "evaluator_source_hash": "평가 시점의 ensemble_source_hash",
  "judge_provider": "agy",
  "judge_model": "…",
  "judge_cli_version": "…",
  "judge_prompt_sha256": "judge-prompt.md 해시 — 프롬프트가 바뀐 판정끼리의 비교를 막는다",
  "judge_schema_sha256": "judge.schema.json 해시",
  "panel_used_in_run": false,
  "draft_doc": "03-drafts/draft-00.md",
  "final_doc": "03-drafts/draft-03.md — finalize가 고른 마지막 초안",
  "draft_sha256": "심판에 전달한 첫 초안의 해시",
  "final_sha256": "심판에 전달한 마지막 초안의 해시",
  "content_identical": false,
  "calls": [
    {
      "order": "DRAFT_FIRST | FINAL_FIRST",
      "raw_path": "eval/judge-raw/call-1.json",
      "verdicts": { "testable_criteria": "DOC1", "…": "…" },
      "usage": { "…": "보고 시. 아니면 null" }
    }
  ],
  "composite": {
    "testable_criteria": "FINAL_BETTER | DRAFT_BETTER | TIE | UNSTABLE",
    "internal_consistency": "…",
    "requirement_coverage": "…",
    "over_specification": "…",
    "overall": "…"
  },
  "usage_total": { "…": "심판 호출 토큰 합계. 미보고 시 null + calls_unreported" }
}
```

심판 호출의 사용량은 실행의 `manifest.json`이 아니라 이 파일에 기록한다. 평가 비용과 실행 비용을 섞지 않는다.

## 7. 알려진 한계

측정 결과를 보고할 때 함께 명시한다.

- 최종 문서에 남은 문체·구조 단서로 심판이 최종본을 알아챌 수 있다. 순서 스왑은 위치 편향만 제거하며 이 단서는 제거하지 못한다.
- 심판이 "길고 상세한 문서"를 선호하는 편향이 알려져 있다. `over_specification` 축이 이를 일부 상쇄하지만 제거하지는 못한다.
- draft-0 대 final 비교는 "루프가 개선했는가"를 재는 것이지 "최종 문서가 절대적으로 좋은가"를 재는 것이 아니다. 절대 품질은 3층의 케이스 기대 결과로만 다룬다.

## 8. 테스트 계획

심판 호출부는 기존 provider 테스트처럼 모의(mock)로 검증한다.

구현 위치: `tests/test_eval.py`.

- [x] 두 초안 동일(초안 하나뿐인 실행 포함) → 심판 미호출, `IDENTICAL`
- [x] `final.md` 없음(미종료 실행) → `SKIP`, 심판 미호출
- [x] 마지막 초안 선택이 `finalize`와 같은 규칙을 따르는지: `current_round` 초안이 없으면 가장 높은 번호의 초안으로 대체
- [x] 심판 입력 번들에 `final.md`가 들어가지 않는지 (초안 원문만 전달)
- [x] 모의 심판 응답으로 합성 규칙 4가지(일치 승, 일치 패, TIE, 불일치) 검증
- [x] 스키마 위반 심판 응답 → 기존 `SchemaError` 재시도 경로를 타는지 — `validate_judge_schema`가 `run_agy`의 기존 검증 경로에 등록돼 별도 분기가 없다
- [x] 순서 매핑(DOC1/DOC2 ↔ draft/final)이 스왑 호출에서 올바르게 역변환되는지 — 이 매핑 버그는 결과를 정반대로 뒤집으므로 가장 중요한 테스트
- [x] `--repetitions 3`에서 분포 집계 검증
- [x] 심판 원문이 재평가 시에도 덮어쓰이지 않는지 (`call-N.json` 번호가 이어 붙는지)

## 9. 완료 기준

- [ ] **미완료** — 완료된 실제 실행 1건에서 live 호출로 `quality-judgment.json`이 생성된다. 모의 호출로만 검증했으며 `agy` 실호출 확인이 남았다
- [x] `measure-noise`처럼 반복 실행으로 심판 안정성을 확인하는 절차가 README 평가 명령 절에 문서화된다
- [x] 위 테스트가 모두 통과한다
- [ ] **미완료** — 심판 프롬프트(`judge-prompt.md`)를 사용자가 검토했다 (정답지 원칙)

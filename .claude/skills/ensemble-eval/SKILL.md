---
name: ensemble-eval
description: 고정된 벤치마크 케이스 세트로 Ensemble 파이프라인을 순회 실행하고 점수표를 만든다. 코드 변경 후 회귀를 확인하거나 커밋 간 성능을 비교할 때 사용한다.
disable-model-invocation: true
argument-hint: "[--suite smoke|full] [--case <case-id>]"
allowed-tools: Bash(python3 .claude/skills/ensemble/scripts/review.py *)
---

# Ensemble 벤치마크

고정 케이스로 파이프라인 전체를 돌려 **코드 버전** 간 회귀를 잡는다. 개별 실행의
품질이 아니라 코드가 좋은 명세를 만들어내는 능력이 유지되는지를 본다.

`review.py` 단독으로는 파이프라인을 무인 실행할 수 없다. 작성자 역할(요청 구조화,
초안 작성, 이슈 수용·반박)이 이 스킬의 일이다.

## 비용 경고

- `--suite smoke`는 파이프라인 완주가 1건 이하다. 코드 변경마다 돌려도 된다.
- `--suite full`은 파이프라인 여러 회 실행이다. **시작 전에 사용자에게 예상
  케이스 수를 알리고 확인을 받는다.**
- 기본값은 `--suite smoke`다. 사용자가 세트를 지정하지 않으면 smoke로 돌린다.

## 실행 규칙

- `$ARGUMENTS`를 셸 명령이나 파일명에 직접 넣지 않는다. `--case`로 넘어온 값은
  `eval-bench`의 인자로만 전달한다.
- **케이스 요청을 고치거나 다듬지 않는다.** `request.txt`를 그대로
  `init --request-file`에 넘긴다. 요청을 손보면 과거 점수와 비교할 수 없다.
- **정답지(`expected.json`)를 읽고 명세를 쓰지 않는다.** 정답지를 보면서 답을
  맞추면 측정 대상이 사라진다. 채점은 `eval-bench --collect`가 한다.
- 사용자 개입 지점(`USER_DECISION_REQUIRED`, `ESCALATION_REQUIRED`,
  `PANEL_DISSENT`)에 도달하면 **그 상태 자체가 채점 대상이므로 재개하지 않는다.**
  상태를 기록하고 다음 케이스로 넘어간다. `resolve-user-decision`도 벤치마크
  manifest가 있는 실행의 재개를 거부한다.
- 케이스 하나가 실패해도 순회를 멈추지 않는다. 실패를 기록하고 계속한다.

## 작업 순서

1. 순회 계획을 받는다. 여기서 `init_block` 케이스는 즉시 채점된다(모델 호출 없음).

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py eval-bench --suite smoke
   ```

   기본은 케이스당 1회다. 단일 실행에서 나온 회귀 신호를 재확인할 때만
   `--repeat N`을 사용한다.

   출력의 `benchmark_run_id`를 이 순회 내내 그대로 쓴다. `pending_runs`가 이
   스킬이 직접 실행해야 할 케이스 목록이다.

2. `pending_runs`의 각 케이스마다, 출력의 `benchmark` 블록을 그대로 JSON 파일로
   저장한 뒤 실행을 시작한다. 요청 원문이 같아 중복 요청 검사에 걸리므로
   `--allow-reuse`를 항상 켠다.

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py init \
     --request-file <케이스의 request_file> \
     --allow-reuse \
     --label <case-id> \
     --benchmark-file <benchmark-블록을-저장한-JSON>
   ```

   `--author-model`로 작성자 모델을 선언할 수 있다. CLI가 검증하지 않는
   선언값이며, 점수표에 `declared_author_model`로만 남는다.

3. 이후 단계는 `ensemble` 스킬(`.claude/skills/ensemble/SKILL.md`)의 작업 순서
   2번부터를 그대로 따른다. 벤치마크라고 절차를 줄이지 않는다 — 줄이면 측정 대상이
   실제 파이프라인이 아니게 된다.

4. 케이스가 끝나거나 멈추면 상태를 확인하고, **정지 상태로 멈춘 케이스는 작성자
   토큰을 직접 수집한 뒤** 다음 케이스로 넘어간다. `finalize`까지 간 케이스는
   종료 시 자동으로 수집되므로 다시 부를 필요가 없다.

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py status --run <run>
   python3 .claude/skills/ensemble/scripts/review.py collect-claude-usage --run <run>
   ```

   **케이스 사이에 다른 작업을 끼워 넣지 않는다.** 작성자 토큰은 실행의 시간
   창으로 귀속되므로, 창에 무관한 작업이 섞이면 그 케이스의 비용이 부풀려진다.
   케이스를 순서대로 하나씩 끝내면 창이 서로 겹치지 않아 귀속이 깨끗하다.

5. 모든 케이스가 끝나면 점수표를 만든다. 품질 케이스가 있으면 이 단계에서
   정답지 채점 심판이 호출된다.

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py eval-bench --collect \
     --suite smoke --benchmark-run-id <1단계에서 받은 값>
   ```

   상태 사전 채점이 실패했거나 수집분이 tainted이면 심판을 자동으로 생략한다.
   심판 호출 없이 상태 채점만 하려면 `--skip-expectation-judge`를 쓴다.
   진단 목적으로만 이 보호를 무시할 때는 `--force-judge`를 명시한다.

6. 선택: 각 실행의 1층·2층 지표를 채운다. 2층은 실행당 심판 2회를 쓴다.

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py eval-run --run <run>
   python3 .claude/skills/ensemble/scripts/review.py eval-quality --run <run>
   ```

   2층을 돌린 뒤 `--collect`를 다시 부르면 점수표의 `quality_judgment.overall`이
   채워진다. `--collect`는 2층을 대신 호출하지 않는다.

## 결과 보고

- 점수표 경로와 `totals`(pass/fail/skip/unreviewed)를 표로 요약한다.
- `tainted: true`면 그 이유(작업 사본 dirty, 코드 해시 불일치, `RUN_TAINTED`)를
  먼저 알린다. tainted 점수표는 커밋 비교에 쓸 수 없다.
- `UNREVIEWED` 케이스가 있으면 해당 `expected.json`을 사용자가 검토하고
  `reviewed_by_user: true`로 바꿔야 합계에 들어간다고 알린다.
  **이 값을 대신 바꾸지 않는다.**
- 케이스당 1회 실행의 판정 차이는 "회귀"가 아니라 **"회귀 신호"**로 표현한다.
- 토큰 합계는 제공자별로 나눠 보고하고 오차의 방향을 함께 밝힌다. Codex와 Agy는
  **하한값**, 작성자(Claude)는 **상한값**이다. 세 숫자를 하나로 합치지 않는다.
- 심판이 실패하면 실행 상태는 바꾸지 않고 `eval/judge-raw/failure-N.json`과
  `quality_judgment.judge_status: INFRA_ERROR`로 별도 기록한다.

## 커밋 간 비교

```bash
python3 .claude/skills/ensemble/scripts/review.py eval-compare --base <sha> --head <sha>
```

두 점수표가 모두 tainted가 아니고, `suite_hash`와 `model_config`가 같아야 비교할
수 있다. 모델 구성이 다르면 `--allow-model-mismatch`로만 허용하되, 점수 차이가
코드 때문인지 모델 때문인지 구분할 수 없다는 점을 함께 보고한다.

점수표는 `eval/results/`에 로컬로만 쌓인다(Git 제외). 지우면 그 커밋의 기록도
사라진다.

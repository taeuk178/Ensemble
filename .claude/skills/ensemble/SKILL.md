---
name: ensemble
description: 사용자 요청을 Claude와 GPT의 독립 분석, 구조화 리뷰, 수정 합의를 통해 구현 가능한 문서 스펙으로 발전시킨다.
disable-model-invocation: true
argument-hint: "[--from <path> | 무엇을 만들지에 대한 설명]"
allowed-tools: Bash(python3 .claude/skills/ensemble/scripts/review.py *)
---

# Ensemble

이 스킬은 Claude가 작성자 역할을 맡고 `review.py`가 실행 상태·검증·외부 평가를 맡는 문서 스펙 앙상블이다.

## 실행 원칙

- `$ARGUMENTS`를 직접 셸 명령이나 파일명에 보간하지 않는다.
- 옵션을 먼저 해석한다. `--from <path>`와 positional 요청이 함께 있으면 중단하고 사용자에게 하나를 선택하게 한다.
- 인자가 없으면 반드시 사용자에게 **“무엇을 만들 건가요?”**라고 질문한다. 루트 `request.md`를 자동으로 읽지 않는다.
- 처음 실행하기 전에 request·rubric·draft가 OpenAI에 전송되고, 3단계 패널 사용 시 쟁점이 Google에 전송될 수 있음을 알린다.
- `decisions.md`나 전체 registry를 GPT 입력에 넣지 않는다.

## 워크플로

1. 요청을 확보한 후 아래 중 하나를 실행한다.

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py init --request-file <안전한-임시-파일>
   python3 .claude/skills/ensemble/scripts/review.py init --from <명시된-request.md>
   ```

   출력 JSON의 `run_dir`를 이후 모든 명령의 `--run`에 사용한다.

2. 사용자 원문을 보존한 채 `request.md`의 구조화된 작업 입력·가정·확인 항목을 구체화한다. 프로젝트별 수용 기준이 필요하면 `rubric.md`에 검증 가능한 `AC-NN` 기준을 추가한다. 첫 draft 전에만 다음 명령으로 교체할 수 있다.

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py save --run <run> --kind request --source <구조화-request>
   python3 .claude/skills/ensemble/scripts/review.py save --run <run> --kind rubric --source <rubric>
   ```

3. run의 `request.md`와 `rubric.md`만 보고 Claude 독립 제안을 작성한다. 내용을 임시 파일에 쓴 뒤 저장한다.

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py save --run <run> --kind claude-proposal --source <file>
   python3 .claude/skills/ensemble/scripts/review.py propose --run <run>
   ```

4. 두 제안을 읽은 뒤 `drafts/round-0.md`를 합성해 저장한다.

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py save --run <run> --kind draft --round 0 --source <file>
   ```

5. 일반 리뷰를 실행한다.

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py review --run <run> --round 1
   ```

6. `NEEDS_REVISION`이면 모든 이슈에 대해 `decision` 명령으로 `ACCEPT`/`REJECT`/`DEFER`를 기록한다. `ACCEPT`는 draft를 수정하고 다음 번호로 저장한다. `REJECT`는 근거를 명시하며 직접 해결 처리하지 않는다. 사용자 원문이나 자유 서술을 셸 인자에 넣지 말고 아래 객체를 JSON 파일로 저장해 전달한다.

   ```json
   {
     "issue_id": "R1-I1",
     "round": 1,
     "disposition": "ACCEPT",
     "author_severity": 4,
     "claim": "실패 복구 흐름이 필요하다.",
     "evidence_ref": "rubric.md AC-02",
     "requested_disposition": "MODIFY",
     "argument": "구현자가 실패 상태를 임의 결정하지 않게 한다.",
     "action": "오류 흐름 절을 추가한다."
   }
   ```

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py decision --run <run> --input <decision.json>
   ```

7. `APPROVED`이면 FINAL_BLIND를 실행한다.

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py final-blind --run <run>
   ```

   신규·미수용 blocker가 있으면 아래 명령으로 이슈를 레지스트리에 승격한 뒤 일반 루프로 되돌리고, 없으면 finalize한다.

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py promote-final --run <run>
   ```

8. 종료 상태를 자동 판정해 `final.md`와 manifest를 완성한다.

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py finalize --run <run> --status auto
   ```

9. `USER_DECISION_REQUIRED`나 `PANEL_DISSENT`는 자동으로 해결하지 않는다. 사용자의 선택을 받은 뒤 `accept-risk`, 수정 재개, `STABLE_DISSENT`, `CANCELLED` 중 하나를 명시적으로 적용한다.

   위험 수용 메모도 파일로 전달한다.

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py accept-risk --run <run> --issue <id> --round <N> --note-file <note>
   ```

## 실패 처리

- `SCHEMA_ERROR`, `SEMANTIC_VALIDATION_ERROR`, `INFRA_ERROR`를 구분해 그대로 보고한다.
- 반복 상한은 승인으로 취급하지 않고 `ITERATION_LIMIT_REACHED`로 종료한다.
- FINAL_BLIND 원본은 수정하지 않는다. 수용 위험 대조 결과는 `final-reconciliation.json`에서 확인한다.

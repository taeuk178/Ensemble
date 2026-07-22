---
name: ensemble
description: 사용자 요청을 Codex와 GPT가 각각 분석하고, 검토와 수정을 거쳐 구현 가능한 명세로 만든다. 제품·기능·문서 요구를 구체화하고 완료 기준과 미결정 사항을 정리할 때 사용한다.
allowed-tools: Bash(python3 .agents/skills/ensemble/scripts/review.py *)
---

# Ensemble

Codex가 명세를 작성하고 GPT가 독립적으로 검토한다. `review.py`로 파일, 검토 순서, 이슈, 종료 상태를 관리한다.

## 실행 규칙

- `$ARGUMENTS`를 셸 명령이나 파일명에 직접 넣지 않는다.
- 옵션을 먼저 해석한다. `--from <path>`와 직접 입력한 요청이 함께 있으면 사용자에게 하나를 선택받는다.
- 입력이 없으면 **“무엇을 만들 건가요?”**라고 묻는다. 루트 `request.md`를 자동으로 읽지 않는다.
- 첫 실행 전에 요청·완료 기준·초안이 OpenAI로 전송된다고 알린다. 추가 평가자를 쓰는 3단계에서는 쟁점이 Google로 전송될 수 있다고 함께 알린다.
- 작성자 결정 전체와 전체 이슈 기록을 GPT 입력에 넣지 않는다.

## 작업 순서

1. 요청으로 새 실행을 만든다.

   ```bash
   python3 .agents/skills/ensemble/scripts/review.py init --request-file <안전한-임시-파일>
   python3 .agents/skills/ensemble/scripts/review.py init --from <명시된-request.md>
   ```

   반환된 `run_dir`를 이후 모든 명령의 `--run`에 사용한다.

2. 사용자 원문은 그대로 두고 `request.md`의 목표·범위·가정·확인 항목을 구체화한다. 필요한 완료 기준은 `rubric.md`에 `AC-NN` 형식으로 추가한다. 두 파일은 첫 초안을 저장하기 전까지만 바꾼다.

   ```bash
   python3 .agents/skills/ensemble/scripts/review.py save --run <run> --kind request --source <구조화-request>
   python3 .agents/skills/ensemble/scripts/review.py save --run <run> --kind rubric --source <rubric>
   ```

3. 해당 실행의 `request.md`와 `rubric.md`만 읽고 Codex의 제안을 작성해 저장한다. 이어 GPT의 독립 제안을 받는다. 내부 호환성을 위해 제안 종류는 `claude-proposal`을 사용한다.

   ```bash
   python3 .agents/skills/ensemble/scripts/review.py save --run <run> --kind claude-proposal --source <file>
   python3 .agents/skills/ensemble/scripts/review.py propose --run <run>
   ```

4. 두 제안을 합쳐 첫 초안을 저장한다.

   ```bash
   python3 .agents/skills/ensemble/scripts/review.py save --run <run> --kind draft --round 0 --source <file>
   ```

5. 초안을 검토한다.

   ```bash
   python3 .agents/skills/ensemble/scripts/review.py review --run <run> --round 1
   ```

   `--round`는 검토 번호다. 초안 번호와 같다고 가정하지 않는다. 특정 초안을 검토할 때만 `--draft-round <N>`을 쓴다.

6. 결과가 `NEEDS_REVISION`이면 모든 이슈에 `ACCEPT`(수용), `REJECT`(반박), `DEFER`(보류) 중 하나를 기록한다. 수용한 이슈는 문서에 반영하고 다음 번호의 초안으로 저장한다. 반박만 했다면 초안을 복사하지 않는다. 자유 서술은 셸 인자가 아닌 JSON 파일로 전달한다.

   ```json
   {
     "issue_id": "R1-I1",
     "round": 1,
     "disposition": "ACCEPT",
     "author_severity": 4,
     "claim": "실패 시 복구 흐름이 필요하다.",
     "evidence_ref": "rubric.md AC-02",
     "requested_disposition": "MODIFY",
     "argument": "저장 실패 시 작성 내용이 사라질 수 있다.",
     "action": "저장 실패와 재시도 흐름을 추가한다."
   }
   ```

   ```bash
   python3 .agents/skills/ensemble/scripts/review.py decision --run <run> --input <decision.json>
   ```

7. 결과가 `APPROVED`이면 이전 검토 이력을 숨긴 최종 독립 검토(`FINAL_BLIND`)를 실행한다.

   ```bash
   python3 .agents/skills/ensemble/scripts/review.py final-blind --run <run>
   ```

   새로 발견된 진행 차단 이슈가 있으면 등록하고 일반 검토로 돌아간다.

   ```bash
   python3 .agents/skills/ensemble/scripts/review.py promote-final --run <run>
   ```

8. 남은 차단 이슈가 없으면 최종 문서를 만든다.

   ```bash
   python3 .agents/skills/ensemble/scripts/review.py finalize --run <run> --status auto
   ```

9. 매 명령의 `state`를 확인한다. `USER_DECISION_REQUIRED`, `ESCALATION_REQUIRED`, `PANEL_DISSENT`에서는 자동으로 진행하지 않는다. 사용자 선택을 받은 뒤 아래 명령으로 재개하거나 위험 수용을 기록한다.

   ```bash
   python3 .agents/skills/ensemble/scripts/review.py resolve-user-decision \
     --run <run> --action REVISE --note-file <user-decision.txt>
   python3 .agents/skills/ensemble/scripts/review.py accept-risk \
     --run <run> --issue <id> --round <N> --note-file <note>
   ```

   문서를 고치지 않고 검토만 이어가려면 `--action CONTINUE`를 사용한다.

## 오류 처리

- `SCHEMA_ERROR`(형식 오류), `SEMANTIC_VALIDATION_ERROR`(내용 규칙 오류), `INFRA_ERROR`(실행 환경 오류)를 구분해 보고한다.
- 최대 반복 횟수에 도달하면 승인하지 않고 `ITERATION_LIMIT_REACHED`로 끝낸다.
- 최종 독립 검토 원본은 수정하지 않는다. 위험 수용 대조 결과는 `final-reconciliation.json`에서 확인한다.
- 전체 흐름은 `timeline.md`에서 확인한다. 세부 근거는 `decisions.md`, `reviews/`, `issue-registry.json`을 기준으로 한다.
- 실행 중 Ensemble 코드가 바뀌어 `RUN_TAINTED`가 되면 해당 실행을 버리고 새로 시작한다.

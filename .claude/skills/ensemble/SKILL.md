---
name: ensemble
description: 사용자 요청을 Claude와 GPT가 각각 분석하고, 검토와 수정을 거쳐 구현 가능한 명세로 만든다. 제품·기능·문서 요구를 구체화하고 완료 기준과 미결정 사항을 정리할 때 사용한다.
disable-model-invocation: true
argument-hint: "[--from <path> | 무엇을 만들지에 대한 설명]"
allowed-tools: Bash(python3 .claude/skills/ensemble/scripts/review.py *)
---

# Ensemble

Claude가 명세를 작성하고 GPT가 독립적으로 검토한다. `review.py`로 파일, 검토 순서, 이슈, 종료 상태를 관리한다.

## 실행 규칙

- `$ARGUMENTS`를 셸 명령이나 파일명에 직접 넣지 않는다.
- 옵션을 먼저 해석한다. `--from <path>`와 직접 입력한 요청이 함께 있으면 사용자에게 하나를 선택받는다.
- 입력이 없으면 **“무엇을 만들 건가요?”**라고 묻는다. 루트 `request.md`를 자동으로 읽지 않는다.
- 첫 실행 전에 요청·완료 기준·초안이 OpenAI로 전송된다고 알린다. 추가 평가자를 쓰는 3단계에서는 쟁점이 Google로 전송될 수 있다고 함께 알린다.
- 작성자 결정 전체와 전체 이슈 기록을 GPT 입력에 넣지 않는다.

## 진행 상황 알림

전체 흐름이 길고 외부 호출이 느리므로, 사용자가 지금 어디까지 왔는지 항상 알 수 있게 한다.

- 각 단계의 명령을 실행하기 전에 `**N단계: <하는 일>**` 한 줄을 먼저 출력한다. `N`은 아래 작업 순서의 번호를 그대로 쓴다.
- 5~7단계처럼 반복되는 단계는 회차를 함께 적는다. 예: `**5단계: 초안 검토 (검토 2회차 · 초안 1)**`
- `propose`, `review`, `final-blind`는 수 분이 걸린다. 끝나면 판정(`APPROVED`, `NEEDS_REVISION` 등)과 새 이슈 수를 한 줄로 알린다.
- 1단계에서 `run_dir`를 만든 직후 실행 ID를 한 번 알린다.
- 상태가 `USER_DECISION_REQUIRED`, `ESCALATION_REQUIRED`, `PANEL_DISSENT`가 되면 멈춘 이유와 사용자가 고를 수 있는 선택지를 함께 제시한다.
- 종료 후에는 단계별 판정과 발견·수정된 이슈를 표로 요약하고 `final.md` 경로를 알린다.

## 작업 순서

1. 요청으로 새 실행을 만든다.

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py init --request-file <안전한-임시-파일>
   python3 .claude/skills/ensemble/scripts/review.py init --from <명시된-request.md>
   ```

   반환된 `run_dir`를 이후 모든 명령의 `--run`에 사용한다.

2. 사용자 원문은 그대로 두고 `request.md`의 목표·범위·가정·확인 항목을 구체화한다. 필요한 완료 기준은 `rubric.md`에 `AC-NN` 형식으로 추가한다. 두 파일은 첫 초안을 저장하기 전까지만 바꾼다.

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py save --run <run> --kind request --source <구조화-request>
   python3 .claude/skills/ensemble/scripts/review.py save --run <run> --kind rubric --source <rubric>
   ```

3. 해당 실행의 `request.md`와 `rubric.md`만 읽고 Claude의 제안을 작성해 저장한다. 이어 GPT의 독립 제안을 받는다.

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py save --run <run> --kind claude-proposal --source <file>
   python3 .claude/skills/ensemble/scripts/review.py propose --run <run>
   ```

4. 두 제안을 합쳐 첫 초안을 저장한다.

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py save --run <run> --kind draft --round 0 --source <file>
   ```

5. 초안을 검토한다.

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py review --run <run> --round 1
   ```

   `--round`는 검토 번호다. 초안 번호와 같다고 가정하지 않는다. 특정 초안을 검토할 때만 `--draft-round <N>`을 쓴다.

   일반 검토는 같은 실행의 같은 요청에서 Codex 세션을 이어 쓴다. 첫 검토에서 만든 세션 ID는 요청 해시와 실행 ID에 묶어 `manifest.json`에 기록한다. 이후 검토는 이 세션을 재개하며, 둘 중 하나라도 다르면 재사용하지 않는다. 제안, 추가 판단, 이슈 점검, 최종 독립 검토에는 이 세션을 쓰지 않는다.
   한 세션은 최대 3개 일반 검토까지만 이어 쓰고 이후에는 이슈 projection을
   바탕으로 새 세션을 시작한다. 첫 검토는 API 계약, 날짜·시간대 경계, 데이터
   연결, 캐시 수명, 실패·재시도, 개인정보 경계를 포함해 문서 전체를 점검한다.

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
   python3 .claude/skills/ensemble/scripts/review.py decision --run <run> --input <decision.json>
   ```

   같은 이슈를 수정했는데 다음 검토에서도 다시 수용하게 되면 작은 패치를
   반복하지 않는다. 명령 결과의 `repair_plan_required_issue_ids`를 확인하고
   아래 계획을 먼저 기록한다.

   ```json
   {
     "root_cause": "근본 원인",
     "invariant": "항상 지켜야 하는 조건",
     "counterexample": "현재 설계가 실패하는 구체적 예",
     "state_model": "필요한 상태·데이터·전이",
     "verification_steps": ["검증 예시 1", "검증 예시 2"]
   }
   ```

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py repair-plan \
     --run <run> --issue <id> --round <N> --input <repair-plan.json>
   ```

7. 결과가 `APPROVED`이면 이전 검토 이력을 숨긴 최종 독립 검토(`FINAL_BLIND`)를 실행한다.

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py final-blind --run <run>
   ```

   현재 초안의 일반 검토 결과가 `APPROVED`일 때만 실행한다. 같은 초안에는
   한 번만 실행한다. 새로 발견된 진행 차단 이슈가 있으면 등록하고 일반
   검토로 돌아간 뒤, 수정 초안을 다시 승인받아야 다음 최종 검토를 실행한다.
   승격은 일반 검토 횟수를 소비하지 않지만, 확증 편향을 줄이기 위해 기존
   Codex 검토 세션을 닫고 다음 일반 검토에서 새 세션을 시작한다.

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py promote-final --run <run>
   ```

8. 남은 차단 이슈가 없으면 최종 문서를 만든다.

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py finalize --run <run> --status auto
   ```

9. 매 명령의 `state`를 확인한다. `USER_DECISION_REQUIRED`, `ESCALATION_REQUIRED`, `PANEL_DISSENT`에서는 자동으로 진행하지 않는다. `decision_owner: USER`인 이슈는 작성자가 대신 정하지 않는다. 사용자 선택을 받은 뒤 권위 있는 결정 내용을 구조화 JSON으로 기록해 재개하거나 위험 수용을 기록한다.

   ```json
   {
     "action": "REVISE",
     "audit_note": "사용자와 기본 경로를 확정했다.",
     "authoritative_decisions": [
       {
         "decision": "기본 입력은 현재 디렉터리의 .env이다.",
         "supersedes": []
       }
     ]
   }
   ```

   ```bash
   python3 .claude/skills/ensemble/scripts/review.py resolve-user-decision \
     --run <run> --decision-file <user-decision.json>
   python3 .claude/skills/ensemble/scripts/review.py accept-risk \
     --run <run> --issue <id> --round <N> --note-file <note>
   ```

   문서를 고치지 않고 검토만 이어가려면 JSON의 `action`을 `CONTINUE`로 둔다.
   기록된 결정은 `01-input/user-decisions.json`에 투영되어 다음 일반·최종·패널
   검토의 권위 입력으로 전달된다. 기존 결정을 바꾸면 해당 ID를 `supersedes`에
   넣는다. 이 파일을 직접 수정하거나 작성자 판단을 `source: USER`로 기록하지
   않는다. 직접 수정은 다음 모델 호출 전에 무결성 오류로 거부된다.
   `accept-risk`는 사용자가 명시적으로 위험 수용을 선택했고 해당 이슈가
   `pending_user_issue_ids`에 있을 때만 실행한다.

## 오류 처리

- `SCHEMA_ERROR`(형식 오류), `SEMANTIC_VALIDATION_ERROR`(내용 규칙 오류), `INFRA_ERROR`(실행 환경 오류)를 구분해 보고한다.
- 일반 검토, 최종 독립 검토, 전체 provider 호출 한도를 각각 센다. 어느 한도든
  다음 필수 단계를 수행할 수 없으면 승인하지 않고 `ITERATION_LIMIT_REACHED`로 끝낸다.
- 기본 한도는 일반 검토 5회, 최종 독립 검토 2회다.
- 최종 독립 검토 원본은 수정하지 않는다. 위험 수용 대조 결과는 `final-reconciliation.json`에서 확인한다.
- 전체 흐름은 `timeline.md`에서 확인한다. 세부 근거는 `decisions.md`, `reviews/`, `issue-registry.json`을 기준으로 한다.
- 실행 중 Ensemble 코드가 바뀌어 `RUN_TAINTED`가 되면 해당 실행을 버리고 새로 시작한다.

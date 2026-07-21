# Fixture: Inconsistent terminology

## 목표

사용자가 작업을 보관할 수 있는 문서를 정의한다.

## 데이터 모델

- `Task`: 사용자가 만든 작업이다.
- `Archive`: 완료된 작업의 보관 장소다.

## 사용자 흐름

사용자가 할 일을 완료하면 `Todo`를 `History`로 이동한다.

## 완료 조건

- 완료된 Task가 Archive 목록에 표시된다.

<!-- 의도된 결함: Task/Todo, Archive/History 용어가 일치하지 않는다. -->

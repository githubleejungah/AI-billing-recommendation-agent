# 과금 추천 에이전트 — 개발 규칙

고객의 이용·청구 데이터를 매달 분석해 더 맞는 요금제를 먼저 알리고, 고객이 직접 바꾸거나 AI 상담·담당자 연결을 할 수 있게 하는 제품입니다. 과정 프로젝트이자 포트폴리오이며, 데이터는 모두 가상입니다.

## 먼저 읽을 것

- `design/requirements.md` 명세(요구사항 ID, 계산 규칙, 결정 기록, 요구사항-테스트 연결표)
- `design/tasks.md` 작업 순서와 승인 게이트. **게이트를 통과하기 전의 작업은 시작하지 않는다.**
- `design/plan.md` 구조, `design/security.md` 데이터 보안 정책

## 일하는 순서 (SDD)

1. 명세를 바꿔야 하면 코드보다 명세를 먼저 고치고 변경 이력에 남긴다.
2. 구현과 테스트를 함께 쓴다. 기대 금액은 손으로 계산한 값을 테스트 주석에 적는다.
3. 테스트가 통과해야 끝난 것이다. 끝나면 연결표 상태와 `tasks.md`를 고친다.

## 절대 규칙

- **가상 데이터만 쓴다.** 실제 고객 데이터를 이 저장소, MCP 도구, Claude에 연결하지 않는다.
- **금액은 AI가 계산하거나 쓰지 않는다.** 숫자는 `src/simulator.py`의 결과로만 채운다(자리표시자).
- **AI가 SQL을 만들지 않는다.** DB는 정해진 질의만, 읽기 전용으로 연다.
- **Claude로 고객 이름·ID·이용 수치를 보내지 않는다.** 상황 카드(범주 정보)만 보낸다.
- **AI는 요금제 변경을 실행하지 않는다.** 결제 권한자가 확인 화면에서 직접 누를 때만 접수된다.
- 서류·해설 문장은 정해진 틀에 값을 채운다. AI가 쓴 문장을 서류에 넣지 않는다.

## 보호 파일 (훅이 막음, 사람이 직접 고침)

`.env`(읽기 포함), `.mcp.json`, `.claude/settings.json`, `.claude/hooks/`, `logs/`, `data/`, `database/billing_mock.db`, `reference/`, `config/data_policy.toml`(보안 책임자가 승인하는 정책 파일. 고칠 때는 `[approval]`도 새로 적음)

## 명령 (Windows PowerShell, 프로젝트 폴더에서)

```powershell
.venv\Scripts\python -m pytest -q          # 전체 테스트
.venv\Scripts\python database\init_db.py    # 가상 DB 다시 만들기(고객 203명)
.venv\Scripts\python src\simulator.py user_002
```

## 폴더

- `src/` 계산(simulator), 서류(documents·charts·captions), 셀프 변경(change), 검색(knowledge), MCP 서버, 감사 기록(audit), 앱 저장소(history)
- `database/init_db.py` 가상 데이터 생성(시드 고정). `knowledge/` 지식 문서(금액 없음). `eval/` 평가 세트
- `tests/` 테스트. `design/` 명세·계획·작업·보안 정책

## 문서 쓰는 방식

문서와 메시지는 한국어로, 짧은 문장으로 쓴다. 용어는 처음 나올 때 풀어 쓴다.

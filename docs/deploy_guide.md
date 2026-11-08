# Azure 시연 배포 안내 (T16)

시연 영상을 찍을 때만 올리고, 찍고 나면 바로 지웁니다(DEP-03). **무료 크레딧 만료일 2026-12-02 전에 끝냅니다.**

## 무엇이 올라가나

| 자원 | 하는 일 |
|---|---|
| 자원 그룹 `rg-billing-<환경 이름>` | 아래 자원을 한데 묶음. 지울 때 통째로 지움 |
| App Service(B1, 리눅스) `app-billing-<환경 이름>` | 고객 화면과 담당자 화면을 한 주소의 두 페이지로 엶 |
| 로그인(Entra ID) | 로그인하지 않으면 화면이 열리지 않음(DEP-04) |
| Key Vault | Claude API 키를 보관. 앱은 관리 ID로 읽음(DEP-02) |
| Application Insights | 앱 상태·오류 확인 |
| 예산 알림 | 월 30,000원의 80%를 넘거나 넘을 것 같으면 메일 |

올라가는 데이터는 가상 데이터뿐입니다. 서버가 시작할 때 `init_db.py`로 새로 만듭니다. 이 PC의 앱 저장소·감사 기록·`.env`는 올라가지 않습니다(`.webappignore`).

## 1. 준비 (처음 한 번, 약 5분)

프로젝트 폴더의 PowerShell에서 실행합니다. 환경 이름은 영문 소문자·숫자 3~20자입니다. 다른 사람과 겹치지 않게 날짜를 붙입니다(예: `billdemo1108`).

```powershell
azd env new billdemo1108 --location koreacentral
```

```powershell
powershell -ExecutionPolicy Bypass -File infra\scripts\setup_auth.ps1
```

마지막 명령은 로그인용 앱 등록을 만듭니다. 끝에 로그인 주소가 나옵니다.

## 2. 올리기 (약 10~15분)

```powershell
azd up
```

중간에 물어보는 값:

- `anthropicApiKey`: Claude API 키. 입력해도 화면에 보이지 않습니다. Key Vault에만 저장됩니다.
- `slackWebhookUrl`: 비워 두고 Enter.
- `budgetContactEmail`: 예산 알림을 받을 메일 주소.
- 구독을 물으면 "Azure subscription 1"을 고릅니다.

끝나면 `WEB_URL`이 나옵니다. **올린 날짜와 시각을 `design/tasks.md` T16 행에 적습니다.**

예산 알림 때문에 실패하면(구독 종류에 따라 막힐 수 있음) 아래를 실행하고 다시 `azd up`합니다.

```powershell
azd env set AZURE_CREATE_BUDGET false
```

## 3. 스모크 테스트 (DEP-05, 약 5분)

| 확인 | 방법 | 기대 |
|---|---|---|
| 로그인 없이 막힘 | 브라우저 시크릿 창으로 `WEB_URL` 열기 | Microsoft 로그인 화면으로 넘어감 |
| 화면 응답 | 로그인 후 열기 | 첫 접속은 1~3분 걸릴 수 있음(글꼴 설치, 가상 DB 생성). 고객 화면이 뜸 |
| 서류 1건 발행 | 고객 화면 → 비용 절감 검토서 만들기 → PDF | 한글이 보이고 절감액 598,000원 |
| 셀프 변경 접수·취소 | 요금제 변경 → 확인 화면 → 체크 → 접수 → 접수 취소 | 접수 번호, "접수를 취소했습니다" |

결과를 `design/tasks.md` T16 메모에 적습니다. 화면이 안 뜨면 Azure 포털 → App Service → "로그 스트림"을 봅니다.

## 4. 시연 영상 촬영

`docs/demo_guide.md`의 순서 1~10을 그대로 합니다. 화면 이동은 왼쪽 메뉴의 "고객 화면 / 담당자 화면"으로 합니다. 선별(1번)은 한 번만 누릅니다.

## 5. 지우기 (촬영 당일, 꼭)

```powershell
azd down --force --purge
```

```powershell
powershell -ExecutionPolicy Bypass -File infra\scripts\remove_auth.ps1
```

`--purge`는 Key Vault를 완전히 지웁니다(같은 이름으로 다시 올릴 수 있게). **지운 날짜를 tasks.md에 적고,** Azure 포털 → 자원 그룹 목록에서 `rg-billing-…`이 없어졌는지 확인합니다.

## 비용

B1 App Service가 대부분입니다(월 약 2만 원 수준 요금제라 하루면 1,000원 안팎). 지우면 멈춥니다. 무료 체험은 종량제로 업그레이드하지 않는 한 청구되지 않습니다.

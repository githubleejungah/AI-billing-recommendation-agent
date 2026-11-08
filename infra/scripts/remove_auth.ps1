# azd down 뒤에 한 번: 로그인용 Entra ID 앱 등록을 지운다(DEP-03, 남는 것 없이 정리).
$ErrorActionPreference = 'Stop'
$appId = (azd env get-value AUTH_CLIENT_ID).Trim()
if ($appId) {
    az ad app delete --id $appId
    Write-Output "앱 등록 삭제: $appId"
}

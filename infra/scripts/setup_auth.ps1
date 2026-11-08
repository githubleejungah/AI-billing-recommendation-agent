# azd up 전에 한 번: 로그인용 Entra ID 앱 등록을 만들고 클라이언트 ID를 azd 환경에 넣는다(DEP-04).
# 비밀(클라이언트 시크릿)은 만들지 않는다. 로그인은 ID 토큰만 쓴다.
$ErrorActionPreference = 'Stop'
$envName = (azd env get-value AZURE_ENV_NAME).Trim()
$siteHost = "app-billing-$envName.azurewebsites.net"  # infra/app.bicep의 appName 규칙과 같다
$displayName = "billing-demo-$envName"

$appId = az ad app list --display-name $displayName --query "[0].appId" -o tsv
if (-not $appId) {
    $appId = az ad app create --display-name $displayName --sign-in-audience AzureADMyOrg `
        --web-redirect-uris "https://$siteHost/.auth/login/aad/callback" `
        --enable-id-token-issuance true --query appId -o tsv
}
azd env set AUTH_CLIENT_ID $appId
Write-Output "앱 등록 준비: $displayName ($appId), 로그인 주소 https://$siteHost"

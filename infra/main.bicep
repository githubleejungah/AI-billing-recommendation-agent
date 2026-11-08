// Azure 시연 배포(DEP-01~04). azd up으로 올리고 촬영 뒤 azd down --purge로 지운다.
// 비밀값은 @secure() 매개변수로만 받아 Key Vault에 넣는다. 이 파일에는 비밀값이 없다(DEP-02).
targetScope = 'subscription'

@minLength(3)
@maxLength(20)
@description('azd 환경 이름. 자원 이름에 들어간다(영문 소문자·숫자).')
param environmentName string

@description('지역. 한국 중부가 기본(DEP-01).')
param location string = 'koreacentral'

@secure()
@description('Claude API 키. azd up이 물어본다. Key Vault에만 저장된다.')
param anthropicApiKey string

@secure()
@description('Slack Webhook 주소(없으면 비움).')
param slackWebhookUrl string = ''

@description('Entra ID 앱 등록의 클라이언트 ID. infra/scripts/setup_auth.ps1이 azd 환경에 넣는다.')
param authClientId string

@description('예산 알림을 받을 이메일.')
param budgetContactEmail string

@description('월 예산(원). 넘을 것 같으면 메일이 온다.')
param budgetAmount int = 30000

@description('예산 알림을 만들지 여부(true/false). 구독 종류 때문에 실패하면 false로 다시 올린다.')
@allowed([ 'true', 'false' ])
param createBudget string = 'true'

param budgetStartDate string = '${utcNow('yyyy-MM')}-01'

var tags = {
  'azd-env-name': environmentName
  purpose: 'demo-virtual-data-only'
}

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-billing-${environmentName}'
  location: location
  tags: tags
}

module app 'app.bicep' = {
  name: 'billing-app'
  scope: rg
  params: {
    environmentName: environmentName
    location: location
    tags: tags
    anthropicApiKey: anthropicApiKey
    slackWebhookUrl: slackWebhookUrl
    authClientId: authClientId
    budgetContactEmail: budgetContactEmail
    budgetAmount: budgetAmount
    createBudget: createBudget == 'true'
    budgetStartDate: budgetStartDate
  }
}

output AZURE_RESOURCE_GROUP string = rg.name
output WEB_APP_NAME string = app.outputs.name
output WEB_URL string = app.outputs.url

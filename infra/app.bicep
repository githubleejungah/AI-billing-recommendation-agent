// 자원 그룹 안의 자원: 앱 호스팅 1개(고객·담당자 화면을 한 주소의 두 페이지로), Key Vault, 관리 ID,
// Application Insights, 로그인(Entra ID), 예산 알림(DEP-01~04).
param environmentName string
param location string
param tags object
@secure()
param anthropicApiKey string
@secure()
param slackWebhookUrl string
param authClientId string
param budgetContactEmail string
param budgetAmount int
param createBudget bool
param budgetStartDate string

var token = uniqueString(subscription().id, resourceGroup().id, environmentName)
var appName = 'app-billing-${environmentName}' // setup_auth.ps1이 같은 규칙으로 로그인 주소를 만든다
var keyVaultSecretsUser = '4633458b-17de-408a-b874-0445c86b69e6'

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${token}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${token}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv-${token}'
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
  }
}

resource apiKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'anthropic-api-key'
  properties: { value: anthropicApiKey }
}

resource slackSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (!empty(slackWebhookUrl)) {
  parent: vault
  name: 'slack-webhook-url'
  properties: { value: slackWebhookUrl }
}

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: 'plan-${token}'
  location: location
  tags: tags
  sku: { name: 'B1' }
  kind: 'linux'
  properties: { reserved: true }
}

var baseSettings = [
  { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
  // 앱은 지금처럼 환경 변수로 읽고, 값은 관리 ID가 Key Vault에서 가져온다(DEP-02)
  { name: 'ANTHROPIC_API_KEY', value: '@Microsoft.KeyVault(SecretUri=${apiKeySecret.properties.secretUri})' }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
  // 앱 저장소는 배포할 때마다 바뀌지 않는 /home에 둔다
  { name: 'BILLING_APP_DB', value: '/home/data/app.db' }
]
var slackSettings = empty(slackWebhookUrl) ? [] : [
  { name: 'SLACK_WEBHOOK_URL', value: '@Microsoft.KeyVault(SecretUri=${slackSecret!.properties.secretUri})' }
]

resource web 'Microsoft.Web/sites@2023-12-01' = {
  name: appName
  location: location
  tags: union(tags, { 'azd-service-name': 'web' })
  kind: 'app,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    keyVaultReferenceIdentity: 'SystemAssigned'
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.13'
      appCommandLine: 'bash startup.sh'
      webSocketsEnabled: true // Streamlit 화면은 웹소켓으로 통신한다
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appSettings: concat(baseSettings, slackSettings)
    }
  }
}

resource vaultReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, web.id, keyVaultSecretsUser)
  scope: vault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUser)
    principalId: web.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// 로그인 없이는 열리지 않는다(DEP-04). 클라이언트 비밀 없이 ID 토큰으로 로그인한다.
resource auth 'Microsoft.Web/sites/config@2023-12-01' = {
  parent: web
  name: 'authsettingsV2'
  properties: {
    platform: { enabled: true }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: authClientId
          openIdIssuer: '${environment().authentication.loginEndpoint}${subscription().tenantId}/v2.0'
        }
      }
    }
    login: { tokenStore: { enabled: true } }
  }
}

resource budget 'Microsoft.Consumption/budgets@2023-11-01' = if (createBudget) {
  name: 'budget-billing-${environmentName}'
  properties: {
    category: 'Cost'
    amount: budgetAmount
    timeGrain: 'Monthly'
    timePeriod: { startDate: budgetStartDate }
    notifications: {
      actual80: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 80
        thresholdType: 'Actual'
        contactEmails: [ budgetContactEmail ]
      }
      forecast100: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: [ budgetContactEmail ]
      }
    }
  }
}

output name string = web.name
output url string = 'https://${web.properties.defaultHostName}'

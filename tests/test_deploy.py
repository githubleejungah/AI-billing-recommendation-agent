"""배포 코드 검사(DEP-01~04). 파일 내용으로 확인한다(Azure에 접속하지 않음).

실제 배포 확인(로그인 없이 막힘, 스모크 테스트, 삭제)은 docs/deploy_guide.md 순서로 사람이 하고 tasks.md에 적는다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
MAIN = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
APP = (ROOT / "infra" / "app.bicep").read_text(encoding="utf-8")


def test_secrets_are_secure_params_and_key_vault_references():
    for text in (MAIN, APP):
        for name in ("anthropicApiKey", "slackWebhookUrl"):
            assert re.search(rf"@secure\(\)\s*(@description\('[^']*'\)\s*)?param {name} string", text), name
    assert "'@Microsoft.KeyVault(SecretUri=${apiKeySecret.properties.secretUri})'" in APP
    assert "enableRbacAuthorization: true" in APP and "type: 'SystemAssigned'" in APP
    params = json.loads((ROOT / "infra" / "main.parameters.json").read_text(encoding="utf-8"))["parameters"]
    assert not {"anthropicApiKey", "slackWebhookUrl"} & set(params)  # 파일에 두지 않고 azd가 물어본다


def test_login_is_required_and_https_only():
    assert "requireAuthentication: true" in APP
    assert "unauthenticatedClientAction: 'RedirectToLoginPage'" in APP
    assert "httpsOnly: true" in APP and "minTlsVersion: '1.2'" in APP


def test_region_budget_and_monitoring():
    assert "param location string = 'koreacentral'" in MAIN
    assert "Microsoft.Consumption/budgets" in APP and "Microsoft.Insights/components" in APP


def test_upload_excludes_secrets_and_records():
    ignored = {line.strip() for line in (ROOT / ".webappignore").read_text(encoding="utf-8").splitlines()}
    assert {".env", ".env.*", ".azure/", "data/", "logs/", "output/", "reference/", "eval/results/"} <= ignored
    assert ".azure/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_startup_opens_both_screens_and_builds_virtual_db():
    startup = (ROOT / "startup.sh").read_bytes()
    assert b"\r\n" not in startup  # 리눅스에서 도는 스크립트
    text = startup.decode("utf-8")
    assert "database/init_db.py" in text and "src/app_main.py" in text
    assert "appCommandLine: 'bash startup.sh'" in APP


def test_auth_script_uses_same_app_name_as_bicep():
    script = (ROOT / "infra" / "scripts" / "setup_auth.ps1").read_text(encoding="utf-8")
    assert "app-billing-$envName.azurewebsites.net" in script
    assert "var appName = 'app-billing-${environmentName}'" in APP
    assert "credential" not in script  # 클라이언트 시크릿을 만들지 않는다


def test_main_entry_shows_customer_page_first(tmp_path, monkeypatch):
    monkeypatch.setenv("BILLING_APP_DB", str(tmp_path / "app.db"))
    at = AppTest.from_file(str(ROOT / "src" / "app_main.py"), default_timeout=120).run()
    assert not at.exception
    assert any("요금제 알림" in t.value for t in at.title)

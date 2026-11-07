"""저장소 위생 테스트(T0). 비밀값, 저장소 제외 목록, 로컬 전용 화면, MCP 설정을 확인한다."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 저장소에 올라가지 않는 폴더. .env는 원래 비밀값을 담는 파일이라 검사하지 않는다(.gitignore로 제외 확인).
SKIP_DIRS = {".venv", ".git", "reference", "__pycache__", ".pytest_cache", "output", "logs", "data"}
TEXT_SUFFIXES = {".py", ".md", ".json", ".toml", ".txt", ".ini", ".cfg", ".yml", ".yaml", ".example", ".gitignore"}
SECRET_PATTERNS = {
    "Anthropic API 키": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "Slack Webhook URL": re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_\-]+"),
}


def repository_text_files() -> list[Path]:
    files = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or SKIP_DIRS.intersection(path.relative_to(ROOT).parts):
            continue
        if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in TEXT_SUFFIXES:
            files.append(path)
    return files


def find_secrets(text: str) -> list[str]:
    return [label for label, pattern in SECRET_PATTERNS.items() if pattern.search(text)]


# --- SEC-09 비밀값 ---

def test_secret_detector_catches_fake_values():
    # 가짜 값은 실행할 때 만든다. 소스에 그대로 쓰면 이 파일이 검사에 걸린다.
    fake_key = "sk-" + "ant-" + "a" * 30
    fake_hook = "https://hooks." + "slack.com/services/T000/B000/xxxx"
    assert find_secrets(fake_key) == ["Anthropic API 키"]
    assert find_secrets(fake_hook) == ["Slack Webhook URL"]


def test_no_secrets_in_repository():
    files = repository_text_files()
    assert any(f.name == "README.md" for f in files), "검사 대상 파일을 찾지 못함"
    found = {str(f.relative_to(ROOT)): hits for f in files if (hits := find_secrets(f.read_text(encoding="utf-8", errors="ignore")))}
    assert found == {}


def test_gitignore_excludes_secrets_and_customer_outputs():
    lines = {line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert {".env", ".env.*", "!.env.example"} <= lines
    assert {"output/", "logs/", "data/", "database/billing_mock.db"} <= lines


def test_env_example_has_no_values_for_secrets():
    values = dict(
        line.split("=", 1) for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    assert values["ANTHROPIC_API_KEY"].strip() == ""
    assert values["SLACK_WEBHOOK_URL"].strip() == ""


# --- UI-01 로컬 전용 화면 ---

def test_streamlit_binds_localhost_only():
    config = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert config["server"]["address"] == "127.0.0.1"
    assert config["browser"]["gatherUsageStats"] is False


# --- RET-02 Claude Code용 MCP 설정 ---

def test_mcp_config_registers_only_own_server():
    config = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    servers = config["mcpServers"]
    assert list(servers) == ["billing-tools"]
    assert servers["billing-tools"]["args"][-1].endswith("/src/mcp_server.py")
    assert not (ROOT / ".mcp" / "config.json").exists(), "Claude Code는 .mcp/config.json을 읽지 않는다"

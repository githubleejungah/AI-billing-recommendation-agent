"""개발 하네스 훅 검증(HAR-02~04). 훅 스크립트를 실제처럼 JSON 입력으로 실행해 종료 코드를 확인한다."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / ".claude" / "hooks"
sys.path.insert(0, str(HOOKS))

import run_tests  # noqa: E402

PROJECT = "C:/Users/User/실습산출물/ai-billing-recommendation-agent"  # Windows 경로 형태도 처리하는지 본다


def guard(tool: str, **tool_input) -> tuple[int, str]:
    event = {"tool_name": tool, "tool_input": tool_input, "cwd": PROJECT}
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}  # Windows 기본(cp949) 출력이면 한글 안내문을 읽지 못한다
    done = subprocess.run([sys.executable, str(HOOKS / "guard_files.py")], input=json.dumps(event, ensure_ascii=False),
                          capture_output=True, text=True, encoding="utf-8", env=env)
    return done.returncode, done.stderr


@pytest.mark.parametrize("tool, args", [
    ("Read", {"file_path": f"{PROJECT}/.env"}),
    ("Read", {"file_path": "C:\\Users\\User\\실습산출물\\ai-billing-recommendation-agent\\.env"}),
    ("Edit", {"file_path": f"{PROJECT}/.env.local"}),
    ("Write", {"file_path": f"{PROJECT}/.mcp.json"}),
    ("Edit", {"file_path": f"{PROJECT}/.claude/settings.json"}),
    ("Write", {"file_path": f"{PROJECT}/.claude/hooks/guard_files.py"}),
    ("Write", {"file_path": f"{PROJECT}/logs/audit.jsonl"}),
    ("Edit", {"file_path": f"{PROJECT}/data/app.db"}),
    ("Write", {"file_path": f"{PROJECT}/database/billing_mock.db"}),
    ("Write", {"file_path": f"{PROJECT}/reference/original/notes.md"}),
    ("Edit", {"file_path": f"{PROJECT}/config/data_policy.toml"}),
    ("Bash", {"command": "sed -i s/false/true/ config/data_policy.toml"}),
    ("Bash", {"command": "cat .env"}),
    ("Bash", {"command": "Get-Content .env.production"}),
    ("Bash", {"command": "rm .claude/settings.json"}),
    ("Bash", {"command": "Remove-Item .claude\\hooks\\run_tests.py"}),
    ("Bash", {"command": "echo {} > .mcp.json"}),
])
def test_protected_paths_are_blocked(tool, args):
    code, message = guard(tool, **args)
    assert code == 2, (tool, args)
    assert message.strip()


@pytest.mark.parametrize("tool, args", [
    ("Read", {"file_path": f"{PROJECT}/.env.example"}),
    ("Read", {"file_path": f"{PROJECT}/.mcp.json"}),  # 읽기는 허용
    ("Read", {"file_path": f"{PROJECT}/config/data_policy.toml"}),  # 정책 파일도 읽기는 허용
    ("Edit", {"file_path": f"{PROJECT}/config/placeholders.toml"}),  # 자리표시자 등록 파일은 개발 파일
    ("Read", {"file_path": f"{PROJECT}/.claude/settings.json"}),
    ("Edit", {"file_path": f"{PROJECT}/src/simulator.py"}),
    ("Write", {"file_path": f"{PROJECT}/design/requirements.md"}),
    ("Write", {"file_path": "src/new_module.py"}),
    ("Bash", {"command": ".venv\\Scripts\\python -m pytest -q"}),
    ("Bash", {"command": "cat .env.example"}),
    ("Bash", {"command": "cat .mcp.json"}),
    ("Bash", {"command": "git status"}),
])
def test_normal_work_is_allowed(tool, args):
    code, message = guard(tool, **args)
    assert code == 0, (tool, args, message)


def test_bad_input_does_not_block():
    done = subprocess.run([sys.executable, str(HOOKS / "guard_files.py")], input="not json",
                          capture_output=True, text=True)
    assert done.returncode == 0


@pytest.mark.parametrize("path, expected", [
    (f"{PROJECT}/src/simulator.py", True),
    (f"{PROJECT}/tests/test_change.py", True),
    ("C:\\Users\\User\\실습산출물\\ai-billing-recommendation-agent\\database\\init_db.py", True),
    (f"{PROJECT}/design/requirements.md", False),
    (f"{PROJECT}/knowledge/plan_pro.md", False),
    (f"{PROJECT}/src/README.txt", False),
])
def test_tests_run_only_for_code_changes(path, expected):
    assert run_tests.should_test(path, PROJECT) is expected


def test_settings_register_both_hooks():
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    pre = settings["hooks"]["PreToolUse"][0]
    post = settings["hooks"]["PostToolUse"][0]
    assert set(pre["matcher"].split("|")) >= {"Read", "Edit", "Write", "Bash"}
    assert "guard_files.py" in pre["hooks"][0]["command"]
    assert "run_tests.py" in post["hooks"][0]["command"]


def test_claude_md_lists_protected_files():
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for name in (".env", ".mcp.json", ".claude/settings.json", ".claude/hooks/", "logs/", "data/",
                 "config/data_policy.toml"):
        assert name in text

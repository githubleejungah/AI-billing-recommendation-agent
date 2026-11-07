"""보호 파일 훅(HAR-02). Claude Code가 도구를 쓰기 직전(PreToolUse)에 실행된다.

입력: 표준 입력으로 받은 JSON(tool_name, tool_input, cwd).
종료 코드 2 = 막고, 표준 오류의 메시지를 Claude에 돌려준다. 0 = 허용.
- .env 파일은 읽기도 막는다(API 키가 대화로 들어가지 않게).
- .mcp.json, 하네스 파일(.claude/settings.json, .claude/hooks/), 감사 기록·앱 데이터, 가상 DB, 과정 자료는 고치지 못하게 한다.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import PurePosixPath

PROTECTED = [".mcp.json", ".claude/settings.json", ".claude/hooks/", "logs/", "data/",
             "database/billing_mock.db", "reference/"]
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# 셸 명령에서 파일을 바꾸는 표현(bash, PowerShell)
SHELL_WRITES = re.compile(r">|\b(rm|mv|cp|del|tee|truncate|Remove-Item|Move-Item|Copy-Item|Set-Content|"
                          r"Add-Content|Out-File|Clear-Content|New-Item)\b|sed\s+-i", re.IGNORECASE)
ENV_IN_COMMAND = re.compile(r"(^|[\s/\\\"'=])\.env(?!\.example)(\.[\w-]+)?(?=$|[\s\"';|&)])")


def is_env_file(path: str) -> bool:
    name = PurePosixPath(path.replace("\\", "/")).name
    return name == ".env" or (name.startswith(".env.") and name != ".env.example")


def relative(path: str, root: str) -> str:
    path = path.replace("\\", "/")
    root = root.replace("\\", "/").rstrip("/")
    if root and path.lower().startswith(root.lower() + "/"):
        path = path[len(root) + 1:]
    return path[2:] if path.startswith("./") else path


def is_protected(path: str, root: str) -> bool:
    rel = relative(path, root)
    return any(rel == p.rstrip("/") or rel.startswith(p) for p in PROTECTED)


def check(event: dict) -> str | None:
    """막을 이유를 돌려준다. 허용이면 None."""
    tool = event.get("tool_name", "")
    data = event.get("tool_input") or {}
    root = event.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    if tool == "Bash":
        command = data.get("command", "")
        if ENV_IN_COMMAND.search(command):
            return ".env 파일은 명령으로도 열 수 없습니다. 비밀값은 사람이 직접 관리합니다."
        if SHELL_WRITES.search(command) and any(p.rstrip("/") in command.replace("\\", "/") for p in PROTECTED):
            return "보호 파일을 바꾸는 명령입니다. 하네스·감사 기록·MCP 설정은 사람이 직접 고칩니다(CLAUDE.md)."
        return None
    path = data.get("file_path") or data.get("notebook_path") or data.get("path") or ""
    if not path:
        return None
    if is_env_file(path):
        return f".env 파일은 읽거나 고칠 수 없습니다: {path}. 키가 필요하면 .env.example의 이름만 참고하세요."
    if tool in WRITE_TOOLS and is_protected(path, root):
        return f"보호 파일입니다: {relative(path, root)}. 이 파일은 사람이 직접 고칩니다(CLAUDE.md의 보호 파일)."
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0  # 입력을 못 읽으면 막지 않는다(훅 오류로 작업 전체가 멈추지 않게)
    reason = check(event)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

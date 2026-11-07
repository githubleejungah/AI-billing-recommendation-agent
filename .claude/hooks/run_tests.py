"""고친 뒤 테스트 훅(HAR-03). Claude Code가 파일을 고친 직후(PostToolUse)에 실행된다.

src/, tests/, database/의 파이썬 파일이 바뀌면 전체 테스트를 돌린다.
실패하면 결과 끝부분을 표준 오류로 내고 종료 코드 2를 돌려준다(Claude가 결과를 보고 고친다).
가상환경(.venv)의 파이썬이 있으면 그것을 쓴다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

WATCHED = ("src/", "tests/", "database/")
TIMEOUT_SECONDS = 300


def should_test(path: str, root: str) -> bool:
    path = path.replace("\\", "/")
    root = root.replace("\\", "/").rstrip("/")
    if root and path.lower().startswith(root.lower() + "/"):
        path = path[len(root) + 1:]
    return path.endswith(".py") and path.startswith(WATCHED)


def python_for(root: Path) -> str:
    for candidate in (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    root = event.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    data = event.get("tool_input") or {}
    path = data.get("file_path") or data.get("notebook_path") or ""
    if not should_test(path, root):
        return 0
    try:
        result = subprocess.run([python_for(Path(root)), "-m", "pytest", "-q", "-x", "--no-header"],
                                cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        print(f"테스트가 {TIMEOUT_SECONDS}초 안에 끝나지 않았습니다.", file=sys.stderr)
        return 2
    if result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-40:])
        print(f"테스트 실패. 고친 내용을 확인하세요.\n{tail}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

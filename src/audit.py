"""감사 기록(SEC-11). 추가만 하는 JSON Lines 파일.

내용(요청 본문, 서류 본문, 고객 이름, 비밀값)은 남기지 않고, 무엇이 있었는지만 남긴다(security.md 9장).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

AUDIT_PATH = Path(__file__).resolve().parent.parent / "logs" / "audit.jsonl"
FORBIDDEN_KEYS = {"name", "customer_name", "text", "body", "content", "api_key", "webhook_url"}


def record(event: str, path: Path = AUDIT_PATH, **fields) -> dict:
    bad = FORBIDDEN_KEYS & set(fields)
    if bad:
        raise ValueError(f"감사 기록에 남길 수 없는 항목: {sorted(bad)}")
    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        "pc_user": os.environ.get("USERNAME") or os.environ.get("USER") or "unknown",
        **fields,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read(path: Path = AUDIT_PATH) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

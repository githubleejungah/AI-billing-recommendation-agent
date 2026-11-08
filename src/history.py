"""앱 전용 저장소(data/app.db). 청구 DB는 읽기 전용(RET-01)이라 앱이 만드는 기록은 여기에 둔다.

고객은 가칭 코드(code)로만 가리키고 이름과 리포트 문장은 넣지 않는다(requirements.md 1장 app.db).
서류 발행 기록(T10), 변경 접수(T11)를 쓰고, 나머지 표는 T5·T9·T12에서 쓴다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

APP_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "app.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS recommendation_history (
    run_id TEXT PRIMARY KEY, code TEXT NOT NULL, analyzed_at TEXT NOT NULL,
    action TEXT, plan_id TEXT, promo_id TEXT, saving_monthly INTEGER, saving_12m INTEGER,
    stability TEXT, model_id TEXT, outcome TEXT DEFAULT '미기록', outcome_at TEXT, contract_ended_at TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY, code TEXT NOT NULL, created_at TEXT NOT NULL, valid_until TEXT NOT NULL,
    plan_id TEXT, promo_id TEXT, saving_12m INTEGER, status TEXT NOT NULL,
    kind TEXT, parts TEXT, peak_months TEXT, stage TEXT, send_on TEXT, tier INTEGER, run_id TEXT
);
CREATE TABLE IF NOT EXISTS opt_outs (code TEXT PRIMARY KEY, opted_out_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quality_checks (
    run_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL, total INTEGER NOT NULL, passed INTEGER NOT NULL,
    pass_rate REAL NOT NULL, counts TEXT NOT NULL, excluded_codes TEXT NOT NULL, flagged_codes TEXT NOT NULL,
    stopped INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS change_requests (
    request_id TEXT PRIMARY KEY, code TEXT NOT NULL, requested_at TEXT NOT NULL, requested_by_role TEXT NOT NULL,
    from_plan TEXT, to_plan TEXT, promo_id TEXT, effective_date TEXT, cancel_deadline TEXT,
    status TEXT NOT NULL, recalculated_saving_12m INTEGER, quote_sha256 TEXT, cancelled_at TEXT, applied_at TEXT
);
-- 고객마다 접수 상태인 변경은 하나만(CHG-06: 같은 변경을 두 번 접수하지 않음, 동시에 눌러도 막힘)
CREATE UNIQUE INDEX IF NOT EXISTS one_open_change ON change_requests(code) WHERE status = '접수';
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY, code TEXT NOT NULL, created_at TEXT NOT NULL, question_masked TEXT NOT NULL,
    reason TEXT NOT NULL, status TEXT NOT NULL, answer TEXT, answered_at TEXT, faq_candidate INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS documents (
    document_no TEXT PRIMARY KEY, kind TEXT NOT NULL, code TEXT NOT NULL, issued_at TEXT NOT NULL,
    valid_until TEXT, sha256 TEXT NOT NULL, formats TEXT NOT NULL
);
"""


# 이미 만들어진 app.db에 나중에 추가한 열(CREATE TABLE IF NOT EXISTS는 열을 추가하지 않는다)
ADDED_COLUMNS = {"change_requests": ["quote_sha256 TEXT", "cancelled_at TEXT", "applied_at TEXT"],
                 "notifications": ["kind TEXT", "parts TEXT", "peak_months TEXT", "stage TEXT", "send_on TEXT",
                                   "tier INTEGER", "run_id TEXT"]}


def connect(path: Path = APP_DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    for table, columns in ADDED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if existing:
            for column in columns:
                if column.split()[0] not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
    conn.executescript(SCHEMA)
    return conn


def record_document(conn: sqlite3.Connection, document_no: str, kind: str, code: str, issued_at: str,
                    valid_until: str | None, sha256: str, formats: list[str]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO documents VALUES (?, ?, ?, ?, ?, ?, ?)",
        (document_no, kind, code, issued_at, valid_until, sha256, ",".join(formats)),
    )
    conn.commit()

"""청구 데이터 읽기(RET-04). 청구 DB를 읽는 곳은 이 모듈 하나뿐이다.

계산(simulator), 서류(documents), 셀프 변경(change), MCP 도구는 모두 이 모듈의 함수로 읽는다.
지금은 가상 SQLite를 읽기 전용으로 연다(RET-01). 실제 청구 시스템(읽기 전용 복제본이나 API)으로 바꿀 때는
이 모듈의 함수만 같은 모양의 값을 돌려주도록 바꾸면 된다. AI가 만든 SQL은 실행하지 않는다(RET-02).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "database" / "billing_mock.db"


class CustomerNotFound(Exception):
    pass


@dataclass(frozen=True)
class Plan:
    plan_id: str
    plan_name: str
    base_price: int
    included_api_calls: int
    included_storage_gb: float
    api_overage_per_1k: int
    storage_overage_per_gb: int


@dataclass(frozen=True)
class Promotion:
    promo_id: str
    promo_name: str
    plan_id: str
    discount_type: str
    discount_value: int
    duration_months: int
    valid_from: str
    valid_to: str
    min_tenure_months: int
    upgrade_only: bool


@dataclass(frozen=True)
class LoyaltyTier:
    tier: int
    tier_name: str
    min_tenure_months: int
    bonus_pct: int


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """읽기 전용 연결. 쓰기 쿼리는 SQLite가 거부한다."""
    if not db_path.exists():
        raise FileNotFoundError(f"DB가 없습니다: {db_path}. 먼저 python database/init_db.py 를 실행하세요.")
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_plans(conn: sqlite3.Connection) -> list[Plan]:
    rows = conn.execute("SELECT * FROM plans ORDER BY base_price").fetchall()
    return [Plan(**dict(row)) for row in rows]


def load_promotions(conn: sqlite3.Connection) -> list[Promotion]:
    rows = conn.execute("SELECT * FROM promotions ORDER BY promo_id").fetchall()
    return [Promotion(**{**dict(row), "upgrade_only": bool(row["upgrade_only"])}) for row in rows]


def load_loyalty_tiers(conn: sqlite3.Connection) -> list[LoyaltyTier]:
    """장기 이용 혜택 단계(LOY-02). 낮은 단계부터. 단계 기준과 비율은 코드가 아니라 이 표가 정한다."""
    rows = conn.execute("SELECT * FROM loyalty_tiers ORDER BY min_tenure_months").fetchall()
    return [LoyaltyTier(**dict(row)) for row in rows]


def load_customer(conn: sqlite3.Connection, user_id: str) -> dict:
    """고객 기본 정보(user_id, name, current_plan_id, signup_date). 가칭 코드는 넣지 않는다."""
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        raise CustomerNotFound(user_id)
    return dict(row)


def load_usage(conn: sqlite3.Connection, user_id: str, first_month: str, last_month: str) -> list[dict]:
    """first_month~last_month(YYYY-MM, 양 끝 포함)의 월별 이용 기록. 오래된 달부터."""
    rows = conn.execute(
        "SELECT target_month, api_calls_used, storage_gb_used, active_days FROM usage_logs"
        " WHERE user_id = ? AND target_month BETWEEN ? AND ? ORDER BY target_month",
        (user_id, first_month, last_month),
    ).fetchall()
    return [dict(row) for row in rows]


def load_identity(conn: sqlite3.Connection, user_id: str) -> dict:
    """서류와 사내 기록에 쓰는 이름·가칭 코드. 이름은 외부 AI와 Slack에 보내지 않는다(SEC-03, 12)."""
    row = conn.execute(
        "SELECT u.name, c.code FROM users u JOIN customer_codes c USING (user_id) WHERE u.user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        raise CustomerNotFound(user_id)
    return {"name": row[0], "code": row[1]}


def customer_ids(conn: sqlite3.Connection) -> list[str]:
    """모든 고객 ID(월간 선별 대상 목록)."""
    return [row[0] for row in conn.execute("SELECT user_id FROM users ORDER BY user_id")]


def code_map(conn: sqlite3.Connection) -> dict[str, str]:
    """고객 ID → 가칭 코드. 선별 결과와 사내 기록에 이름 대신 쓴다(SEC-12)."""
    return {row[0]: row[1] for row in conn.execute("SELECT user_id, code FROM customer_codes ORDER BY user_id")}


def load_usage_rows(conn: sqlite3.Connection) -> list[dict]:
    """데이터 품질 점검용 원본 이용 기록 전부(DQ-01). 값을 고치거나 거르지 않는다.

    달 형식이 틀린 기록(예: 2026-13)은 기간 조건으로 거르면 보이지 않으므로 기간은 점검하는 쪽에서 고른다.
    """
    rows = conn.execute(
        "SELECT user_id, target_month, api_calls_used, storage_gb_used, active_days FROM usage_logs"
        " ORDER BY user_id, target_month"
    ).fetchall()
    return [dict(row) for row in rows]


def customer_names(conn: sqlite3.Connection) -> list[str]:
    """DB에 있는 모든 고객 이름. 외부 전송 전 검사와 질문 가림에만 쓴다(SEC-08, ASK-05)."""
    return [row[0] for row in conn.execute("SELECT name FROM users ORDER BY user_id")]


def customer_codes(conn: sqlite3.Connection) -> list[str]:
    """모든 가칭 코드. 외부 전송 전 검사에만 쓴다(SEC-08 규칙 2)."""
    return [row[0] for row in conn.execute("SELECT code FROM customer_codes ORDER BY user_id")]

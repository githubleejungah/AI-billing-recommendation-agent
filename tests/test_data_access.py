"""데이터 접근 한 곳(RET-04)과 기준일 설정(SIM-09) 검증."""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "database"))

import data_access  # noqa: E402
import init_db  # noqa: E402
import settings  # noqa: E402
import simulator  # noqa: E402

BILLING_TABLES = r"(plans|promotions|users|usage_logs|customer_codes)"
SQL_ON_BILLING = re.compile(rf"\b(FROM|JOIN|INTO|UPDATE)\s+{BILLING_TABLES}\b", re.IGNORECASE)


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("db") / "billing_mock.db"
    init_db.build(db_path)
    connection = data_access.connect(db_path)
    yield connection
    connection.close()


# --- RET-04: 청구 데이터는 data_access 한 곳에서만 읽는다 ---

def test_only_data_access_queries_billing_tables():
    offenders = []
    for path in (ROOT / "src").glob("*.py"):
        if path.name == "data_access.py":
            continue
        text = path.read_text(encoding="utf-8")
        if SQL_ON_BILLING.search(text) or "mode=ro" in text:
            offenders.append(path.name)
    assert offenders == []


def test_simulator_names_point_to_data_access():
    assert simulator.load_plans is data_access.load_plans
    assert simulator.load_customer is data_access.load_customer
    assert simulator.CustomerNotFound is data_access.CustomerNotFound
    assert simulator.DB_PATH == data_access.DB_PATH


def test_connection_is_read_only(conn):
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE plans SET base_price = 0")


def test_load_usage_respects_range(conn):
    rows = data_access.load_usage(conn, "user_201", "2025-11", "2026-10")
    assert [r["target_month"] for r in rows][0] == "2025-11" and len(rows) == 12


def test_identity_and_unknown_customer(conn):
    identity = data_access.load_identity(conn, "user_002")
    assert set(identity) == {"name", "code"}
    with pytest.raises(data_access.CustomerNotFound):
        data_access.load_identity(conn, "user_999")
    with pytest.raises(data_access.CustomerNotFound):
        data_access.load_customer(conn, "user_999")


# --- SIM-09: 분석 기준일은 설정 값 ---

def test_default_as_of():
    assert settings.as_of() == date(2026, 11, 1)


def test_as_of_from_environment(monkeypatch, conn):
    monkeypatch.setenv("BILLING_AS_OF", "2026-12-01")
    assert settings.as_of() == date(2026, 12, 1)
    result = simulator.simulate(conn, "user_002")
    assert result["as_of"] == "2026-12-01"
    assert result["schedule"]["months"][0] == "2027-01"  # 비교 기간은 기준일 다음 달부터


def test_explicit_as_of_wins_over_environment(monkeypatch, conn):
    monkeypatch.setenv("BILLING_AS_OF", "2026-12-01")
    result = simulator.simulate(conn, "user_002", as_of=date(2026, 11, 1))
    assert result["as_of"] == "2026-11-01" and result["schedule"]["months"][0] == "2026-12"


@pytest.mark.parametrize("value", ["2026/11/01", "2026-11-15", "내일"])
def test_invalid_as_of_is_rejected(monkeypatch, value):
    monkeypatch.setenv("BILLING_AS_OF", value)
    with pytest.raises(ValueError):
        settings.as_of()

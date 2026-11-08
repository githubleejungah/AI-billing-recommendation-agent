"""셀프 요금제 변경 검증(CHG-01~07).

기대 금액은 손으로 계산한 값이다(test_documents.py 머리말과 같은 계산).
- user_002(가입 19개월, 장기 혜택 2단계 → 2027-04부터 3단계): 베이직 유지 월 124,500원(세액 12,450원, 합계 136,950원),
  2027-04부터 123,500원. 프로 + 3개월 30% 할인: 2026-12~2027-02 월 56,000원, 2027-03~2027-11 월 80,000원.
  12개월 절감 598,000원(세액 148,600 − 88,800 = 59,800원, 합계 657,800원).
- 적용일은 비교 기간 첫 달 1일(2026-12-01), 취소 기한은 그 전날(2026-11-30).
"""

from __future__ import annotations

import io
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "database"))

import audit  # noqa: E402
import change  # noqa: E402
import documents  # noqa: E402
import history  # noqa: E402
import init_db  # noqa: E402
import simulator  # noqa: E402

TODAY = date(2026, 11, 7)
ADMIN, MEMBER = change.ROLE_BILLING_ADMIN, change.ROLE_MEMBER


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("db") / "billing_mock.db"
    init_db.build(db_path)
    connection = simulator.connect(db_path)
    yield connection
    connection.close()


@pytest.fixture()
def paths(tmp_path):
    return {"app_db": tmp_path / "app.db", "audit_path": tmp_path / "audit.jsonl"}


def submit(conn, paths, user_id="user_002", role=ADMIN, quote=None, today=TODAY, invoice=True, **kwargs):
    quote = quote or change.prepare(conn, user_id, today)
    return change.submit(conn, user_id, role, quote["quote_sha256"], invoice, today, **paths, **kwargs)


def rows(paths, sql, *args):
    with sqlite3.connect(paths["app_db"]) as app:
        return app.execute(sql, args).fetchall()


# --- 확인 화면(CHG-02, 03) ---

def test_quote_for_user_002_matches_hand_calculation(conn):
    q = change.prepare(conn, "user_002", TODAY)
    assert (q["action"], q["from_plan"]["name"], q["to_plan"]["name"]) == ("upgrade", "베이직", "프로")
    assert q["promotion"]["name"] == "프로 전환 3개월 30% 할인"
    assert (q["effective_date"], q["cancel_deadline"]) == ("2026-12-01", "2026-11-30")
    assert q["current_monthly"] == {"supply": 124500, "tax": 12450, "total": 136950}
    assert q["monthly"] == [
        {"first": "2026-12", "last": "2027-02", "supply": 56000, "tax": 5600, "total": 61600},
        {"first": "2027-03", "last": "2027-11", "supply": 80000, "tax": 8000, "total": 88000},
    ]
    assert q["after_promotion"] == {"supply": 80000, "tax": 8000, "total": 88000}
    assert q["saving_12m"] == {"supply": 598000, "tax": 59800, "total": 657800}
    assert len(q["invoice_checks"]) == 2
    assert q["changeable"] and not q["needs_reconfirm"]


def _months_between(first: str, last: str) -> int:
    y1, m1 = map(int, first.split("-"))
    y2, m2 = map(int, last.split("-"))
    return (y2 - y1) * 12 + (m2 - m1) + 1


@pytest.mark.parametrize("user_id", ["user_002", "user_007", "user_004", "user_013"])
def test_quote_equals_simulator_and_review_document(conn, user_id):
    q = change.prepare(conn, user_id, TODAY)
    sim = simulator.simulate(conn, user_id)["recommendation"]
    review = documents.build_data(conn, user_id)
    assert sum(run["supply"] * _months_between(run["first"], run["last"]) for run in q["monthly"]) \
        == sim["recommended_total_12m"]
    assert q["saving_12m"]["supply"] == sim["saving_12m"]
    assert q["saving_12m"] == {k: review["comparison"][2][k] for k in ("supply", "tax", "total")}


def test_downgrade_shows_notice_and_keep_is_not_changeable(conn, paths):
    assert change.DOWNGRADE_NOTICE in change.prepare(conn, "user_007", TODAY)["notices"]
    keep = change.prepare(conn, "user_001", TODAY)
    assert not keep["changeable"]
    with pytest.raises(change.ChangeError):
        submit(conn, paths, "user_001", quote=keep)


def test_stale_analysis_cannot_be_used(conn):
    with pytest.raises(change.ChangeError):
        change.prepare(conn, "user_002", date(2026, 12, 1))


# --- 알림과 대조(CHG-02, 07) ---

def test_notification_still_valid(conn):
    notice = {"plan_id": "PLAN_PRO", "promo_id": "PROMO_PRO_UPGRADE", "saving_12m": 598000, "valid_until": "2026-12-01"}
    q = change.prepare(conn, "user_002", TODAY, notice)
    assert q["notification_check"]["status"] == "유효"
    assert not q["needs_reconfirm"]


def test_expired_notification_needs_reconfirm(conn):
    notice = {"plan_id": "PLAN_PRO", "promo_id": "PROMO_PRO_UPGRADE", "saving_12m": 598000, "valid_until": "2026-11-06"}
    q = change.prepare(conn, "user_002", TODAY, notice)
    assert q["notification_check"]["status"] == "만료"
    assert q["needs_reconfirm"]


def test_changed_recommendation_is_shown(conn):
    notice = {"plan_id": "PLAN_ENTERPRISE", "promo_id": None, "saving_12m": 700000, "valid_until": "2026-12-01"}
    check = change.prepare(conn, "user_002", TODAY, notice)["notification_check"]
    assert check["status"] == "달라짐"
    assert check["differences"] == ["추천 요금제", "이벤트", "12개월 절감액"]


# --- 접수(CHG-01, 04, 06) ---

def test_member_cannot_change(conn, paths):
    with pytest.raises(PermissionError):
        submit(conn, paths, role=MEMBER)
    assert [e["event"] for e in audit.read(paths["audit_path"])] == ["change_denied"]
    assert not paths["app_db"].exists() or rows(paths, "SELECT COUNT(*) FROM change_requests")[0][0] == 0


def test_invoice_info_must_be_confirmed(conn, paths):
    with pytest.raises(change.ChangeError):
        submit(conn, paths, invoice=False)


def test_submit_records_request_history_audit_and_message(conn, paths):
    result = submit(conn, paths, request_id="CR-20261107-ABC123")
    req = result["request"]
    assert not result["duplicate"]
    assert (req["from_plan"], req["to_plan"], req["promo_id"]) == ("PLAN_BASIC", "PLAN_PRO", "PROMO_PRO_UPGRADE")
    assert (req["effective_date"], req["cancel_deadline"], req["status"]) == ("2026-12-01", "2026-11-30", "접수")
    assert req["recalculated_saving_12m"] == 598000
    assert rows(paths, "SELECT outcome, saving_12m FROM recommendation_history WHERE run_id = ?",
                "self-CR-20261107-ABC123") == [("셀프 수락", 598000)]
    raw = paths["audit_path"].read_text(encoding="utf-8")
    name = documents._identity(conn, "user_002")["name"]
    assert "change_requested" in raw and name not in raw and "user_002" not in raw
    message = result["sales_message"]["text"]
    assert req["code"] in message and name not in message and "user_002" not in message
    assert "598,000원" in message


def test_request_id_has_no_sequence_number(conn, paths):
    req = submit(conn, paths)["request"]
    assert req["request_id"].startswith("CR-20261107-") and len(req["request_id"]) == len("CR-20261107-") + 6


def test_double_click_creates_one_request(conn, paths):
    first = submit(conn, paths)
    second = submit(conn, paths)
    assert second["duplicate"] and second["sales_message"] is None
    assert second["request"]["request_id"] == first["request"]["request_id"]
    assert rows(paths, "SELECT COUNT(*) FROM change_requests")[0][0] == 1


def test_simultaneous_insert_is_blocked_by_index(conn, paths, monkeypatch):
    """두 요청이 같은 순간에 '접수 없음'을 확인하고 동시에 넣는 경우: 색인이 두 번째를 막는다."""
    first = submit(conn, paths)
    real = change._open_request
    calls = {"n": 0}

    def racing(app, code):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(app, code)

    monkeypatch.setattr(change, "_open_request", racing)
    second = submit(conn, paths)
    assert second["duplicate"]
    assert second["request"]["request_id"] == first["request"]["request_id"]
    assert rows(paths, "SELECT COUNT(*) FROM change_requests")[0][0] == 1


def test_tampered_quote_is_rejected(conn, paths):
    with pytest.raises(change.QuoteChanged) as caught:
        change.submit(conn, "user_002", ADMIN, "0" * 64, True, TODAY, **paths)
    assert caught.value.fresh["saving_12m"]["supply"] == 598000


@pytest.fixture()
def writable(tmp_path):
    """이용 기록을 바꿔 볼 별도 DB(모듈 공용 DB는 건드리지 않는다)."""
    db_path = tmp_path / "billing_mock.db"
    init_db.build(db_path)
    reader = simulator.connect(db_path)
    writer = sqlite3.connect(db_path)
    yield reader, writer
    reader.close()
    writer.close()


def test_usage_change_after_confirmation_forces_reconfirm(writable, paths):
    reader, writer = writable
    quote = change.prepare(reader, "user_002", TODAY)
    writer.execute("UPDATE usage_logs SET api_calls_used = api_calls_used + 30000 "
                   "WHERE user_id = 'user_002' AND target_month = '2026-10'")
    writer.commit()
    with pytest.raises(change.QuoteChanged) as caught:
        change.submit(reader, "user_002", ADMIN, quote["quote_sha256"], True, TODAY, **paths)
    assert caught.value.fresh["quote_sha256"] != quote["quote_sha256"]
    assert not paths["app_db"].exists() or rows(paths, "SELECT COUNT(*) FROM change_requests")[0][0] == 0


def test_open_request_blocks_a_different_change(writable, paths):
    reader, writer = writable
    submit(reader, paths)
    writer.execute("UPDATE usage_logs SET api_calls_used = api_calls_used + 30000 "
                   "WHERE user_id = 'user_002' AND target_month = '2026-10'")
    writer.commit()
    with pytest.raises(change.ChangeError, match="이미 접수된 변경"):
        submit(reader, paths)


# --- 취소·적용(CHG-05) ---

def test_cancel_before_deadline_then_new_request_allowed(conn, paths):
    req = submit(conn, paths)["request"]
    result = change.cancel(conn, "user_002", req["request_id"], ADMIN, date(2026, 11, 30), **paths)
    assert result["request"]["status"] == "취소"
    assert "취소" in result["sales_message"]["text"]
    assert rows(paths, "SELECT outcome FROM recommendation_history WHERE run_id = ?",
                f"self-{req['request_id']}") == [("취소",)]
    assert "change_cancelled" in [e["event"] for e in audit.read(paths["audit_path"])]
    again = submit(conn, paths)
    assert not again["duplicate"]


def test_cancel_after_deadline_is_refused(conn, paths):
    req = submit(conn, paths)["request"]
    with pytest.raises(change.ChangeError, match="취소 기한"):
        change.cancel(conn, "user_002", req["request_id"], ADMIN, date(2026, 12, 1), **paths)


def test_applied_request_cannot_be_cancelled(conn, paths):
    req = submit(conn, paths)["request"]
    assert change.apply_due(date(2026, 11, 30), **paths) == []
    assert change.apply_due(date(2026, 12, 1), **paths) == [req["request_id"]]
    with pytest.raises(change.ChangeError, match="적용"):
        change.cancel(conn, "user_002", req["request_id"], ADMIN, date(2026, 11, 30), **paths)


def test_only_admin_of_same_customer_can_cancel(conn, paths):
    req = submit(conn, paths)["request"]
    with pytest.raises(PermissionError):
        change.cancel(conn, "user_002", req["request_id"], MEMBER, TODAY, **paths)
    with pytest.raises(change.ChangeError, match="찾을 수 없"):
        change.cancel(conn, "user_007", req["request_id"], ADMIN, TODAY, **paths)


# --- 변경 확인서·기타 ---

def test_confirmation_document_uses_request(conn, paths):
    from docx import Document

    req = submit(conn, paths)["request"]
    result = documents.issue(conn, "user_002", "user_002", kind="confirmation",
                             change=change.confirmation_change(req), issued=TODAY, formats=("docx",), **paths)
    text = " ".join(cell.text for t in Document(io.BytesIO(result["files"]["docx"])).tables
                    for row in t.rows for cell in row.cells)
    assert req["request_id"] in text and "2026-12-01" in text and "2026-11-30" in text


def test_change_module_uses_no_ai():
    source = (ROOT / "src" / "change.py").read_text(encoding="utf-8")
    assert "anthropic" not in source and "import agent" not in source


def test_old_app_db_gets_new_columns(tmp_path):
    path = tmp_path / "app.db"
    with sqlite3.connect(path) as old:
        old.execute("CREATE TABLE change_requests (request_id TEXT PRIMARY KEY, code TEXT NOT NULL, "
                    "requested_at TEXT NOT NULL, requested_by_role TEXT NOT NULL, from_plan TEXT, to_plan TEXT, "
                    "promo_id TEXT, effective_date TEXT, cancel_deadline TEXT, status TEXT NOT NULL, "
                    "recalculated_saving_12m INTEGER)")
    app = history.connect(path)
    columns = {row[1] for row in app.execute("PRAGMA table_info(change_requests)")}
    app.close()
    assert {"quote_sha256", "cancelled_at", "applied_at"} <= columns

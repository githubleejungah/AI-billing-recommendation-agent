"""시뮬레이터 계산 검증. 기대값은 모두 손으로 계산한 값이다(주석에 식을 적었다).

단가는 명세 v2.4 기준이다(베이직 API 1,000회당 2,000원·GB당 500원, 프로 1,500원·300원, 엔터프라이즈 1,000원·200원).
"""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "database"))

import init_db  # noqa: E402
import simulator  # noqa: E402


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("db") / "billing_mock.db"
    init_db.build(db_path)
    connection = simulator.connect(db_path)
    yield connection
    connection.close()


def run(conn, user_id):
    return simulator.simulate(conn, user_id)


def option(result, plan_id):
    return next(o for o in result["options"] if o["plan_id"] == plan_id)


# --- 월 청구액 ---

def test_basic_overage_bomb(conn):
    # 베이직: 30,000 + ⌈32,000/1,000⌉×2,000 + ⌈70GB⌉×500 = 30,000 + 64,000 + 35,000 = 129,000
    assert option(run(conn, "user_002"), "PLAN_BASIC")["monthly"] == {
        "base_price": 30000, "api_overage": 64000, "storage_overage": 35000, "total": 129000}


def test_single_extra_call_rounds_up_to_one_unit(conn):
    # 10,001회 → 초과 1회 → 1,000회 단위 1개 = 2,000원
    assert option(run(conn, "user_017"), "PLAN_BASIC")["monthly"]["total"] == 32000


def test_exactly_included_amount_has_no_overage(conn):
    assert option(run(conn, "user_016"), "PLAN_PRO")["monthly"]["total"] == 80000


# --- 추천과 절감액 ---

def test_upgrade_with_promotion(conn):
    # 현재 129,000×12 = 1,548,000 / 프로 80,000×12 − 24,000×3 = 888,000 → 절감 660,000(660,000/1,548,000 = 42.6% → 43%)
    r = run(conn, "user_002")["recommendation"]
    assert (r["action"], r["plan_id"], r["promo_id"]) == ("upgrade", "PLAN_PRO", "PROMO_PRO_UPGRADE")
    assert (r["monthly_saving"], r["saving_12m"], r["saving_pct"]) == (49000, 660000, 43)


def test_promotion_decides_boundary_case(conn):
    # 베이직: 30,000 + ⌈23,000/1,000⌉×2,000 = 76,000 → ×12 = 912,000 > 프로 888,000(할인) → 할인 덕분에 프로
    # 할인이 없으면 프로 960,000 > 912,000 이라 베이직이 맞다.
    r = run(conn, "user_005")["recommendation"]
    assert (r["action"], r["plan_id"], r["saving_12m"]) == ("upgrade", "PLAN_PRO", 24000)


def test_same_usage_without_tenure_keeps_plan(conn):
    # user_005와 사용량이 같지만 가입 3개월이라 할인 대상이 아니다. 912,000 < 960,000
    result = run(conn, "user_004")
    assert result["recommendation"]["action"] == "keep"
    skipped = option(result, "PLAN_PRO")["skipped_promotions"]
    assert {"promo_id": "PROMO_PRO_UPGRADE", "promo_name": "프로 전환 3개월 30% 할인",
            "reason": "이용 6개월 이상 필요(현재 3개월)"} in skipped


def test_downgrade(conn):
    r = run(conn, "user_007")["recommendation"]
    # 엔터프라이즈 200,000 vs 프로 80,000 + ⌈10,000/1,000⌉×1,500 = 95,000 (스토리지 150GB는 제공량 안)
    assert (r["action"], r["plan_id"], r["monthly_saving"]) == ("downgrade", "PLAN_PRO", 105000)


def test_saving_percent_rounds_half_up(conn):
    # user_007: 절감 105,000×12 = 1,260,000 / 2,400,000 = 52.5% → 53% (짝수 쪽 반올림이면 52%가 된다)
    assert run(conn, "user_007")["recommendation"]["saving_pct"] == 53


def test_enterprise_fixed_discount(conn):
    # 프로 80,000 + 70×1,500 + 200×300 = 245,000 → ×12 = 2,940,000 / 엔터프라이즈 200,000×12 − 50,000×6 = 2,100,000
    r = run(conn, "user_008")["recommendation"]
    assert (r["plan_id"], r["promo_id"], r["saving_12m"]) == ("PLAN_ENTERPRISE", "PROMO_ENT_UPGRADE", 840000)


def test_keep_with_loyalty_promotion(conn):
    r = run(conn, "user_014")["recommendation"]
    assert (r["action"], r["promo_id"], r["saving_12m"]) == ("keep_with_promotion", "PROMO_BASIC_LOYAL", 20000)


def test_tie_keeps_current_plan(conn):
    # 베이직 30,000 + ⌈25,000/1,000⌉×2,000 = 80,000 = 프로 80,000 (가입 4개월이라 전환 할인 없음)
    r = run(conn, "user_019")["recommendation"]
    assert (r["action"], r["plan_id"]) == ("keep", "PLAN_BASIC")


def test_expired_promotion_is_skipped(conn):
    skipped = option(run(conn, "user_018"), "PLAN_PRO")["skipped_promotions"]
    assert any(s["promo_id"] == "PROMO_PRO_SUMMER" and s["reason"] == "이벤트 기간 아님" for s in skipped)


def test_low_data_flag(conn):
    # 기록 1개월. 베이직 30,000 + 30×2,000 = 90,000 > 프로 80,000(가입 1개월이라 할인 없음) → 프로, 12개월 120,000 절감
    r = run(conn, "user_013")["recommendation"]
    assert (r["action"], r["plan_id"], r["promo_id"], r["saving_12m"], r["low_data"]) == (
        "upgrade", "PLAN_PRO", None, 120000, True)


def test_no_usage_gives_no_recommendation(conn):
    result = run(conn, "user_010")
    assert (result["status"], result["recommendation"]) == ("no_usage", None)


def test_churn_risk(conn):
    # 활동일 (26+25+24)/3 = 25 → (12+8+5)/3 ≈ 8.3, −67%
    assert run(conn, "user_015")["activity"]["churn_risk"] is True
    assert run(conn, "user_001")["activity"]["churn_risk"] is False


def test_tenure_counts_whole_months():
    assert simulator.tenure_months("2025-03-20", date(2026, 11, 1)) == 19
    assert simulator.tenure_months("2026-10-01", date(2026, 11, 1)) == 1


# --- 추천 안정성(SIM-04) ---

def test_stable_recommendation(conn):
    # user_002: 사용량 −20%(API 33,600, 120×0.8=96GB)면 베이직 101,000/월 → 1,212,000 > 888,000,
    # +20%(API 50,400, 144GB)면 프로 81,500/월 → 978,000 − 72,000 = 906,000 < 엔터프라이즈 2,100,000. 둘 다 프로 + 할인.
    s = run(conn, "user_002")["stability"]
    assert (s["status"], s["change_pct"]) == ("안정", 20)


def test_changes_when_usage_rises(conn):
    # user_019: 동점(80,000). +20%면 API 42,000 → 베이직 30,000 + 32×2,000 = 94,000 > 프로 80,000 → 프로
    s = run(conn, "user_019")["stability"]
    assert s["status"] == "사용량이 늘면 바뀜"
    assert (s["if_up"]["plan_id"], s["if_down"]["plan_id"]) == ("PLAN_PRO", "PLAN_BASIC")


def test_changes_when_usage_falls(conn):
    # user_005: −20%면 API 26,400 → 베이직 30,000 + ⌈16.4⌉×2,000 = 64,000 → 768,000 < 888,000 → 베이직 유지
    s = run(conn, "user_005")["stability"]
    assert s["status"] == "사용량이 줄면 바뀜"
    assert (s["if_down"]["plan_id"], s["if_down"]["promo_id"]) == ("PLAN_BASIC", None)


# --- 월별 일정(SIM-05) ---

def test_schedule_starts_next_month_and_applies_discount_only_during_promotion(conn):
    sched = run(conn, "user_002")["schedule"]
    assert sched["months"][0] == "2026-12" and sched["months"][-1] == "2027-11" and len(sched["months"]) == 12
    totals = [m["total"] for m in sched["recommended"]]
    # 프로 80,000에서 3개월 동안 30%(24,000) 할인
    assert totals == [56000] * 3 + [80000] * 9
    assert [m["discount"] for m in sched["baseline"]] == [0] * 12
    assert sum(m["total"] for m in sched["baseline"]) == 1548000


def test_schedule_sums_match_twelve_month_totals(conn):
    ids = [row[0] for row in conn.execute("SELECT user_id FROM users")]
    for user_id in ids:
        result = run(conn, user_id)
        if result["status"] != "ok":
            continue
        r, sched = result["recommendation"], result["schedule"]
        assert sum(m["total"] for m in sched["recommended"]) == r["recommended_total_12m"], user_id
        assert sum(m["total"] for m in sched["baseline"]) == r["current_total_12m"], user_id


# --- 가칭 연결표(SEC-12) ---

def test_customer_codes_are_unique_random_and_fixed(conn, tmp_path):
    rows = [tuple(r) for r in conn.execute("SELECT user_id, code FROM customer_codes ORDER BY user_id")]
    codes = [code for _, code in rows]
    assert len(rows) == 203 and len(set(codes)) == 203  # 시나리오 20 + 무작위 180 + 성수기 3(v3.2)
    assert all(re.fullmatch(r"[A-Z0-9]{3}-[A-Z0-9]{3}", c) for c in codes)
    assert not any(set(c) & set("0O1IL2Z5S8B") for c in codes)  # 헷갈리는 글자 없음
    assert codes != sorted(codes)  # 고객 번호 순서와 관계없음
    assert all(user_id[-3:] not in code for user_id, code in rows)
    init_db.build(tmp_path / "again.db")
    with sqlite3.connect(tmp_path / "again.db") as other:
        assert other.execute("SELECT user_id, code FROM customer_codes ORDER BY user_id").fetchall() == rows


def test_customer_tool_does_not_expose_codes(conn):
    assert "code" not in simulator.load_customer(conn, "user_002")


# --- 가상 고객 분포(명세 v3.0) ---

def test_most_random_customers_fit_their_plan(conn):
    ids = [row[0] for row in conn.execute("SELECT user_id FROM users WHERE user_id BETWEEN 'user_021' AND 'user_200'")]
    results = [run(conn, user_id) for user_id in ids]
    with_usage = [r for r in results if r["status"] == "ok"]
    changes = [r for r in with_usage if r["recommendation"]["action"] in ("upgrade", "downgrade")]
    assert 0.10 <= len(changes) / len(with_usage) <= 0.35


# --- 안전성과 재현성 ---

def test_unknown_customer(conn):
    with pytest.raises(simulator.CustomerNotFound):
        run(conn, "user_999")


def test_connection_is_read_only(conn):
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM users")


def test_same_seed_same_data(tmp_path):
    def dump(path):
        init_db.build(path)
        with sqlite3.connect(path) as c:
            rows = c.execute("SELECT * FROM usage_logs ORDER BY log_id").fetchall()
        return rows

    assert dump(tmp_path / "a.db") == dump(tmp_path / "b.db")


def test_all_customers_simulate(conn):
    ids = [row[0] for row in conn.execute("SELECT user_id FROM users")]
    assert len(ids) == 203
    for user_id in ids:
        result = run(conn, user_id)
        if result["status"] == "ok":
            r = result["recommendation"]
            assert r["saving_12m"] >= 0
            assert r["current_total_12m"] - r["recommended_total_12m"] == r["saving_12m"]

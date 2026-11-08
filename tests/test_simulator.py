"""시뮬레이터 계산 검증. 기대값은 모두 손으로 계산한 값이다(주석에 식을 적었다).

단가는 명세 v2.4 기준이다(베이직 API 1,000회당 2,000원·GB당 500원, 프로 1,500원·300원, 엔터프라이즈 1,000원·200원).
(T3d) 장기 이용 혜택(명세 v3.5, LOY-01)을 반영해 기대값을 다시 계산했다.
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
# (T3d) 장기 이용 혜택(LOY-01): 가입 6·12·24개월 이상이면 모든 요금제의 기본 제공량이 5·10·15% 늘어난다.
# 베이직 10,000회·50GB → 10,500·52 / 11,000·55 / 11,500·57 (GB는 늘린 양을 내림: 2.5 → 2, 7.5 → 7)
# 프로 50,000·200 → 52,500·210 / 55,000·220 / 57,500·230, 엔터프라이즈 3단계 230,000·1,150
# 비교 기간 i번째 달(2026-12가 0)의 가입 기간은 (기준일 가입 기간 + i + 1)개월이다.

def test_basic_overage_bomb(conn):
    # user_002 가입 2025-03-20 → 기준일 19개월, 2단계(10%): 제공 11,000회·55GB
    # 베이직: 30,000 + ⌈31,000/1,000⌉×2,000 + ⌈65GB⌉×500 = 30,000 + 62,000 + 32,500 = 124,500
    assert option(run(conn, "user_002"), "PLAN_BASIC")["monthly"] == {
        "base_price": 30000, "api_overage": 62000, "storage_overage": 32500, "total": 124500}


def test_single_extra_call_rounds_up_to_one_unit(conn):
    # user_017 가입 16개월, 2단계 제공 11,000회. 11,001회 → 초과 1회 → 1,000회 단위 1개 = 2,000원
    assert option(run(conn, "user_017"), "PLAN_BASIC")["monthly"]["total"] == 32000


def test_exactly_included_amount_has_no_overage(conn):
    # user_016 가입 18개월, 2단계 프로 제공 55,000회·220GB와 사용량이 정확히 같다
    assert option(run(conn, "user_016"), "PLAN_PRO")["monthly"]["total"] == 80000


# --- 추천과 절감액 ---

def test_upgrade_with_promotion(conn):
    # user_002: 2026-12~2027-03(가입 20~23개월) 2단계 124,500 × 4 = 498,000
    #           2027-04~2027-11(24개월부터) 3단계: 제공 11,500·57 → 30,000 + ⌈30.5⌉×2,000 + 63×500 = 123,500 × 8 = 988,000
    # 현재 유지 1,486,000 / 프로 80,000×12 − 24,000×3 = 888,000 → 절감 598,000(598,000/1,486,000 = 40.2% → 40%)
    # 월 절감액(할인 전) = (1,486,000 − 960,000) / 12 = 43,833.3 → 43,833
    r = run(conn, "user_002")["recommendation"]
    assert (r["action"], r["plan_id"], r["promo_id"]) == ("upgrade", "PLAN_PRO", "PROMO_PRO_UPGRADE")
    assert (r["monthly_saving"], r["saving_12m"], r["saving_pct"]) == (43833, 598000, 40)


def test_promotion_decides_boundary_case(conn):
    # user_005 가입 22개월. 2026-12는 2단계(11,000회): 30,000 + ⌈23,000⌉×2,000 = 76,000
    # 2027-01부터 3단계(11,500회): 30,000 + ⌈22.5⌉×2,000 = 76,000 → 베이직 12개월 912,000
    # 프로 + 할인 888,000 < 912,000 → 할인 덕분에 프로. 할인이 없으면 프로 960,000 > 912,000 이라 베이직이 맞다.
    r = run(conn, "user_005")["recommendation"]
    assert (r["action"], r["plan_id"], r["saving_12m"]) == ("upgrade", "PLAN_PRO", 24000)


def test_same_usage_without_tenure_keeps_plan(conn):
    # user_005와 사용량이 같지만 가입 3개월이라 할인 대상이 아니다.
    # 2026-12~2027-01 단계 없음 ⌈24⌉ → 78,000 × 2, 2027-02~07 1단계(10,500회) ⌈23.5⌉ → 78,000 × 6,
    # 2027-08~11 2단계(11,000회) ⌈23⌉ → 76,000 × 4 = 928,000 < 프로 960,000 → 유지
    result = run(conn, "user_004")
    assert result["recommendation"]["action"] == "keep"
    assert result["recommendation"]["current_total_12m"] == 928000
    skipped = option(result, "PLAN_PRO")["skipped_promotions"]
    assert {"promo_id": "PROMO_PRO_UPGRADE", "promo_name": "프로 전환 3개월 30% 할인",
            "reason": "이용 6개월 이상 필요(현재 3개월)"} in skipped


def test_downgrade(conn):
    r = run(conn, "user_007")["recommendation"]
    # user_007 가입 33개월, 3단계. 엔터프라이즈 200,000 vs 프로 80,000 + ⌈2,500/1,000⌉×1,500 = 84,500
    # (프로 제공 57,500회·230GB, 스토리지 150GB는 제공량 안) → 월 절감 115,500, 12개월 1,386,000
    assert (r["action"], r["plan_id"], r["monthly_saving"], r["saving_12m"]) == (
        "downgrade", "PLAN_PRO", 115500, 1386000)


def test_saving_percent_rounds_half_up(conn):
    # 1,260,000 / 2,400,000 = 52.5% → 53% (짝수 쪽 반올림이면 52%가 된다). T10에서 찾은 사례(장기 혜택 전 user_007)
    assert simulator.saving_percent(1260000, 2400000) == 53
    assert simulator.saving_percent(1386000, 2400000) == 58  # user_007: 57.75% → 58%
    assert run(conn, "user_007")["recommendation"]["saving_pct"] == 58


def test_enterprise_fixed_discount(conn):
    # user_008 가입 41개월, 3단계. 프로 80,000 + ⌈62,500/1,000⌉×1,500 + 170×300 = 80,000 + 94,500 + 51,000 = 225,500
    # → ×12 = 2,706,000 / 엔터프라이즈 200,000×12 − 50,000×6 = 2,100,000 → 절감 606,000
    r = run(conn, "user_008")["recommendation"]
    assert (r["plan_id"], r["promo_id"], r["saving_12m"]) == ("PLAN_ENTERPRISE", "PROMO_ENT_UPGRADE", 606000)


def test_keep_with_loyalty_promotion(conn):
    # user_014 가입 34개월: 장기 혜택 3단계와 장기 고객 이벤트를 함께 받는다(LOY-03). 사용량은 제공량 안이라 30,000.
    # 할인 10,000 × 2개월 = 20,000
    result = run(conn, "user_014")
    r = result["recommendation"]
    assert (r["action"], r["promo_id"], r["saving_12m"]) == ("keep_with_promotion", "PROMO_BASIC_LOYAL", 20000)
    assert result["loyalty"]["tier"] == 3


def test_tie_keeps_current_plan(conn):
    # user_019 가입 3개월(전환 할인 없음), API 33,000회, 61.5GB
    # 2026-12~2027-01 단계 없음: 30,000 + 23×2,000 + ⌈11.5⌉×500 = 82,000 × 2 = 164,000
    # 2027-02~07 1단계(10,500·52): 30,000 + ⌈22.5⌉×2,000 + ⌈9.5⌉×500 = 81,000 × 6 = 486,000
    # 2027-08~11 2단계(11,000·55): 30,000 + 22×2,000 + ⌈6.5⌉×500 = 77,500 × 4 = 310,000
    # 합계 960,000 = 프로 80,000 × 12 → 같으면 현재 요금제
    r = run(conn, "user_019")["recommendation"]
    assert (r["action"], r["plan_id"], r["current_total_12m"]) == ("keep", "PLAN_BASIC", 960000)


def test_expired_promotion_is_skipped(conn):
    skipped = option(run(conn, "user_018"), "PLAN_PRO")["skipped_promotions"]
    assert any(s["promo_id"] == "PROMO_PRO_SUMMER" and s["reason"] == "이벤트 기간 아님" for s in skipped)


def test_low_data_flag(conn):
    # user_013 기록 1개월, 가입 1개월(할인 없음). API 40,000회, 40GB
    # 2026-12~2027-03 단계 없음 30,000 + 30×2,000 = 90,000 × 4, 2027-04~09 1단계 ⌈29.5⌉ → 90,000 × 6,
    # 2027-10~11 2단계 29 → 88,000 × 2 → 1,076,000. 프로 960,000 → 절감 116,000
    r = run(conn, "user_013")["recommendation"]
    assert (r["action"], r["plan_id"], r["promo_id"], r["saving_12m"], r["low_data"]) == (
        "upgrade", "PLAN_PRO", None, 116000, True)


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


# --- 장기 이용 혜택(LOY-01~03) ---

@pytest.mark.parametrize("tenure, tier", [(0, 0), (5, 0), (6, 1), (11, 1), (12, 2), (23, 2), (24, 3), (60, 3)])
def test_loyalty_tier_boundaries(conn, tenure, tier):
    tiers = simulator.load_loyalty_tiers(conn)
    assert simulator.loyalty(tenure, tiers, date(2026, 11, 1))["tier"] == tier


def test_loyalty_tiers_come_from_the_table(conn):
    # 단계 기준과 비율은 원천 데이터 표에서 읽는다(LOY-02)
    tiers = simulator.load_loyalty_tiers(conn)
    assert [(t.tier, t.min_tenure_months, t.bonus_pct) for t in tiers] == [(1, 6, 5), (2, 12, 10), (3, 24, 15)]


def test_bonus_adds_to_included_amounts_rounded_down(conn):
    plans = {p.plan_id: p for p in simulator.load_plans(conn)}
    basic, pro, ent = plans["PLAN_BASIC"], plans["PLAN_PRO"], plans["PLAN_ENTERPRISE"]

    def included(plan, pct):
        bonus = simulator.with_bonus(plan, pct)
        return bonus.included_api_calls, bonus.included_storage_gb

    # 50GB × 5% = 2.5 → 2, × 15% = 7.5 → 7
    assert {pct: included(basic, pct) for pct in (0, 5, 10, 15)} == {
        0: (10000, 50), 5: (10500, 52), 10: (11000, 55), 15: (11500, 57)}
    assert included(pro, 15) == (57500, 230)
    assert included(ent, 5) == (210000, 1050)
    assert simulator.with_bonus(basic, 10).base_price == 30000  # 기본 요금은 그대로


def test_next_tier_is_reached_during_the_horizon(conn):
    # user_002 기준일 19개월(2단계). 24개월은 5개월 뒤 = 2027-04부터 3단계
    loy = run(conn, "user_002")["loyalty"]
    assert (loy["tier"], loy["tier_name"], loy["bonus_pct"], loy["is_top"]) == (2, "2단계", 10, False)
    assert loy["next"] == {"tier": 3, "tier_name": "3단계", "bonus_pct": 15, "min_tenure_months": 24,
                           "in_months": 5, "month": "2027-04", "in_horizon": True}
    assert loy["monthly_bonus_pct"] == [10] * 4 + [15] * 8


def test_bonus_stays_when_plan_changes(conn):
    # 혜택은 고객에게 붙는다. user_002가 프로로 바꿔도 같은 단계(10%)의 프로 제공량을 쓴다.
    pro = option(run(conn, "user_002"), "PLAN_PRO")
    assert (pro["included_api_calls"], pro["included_storage_gb"]) == (55000, 220)


def test_bonus_flips_a_random_customer_to_downgrade(conn):
    # user_198(무작위 고객, 일부러 맞추지 않은 자연 사례): 엔터프라이즈, 가입 2026-06-25 → 기준일 4개월(단계 없음)
    # 예상 = 최근 3개월 평균: API (124,806 + 115,646 + 118,511) / 3 = 119,654.3 → 119,654, 스토리지 783.8 / 3 → 261.3GB
    # 프로 2026-12(단계 없음, 50,000·200): 80,000 + ⌈69.654⌉×1,500 + ⌈61.3⌉×300 = 80,000 + 105,000 + 18,600 = 203,600
    #      2027-01~06(1단계 52,500·210): 80,000 + 68×1,500 + 52×300 = 197,600 × 6 = 1,185,600
    #      2027-07~11(2단계 55,000·220): 80,000 + 65×1,500 + 42×300 = 190,100 × 5 = 950,500
    #      합계 2,339,700 < 엔터프라이즈 200,000 × 12 = 2,400,000 → 하향, 절감 60,300(2.5% → 3%)
    # 지금 달만 보면 프로(203,600)가 더 비싸다. 혜택 전에는 엔터프라이즈 유지였다(T3d 추천 분포 기록).
    result = run(conn, "user_198")
    r = result["recommendation"]
    assert (r["action"], r["plan_id"], r["saving_12m"], r["saving_pct"], r["monthly_saving"]) == (
        "downgrade", "PLAN_PRO", 60300, 3, 5025)
    assert option(result, "PLAN_PRO")["monthly"]["total"] == 203600


def test_top_tier_and_no_usage_customers(conn):
    top = run(conn, "user_015")["loyalty"]  # 가입 2023-03-01 → 44개월
    assert (top["tier"], top["is_top"], top["next"]) == (3, True, None)
    no_usage = run(conn, "user_010")["loyalty"]  # 가입 2025-06-01 → 17개월, 7개월 뒤 3단계
    assert (no_usage["tier"], no_usage["next"]["in_months"], no_usage["next"]["month"]) == (2, 7, "2027-06")


def _reference_months(conn, user_id, plan_row, as_of=(2026, 11)):
    """시뮬레이터 코드를 쓰지 않고 명세 2장 규칙만으로 다시 계산한 비교 기간 12개월 청구액(할인 전).

    원천 표를 직접 읽고, 단계 기준·비율은 init_db의 원천 데이터에서 가져온다. 3개월 평균 고객만 대상이다.
    """
    signup = conn.execute("SELECT signup_date FROM users WHERE user_id = ?", (user_id,)).fetchone()[0]
    sy, sm, sd = (int(x) for x in signup.split("-"))
    rows = conn.execute("SELECT api_calls_used, storage_gb_used FROM usage_logs WHERE user_id = ? "
                        "ORDER BY target_month DESC LIMIT 3", (user_id,)).fetchall()
    api = round(sum(r[0] for r in rows) / len(rows))
    gb = round(sum(r[1] for r in rows) / len(rows), 1)
    _, _, base, inc_api, inc_gb, per_1k, per_gb = plan_row
    totals = []
    for offset in range(1, 13):  # 기준일 다음 달부터 12개월, 그달 1일의 가입 기간
        y, m = as_of[0] + (as_of[1] - 1 + offset) // 12, (as_of[1] - 1 + offset) % 12 + 1
        tenure = (y - sy) * 12 + (m - sm) - (1 if 1 < sd else 0)
        pct = max([p for _, _, need, p in init_db.LOYALTY_TIERS if tenure >= need], default=0)
        limit_api = inc_api + inc_api * pct // 100
        limit_gb = inc_gb + int(inc_gb * pct // 100)
        over_api = max(0, api - limit_api)
        over_gb = max(0.0, round(gb - limit_gb, 1))
        totals.append(base + -(-over_api // 1000) * per_1k + int(-(-over_gb // 1)) * per_gb)
    return totals


def test_every_customer_matches_an_independent_recalculation(conn):
    """시나리오 고객만 손으로 계산했으므로, 나머지 고객의 혜택 계산 버그를 잡기 위해 전원을 따로 계산해 맞춘다."""
    plans = {row[0]: tuple(row) for row in conn.execute("SELECT * FROM plans")}
    checked = 0
    for (user_id,) in conn.execute("SELECT user_id FROM users ORDER BY user_id").fetchall():
        result = run(conn, user_id)
        if result["status"] != "ok" or result["forecast"]["method"] != "3개월 평균":
            continue
        for o in result["options"]:
            expected = _reference_months(conn, user_id, plans[o["plan_id"]])
            assert [c["total"] for c in o["months_cost"]] == expected, (user_id, o["plan_id"])
        checked += 1
    assert checked >= 195  # 기록 없는 고객과 계절 반영 고객만 빠진다


# --- 추천 안정성(SIM-04) ---

def test_stable_recommendation(conn):
    # user_002: −20%(API 33,600, 96GB)면 베이직 2단계 96,500·3단계 95,500 → 1,150,000 > 888,000,
    # +20%(API 50,400, 144GB)면 프로 제공량 안 80,000 → 888,000 < 엔터프라이즈 2,100,000. 둘 다 프로 + 할인.
    s = run(conn, "user_002")["stability"]
    assert (s["status"], s["change_pct"]) == ("안정", 20)


def test_changes_when_usage_rises(conn):
    # user_019: 동점(960,000). +20%면 API 39,600 → 베이직이 매달 80,000을 넘어 프로가 싸다 → 프로
    s = run(conn, "user_019")["stability"]
    assert s["status"] == "사용량이 늘면 바뀜"
    assert (s["if_up"]["plan_id"], s["if_down"]["plan_id"]) == ("PLAN_PRO", "PLAN_BASIC")


def test_changes_when_usage_falls(conn):
    # user_005: −20%면 API 27,200 → 베이직 2026-12 30,000 + ⌈16.2⌉×2,000 = 64,000, 이후 ⌈15.7⌉ → 62,000 × 11
    # = 746,000 < 888,000 → 베이직 유지
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
    # 현재 유지: 2단계 124,500 × 4 + 3단계 123,500 × 8
    assert [m["total"] for m in sched["baseline"]] == [124500] * 4 + [123500] * 8
    assert sum(m["total"] for m in sched["baseline"]) == 1486000


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

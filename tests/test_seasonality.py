"""성수기 판정과 계절 반영 예측 검증(SIM-07, 08, DOC-08, VIZ-05).

시나리오 고객 3명의 24개월 기록(2024-11~2026-10)은 database/init_db.py의 SEASONAL 표에 있다.
기대값은 모두 손으로 계산했다(단가 v2.4: 베이직 30,000원·10,000회·50GB·1,000회당 2,000원·GB당 500원,
프로 80,000원·50,000회·200GB·1,500원·300원).

user_201 (주)하늘세무회계, 베이직. 평소 API 20,000회·45GB. 성수기(1·2·3·5월) 2025년 100,000회·150GB, 2026년 110,000회·160GB.
- 두 해 중앙값: API 20,000회(12개 중 8개가 20,000), 스토리지 45GB.
  높은 달 배수: 2025년 100,000/20,000 = 5.0, 2026년 110,000/20,000 = 5.5(스토리지 150/45 = 3.3, 160/45 = 3.6보다 큼)
- 성장률: 최근 3개월(2026-08~10) 합 60,000 ÷ 1년 전(2025-08~10) 60,000 = 1.00
- 달별 예상: 2027-01·02·03·05는 110,000회·160GB, 나머지 20,000회·45GB
- 베이직: 평소 30,000 + ⌈10,000/1,000⌉×2,000 = 50,000. 성수기 30,000 + 100×2,000 + 110×500 = 285,000
  12개월 = 50,000×8 + 285,000×4 = 1,540,000
- 프로: 평소 80,000. 성수기 80,000 + 60×1,500 = 170,000. 12개월 = 80,000×8 + 170,000×4 = 1,320,000
  전환 할인 24,000×3 = 72,000 → 1,248,000. 절감 292,000(18.96% → 19%)
- 3개월 평균만 쓰면(20,000회·45GB) 베이직 50,000 vs 프로 80,000이라 유지가 맞다. 계절 반영이 추천을 바꾼다.
- 다가오는 성수기 1~3월(2027-01~03): 신청 마감 2026-12-31. 베이직 285,000×3 = 855,000,
  프로 (170,000−24,000)×2 + 170,000 = 462,000. 평소 달 50,000 → 추가 비용 855,000 − 150,000 = 705,000

user_202 (주)온누리선물몰, 베이직, 가입 2024-09(장기 고객 할인 대상). 평소 8,000회·30GB.
1·2월 2025년 32,000회, 2026년 40,000회. 2026-08~10은 10,000회.
- 배수: 32,000/8,000 = 4.0, 40,000/8,000 = 5.0. 2026-08~10의 10,000회는 8,000×1.3 = 10,400 미만이라 높은 달이 아니다.
- 성장률: API 30,000 ÷ 24,000 = 1.25, 스토리지 1.00
- 달별 예상 API: 1·2월 40,000×1.25 = 50,000, 8~10월 10,000×1.25 = 12,500, 나머지 8,000×1.25 = 10,000
- 베이직: 평소 30,000, 1·2월 30,000 + 40×2,000 = 110,000, 8~10월 30,000 + ⌈2,500/1,000⌉×2,000 = 36,000
  12개월 = 30,000×7 + 110,000×2 + 36,000×3 = 538,000. 장기 고객 할인 10,000×2 → 518,000(유지 + 이벤트)
- 프로: 80,000×12 − 72,000 = 888,000 → 유지가 유리(변경 권유 없이 예산 안내 대상)

user_203 (주)바람캠핑: 2025-08에만 60,000회(평소 9,000회). 한 해만 높으므로 성수기 아님 → 3개월 평균 예측.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import sys
from datetime import date
from fractions import Fraction
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "database"))

import charts  # noqa: E402
import documents  # noqa: E402
import init_db  # noqa: E402
import simulator  # noqa: E402

TODAY = date(2026, 11, 7)


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "billing_mock.db"
    init_db.build(path)
    return path


@pytest.fixture(scope="module")
def conn(db):
    connection = simulator.connect(db)
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def sims(conn):
    return {u: simulator.simulate(conn, u) for u in ("user_201", "user_202", "user_203")}


# --- 기존 고객은 그대로(결정 30) ---

def _h(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()[:16]


def test_existing_200_customers_are_unchanged(db, conn):
    """T3b 전 값과 같은지 지문으로 확인한다(데이터, 고객, 가칭 코드, 추천 결과)."""
    with sqlite3.connect(db) as raw:
        usage = raw.execute("SELECT user_id, target_month, api_calls_used, storage_gb_used, active_days FROM usage_logs"
                            " WHERE user_id <= 'user_200' ORDER BY user_id, target_month").fetchall()
        users = raw.execute("SELECT * FROM users WHERE user_id <= 'user_200' ORDER BY user_id").fetchall()
        codes = raw.execute("SELECT * FROM customer_codes WHERE user_id <= 'user_200' ORDER BY user_id").fetchall()
    assert _h([list(r) for r in usage]) == "3553c74e25d902a1"
    assert _h([list(r) for r in users]) == "7028aa485ff6d564"
    assert _h([list(r) for r in codes]) == "7e9ab66462297b25"
    results = []
    for user_id in [u[0] for u in users]:
        r = simulator.simulate(conn, user_id)
        if r["status"] != "ok":
            results.append([user_id, None])
            continue
        rec = r["recommendation"]
        assert r["forecast"]["method"] == "3개월 평균"
        results.append([user_id, rec["plan_id"], rec["promo_id"], rec["current_total_12m"], rec["recommended_total_12m"],
                        r["stability"]["status"], [m["total"] for m in r["schedule"]["recommended"]]])
    assert _h(results) == "77c68263bf3995fc"


# --- 성수기 판정(SIM-07) ---

def test_tax_firm_peaks_and_evidence(sims):
    season = sims["user_201"]["seasonality"]
    assert season["status"] == "있음"
    assert season["peak_months"] == [1, 2, 3, 5]
    assert season["bundles"] == [[1, 2, 3], [5]]
    january = season["evidence"][0]
    assert january == {"calendar_month": 1, "occurrences": [{"month": "2025-01", "ratio": "5.0"},
                                                            {"month": "2026-01", "ratio": "5.5"}]}


def test_gift_shop_peaks_but_growth_months_are_not_peaks(sims):
    season = sims["user_202"]["seasonality"]
    assert season["peak_months"] == [1, 2]
    assert [o["ratio"] for o in season["evidence"][0]["occurrences"]] == ["4.0", "5.0"]


def test_one_off_spike_is_not_a_peak(sims):
    r = sims["user_203"]
    assert r["seasonality"]["status"] == "없음" and r["seasonality"]["peak_months"] == []
    assert r["forecast"]["method"] == "3개월 평균" and r["forecast"]["api_calls"] == 9000
    assert "upcoming" not in r["seasonality"]


def test_short_history_is_not_judged(conn):
    season = simulator.simulate(conn, "user_002")["seasonality"]
    assert season == {"status": "판정 안 함", "months_recorded": 6, "peak_months": [], "bundles": [], "evidence": []}


def test_threshold_is_inclusive_and_needs_both_years():
    """정확히 1.3배인 달은 높은 달이다. 한 해만 높으면 성수기가 아니다."""
    months = simulator._months_before(simulator.AS_OF, 24)

    def usage(values):
        return [{"target_month": m, "api_calls_used": v, "storage_gb_used": 10.0, "active_days": 20}
                for m, v in zip(months, values)]

    flat = [10000] * 24
    exactly = flat.copy()
    exactly[2] = exactly[14] = 13000  # 2025-01, 2026-01이 정확히 1.3배
    assert simulator.seasonality(usage(exactly))["peak_months"] == [1]
    below = flat.copy()
    below[2] = below[14] = 12999
    assert simulator.seasonality(usage(below))["peak_months"] == []
    one_year = flat.copy()
    one_year[14] = 30000
    assert simulator.seasonality(usage(one_year))["peak_months"] == []


def test_bundles_wrap_around_new_year():
    assert simulator._bundles([1, 2, 11, 12]) == [[11, 12, 1, 2]]
    assert simulator._bundles([1, 3, 5]) == [[1], [3], [5]]
    assert simulator.month_label([12, 1]) == "12~1월"


# --- 계절 반영 예측(SIM-08) ---

def test_tax_firm_monthly_forecast(sims):
    f = sims["user_201"]["forecast"]
    assert f["method"] == "계절 반영" and f["growth"] == {"api_calls": "1.00", "storage_gb": "1.00"}
    peaks = {"2027-01", "2027-02", "2027-03", "2027-05"}
    for m in f["monthly"]:
        expected = (110000, 160.0) if m["month"] in peaks else (20000, 45.0)
        assert (m["api_calls"], m["storage_gb"]) == expected, m["month"]
    assert f["monthly"][-1]["base_month"] == "2025-11"  # 2026-11은 아직 기록이 없어 최근 12개월 중 같은 달을 쓴다


def test_gift_shop_growth_is_applied(sims):
    f = sims["user_202"]["forecast"]
    assert f["growth"] == {"api_calls": "1.25", "storage_gb": "1.00"}
    api = {m["month"]: m["api_calls"] for m in f["monthly"]}
    assert api["2027-01"] == api["2027-02"] == 50000
    assert api["2027-08"] == api["2027-09"] == api["2027-10"] == 12500
    assert api["2026-12"] == api["2027-04"] == 10000


def test_tax_firm_recommendation_changes_with_season(sims):
    r = sims["user_201"]
    rec = r["recommendation"]
    assert (rec["action"], rec["plan_id"], rec["promo_id"]) == ("upgrade", "PLAN_PRO", "PROMO_PRO_UPGRADE")
    assert (rec["current_total_12m"], rec["recommended_total_12m"], rec["saving_12m"], rec["saving_pct"]) \
        == (1540000, 1248000, 292000, 19)
    assert rec["monthly_saving"] == 18333  # (1,540,000 − 1,320,000) ÷ 12 = 18,333.3
    assert [m["total"] for m in r["schedule"]["baseline"]] == \
        [50000, 285000, 285000, 285000, 50000, 285000] + [50000] * 6
    assert [m["total"] for m in r["schedule"]["recommended"]] == \
        [56000, 146000, 146000, 170000, 80000, 170000] + [80000] * 6
    assert r["stability"]["status"] == "안정"


def test_three_month_average_alone_would_keep_basic(conn):
    """계절 반영이 없으면(같은 사용량을 3개월 평균으로) 유지가 맞다. 계절 반영이 추천을 바꾼다는 근거."""
    plans = simulator.load_plans(conn)
    by_id = {p.plan_id: p for p in plans}
    expected = {"api_calls": 20000, "storage_gb": 45.0}
    options = simulator._compare(plans, simulator.load_promotions(conn), by_id["PLAN_BASIC"], expected, 45,
                                 simulator.AS_OF)
    assert simulator._best(options, by_id)["plan_id"] == "PLAN_BASIC"


def test_gift_shop_keeps_plan(sims):
    rec = sims["user_202"]["recommendation"]
    assert (rec["action"], rec["plan_id"], rec["promo_id"]) == ("keep_with_promotion", "PLAN_BASIC", "PROMO_BASIC_LOYAL")
    assert (rec["current_total_12m"], rec["recommended_total_12m"]) == (538000, 518000)
    pro = next(o for o in sims["user_202"]["options"] if o["plan_id"] == "PLAN_PRO")
    assert pro["total_12m"] == 888000


def test_upcoming_peak_and_extra_cost(sims):
    up = sims["user_201"]["seasonality"]["upcoming"]
    assert up == {"label": "1~3월", "months": ["2027-01", "2027-02", "2027-03"], "deadline": "2026-12-31",
                  "current_bill": 855000, "recommended_bill": 462000, "usual_month": 50000, "extra_cost": 705000}
    shop = sims["user_202"]["seasonality"]["upcoming"]
    # 1~2월: 110,000×2 = 220,000, 평소 30,000 → 추가 160,000
    assert (shop["label"], shop["deadline"], shop["current_bill"], shop["extra_cost"]) == ("1~2월", "2026-12-31", 220000, 160000)


def test_growth_is_clamped():
    months = simulator._months_before(simulator.AS_OF, 24)
    values = [1000] * 21 + [10000] * 3  # 최근 3개월만 10배
    usage = [{"target_month": m, "api_calls_used": v, "storage_gb_used": 10.0, "active_days": 20}
             for m, v in zip(months, values)]
    _monthly, growth = simulator.seasonal_usage(usage, simulator.AS_OF, simulator._months_after(simulator.AS_OF, 12))
    assert growth["api_calls"] == "2.00"
    assert simulator.GROWTH_LIMITS == (Fraction(1, 2), Fraction(2))


def test_schedule_sums_match_totals_for_seasonal(sims):
    for r in sims.values():
        rec, sched = r["recommendation"], r["schedule"]
        assert sum(m["total"] for m in sched["baseline"]) == rec["current_total_12m"]
        assert sum(m["total"] for m in sched["recommended"]) == rec["recommended_total_12m"]


# --- 서류와 그래프(DOC-08, VIZ-05) ---

def test_review_has_upcoming_peak_section(conn):
    data = documents.build_data(conn, "user_201")
    season = data["season"]
    assert season["peak_label"] == "1~3월, 5월" and season["deadline"] == "2026-12-31"
    assert season["current"] == {"supply": 855000, "tax": 85500, "total": 940500}
    assert season["recommended"] == {"supply": 462000, "tax": 46200, "total": 508200}
    assert "계절 반영" in data["basis"][1] and "API 1.00배" in data["basis"][1]
    assert documents.build_data(conn, "user_203")["season"] is None
    assert documents.build_data(conn, "user_002")["season"] is None


def test_upcoming_peak_in_every_format(conn, tmp_path):
    from docx import Document
    from openpyxl import load_workbook
    from pypdf import PdfReader

    result = documents.issue(conn, "user_201", "user_201", issued=TODAY, formats=("pdf", "docx", "xlsx"),
                             app_db=tmp_path / "app.db", audit_path=tmp_path / "a.jsonl")
    files = result["files"]
    pdf = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(files["pdf"])).pages)
    docx = Document(io.BytesIO(files["docx"]))
    cells = " ".join(c.text for t in docx.tables for row in t.rows for c in row.cells)
    for amount in ("855,000", "85,500", "940,500", "462,000", "508,200"):
        assert amount in pdf and amount in cells
    wb = load_workbook(io.BytesIO(files["xlsx"]))
    summary = list(wb["요약"].iter_rows(values_only=True))
    assert ("현재 요금제 유지(베이직)", 855000, 85500, 940500) in [tuple(r[:4]) for r in summary]
    forecast_rows = [r for r in wb["근거"].iter_rows(values_only=True) if r and r[0] == "2027-01"]
    assert forecast_rows == [("2027-01", 110000, 160, "2026-01")]
    basis = list(wb["근거"].iter_rows(values_only=True))
    assert basis[0][:3] == ("월", "API 호출(회)", "스토리지(GB)")
    usage_rows = []
    for row in basis[1:]:
        if row[0] is None:
            break
        usage_rows.append(row)
    assert len(usage_rows) == 24 and usage_rows[0][0] == "2024-11"


def test_seasonal_captions(conn):
    caps = documents.build_data(conn, "user_201")["captions"]
    assert caps["usage"][0] == "1~3월은 2년 연속 평소보다 많이 썼습니다(2025-01 평소의 5.0배, 2026-01 5.5배)."
    assert "성장률(API 1.00배, 스토리지 1.00배)" in caps["usage"][2]
    assert caps["usage"][3] == "가장 많이 쓸 것으로 예상되는 달은 2027-01입니다(API 호출 110,000회, 스토리지 160GB)."
    assert "API 호출은 100,000회 초과, 스토리지는 110GB 초과입니다." in caps["usage"][4]
    assert "API 호출은 60,000회 초과, 스토리지는 제공량 이내입니다." in caps["usage"][5]
    assert caps["cumulative"][-1] == "성수기(1~3월, 5월)에는 지금 요금제의 선이 가파르게 올라가 간격이 크게 벌어집니다."
    assert caps["composition"][-1] == "예상 사용량 기준으로 API 초과 요금 2027-01~2027-03·2027-05 월 90,000원이 포함됩니다."
    shop = documents.build_data(conn, "user_202")["captions"]
    # 같은 요금제를 유지하므로 두 선이 함께 오른다(간격이 벌어진다고 쓰면 틀림)
    assert shop["cumulative"][-1] == "성수기(1~2월)에는 두 선 모두 가파르게 올라갑니다."
    assert "이벤트(베이직 2년 이상 고객 2개월 1만 원 할인)를 신청하면 518,000원" in shop["cumulative"][0]


def test_usage_chart_shows_24_months(conn):
    from PIL import Image

    data = documents.stamp(documents.build_data(conn, "user_201"), TODAY)
    assert len(data["usage"]) == 24 and data["peak_months"] == [1, 2, 3, 5]
    image = Image.open(io.BytesIO(charts.usage(data)))
    assert image.format == "PNG"

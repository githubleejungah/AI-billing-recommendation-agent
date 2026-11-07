"""요금제·이벤트 비교 시뮬레이터. 금액 계산은 모두 여기서 하고, AI는 이 결과만 설명한다.

실행: python src/simulator.py user_002
규칙은 design/requirements.md 2장을 따른다.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from contextlib import closing
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from pathlib import Path

import data_access
import settings

DB_PATH = data_access.DB_PATH
AS_OF = settings.DEFAULT_AS_OF  # 기본 분석 기준일. 실행할 때는 settings.as_of()(SIM-09)
FORECAST_MONTHS = 3
HORIZON_MONTHS = 12
STABILITY_CHANGE_PCT = 20  # 추천 안정성 계산의 사용량 변화폭(%). T5a에서 정책 파일 값으로 바꾼다.
# (명세 v3.2) 성수기 판정과 계절 반영 예측. T5a에서 정책 파일 값으로 바꾼다.
ANALYSIS_MONTHS = 24  # 읽는 기록의 범위(최근 24개월)
SEASON_MIN_MONTHS = 24  # 성수기를 판정하려면 필요한 기록
SEASON_HIGH_RATIO = Fraction(13, 10)  # 그해 중앙값의 1.3배 이상이면 "높은 달"
GROWTH_LIMITS = (Fraction(1, 2), Fraction(2))  # 성장률은 0.5~2.0 사이로 자른다


# 청구 데이터 읽기는 data_access 한 곳에서 한다(RET-04). 예전 이름으로도 부를 수 있게 그대로 내보낸다.
CustomerNotFound = data_access.CustomerNotFound
Plan = data_access.Plan
Promotion = data_access.Promotion
connect = data_access.connect
load_plans = data_access.load_plans
load_promotions = data_access.load_promotions
load_customer = data_access.load_customer


def load_usage(conn: sqlite3.Connection, user_id: str, as_of: date | None = None) -> list[dict]:
    """기준일 전 최근 24개월(ANALYSIS_MONTHS)의 기록."""
    window = _months_before(as_of or settings.as_of(), ANALYSIS_MONTHS)
    return data_access.load_usage(conn, user_id, window[0], window[-1])


def forecast(usage: list[dict]) -> dict | None:
    """최근 3개월(없으면 있는 만큼) 평균. 기록이 없으면 None."""
    recent = usage[-FORECAST_MONTHS:]
    if not recent:
        return None
    count = len(recent)
    return {
        "api_calls": round(sum(row["api_calls_used"] for row in recent) / count),
        "storage_gb": round(sum(row["storage_gb_used"] for row in recent) / count, 1),
        "months_used": count,
        "months": [row["target_month"] for row in recent],
        "method": "3개월 평균",
    }


def monthly_cost(plan: Plan, api_calls: int, storage_gb: float) -> dict:
    api_over = max(0, api_calls - plan.included_api_calls)
    storage_over = max(0.0, round(storage_gb - plan.included_storage_gb, 1))
    api_charge = math.ceil(api_over / 1000) * plan.api_overage_per_1k
    storage_charge = math.ceil(storage_over) * plan.storage_overage_per_gb
    return {
        "base_price": plan.base_price,
        "api_overage": api_charge,
        "storage_overage": storage_charge,
        "total": plan.base_price + api_charge + storage_charge,
    }


def tenure_months(signup_date: str, as_of: date) -> int:
    signup = date.fromisoformat(signup_date)
    months = (as_of.year - signup.year) * 12 + as_of.month - signup.month
    return months - (1 if as_of.day < signup.day else 0)


def monthly_discount(promo: Promotion, plan: Plan) -> int:
    if promo.discount_type == "percent":
        return plan.base_price * promo.discount_value // 100
    return min(promo.discount_value, plan.base_price)


def promo_ineligibility(promo: Promotion, current: Plan, target: Plan, tenure: int, as_of: date) -> str | None:
    """적용할 수 없으면 이유를, 적용할 수 있으면 None을 돌려준다."""
    if promo.plan_id != target.plan_id:
        return "다른 요금제 대상"
    if not promo.valid_from <= as_of.isoformat() <= promo.valid_to:
        return "이벤트 기간 아님"
    if tenure < promo.min_tenure_months:
        return f"이용 {promo.min_tenure_months}개월 이상 필요(현재 {tenure}개월)"
    if promo.upgrade_only and target.base_price <= current.base_price:
        return "상위 요금제로 바꿀 때만 적용"
    return None


def _months_after(as_of: date, count: int) -> list[str]:
    """기준일 다음 달부터 count개월(YYYY-MM). 요금제 변경이 적용되는 다음 청구 주기와 같다."""
    months, year, month = [], as_of.year, as_of.month
    for _ in range(count):
        month += 1
        if month > 12:
            year, month = year + 1, 1
        months.append(f"{year}-{month:02d}")
    return months


def _shift(month: str, delta: int) -> str:
    year, mon = int(month[:4]), int(month[5:]) - 1 + delta
    return f"{year + mon // 12}-{mon % 12 + 1:02d}"


def _months_before(as_of: date, count: int) -> list[str]:
    """기준일 전 count개월(오래된 달부터). 기준일 2026-11-01, 24개월이면 2024-11 ~ 2026-10."""
    last = f"{as_of.year}-{as_of.month:02d}"
    return [_shift(last, -offset) for offset in range(count, 0, -1)]


def _half_up(value: Fraction, places: int = 0) -> Decimal:
    quantum = Decimal(1).scaleb(-places)
    return (Decimal(value.numerator) / Decimal(value.denominator)).quantize(quantum, rounding=ROUND_HALF_UP)


def _bundles(peaks: list[int]) -> list[list[int]]:
    """연속된 성수기 월을 묶는다. 12월과 1월은 이어진 것으로 본다(예: [12, 1])."""
    chosen = set(peaks)
    if len(chosen) == 12:
        return [list(range(1, 13))]
    bundles = []
    for month in sorted(chosen):
        if (month - 2) % 12 + 1 in chosen:
            continue  # 앞 달도 성수기면 묶음의 시작이 아니다
        bundle = [month]
        while bundle[-1] % 12 + 1 in chosen:
            bundle.append(bundle[-1] % 12 + 1)
        bundles.append(bundle)
    return bundles


def month_label(bundle: list[int]) -> str:
    return f"{bundle[0]}월" if len(bundle) == 1 else f"{bundle[0]}~{bundle[-1]}월"


def seasonality(usage: list[dict], as_of: date | None = None) -> dict:
    """성수기 판정(SIM-07). 두 해 모두 그해 중앙값의 1.3배 이상인 달(API·스토리지 중 하나라도)만 성수기다."""
    window = _months_before(as_of or settings.as_of(), SEASON_MIN_MONTHS)
    by_month = {row["target_month"]: row for row in usage}
    recorded = sum(1 for month in window if month in by_month)
    if recorded < SEASON_MIN_MONTHS:
        return {"status": "판정 안 함", "months_recorded": recorded, "peak_months": [], "bundles": [], "evidence": []}
    high_by_year = []
    for year in (window[:12], window[12:]):  # 그 전 12개월, 최근 12개월
        found: dict[int, tuple[Fraction, str]] = {}
        for key in ("api_calls_used", "storage_gb_used"):
            values = [Fraction(str(by_month[month][key])) for month in year]
            median = Fraction(statistics.median(values))
            if median == 0:
                continue
            for month, value in zip(year, values):
                if value >= SEASON_HIGH_RATIO * median:
                    ratio, calendar = value / median, int(month[5:])
                    if calendar not in found or ratio > found[calendar][0]:
                        found[calendar] = (ratio, month)
        high_by_year.append(found)
    peaks = sorted(set(high_by_year[0]) & set(high_by_year[1]))
    evidence = [{"calendar_month": calendar,
                 "occurrences": [{"month": year[calendar][1], "ratio": str(_half_up(year[calendar][0], 1))}
                                 for year in high_by_year]}
                for calendar in peaks]
    return {"status": "있음" if peaks else "없음", "months_recorded": recorded, "peak_months": peaks,
            "bundles": _bundles(peaks), "evidence": evidence}


def seasonal_usage(usage: list[dict], as_of: date, months: list[str]) -> tuple[list[dict], dict]:
    """계절 반영 예측(SIM-08). 그달 예상 = 최근 12개월 기록 중 같은 달 사용량 × 성장률.

    성장률 = 최근 3개월 합 ÷ 1년 전 같은 3개월 합(API·스토리지 각각, 0.5~2.0으로 자름).
    API는 정수, 스토리지는 0.1GB로 반올림한다(0.5는 올림).
    """
    by_month = {row["target_month"]: row for row in usage}
    window = _months_before(as_of, SEASON_MIN_MONTHS)
    recent = window[-FORECAST_MONTHS:]
    growth = {}
    for key in ("api_calls_used", "storage_gb_used"):
        now = sum(Fraction(str(by_month[m][key])) for m in recent)
        before = sum(Fraction(str(by_month[_shift(m, -12)][key])) for m in recent)
        ratio = now / before if before else Fraction(1)
        growth[key] = min(max(ratio, GROWTH_LIMITS[0]), GROWTH_LIMITS[1])
    base_month = {int(month[5:]): month for month in window[-12:]}
    monthly = []
    for month in months:
        base = by_month[base_month[int(month[5:])]]
        monthly.append({
            "month": month,
            "base_month": base["target_month"],
            "api_calls": int(_half_up(Fraction(base["api_calls_used"]) * growth["api_calls_used"])),
            "storage_gb": float(_half_up(Fraction(str(base["storage_gb_used"])) * growth["storage_gb_used"], 1)),
        })
    return monthly, {"api_calls": str(_half_up(growth["api_calls_used"], 2)),
                     "storage_gb": str(_half_up(growth["storage_gb_used"], 2))}


def _compare(plans, promotions, current: Plan, expected: dict, tenure: int, as_of: date,
             monthly_usage: list[dict] | None = None) -> list[dict]:
    """예상 사용량으로 요금제마다 월 청구액, 받을 수 있는 가장 큰 이벤트, 12개월 총액을 계산한다.

    monthly_usage(계절 반영 예측)가 있으면 달마다 다른 사용량으로, 없으면 3개월 평균으로 12개월을 계산한다.
    """
    options = []
    usages = monthly_usage or [expected] * HORIZON_MONTHS
    for plan in plans:
        cost = monthly_cost(plan, expected["api_calls"], expected["storage_gb"])
        months_cost = [monthly_cost(plan, u["api_calls"], u["storage_gb"]) for u in usages]
        regular_total = sum(c["total"] for c in months_cost)
        best_promo, best_discount, skipped = None, 0, []
        for promo in promotions:
            reason = promo_ineligibility(promo, current, plan, tenure, as_of)
            if reason == "다른 요금제 대상":
                continue
            if reason:
                skipped.append({"promo_id": promo.promo_id, "promo_name": promo.promo_name, "reason": reason})
                continue
            discount = monthly_discount(promo, plan) * min(promo.duration_months, HORIZON_MONTHS)
            if discount > best_discount:
                best_promo, best_discount = promo, discount
        options.append({
            "plan_id": plan.plan_id,
            "plan_name": plan.plan_name,
            "is_current": plan.plan_id == current.plan_id,
            "included_api_calls": plan.included_api_calls,  # 리포트 자리표시자용 기본 제공량(T4)
            "included_storage_gb": plan.included_storage_gb,
            "monthly": cost,  # 최근 3개월 평균 사용량 기준 월 청구액
            "months_cost": months_cost,  # 비교 기간 달마다의 청구액(할인 전)
            "promotion": None if best_promo is None else {
                "promo_id": best_promo.promo_id,
                "promo_name": best_promo.promo_name,
                "discount_total": best_discount,
                "monthly_discount": monthly_discount(best_promo, plan),
                "months": min(best_promo.duration_months, HORIZON_MONTHS),
                "discount_type": best_promo.discount_type,
                "discount_value": best_promo.discount_value,
                "valid_to": best_promo.valid_to,
                "min_tenure_months": best_promo.min_tenure_months,
            },
            "skipped_promotions": skipped,
            "total_12m": regular_total - best_discount,
        })
    return options


def _best(options: list[dict], by_id: dict[str, Plan]) -> dict:
    # 가장 싼 선택. 같으면 현재 요금제, 그다음 기본 요금이 낮은 요금제를 고른다.
    return min(options, key=lambda o: (o["total_12m"], not o["is_current"], by_id[o["plan_id"]].base_price))


def monthly_schedule(option: dict, months: list[str], with_promotion: bool = True) -> list[dict]:
    """월별 일정(SIM-05). 할인은 이벤트 할인 기간 동안의 달에만 들어간다."""
    promo = option["promotion"] if with_promotion else None
    rows = []
    for index, month in enumerate(months):
        cost = option["months_cost"][index]
        discount = promo["monthly_discount"] if promo and index < promo["months"] else 0
        rows.append({
            "month": month,
            "base": cost["base_price"],
            "api_overage": cost["api_overage"],
            "storage_overage": cost["storage_overage"],
            "discount": discount,
            "total": cost["total"] - discount,
        })
    return rows


def stability(plans, promotions, current: Plan, expected: dict, tenure: int, as_of: date,
              recommended: dict, change_pct: int = STABILITY_CHANGE_PCT, monthly_usage: list[dict] | None = None) -> dict:
    """추천 안정성(SIM-04). 예상 사용량을 함께 줄이거나 늘려 다시 비교한다. 추천 자체는 바꾸지 않는다."""
    by_id = {plan.plan_id: plan for plan in plans}
    key = (recommended["plan_id"], recommended["promotion"]["promo_id"] if recommended["promotion"] else None)
    cases = {}
    for name, factor in (("if_down", 1 - change_pct / 100), ("if_up", 1 + change_pct / 100)):
        def scale(u):
            return {**u, "api_calls": round(u["api_calls"] * factor), "storage_gb": round(u["storage_gb"] * factor, 1)}

        scaled_months = [scale(u) for u in monthly_usage] if monthly_usage else None
        best = _best(_compare(plans, promotions, current, scale(expected), tenure, as_of, scaled_months), by_id)
        cases[name] = {"plan_id": best["plan_id"], "plan_name": best["plan_name"],
                       "promo_id": best["promotion"]["promo_id"] if best["promotion"] else None}
    down = (cases["if_down"]["plan_id"], cases["if_down"]["promo_id"]) != key
    up = (cases["if_up"]["plan_id"], cases["if_up"]["promo_id"]) != key
    status = {(False, False): "안정", (True, False): "사용량이 줄면 바뀜",
              (False, True): "사용량이 늘면 바뀜", (True, True): "사용량이 줄거나 늘면 바뀜"}[(down, up)]
    return {"status": status, "change_pct": change_pct, **cases}


def simulate(conn: sqlite3.Connection, user_id: str, as_of: date | None = None) -> dict:
    as_of = as_of or settings.as_of()
    customer = load_customer(conn, user_id)
    plans = load_plans(conn)
    by_id = {plan.plan_id: plan for plan in plans}
    current = by_id[customer["current_plan_id"]]
    usage = load_usage(conn, user_id, as_of)
    expected = forecast(usage)
    season = seasonality(usage, as_of)
    months = _months_after(as_of, HORIZON_MONTHS)
    monthly_usage = None
    if expected is not None and season["status"] == "있음":
        monthly_usage, growth = seasonal_usage(usage, as_of, months)
        expected = {**expected, "method": "계절 반영", "growth": growth, "monthly": monthly_usage}
    result = {
        "user_id": user_id,
        "name": customer["name"],
        "as_of": as_of.isoformat(),
        "current_plan_id": current.plan_id,
        "current_plan_name": current.plan_name,
        "usage_history": usage,
        "forecast": expected,
        "activity": activity_trend(usage),
        "seasonality": season,
    }
    if expected is None:
        return {**result, "status": "no_usage", "options": [], "recommendation": None}

    tenure = tenure_months(customer["signup_date"], as_of)
    promotions = load_promotions(conn)
    options = _compare(plans, promotions, current, expected, tenure, as_of, monthly_usage)
    best = _best(options, by_id)
    current_option = next(o for o in options if o["is_current"])
    # 비교 기준은 "아무것도 바꾸지 않을 때", 곧 현재 요금제를 이벤트 없이 12개월 쓰는 금액이다.
    baseline_12m = sum(c["total"] for c in current_option["months_cost"])
    best_regular_12m = sum(c["total"] for c in best["months_cost"])
    if best["is_current"]:
        action = "keep_with_promotion" if best["promotion"] else "keep"
    else:
        action = "upgrade" if by_id[best["plan_id"]].base_price > current.base_price else "downgrade"
    recommendation = {
        "action": action,
        "plan_id": best["plan_id"],
        "plan_name": best["plan_name"],
        "promo_id": best["promotion"]["promo_id"] if best["promotion"] else None,
        # 할인 전 월 절감액. 달마다 금액이 다른 계절 반영 고객은 12개월 평균(0.5는 올림)이다.
        "monthly_saving": ((baseline_12m - best_regular_12m) * 2 + HORIZON_MONTHS) // (2 * HORIZON_MONTHS),
        "saving_12m": baseline_12m - best["total_12m"],
        # 리포트가 비율을 직접 계산하지 않도록 여기서 정수 %로 준다.
        # 반올림은 0.5를 올린다(파이썬 round()는 짝수 쪽으로 보내서 52.5가 52가 된다). 절감액은 0 이상이다.
        "saving_pct": ((baseline_12m - best["total_12m"]) * 200 + baseline_12m) // (2 * baseline_12m),
        "current_total_12m": baseline_12m,
        "recommended_total_12m": best["total_12m"],
        "low_data": expected["months_used"] < FORECAST_MONTHS,
    }
    schedule = {
        "months": months,
        # 비교 기준: 지금 요금제를 이벤트 없이 그대로 쓸 때
        "baseline": monthly_schedule(current_option, months, with_promotion=False),
        "recommended": monthly_schedule(best, months),
    }
    if season["status"] == "있음":
        season["upcoming"] = upcoming_peak(season, schedule)
    return {**result, "status": "ok", "tenure_months": tenure, "options": options, "recommendation": recommendation,
            "stability": stability(plans, promotions, current, expected, tenure, as_of, best,
                                   monthly_usage=monthly_usage),
            "schedule": schedule}


def upcoming_peak(season: dict, schedule: dict) -> dict | None:
    """비교 기간 안에서 가장 먼저 시작하는 성수기 묶음과 신청 마감일, 그 기간의 청구액(공급가액)."""
    months = schedule["months"]
    starts = []
    for bundle in season["bundles"]:
        index = next((i for i, m in enumerate(months) if int(m[5:]) == bundle[0]), None)
        if index is not None:
            starts.append((index, bundle))
    if not starts:
        return None
    index, bundle = min(starts)
    span = list(range(index, min(index + len(bundle), len(months))))
    first = date.fromisoformat(months[index] + "-01")
    off_peak = [row["total"] for row in schedule["baseline"] if int(row["month"][5:]) not in season["peak_months"]]
    usual = statistics.median_low(off_peak) if off_peak else 0  # 평소 달 청구액(짝수 개면 작은 쪽)
    current_bill = sum(schedule["baseline"][i]["total"] for i in span)
    return {
        "label": month_label(bundle),
        "months": [months[i] for i in span],
        "deadline": (first - timedelta(days=1)).isoformat(),  # 그 전달 말일까지 신청해야 첫 달부터 적용
        "current_bill": current_bill,
        "recommended_bill": sum(schedule["recommended"][i]["total"] for i in span),
        "usual_month": usual,
        "extra_cost": current_bill - usual * len(span),
    }


def activity_trend(usage: list[dict]) -> dict | None:
    """최근 3개월 평균 활동일을 그 전 3개월과 비교한다. 30% 이상 줄면 이탈 위험으로 본다."""
    if len(usage) < 2 * FORECAST_MONTHS:
        return None
    before = sum(row["active_days"] for row in usage[-6:-3]) / 3
    recent = sum(row["active_days"] for row in usage[-3:]) / 3
    change = round((recent - before) / before * 100) if before else 0
    return {"recent_avg_days": round(recent, 1), "previous_avg_days": round(before, 1),
            "change_pct": change, "churn_risk": change <= -30}


def _print(result: dict) -> None:
    print(f"[{result['user_id']}] {result['name']} / 현재 {result['current_plan_name']}")
    if result["status"] == "no_usage":
        print("  이용 기록이 없어 추천하지 않습니다.")
        return
    f = result["forecast"]
    print(f"  예상 사용량(최근 {f['months_used']}개월 평균): API {f['api_calls']:,}회, 스토리지 {f['storage_gb']}GB")
    for o in result["options"]:
        promo = f" - 이벤트 {o['promotion']['discount_total']:,}원" if o["promotion"] else ""
        mark = " (현재)" if o["is_current"] else ""
        print(f"  {o['plan_name']}{mark}: 월 {o['monthly']['total']:,}원, 12개월 {o['total_12m']:,}원{promo}")
    r = result["recommendation"]
    print(f"  추천: {r['action']} → {r['plan_name']} {r['promo_id'] or ''} / 12개월 절감 {r['saving_12m']:,}원")


def main() -> int:
    parser = argparse.ArgumentParser(description="요금제 비교 시뮬레이션")
    parser.add_argument("user_id")
    parser.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")
    args = parser.parse_args()
    try:
        with closing(connect()) as conn:
            result = simulate(conn, args.user_id)
    except CustomerNotFound:
        print(f"고객을 찾을 수 없습니다: {args.user_id}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())

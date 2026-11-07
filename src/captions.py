"""그래프 해설 문장(VIZ-04).

정해진 문장 틀에 서류 데이터의 값을 채운다. AI가 쓰지 않으므로 같은 데이터면 같은 문장이 나오고,
문장 속 숫자는 표·그래프의 숫자와 같다(DOC-02). 서류(PDF·DOCX)와 화면(T13)이 같은 문장을 쓴다.
"""

from __future__ import annotations


def _months(first: str, last: str) -> str:
    return first if first == last else f"{first}~{last}"


def _runs(monthly: list[dict], value) -> list[tuple[str, str, int]]:
    """값이 같은 연속 구간으로 묶는다: [(첫 달, 마지막 달, 값)]."""
    runs: list[list] = []
    for m in monthly:
        v = value(m)
        if runs and runs[-1][2] == v:
            runs[-1][1] = m["month"]
        else:
            runs.append([m["month"], m["month"], v])
    return [tuple(r) for r in runs]


def _amount_range(values: list[int]) -> str:
    low, high = min(values), max(values)
    return f"월 {low:,}원" if low == high else f"월 {low:,}~{high:,}원"


def cumulative(doc: dict) -> list[str]:
    baseline, recommended, saving = doc["comparison"]
    current, plan = doc["current_plan"], doc["recommended_plan"]
    if saving["supply"] <= 0:
        return [f"지금 요금제({current})가 비교한 요금제 중 12개월 총액이 가장 낮아 바꿀 필요가 없습니다. "
                f"12개월 예상 금액은 {baseline['supply']:,}원입니다(부가세 별도)."]
    if plan == current and doc["promotion"]:
        option = f"이벤트({doc['promotion']['name']})를 신청하면"
    else:
        option = f"추천 요금제({plan})로 바꾸면"
    lines = [f"지금 요금제({current})를 유지하면 12개월 동안 {baseline['supply']:,}원, "
             f"{option} {recommended['supply']:,}원이 듭니다(부가세 별도).",
             f"두 선의 마지막 간격 {saving['supply']:,}원({doc['saving_pct']}%)이 12개월 절감액입니다."]
    discounted = sum(1 for m in doc["monthly"] if m["discount"] > 0)
    if discounted:
        lines.append(f"처음 {discounted}개월은 이벤트 할인이 더해져 간격이 더 빠르게 벌어집니다.")
    if doc.get("season"):
        peaks = set(doc["peak_months"])
        gaps = {True: [], False: []}
        for m in doc["monthly"]:
            gaps[int(m["month"][5:]) in peaks].append(m["baseline"]["supply"] - m["recommended"]["supply"])
        widens = gaps[True] and gaps[False] and min(gaps[True]) > max(gaps[False])
        label = doc["season"]["peak_label"]
        lines.append(f"성수기({label})에는 지금 요금제의 선이 가파르게 올라가 간격이 크게 벌어집니다." if widens
                     else f"성수기({label})에는 두 선 모두 가파르게 올라갑니다.")
    return lines


def composition(doc: dict) -> list[str]:
    monthly, plan = doc["monthly"], doc["recommended_plan"]
    runs = _runs(monthly, lambda m: m["recommended"]["supply"])
    spans = ", ".join(f"{_months(a, b)} {v:,}원" for a, b, v in runs)
    if plan == doc["current_plan"] and doc["promotion"]:
        lines = [f"이벤트를 신청하고 지금 요금제({plan})를 유지할 때 월 청구액(부가세 별도)은 {spans}입니다."]
    elif plan == doc["current_plan"]:
        lines = [f"지금 요금제({plan})를 유지할 때 월 청구액(부가세 별도)은 {spans}입니다."]
    else:
        lines = [f"추천 요금제({plan})의 월 청구액(부가세 별도)은 {spans}입니다."]
    for a, b, v in _runs(monthly, lambda m: m["discount"]):
        if v:
            base = next(m for m in monthly if m["month"] == a)
            full = base["parts"]["base_after_discount"] + v
            lines.append(f"{_months(a, b)}에는 기본 요금 {full:,}원에서 {v:,}원이 할인됩니다.")
    extras = []
    for key, name in (("api_overage", "API 초과 요금"), ("storage_overage", "스토리지 초과 요금")):
        values = [m["parts"][key] for m in monthly]
        if not any(values):
            continue
        if all(values):
            extras.append(f"{name} {_amount_range(values)}")
            continue
        # 일부 달에만 있으면(계절 반영 고객) 그 달과 금액을 적는다.
        spans = [(a, b, v) for a, b, v in _runs(monthly, lambda m, k=key: m["parts"][k]) if v]
        by_amount: dict[int, list[str]] = {}
        for a, b, v in spans:
            by_amount.setdefault(v, []).append(_months(a, b))
        extras.append(f"{name} " + ", ".join(f"{'·'.join(ms)} 월 {v:,}원" for v, ms in by_amount.items()))
    if extras:
        lines.append(f"예상 사용량 기준으로 {', '.join(extras)}이 포함됩니다.")
    else:
        lines.append("예상 사용량이 기본 제공량 이내라 초과 요금은 없습니다.")
    return lines


def _against(forecast: dict, included: dict) -> str:
    api_over = forecast["api_calls"] - included["api_calls"]
    gb_over = forecast["storage_gb"] - included["storage_gb"]
    if api_over <= 0 and gb_over <= 0:
        return "API 호출과 스토리지 모두 제공량 이내입니다"
    api = f"API 호출은 {api_over:,.0f}회 초과" if api_over > 0 else "API 호출은 제공량 이내"
    gb = f"스토리지는 {gb_over:,g}GB 초과" if gb_over > 0 else "스토리지는 제공량 이내"
    return f"{api}, {gb}입니다"


def _limits(plan: dict) -> str:
    return f"API {plan['api_calls']:,}회, {plan['storage_gb']:,g}GB"


def _stability_lines(doc: dict, rec_plan: str) -> list[str]:
    s = doc["stability_detail"]
    pct = s["change_pct"]
    lines = []
    if not s["if_down"] and not s["if_up"]:
        lines.append(f"사용량이 {pct}% 늘거나 줄어도 추천은 바뀌지 않습니다.")
    for case, verb in (("if_down", "줄면"), ("if_up", "늘면")):
        other = s[case]
        if not other:
            continue
        if other == rec_plan:
            lines.append(f"사용량이 {pct}% {verb} 같은 요금제라도 받을 수 있는 이벤트가 달라질 수 있습니다.")
        else:
            lines.append(f"사용량이 {pct}% {verb} {other} 요금제가 더 유리해질 수 있습니다.")
    return lines


def _seasonal_usage(doc: dict) -> list[str]:
    """계절 반영 고객(VIZ-05): 성수기 근거, 예측 방법, 성수기 달 예상과 제공량 비교."""
    forecast, inc, season = doc["forecast"], doc["included"], doc["season"]
    lines = []
    for item in season["evidence"]:
        a, b = item["occurrences"]
        lines.append(f"{item['label']}은 2년 연속 평소보다 많이 썼습니다"
                     f"({a['month']} 평소의 {a['ratio']}배, {b['month']} {b['ratio']}배).")
    growth = forecast["growth"]
    lines.append(f"그래서 예상 사용량은 최근 12개월 중 같은 달 사용량에 성장률(API {growth['api_calls']}배, "
                 f"스토리지 {growth['storage_gb']}배)을 곱해 달마다 따로 계산했습니다.")
    peak = max(forecast["monthly"], key=lambda m: (m["api_calls"], m["storage_gb"]))
    lines.append(f"가장 많이 쓸 것으로 예상되는 달은 {peak['month']}입니다(API 호출 {peak['api_calls']:,}회, "
                 f"스토리지 {peak['storage_gb']:,g}GB).")
    lines.append(f"이 달을 지금 요금제({inc['current']['plan']}) 기본 제공량({_limits(inc['current'])})과 비교하면 "
                 + _against(peak, inc["current"]) + ".")
    if inc["recommended"]["plan"] != inc["current"]["plan"]:
        lines.append(f"추천 요금제({inc['recommended']['plan']}) 기본 제공량({_limits(inc['recommended'])})과 비교하면 "
                     + _against(peak, inc["recommended"]) + ".")
    return lines + _stability_lines(doc, inc["recommended"]["plan"])


def usage(doc: dict) -> list[str]:
    if doc["forecast"]["method"] == "계절 반영" and doc.get("season"):
        return _seasonal_usage(doc)
    forecast, inc = doc["forecast"], doc["included"]
    months = forecast["months"]
    basis = "평균인" if len(months) > 1 else "사용량인"
    lines = [f"예상 사용량은 최근 {len(months)}개월({_months(months[0], months[-1])}) {basis} "
             f"API 호출 {forecast['api_calls']:,}회, 스토리지 {forecast['storage_gb']:,g}GB입니다."]
    if len(months) < 3:
        lines.append(f"이용 기록이 {len(months)}개월뿐이라 실제 사용량과 차이가 클 수 있습니다.")
    cur = inc["current"]
    lines.append(f"지금 요금제({cur['plan']}) 기본 제공량({_limits(cur)})과 비교하면 " + _against(forecast, cur) + ".")
    rec = inc["recommended"]
    if rec["plan"] != cur["plan"]:
        lines.append(f"추천 요금제({rec['plan']}) 기본 제공량({_limits(rec)})과 비교하면 " + _against(forecast, rec) + ".")
    return lines + _stability_lines(doc, rec["plan"])


def all_captions(doc: dict) -> dict[str, list[str]]:
    return {"cumulative": cumulative(doc), "composition": composition(doc), "usage": usage(doc)}

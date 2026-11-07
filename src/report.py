"""리포트 검증과 숫자 채우기(REP-01~05), Slack 메시지 생성.

Claude는 리포트 초안에 숫자를 쓰지 않고 등록된 자리표시자(예: {current_monthly})만 쓴다.
이 모듈은 초안을 검증하고(fail-closed), 통과하면 시뮬레이터 결과로 자리표시자를 채운다.
- 등록되지 않은 키, 값이 없는 키, 자리표시자 바깥의 숫자, 한글로 쓴 금액, 필수 키 누락을 막는다(REP-01, 02).
- 인용 [근거: 문서ID#번호]은 이번 분석에서 실제로 검색된 조각이어야 하고, 대상 아닌 이벤트 조각을
  추천 근거로 쓰면 안 된다(REP-04). 추천 리포트는 "추천"과 "응대 안내"에 인용이 하나 이상 있어야 한다(REP-05).
- 채운 뒤에도 모든 숫자가 시뮬레이터 결과에서 나온 것인지 다시 확인한다(두 번째 방어선).
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "config" / "placeholders.toml"
FORMATS = {"won", "count", "gb", "months", "percent", "date", "discount", "text"}
REQUIRED_WHEN = {"always", "promotion", "saving", "never"}

PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_]+)\}")
CITATION = re.compile(r"\[근거:\s*([a-z0-9_]+#\d+)\s*\]")
# 1,234 / 1,234.5 / 1234 / 12.5. 앞이 글자·숫자·밑줄·점이면 다른 숫자의 일부(user_002, 1.5.3)로 보고 건너뛴다.
NUMBER = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
DIGIT = re.compile(r"\d")
LIST_NUMBER = re.compile(r"^(\s*#*\s*)\d+\.\s", re.MULTILINE)  # 줄 맨 앞의 목록·소제목 번호(1.)
ALIAS = re.compile(r"(?<![A-Za-z0-9])C\d+(?!\d)")  # 실행마다 붙는 고객 별칭(SEC-05)
KOREAN_AMOUNT = re.compile(r"[일이삼사오육칠팔구]?[십백천만억]+\s*원")
UNIT_AFTER_KEY = re.compile(r"\{[A-Za-z0-9_]+\}(원|회|GB|개월|%)")
SLACK_TEXT_LIMIT = 3000


class ReportRejected(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


# --- 등록 파일 ---

def load_registry(path: Path = REGISTRY_PATH) -> dict:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    keys = data.get("keys", {})
    for name, spec in keys.items():
        if spec.get("format") not in FORMATS:
            raise ValueError(f"{name}: format이 잘못되었습니다({spec.get('format')})")
        if spec.get("required_when", "never") not in REQUIRED_WHEN:
            raise ValueError(f"{name}: required_when이 잘못되었습니다")
        if "source" not in spec:
            raise ValueError(f"{name}: source가 없습니다")
    for group in data.get("required_any", []):
        for name in group:
            if name not in keys:
                raise ValueError(f"required_any의 {name}이 keys에 없습니다")
    return {"keys": keys, "required_any": data.get("required_any", [])}


# --- 시뮬레이터 결과 → 사실 목록 ---

def _stability_sentence(sim: dict, by_id: dict) -> str:
    st = sim["stability"]
    pct = st["change_pct"]
    key = (sim["recommendation"]["plan_id"], sim["recommendation"]["promo_id"])
    changed = {case: (st[case]["plan_id"], st[case]["promo_id"]) != key for case in ("if_down", "if_up")}
    if not any(changed.values()):
        return f"사용량이 {pct}% 줄거나 늘어도 같은 추천입니다"
    parts = []
    for case, verb in (("if_down", "줄면"), ("if_up", "늘면")):
        if changed[case]:
            parts.append(f"사용량이 {pct}% {verb} {by_id[st[case]['plan_id']]['plan_name']} 쪽이 더 유리할 수 있습니다")
    return " / ".join(parts)


def facts(sim: dict) -> dict:
    """자리표시자의 source 이름 → 값. 값이 없으면 None."""
    out = {"current.plan_name": sim["current_plan_name"], "horizon_months": 12}
    if sim["status"] != "ok":
        return out
    by_id = {o["plan_id"]: o for o in sim["options"]}
    current = next(o for o in sim["options"] if o["is_current"])
    rec = sim["recommendation"]
    rec_option = by_id[rec["plan_id"]]
    promo = rec_option["promotion"] if rec["promo_id"] else None
    forecast = sim["forecast"]
    skipped = {s["promo_id"]: s for o in sim["options"] for s in o["skipped_promotions"]}
    activity = sim.get("activity")
    season = sim.get("seasonality", {})
    upcoming = season.get("upcoming")
    out.update({
        "current.total": current["monthly"]["total"],
        "current.base": current["monthly"]["base_price"],
        "current.api_overage": current["monthly"]["api_overage"],
        "current.storage_overage": current["monthly"]["storage_overage"],
        "current.included_api_calls": current["included_api_calls"],
        "current.included_storage_gb": current["included_storage_gb"],
        "current.total_12m": rec["current_total_12m"],
        "forecast.api_calls": forecast["api_calls"],
        "forecast.storage_gb": forecast["storage_gb"],
        "forecast.months_used": forecast["months_used"],
        "forecast.method": forecast["method"],
        "rec.plan_name": rec["plan_name"],
        "rec.total": rec_option["monthly"]["total"],
        "rec.included_api_calls": rec_option["included_api_calls"],
        "rec.included_storage_gb": rec_option["included_storage_gb"],
        "rec.total_12m": rec["recommended_total_12m"],
        "promo.name": promo["promo_name"] if promo else None,
        "promo.discount_total": promo["discount_total"] if promo else None,
        "promo.discount": (promo["discount_type"], promo["discount_value"]) if promo else None,
        "promo.months": promo["months"] if promo else None,
        "promo.valid_to": promo["valid_to"] if promo else None,
        "promo.min_tenure_months": promo["min_tenure_months"] if promo else None,
        "skipped_promotions": " / ".join(f"{s['promo_name']}: {s['reason']}" for s in skipped.values()) or None,
        "saving.monthly": rec["monthly_saving"] if rec["saving_12m"] > 0 else None,
        "saving.total_12m": rec["saving_12m"] if rec["saving_12m"] > 0 else None,
        "saving.pct": rec["saving_pct"] if rec["saving_12m"] > 0 else None,
        "activity.change_pct": activity["change_pct"] if activity and activity["churn_risk"] else None,
        "stability.sentence": _stability_sentence(sim, by_id),
        "peak.months": None, "peak.deadline": None, "peak.bill_current": None, "peak.evidence": None,
    })
    if season.get("status") == "있음":
        labels = []
        for bundle in season["bundles"]:
            labels.append(f"{bundle[0]}월" if len(bundle) == 1 else f"{bundle[0]}~{bundle[-1]}월")
        first = season["evidence"][0]["occurrences"]
        out["peak.months"] = ", ".join(labels)
        out["peak.evidence"] = f"{first[0]['month']} 평소의 {first[0]['ratio']}배, {first[1]['month']} {first[1]['ratio']}배"
        if upcoming:
            out["peak.deadline"] = upcoming["deadline"]
            out["peak.bill_current"] = upcoming["current_bill"]
    return out


def format_value(value, fmt: str) -> str:
    if fmt == "won":
        return f"{value:,}원"
    if fmt == "count":
        return f"{value:,}회"
    if fmt == "gb":
        return f"{value:,g}GB"
    if fmt == "months":
        return f"{value}개월"
    if fmt == "percent":
        return f"{value}%"
    if fmt == "discount":
        kind, amount = value
        return f"{amount}%" if kind == "percent" else f"{amount:,}원"
    return str(value)  # date, text


def values(sim: dict, registry: dict) -> dict[str, str | None]:
    """키 → 채울 문자열. 값이 없으면 None."""
    known = facts(sim)
    out = {}
    for name, spec in registry["keys"].items():
        raw = known.get(spec["source"])
        out[name] = None if raw is None else format_value(raw, spec["format"])
    return out


# --- 검증 ---

def _sections(text: str) -> list[tuple[str, str]]:
    """(소제목, 본문) 목록. 소제목 앞의 글은 이름 없는 구역."""
    parts = re.split(r"^#+\s*(.+)$", text, flags=re.MULTILINE)
    sections = [("", parts[0])]
    sections += [(parts[i].strip(), parts[i + 1]) for i in range(1, len(parts), 2)]
    return sections


def _is_ineligible(item: dict) -> bool:
    """조각이 다루는 이벤트가 모두 '대상 아님'이면 추천 근거로 쓸 수 없다(REP-04)."""
    promos = item.get("promotions", [])
    return bool(promos) and all(p.get("status") == "대상 아님" for p in promos)


def check_draft(draft: str, sim: dict, retrieved: dict[str, dict], registry: dict | None = None) -> list[str]:
    """초안의 문제 목록. 빈 목록이면 통과. retrieved는 이번 분석에서 검색된 조각(ID → 검색 결과 항목)."""
    registry = registry or load_registry()
    problems = []
    filled = values(sim, registry)
    used = PLACEHOLDER.findall(draft)

    unknown = sorted({k for k in used if k not in registry["keys"]})
    if unknown:
        problems.append(f"등록되지 않은 자리표시자: {', '.join(unknown)}")
    empty = sorted({k for k in used if k in registry["keys"] and filled[k] is None})
    if empty:
        problems.append(f"이 고객에게는 값이 없는 자리표시자: {', '.join(empty)}")
    if UNIT_AFTER_KEY.search(draft):
        problems.append("자리표시자 뒤에 단위를 또 썼습니다(단위는 코드가 붙임)")

    bare = CITATION.sub("", PLACEHOLDER.sub("", draft))
    bare = ALIAS.sub("", LIST_NUMBER.sub(r"\1", bare))
    if DIGIT.search(bare):
        problems.append("자리표시자 바깥에 숫자가 있습니다: " + ", ".join(sorted(set(NUMBER.findall(bare)))[:5]))
    if KOREAN_AMOUNT.search(bare):
        problems.append("한글로 쓴 금액이 있습니다: " + ", ".join(KOREAN_AMOUNT.findall(bare)[:3]))

    if sim["status"] != "ok":
        if "기록" not in draft:
            problems.append("이용 기록이 없다는 안내가 없음")
        return problems

    rec = sim["recommendation"]
    for name, spec in registry["keys"].items():
        when = spec.get("required_when", "never")
        needed = when == "always" or (when == "promotion" and rec["promo_id"]) or \
            (when == "saving" and rec["saving_12m"] > 0)
        if needed and name not in used:
            problems.append(f"필수 자리표시자 없음: {{{name}}}")
    for group in registry["required_any"]:
        if not any(name in used for name in group):
            problems.append("근거 사용량 자리표시자 없음: " + " 또는 ".join(f"{{{n}}}" for n in group))

    cited_by_section = {}
    for title, body in _sections(draft):
        for chunk_id in CITATION.findall(body):
            cited_by_section.setdefault(title, []).append(chunk_id)
            if chunk_id not in retrieved:
                problems.append(f"이번 분석에서 검색하지 않은 조각 인용: {chunk_id}")
            elif "추천" in title and "응대" not in title and _is_ineligible(retrieved[chunk_id]):
                problems.append(f"고객이 받을 수 없는 이벤트의 조각을 추천 근거로 인용: {chunk_id}")
    for label, match in (("추천", lambda t: "추천" in t and "응대" not in t), ("응대 안내", lambda t: "응대" in t)):
        if not any(match(title) and ids for title, ids in cited_by_section.items()):
            problems.append(f"'{label}' 구역에 근거 인용이 없음(REP-05)")
    return problems


def fill(draft: str, sim: dict, registry: dict | None = None) -> str:
    registry = registry or load_registry()
    filled = values(sim, registry)
    return PLACEHOLDER.sub(lambda m: filled[m.group(1)], draft)


def _normalize(token: str) -> str:
    value = token.replace(",", "")
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value


def numbers_in(text: str) -> list[str]:
    return [_normalize(match.group()) for match in NUMBER.finditer(text)]


def allowed_numbers(sim: dict) -> set[str]:
    """채운 뒤 나와도 되는 숫자: 시뮬레이터 결과에 있는 숫자와 한 자리 목록 번호.

    채운 값이 아니라 시뮬레이터 결과 원본과 대조한다. 채우는 코드에 버그가 있어도 잡기 위해서다.
    """
    return {str(n) for n in range(0, 10)} | set(numbers_in(json.dumps(sim, ensure_ascii=False)))


def verify_and_fill(draft: str, sim: dict, retrieved: dict[str, dict], registry: dict | None = None) -> str:
    """검증을 통과하면 채운 리포트를, 아니면 ReportRejected를 낸다(fail-closed)."""
    registry = registry or load_registry()
    problems = check_draft(draft, sim, retrieved, registry)
    if problems:
        raise ReportRejected(problems)
    text = fill(draft, sim, registry)
    stray = sorted({n for n in numbers_in(CITATION.sub("", text)) if n not in allowed_numbers(sim)})
    if stray:
        raise ReportRejected([f"채운 뒤 시뮬레이터 결과에 없는 숫자: {', '.join(stray)}"])
    return text


# --- Slack ---

def slack_message(report_text: str, sim: dict, code: str) -> dict:
    """Slack Incoming Webhook에 보낼 Block Kit JSON. 보내는 일은 하지 않는다.

    고객은 가칭 코드로만 나타낸다. 이름과 고객 ID(user_xxx)는 넣지 않는다(SEC-12).
    """
    body = re.sub(r"\*\*(.+?)\*\*", r"*\1*", report_text)  # Slack mrkdwn은 굵게를 *로 쓴다.
    body = re.sub(r"^#+\s*(.+)$", r"*\1*", body, flags=re.MULTILINE)
    if len(body) > SLACK_TEXT_LIMIT:
        body = body[: SLACK_TEXT_LIMIT - 1] + "…"
    title = f"요금제 추천 리포트: 고객 {code}"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": title[:150]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
    ]
    rec = sim.get("recommendation")
    if rec:
        blocks.insert(1, {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*현재*\n{sim['current_plan_name']}"},
            {"type": "mrkdwn", "text": f"*추천*\n{rec['plan_name']}"},
            {"type": "mrkdwn", "text": f"*12개월 절감*\n{rec['saving_12m']:,}원"},
            {"type": "mrkdwn", "text": f"*이벤트*\n{rec['promo_id'] or '없음'}"},
        ]})
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": f"분석 기준일 {sim['as_of']} · 금액은 시뮬레이터 계산값"},
    ]})
    return {"text": title, "blocks": blocks}

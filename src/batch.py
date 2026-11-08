"""월간 선별(PRO-01~11). 코드가 지휘하고 AI를 쓰지 않는다(요구사항 결정 21).

실행: python src/batch.py                  # 기준일(BILLING_AS_OF, 기본 2026-11-01) 선별, 미리보기 저장
      python src/batch.py --announce-loyalty # 장기 이용 혜택 시작 공지(한 번만, PRO-10)

순서: 데이터 품질 점검(DQ) → 전원 계산(시뮬레이터) → 분류(고객 알림 / 담당자 연락) → 재알림 제한 →
      같은 고객의 알림 합치기 → 월 최대 수 → 미리보기·앱 저장소 기록·사내 요약.
알림 문구는 정해진 틀에 시뮬레이터 값을 채운다(PRO-03). 고객 알림은 미리보기로만 만든다(PRO-04).
사내 요약과 저장 기록에는 고객 이름과 ID 대신 가칭 코드만 쓴다(SEC-12).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import statistics
import sys
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import audit
import data_access
import data_quality
import history
import policy as policy_module
import settings
import simulator

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"

SAVING, PEAK, BUDGET, TIER, ANNOUNCE, STAFF = "절감 추천", "성수기 예고", "예산 안내", "단계 도달", "혜택 공지", "담당자 연락"
CUSTOMER_KINDS = (SAVING, PEAK, BUDGET, TIER)
PRIMARY_ORDER = (PEAK, BUDGET, SAVING, TIER)  # 합칠 때 대표 종류(PRO-09: 성수기 알림 하나로 합침)
SAVING_ACTIONS = {"upgrade", "downgrade", "keep_with_promotion"}
ADVICE_STABILITY = {"안정", "사용량이 늘면 바뀜"}  # PRO-08: 이때만 성수기 변경 권유
VALID_DAYS = 30  # CHG-07: 알림 생성 후 30일과 이벤트 신청 마감일 중 이른 날
OPT_OUT_TEXT = "수신을 원하지 않으면 알림 설정에서 '혜택·추천 알림 받지 않기'를 누르세요."
FIRST, REMINDER = "1차", "마감 전"


class SelectionStopped(Exception):
    """데이터 품질 점검 통과율이 기준보다 낮아 선별을 멈췄다(DQ-03)."""


# --- 날짜 ---

def next_run(as_of: date) -> date:
    return date(as_of.year + as_of.month // 12, as_of.month % 12 + 1, 1)


def quarter(day: date) -> tuple[int, int]:
    return day.year, (day.month - 1) // 3


def peak_dates(first_month: str, pol: policy_module.Policy) -> dict:
    """성수기 묶음 첫 달(YYYY-MM)의 신청 마감과 알림 날짜(명세 2장 "성수기 알림 일정")."""
    first_day = date.fromisoformat(first_month + "-01")
    deadline = first_day - timedelta(days=1)  # 그 전달 말일까지 신청해야 첫 달부터 적용
    alerts = pol.peak_alerts
    return {"first_day": first_day, "deadline": deadline,
            "first_notice": first_day - timedelta(days=alerts["first_notice_days_before"]),
            "reminder": deadline - timedelta(days=alerts["reminder_days_before_deadline"])}


# --- 시뮬레이터 결과에서 알림 재료 만들기 ---

def _option(sim: dict, plan_id: str) -> dict:
    return next(o for o in sim["options"] if o["plan_id"] == plan_id)


def _label(bundle: list[int]) -> str:
    return simulator.month_label(bundle)


def bundle_bills(sim: dict, bundle: list[int]) -> dict | None:
    """비교 기간 안에서 묶음이 시작하는 달부터의 청구액(공급가액)과 평소 대비 추가 비용(PRO-07)."""
    schedule, season = sim["schedule"], sim["seasonality"]
    months = schedule["months"]
    start = next((i for i, m in enumerate(months) if int(m[5:]) == bundle[0]), None)
    if start is None:
        return None
    span = list(range(start, min(start + len(bundle), len(months))))
    off_peak = [r["total"] for r in schedule["baseline"] if int(r["month"][5:]) not in season["peak_months"]]
    usual = statistics.median_low(off_peak) if off_peak else 0  # 평소 달 청구액(짝수 개면 작은 쪽, simulator와 같음)
    current = sum(schedule["baseline"][i]["total"] for i in span)
    return {"first_month": months[start], "months": [months[i] for i in span], "current_bill": current,
            "recommended_bill": sum(schedule["recommended"][i]["total"] for i in span),
            "usual_month": usual, "extra_cost": current - usual * len(span)}


@dataclass
class Part:
    kind: str
    text: str
    plan_id: str | None = None
    promo_id: str | None = None
    saving_12m: int = 0
    peak_months: str | None = None
    stage: str | None = None
    send_on: date | None = None
    valid_until: date | None = None
    tier: int | None = None


@dataclass
class Notice:
    user_id: str
    code: str
    parts: list[Part] = field(default_factory=list)

    @property
    def kind(self) -> str:
        kinds = {p.kind for p in self.parts}
        return next(k for k in PRIMARY_ORDER if k in kinds)

    @property
    def priority(self) -> int:
        """12개월 절감액(변경 권유가 없는 알림은 0)."""
        return max([p.saving_12m for p in self.parts if p.kind in (SAVING, PEAK)] or [0])

    @property
    def sort_key(self) -> tuple:
        """월 최대 수 안에서 고르는 순서: 마감이 있는 성수기 알림 → 12개월 절감액 순."""
        return (self.kind not in (PEAK, BUDGET), -self.priority, self.code)

    @property
    def counts_toward_limit(self) -> bool:
        """단계 도달 알림만 있는 경우는 혜택 안내라 월 최대 수에 세지 않는다(PRO-10 공지와 같은 성격)."""
        return any(p.kind != TIER for p in self.parts)

    @property
    def send_on(self) -> date:
        """합친 알림은 대표 알림의 날짜에 보낸다(성수기 1차 알림이면 그 날짜)."""
        return self.main_part().send_on

    def main_part(self) -> Part:
        return next(p for p in self.parts if p.kind == self.kind)


def _rec_phrase(sim: dict) -> str:
    rec = sim["recommendation"]
    promo = _option(sim, rec["plan_id"])["promotion"]
    if rec["action"] == "keep_with_promotion":
        return f"지금 요금제({rec['plan_name']})에서 이벤트 '{promo['promo_name']}'을 신청하면"
    return f"{rec['plan_name']} 요금제" + (f"로 바꾸고 이벤트 '{promo['promo_name']}'을 신청하면" if promo else "로 바꾸면")


def _valid_until(sim: dict, created: date, *limits: date) -> date:
    rec = sim["recommendation"]
    promo = _option(sim, rec["plan_id"])["promotion"] if rec and rec["promo_id"] else None
    candidates = [created + timedelta(days=VALID_DAYS), *limits]
    if promo:
        candidates.append(date.fromisoformat(promo["valid_to"]))
    return min(candidates)


def saving_part(sim: dict, pol: policy_module.Policy, as_of: date) -> Part | None:
    """절감 추천(PRO-01~03). 기준은 정책 파일 값이다."""
    rec = sim["recommendation"]
    sel = pol.selection
    if rec["action"] not in SAVING_ACTIONS or rec["saving_12m"] < sel["min_saving_12m"] \
            or rec["saving_pct"] < sel["min_saving_pct"] or (sel["exclude_low_data"] and rec["low_data"]):
        return None
    valid = _valid_until(sim, as_of)
    text = (f"[요금제 추천] 최근 사용량 기준으로 {_rec_phrase(sim)} 12개월 동안 {rec['saving_12m']:,}원"
            f"({rec['saving_pct']}%)을 아낄 수 있습니다(부가세 별도). 이 추천은 {valid.isoformat()}까지 유효합니다.")
    return Part(SAVING, text, rec["plan_id"], rec["promo_id"], rec["saving_12m"], send_on=as_of, valid_until=valid)


def peak_parts(sim: dict, pol: policy_module.Policy, as_of: date, sent_first: set[str],
               blocked_reminder: bool, sent_reminder: frozenset[str] = frozenset()) -> list[Part]:
    """성수기 알림(PRO-07~09). sent_first·sent_reminder는 이 고객에게 이미 보낸 묶음 이름(묶음마다 두 번만)."""
    season = sim.get("seasonality") or {}
    if season.get("status") != "있음" or sim["status"] != "ok":
        return []
    rec, stability = sim["recommendation"], sim["stability"]["status"]
    advice = rec["plan_id"] != sim["current_plan_id"] and stability in ADVICE_STABILITY
    window_end = next_run(as_of)
    parts = []
    for bundle in season["bundles"]:
        bills = bundle_bills(sim, bundle)
        if not bills or bills["extra_cost"] < pol.peak_alerts["min_extra_cost"]:
            continue
        label = _label(bundle)
        when = peak_dates(bills["first_month"], pol)
        if as_of > when["deadline"]:
            continue
        if label not in sent_first and (as_of <= when["first_notice"] < window_end or when["first_notice"] < as_of):
            stage, send_on = FIRST, max(when["first_notice"], as_of)
        elif label in sent_first and label not in sent_reminder and as_of <= when["reminder"] < window_end \
                and not blocked_reminder:
            stage, send_on = REMINDER, when["reminder"]
        else:
            continue
        evidence = next(e for e in season["evidence"] if e["calendar_month"] == bundle[0])["occurrences"]
        head = (f"[성수기 {'예고' if advice else '예산 안내'}] {label}에는 지난 두 해 모두 사용량이 평소보다 많았습니다"
                f"({evidence[0]['month']} 평소의 {evidence[0]['ratio']}배, {evidence[1]['month']} {evidence[1]['ratio']}배). "
                f"지금 요금제를 유지하면 이 기간 예상 청구액은 {bills['current_bill']:,}원입니다(부가세 별도).")
        if advice:
            body = (f" 12개월로 보면 지금 요금제 유지 {rec['current_total_12m']:,}원, {_rec_phrase(sim)} "
                    f"{rec['recommended_total_12m']:,}원입니다. {label}부터 적용하려면 {when['deadline'].isoformat()}까지 신청하세요.")
        else:
            body = " 요금제를 바꾸지 않아도 되며, 예산 편성에 참고하시도록 미리 알려 드립니다."
        valid = _valid_until(sim, as_of, when["deadline"])
        parts.append(Part(PEAK if advice else BUDGET, head + body, rec["plan_id"], rec["promo_id"],
                          rec["saving_12m"] if advice else 0, label, stage, send_on, valid))
    return parts


def tier_part(sim: dict, as_of: date, sent_tiers: set[int]) -> Part | None:
    """단계 도달 알림(PRO-11): 다음 달부터 새 단계가 적용되는 고객. 단계마다 한 번."""
    nxt = (sim.get("loyalty") or {}).get("next")
    if not nxt or nxt["in_months"] != 1 or nxt["tier"] in sent_tiers:
        return None
    text = (f"[장기 이용 혜택] {nxt['month']}부터 {nxt['tier_name']}가 적용되어 모든 요금제의 기본 제공량이 "
            f"{nxt['bonus_pct']}% 늘어납니다. 따로 신청하지 않아도 됩니다.")
    return Part(TIER, text, send_on=as_of, valid_until=as_of + timedelta(days=VALID_DAYS), tier=nxt["tier"])


def announcement_text(sim: dict) -> str:
    """혜택 시작 공지(PRO-10) 한 고객분."""
    loy = sim["loyalty"]
    now = f"{loy['tier_name']}(기본 제공량 {loy['bonus_pct']}% 추가)" if loy["tier"] else "아직 단계가 없습니다"
    nxt = loy["next"]
    after = (f" 다음 단계는 {nxt['month']}부터 {nxt['tier_name']}({nxt['bonus_pct']}% 추가)입니다." if nxt
             else " 이미 최고 단계입니다.")
    return (f"[장기 이용 혜택 시작] 오래 이용해 주신 고객께 모든 요금제의 기본 제공량을 늘려 드립니다. "
            f"현재 단계: {now}.{after} {OPT_OUT_TEXT}")


# --- 앱 저장소에서 지난 알림·거절·접수·수신거부 읽기 ---

@dataclass
class Past:
    notifications: list[dict]
    rejected: list[dict]
    open_changes: set[str]
    opt_outs: set[str]


def load_past(app: sqlite3.Connection) -> Past:
    notes = [dict(r) for r in app.execute("SELECT * FROM notifications")]
    rejected = [dict(r) for r in app.execute(
        "SELECT code, plan_id, promo_id, outcome_at FROM recommendation_history WHERE outcome = '거절'")]
    open_changes = {r[0] for r in app.execute("SELECT code FROM change_requests WHERE status = '접수'")}
    opt_outs = {r[0] for r in app.execute("SELECT code FROM opt_outs")}
    return Past(notes, rejected, open_changes, opt_outs)


def _recently(past: Past, code: str, kinds: tuple[str, ...], as_of: date, days: int, **match) -> bool:
    since = as_of - timedelta(days=days)
    for n in past.notifications:
        if n["code"] != code or n["kind"] not in kinds or date.fromisoformat(n["created_at"][:10]) <= since:
            continue
        if all(n.get(k) == v for k, v in match.items()):
            return True
    return False


def _rejected_this_quarter(past: Past, code: str, plan_id: str, promo_id: str | None, as_of: date) -> bool:
    return any(r["code"] == code and r["plan_id"] == plan_id and r["promo_id"] == promo_id and r["outcome_at"]
               and quarter(date.fromisoformat(r["outcome_at"][:10])) == quarter(as_of) for r in past.rejected)


# --- 선별 ---

def select(conn: sqlite3.Connection, pol: policy_module.Policy, as_of: date, past: Past,
           quality: dict) -> dict:
    """선별 결과(저장 전). 고객 ID는 이 함수 안에서만 쓰고, 저장·요약은 가칭 코드로 한다."""
    codes = data_access.code_map(conn)
    renotify = pol.selection["renotify_days"]
    notices: list[Notice] = []
    staff: list[dict] = []
    skipped: dict[str, list[str]] = {}

    def skip(code: str, reason: str) -> None:
        skipped.setdefault(reason, []).append(code)

    for user_id in data_access.customer_ids(conn):
        code = codes[user_id]
        if user_id in quality["excluded"]:
            skip(code, "데이터 품질 위반")
            continue
        sim = simulator.simulate(conn, user_id, as_of)
        churn = bool(sim.get("activity") and sim["activity"]["churn_risk"])
        if sim["status"] == "no_usage" or churn:
            reason = "첫 사용 지원" if sim["status"] == "no_usage" else "이탈 위험"
            if _recently(past, code, (STAFF,), as_of, renotify):
                skip(code, "담당자 연락 90일 안 재분류")
            else:
                staff.append({"code": code, "reason": reason})
            continue
        if code in past.opt_outs:
            skip(code, "수신거부")
            continue
        has_open_change = code in past.open_changes
        notice = Notice(user_id, code)

        part = None if has_open_change else saving_part(sim, pol, as_of)
        if part:
            if _recently(past, code, (SAVING, PEAK), as_of, renotify, plan_id=part.plan_id, promo_id=part.promo_id):
                skip(code, "같은 추천 90일 안 재알림")
                part = None
            elif _rejected_this_quarter(past, code, part.plan_id, part.promo_id, as_of):
                skip(code, "거절한 추천(이번 분기)")
                part = None
        if part:
            notice.parts.append(part)

        sent = {stage: {n["peak_months"] for n in past.notifications
                        if n["code"] == code and n["kind"] in (PEAK, BUDGET) and n["stage"] == stage}
                for stage in (FIRST, REMINDER)}
        for peak in peak_parts(sim, pol, as_of, sent[FIRST], blocked_reminder=has_open_change,
                               sent_reminder=frozenset(sent[REMINDER])):
            if peak.stage == FIRST and has_open_change and peak.kind == PEAK:
                continue  # 이미 변경을 신청한 고객에게 변경 권유는 보내지 않는다
            notice.parts.append(peak)

        sent_tiers = {n["tier"] for n in past.notifications if n["code"] == code and n["kind"] == TIER}
        tier = tier_part(sim, as_of, sent_tiers)
        if tier:
            notice.parts.append(tier)
        if notice.parts:
            notices.append(notice)

    limited = sorted((n for n in notices if n.counts_toward_limit), key=lambda n: n.sort_key)
    limit = pol.selection["max_alerts_per_month"]
    for over in limited[limit:]:
        skip(over.code, "월 최대 수 초과(다음 달로)")
    chosen = limited[:limit] + sorted((n for n in notices if not n.counts_toward_limit), key=lambda n: n.code)
    return {"as_of": as_of.isoformat(), "notices": chosen, "staff": staff, "skipped": skipped,
            "quality": quality}


# --- 저장과 사내 요약 ---

def new_id(prefix: str, as_of: date) -> str:
    return f"{prefix}-{as_of:%Y%m}-{secrets.token_hex(3).upper()}"


def _notice_rows(result: dict, run_id: str, created: str) -> list[tuple]:
    rows = []
    for n in result["notices"]:
        main = n.main_part()
        valid = min(p.valid_until for p in n.parts)
        peak = next((p for p in n.parts if p.kind in (PEAK, BUDGET)), None)
        tier = next((p for p in n.parts if p.kind == TIER), None)
        rows.append((new_id("NT", date.fromisoformat(result["as_of"])), n.code, created, valid.isoformat(),
                     main.plan_id, main.promo_id, n.priority or None, "미리보기", n.kind,
                     ",".join(p.kind for p in n.parts), peak.peak_months if peak else None,
                     peak.stage if peak else None, n.send_on.isoformat(),
                     tier.tier if tier else None, run_id))
    for s in result["staff"]:
        rows.append((new_id("ST", date.fromisoformat(result["as_of"])), s["code"], created, created[:10],
                     None, None, None, "사내", STAFF, s["reason"], None, None, created[:10], None, run_id))
    return rows


def preview(result: dict) -> list[dict]:
    """고객 알림 미리보기(PRO-03, 04). 같은 고객의 알림은 하나로 합친다(PRO-09, 11)."""
    out = []
    for n in result["notices"]:
        kinds = {p.kind for p in n.parts}
        # 성수기 변경 권유에는 12개월 비교가 들어 있어 절감 추천 문장을 따로 넣지 않는다(PRO-09 합치기)
        texts = [p.text for p in n.parts if not (p.kind == SAVING and PEAK in kinds)]
        out.append({"code": n.code, "kind": n.kind, "parts": [p.kind for p in n.parts],
                    "send_on": n.send_on.isoformat(),
                    "valid_until": min(p.valid_until for p in n.parts).isoformat(),
                    "text": "\n".join(texts) + f"\n자세히 보기: 고객 화면 > 알림\n{OPT_OUT_TEXT}"})
    return out


def staff_summary(result: dict) -> dict:
    """사내 영업 채널 요약(PRO-06). 가칭 코드와 추천 요약만, 이름·ID 없음(SEC-12)."""
    q = result["quality"]
    lines = [f"데이터 품질: 통과 {q['passed']}/{q['total']}명({q['pass_rate']:.1%}), "
             f"제외 {len(q['excluded'])}명, 확인 필요 {len(q['flagged'])}명"]
    for n in result["notices"]:
        main = n.main_part()
        amount = f", 12개월 절감 {n.priority:,}원" if n.priority else ""
        lines.append(f"- {n.code} · {' + '.join(p.kind for p in n.parts)}{amount}"
                     + (f" · {main.peak_months}" if main.peak_months else ""))
    for s in result["staff"]:
        lines.append(f"- {s['code']} · 담당자 연락({s['reason']})")
    for reason, codes in sorted(result["skipped"].items()):
        lines.append(f"제외 - {reason}: {len(codes)}명")
    title = (f"월간 선별 {result['as_of']}: 고객 알림 {len(result['notices'])}건, "
             f"담당자 연락 {len(result['staff'])}건")
    text = "\n".join(lines)
    return {"text": title, "blocks": [
        {"type": "header", "text": {"type": "plain_text", "text": title}},
        {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "금액은 시뮬레이터 계산값(부가세 별도), 고객 알림은 미리보기"}]},
    ]}


def stopped_summary(quality: dict, as_of: date) -> dict:
    title = f"월간 선별 {as_of.isoformat()}: 데이터 품질 점검에서 멈춤"
    text = (f"통과 {quality['passed']}/{quality['total']}명({quality['pass_rate']:.1%}), 기준 "
            f"{float(data_quality.MIN_PASS_RATE):.0%}. 원천 데이터를 확인해 주세요. 항목별: "
            + ", ".join(f"{k} {v}" for k, v in quality["counts"].items()))
    return {"text": title, "blocks": [{"type": "header", "text": {"type": "plain_text", "text": title}},
                                      {"type": "section", "text": {"type": "mrkdwn", "text": text}}]}


def run(conn: sqlite3.Connection, pol: policy_module.Policy, as_of: date | None = None, *,
        app_db: Path = history.APP_DB_PATH, output_dir: Path = OUTPUT_DIR, audit_path: Path = audit.AUDIT_PATH,
        now: datetime | None = None, slack_sender=None) -> dict:
    """선별 한 번. 결과 파일 경로와 요약을 돌려준다. 품질 점검에서 멈추면 SelectionStopped."""
    as_of = as_of or settings.as_of()
    now = now or datetime.now(timezone.utc)
    run_id = f"SEL-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
    codes = data_access.code_map(conn)
    quality = data_quality.check(conn, as_of)
    out_dir = output_dir / "selection"
    with closing(history.connect(app_db)) as app:
        data_quality.save(app, run_id, quality, codes)
        audit.record("quality_check", audit_path, run_id=run_id, pass_rate=quality["pass_rate"],
                     excluded=len(quality["excluded"]), flagged=len(quality["flagged"]), stopped=quality["stopped"])
        if quality["stopped"]:
            message = stopped_summary(quality, as_of)
            _write(out_dir / f"{run_id}_summary.json", message)
            _send(slack_sender, message, run_id)
            raise SelectionStopped(message["blocks"][1]["text"]["text"])
        result = select(conn, pol, as_of, load_past(app), quality)
        created = now.isoformat(timespec="seconds")
        rows = _notice_rows(result, run_id, created)
        app.executemany("INSERT INTO notifications (notification_id, code, created_at, valid_until, plan_id, promo_id,"
                        " saving_12m, status, kind, parts, peak_months, stage, send_on, tier, run_id)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        app.commit()
    summary = staff_summary(result)
    previews = preview(result)
    _write(out_dir / f"{run_id}_preview.json", {"run_id": run_id, "as_of": as_of.isoformat(), "notices": previews})
    _write(out_dir / f"{run_id}_summary.json", summary)
    delivery = _send(slack_sender, summary, run_id)
    audit.record("selection", audit_path, run_id=run_id, customer_alerts=len(result["notices"]),
                 staff_contacts=len(result["staff"]), skipped={k: len(v) for k, v in result["skipped"].items()},
                 slack=delivery)
    return {"run_id": run_id, "result": result, "previews": previews, "summary": summary, "slack": delivery}


def announce_loyalty(conn: sqlite3.Connection, as_of: date | None = None, *, app_db: Path = history.APP_DB_PATH,
                     output_dir: Path = OUTPUT_DIR, audit_path: Path = audit.AUDIT_PATH,
                     now: datetime | None = None) -> dict:
    """장기 이용 혜택 시작 공지(PRO-10). 전체 고객에게 한 번만. 월 최대 수에 세지 않는다."""
    as_of = as_of or settings.as_of()
    now = now or datetime.now(timezone.utc)
    run_id = f"ANN-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
    codes = data_access.code_map(conn)
    with closing(history.connect(app_db)) as app:
        if app.execute("SELECT COUNT(*) FROM notifications WHERE kind = ?", (ANNOUNCE,)).fetchone()[0]:
            raise ValueError("장기 이용 혜택 공지는 이미 만들었습니다(PRO-10: 한 번만).")
        opted = {r[0] for r in app.execute("SELECT code FROM opt_outs")}
        created = now.isoformat(timespec="seconds")
        previews, rows = [], []
        for user_id in data_access.customer_ids(conn):
            code = codes[user_id]
            if code in opted:
                continue
            sim = simulator.simulate(conn, user_id, as_of)
            previews.append({"code": code, "kind": ANNOUNCE, "text": announcement_text(sim)})
            rows.append((new_id("AN", as_of), code, created, (as_of + timedelta(days=VALID_DAYS)).isoformat(),
                         None, None, None, "미리보기", ANNOUNCE, ANNOUNCE, None, None, as_of.isoformat(),
                         sim["loyalty"]["tier"], run_id))
        app.executemany("INSERT INTO notifications (notification_id, code, created_at, valid_until, plan_id, promo_id,"
                        " saving_12m, status, kind, parts, peak_months, stage, send_on, tier, run_id)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        app.commit()
    path = output_dir / "selection" / f"{run_id}_announcement.json"
    _write(path, {"run_id": run_id, "as_of": as_of.isoformat(), "notices": previews})
    audit.record("loyalty_announcement", audit_path, run_id=run_id, count=len(previews))
    return {"run_id": run_id, "count": len(previews), "path": path}


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _send(sender, message: dict, run_id: str) -> str:
    """NTF-02: 월간 선별 결과는 주소가 있으면 자동으로 보낸다. sender가 없으면 JSON만 저장한다."""
    if sender is None:
        return "주소 없음(JSON만 저장)"
    digest = hashlib.sha256(json.dumps(message, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return sender(message, digest, run_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="월간 선별(가상 데이터). 고객 알림은 미리보기로만 만든다.")
    parser.add_argument("--announce-loyalty", action="store_true", help="장기 이용 혜택 시작 공지를 만든다(한 번만)")
    args = parser.parse_args(argv)
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    pol = policy_module.load()
    policy_module.record_start(pol, audit.AUDIT_PATH)
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip() or None
    sender = None
    if webhook:
        import agent

        def sender(message, digest, run_id):
            return agent.send_slack(message, digest, run_id=run_id, code="월간선별", webhook_url=webhook)
    with closing(data_access.connect()) as conn:
        if args.announce_loyalty:
            done = announce_loyalty(conn)
            print(f"혜택 공지 미리보기 {done['count']}건: {done['path']}")
            return 0
        try:
            done = run(conn, pol, slack_sender=sender)
        except SelectionStopped as stopped:
            print(f"선별을 멈췄습니다: {stopped}", file=sys.stderr)
            return 1
    print(done["summary"]["text"])
    print(done["summary"]["blocks"][1]["text"]["text"])
    print(f"Slack: {done['slack']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

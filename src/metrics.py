"""운영 지표(OPS-02). 담당자 화면에 월별로 보여준다.

값은 앱 저장소(추천 이력·알림·변경 접수·상담 접수·품질 점검), 리포트 실행 기록, 감사 기록에서 센다.
고객 이름은 읽지 않는다. 비율의 분모가 0이면 None(화면에서 "—")이다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

ACCEPTED = ("수락", "셀프 수락")
# 예상 비용 계산용 가정 단가(미국 달러, 백만 토큰당 입력·출력). 공개 단가를 확인하면 고친다.
PRICE_USD_PER_MTOK = {"claude-sonnet-5-5": (3.0, 15.0)}


def ratio(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def load_run_logs(runs_dir: Path) -> list[dict]:
    """리포트 실행 기록(AGT-04). 실행 ID 앞 8자리가 날짜(YYYYMMDD)다."""
    if not runs_dir.exists():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(runs_dir.glob("*.json"))]


def _run_month(run_id: str) -> str:
    return f"{run_id[:4]}-{run_id[4:6]}"


def _count(app: sqlite3.Connection, sql: str, *args) -> int:
    return app.execute(sql, args).fetchone()[0]


def _hours(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 3600


def cost_usd(model: str | None, input_tokens: int, output_tokens: int) -> float | None:
    price = PRICE_USD_PER_MTOK.get(model or "")
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


def compute(app: sqlite3.Connection, audit_entries: list[dict], run_logs: list[dict], month: str) -> dict:
    """한 달(YYYY-MM)의 운영 지표."""
    like = f"{month}%"
    notices = _count(app, "SELECT COUNT(*) FROM notifications WHERE created_at LIKE ?", like)
    accepted = _count(app, "SELECT COUNT(*) FROM recommendation_history WHERE outcome IN (?, ?) AND outcome_at LIKE ?",
                      *ACCEPTED, like)
    changes = _count(app, "SELECT COUNT(*) FROM change_requests WHERE requested_at LIKE ?", like)
    cancelled = _count(app, "SELECT COUNT(*) FROM change_requests WHERE requested_at LIKE ? AND status = '취소'", like)

    tickets = [dict(r) for r in app.execute("SELECT reason, created_at, answered_at FROM tickets WHERE created_at LIKE ?",
                                            (like,))]
    month_audit = [e for e in audit_entries if str(e.get("at", "")).startswith(month)]
    chats = sum(1 for e in month_audit if e.get("event") == "chat")
    asked_human = sum(1 for t in tickets if t["reason"] == "고객 요청")  # '담당자 연결' 버튼은 상담 기록(chat)을 남기지 않는다
    questions = chats + asked_human
    by_reason: dict[str, int] = {}
    for t in tickets:
        by_reason[t["reason"]] = by_reason.get(t["reason"], 0) + 1
    answered = [_hours(t["created_at"], t["answered_at"]) for t in tickets if t["answered_at"]]

    runs = [r for r in run_logs if _run_month(r.get("run_id", "")) == month]
    checked = [r for r in runs if r.get("validation")]
    first_failed = sum(1 for r in checked if r["validation"][0].get("kinds"))
    seconds = [sum(s["seconds"] for s in r.get("steps", [])) for r in runs if r.get("status") == "완료"]

    quality = [r[0] for r in app.execute("SELECT pass_rate FROM quality_checks WHERE checked_at LIKE ? ORDER BY checked_at",
                                         (like,))]

    sent = [e for e in month_audit if e.get("event") == "external_send" and e.get("result") == "통과"]
    input_tokens = sum(e.get("input_tokens") or 0 for e in sent)
    output_tokens = sum(e.get("output_tokens") or 0 for e in sent)
    costs = [cost_usd(e.get("model"), e.get("input_tokens") or 0, e.get("output_tokens") or 0) for e in sent]

    return {
        "month": month,
        "notices": notices,
        "acceptance_rate": ratio(accepted, notices),
        "accepted": accepted,
        "self_change_cancel_rate": ratio(cancelled, changes),
        "self_changes": changes,
        "questions": questions,
        "escalation_rate": ratio(len(tickets), questions),
        "escalation_by_reason": {reason: ratio(n, questions) for reason, n in sorted(by_reason.items())},
        "reports": len(runs),
        "first_try_fail_rate": ratio(first_failed, len(checked)),
        "quality_pass_rate": quality[-1] if quality else None,
        "report_seconds_avg": sum(seconds) / len(seconds) if seconds else None,
        "ticket_answer_hours_avg": sum(answered) / len(answered) if answered else None,
        "api_calls": len(sent),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": None if any(c is None for c in costs) else round(sum(costs), 4),
    }

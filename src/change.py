"""셀프 요금제 변경(CHG-01~07). AI는 쓰지 않는다.

흐름: prepare(확인 화면 데이터) → submit(결제 권한자의 접수) → cancel(적용일 전 취소) / apply_due(적용).
확인 화면의 금액은 서류 모듈(documents.build_data)과 같은 계산에서 나온다. 그래서 검토서·확인 화면·변경
확인서의 숫자가 다를 수 없다. 접수할 때 한 번 더 계산해서, 확인 화면을 띄운 뒤 값이 바뀌었으면 접수하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import audit
import documents
import history
import simulator

ROLE_BILLING_ADMIN = "결제 권한자"
ROLE_MEMBER = "일반 사용자"
ROLES = (ROLE_BILLING_ADMIN, ROLE_MEMBER)

STATUS_OPEN, STATUS_CANCELLED, STATUS_APPLIED = "접수", "취소", "적용"
ACTION_LABELS = {
    "upgrade": "상위 요금제로 변경",
    "downgrade": "하위 요금제로 변경",
    "keep_with_promotion": "요금제 유지 + 이벤트 신청",
    "keep": "변경 없음",
}
INVOICE_CHECKS = [
    "세금계산서에 적힐 사업자 정보(상호, 사업자등록번호)가 맞는지 확인했습니다.",
    "세금계산서를 받을 이메일 주소가 맞는지 확인했습니다.",
]
DOWNGRADE_NOTICE = "하위 요금제는 기본 제공량이 줄어듭니다. 사용량이 늘면 초과 요금이 생길 수 있습니다."


class ChangeError(Exception):
    """변경을 접수하거나 취소할 수 없는 경우. 메시지는 고객 화면에 그대로 보여줄 수 있게 쓴다."""


class QuoteChanged(ChangeError):
    """확인 화면을 띄운 뒤 다시 계산한 값이 달라졌다(CHG-02). 새 값으로 다시 확인받아야 한다."""

    def __init__(self, fresh: dict):
        super().__init__("확인하신 뒤 금액이나 추천이 바뀌었습니다. 새 금액을 확인해 주세요.")
        self.fresh = fresh


# --- 확인 화면 데이터 ---

def _runs(monthly: list[dict]) -> list[dict]:
    """금액이 같은 연속 구간으로 묶는다(확인 화면은 12줄 대신 구간으로 보여준다)."""
    runs: list[dict] = []
    for m in monthly:
        amount = m["recommended"]
        if runs and runs[-1]["supply"] == amount["supply"]:
            runs[-1]["last"] = m["month"]
        else:
            runs.append({"first": m["month"], "last": m["month"], **amount})
    return runs


def _quote_hash(quote: dict) -> str:
    keys = ("code", "action", "from_plan", "to_plan", "promotion", "effective_date", "current_monthly",
            "monthly", "after_promotion", "saving_12m", "saving_pct")
    body = {key: quote[key] for key in keys}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _check_notification(notification: dict | None, quote: dict, today: date) -> dict:
    """알림 때 값과 지금 값 비교(CHG-02), 알림 유효기간(CHG-07)."""
    if not notification:
        return {"status": "알림 없음", "differences": []}
    if today > date.fromisoformat(notification["valid_until"]):
        return {"status": "만료", "differences": [],
                "message": f"알림의 추천은 {notification['valid_until']}까지 유효했습니다. 오늘 기준으로 다시 계산한 값입니다."}
    differences = []
    if notification["plan_id"] != quote["to_plan"]["plan_id"]:
        differences.append("추천 요금제")
    if notification.get("promo_id") != (quote["promotion"] or {}).get("promo_id"):
        differences.append("이벤트")
    if notification.get("saving_12m") != quote["saving_12m"]["supply"]:
        differences.append("12개월 절감액")
    if differences:
        return {"status": "달라짐", "differences": differences,
                "message": "알림을 보낸 뒤 이용 기록이 바뀌었습니다. 달라진 항목: " + ", ".join(differences) + ". 새 값을 확인해 주세요."}
    return {"status": "유효", "differences": []}


def prepare(conn: sqlite3.Connection, user_id: str, today: date, notification: dict | None = None) -> dict:
    """확인 화면 데이터(CHG-03). 매번 최신 데이터로 다시 계산한다(CHG-02)."""
    sim = simulator.simulate(conn, user_id)
    if sim["status"] != "ok":
        raise ChangeError("이용 기록이 없어 요금제를 비교할 수 없습니다.")
    data = documents.build_data(conn, user_id)
    rec = sim["recommendation"]
    effective = date.fromisoformat(sim["schedule"]["months"][0] + "-01")  # 다음 청구 주기 첫날
    if today >= effective:
        raise ChangeError("분석 기준일이 지난 계산입니다. 최신 청구 기준으로 다시 분석한 뒤 변경할 수 있습니다.")
    plans = {p.plan_id: p for p in simulator.load_plans(conn)}
    baseline, _recommended, saving = data["comparison"]
    quote = {
        "code": data["customer"]["code"],
        "action": rec["action"],
        "action_label": ACTION_LABELS[rec["action"]],
        "changeable": rec["action"] != "keep",
        "from_plan": {"plan_id": sim["current_plan_id"], "name": sim["current_plan_name"]},
        "to_plan": {"plan_id": rec["plan_id"], "name": plans[rec["plan_id"]].plan_name},
        "promotion": data["promotion"],
        "effective_date": effective.isoformat(),
        "cancel_deadline": (effective - timedelta(days=1)).isoformat(),  # 적용일 전날까지 취소(CHG-05)
        "current_monthly": data["monthly"][0]["baseline"],
        "monthly": _runs(data["monthly"]),
        "after_promotion": data["after_promotion_monthly"] if data["promotion"] else None,
        "saving_12m": {key: saving[key] for key in ("supply", "tax", "total")},
        "saving_pct": data["saving_pct"],
        "saving_monthly": rec["monthly_saving"],
        "stability": sim["stability"]["status"],
        "invoice_checks": INVOICE_CHECKS,
        "notices": [DOWNGRADE_NOTICE] if rec["action"] == "downgrade" else [],
        "basis": data["basis"],
    }
    if not quote["changeable"]:
        quote["notices"].append("지금 요금제가 가장 유리해서 바꿀 것이 없습니다.")
    quote["quote_sha256"] = _quote_hash(quote)
    check = _check_notification(notification, quote, today)
    quote["notification_check"] = check
    quote["needs_reconfirm"] = check["status"] in ("만료", "달라짐")
    return quote


# --- 접수·취소·적용 ---

def _new_request_id(today: date) -> str:
    # 일련번호를 쓰지 않는다: 고객이 보는 번호에서 하루 접수 건수가 드러나지 않게 한다.
    return f"CR-{today:%Y%m%d}-{secrets.token_hex(3).upper()}"


def _open_request(app: sqlite3.Connection, code: str) -> dict | None:
    row = app.execute("SELECT * FROM change_requests WHERE code = ? AND status = ?", (code, STATUS_OPEN)).fetchone()
    return dict(row) if row else None


def sales_message(request: dict, event: str) -> dict:
    """담당 영업에게 보낼 사내 채널 메시지(CHG-06). 이름 대신 가칭 코드만 쓴다(SEC-12). 전송은 NTF 규칙을 따른다."""
    title = {"requested": "셀프 변경 접수", "cancelled": "셀프 변경 취소"}[event]
    promo = f" + {request['promo_id']}" if request.get("promo_id") else ""
    text = (f"[{title}] 고객 {request['code']} · {request['from_plan']} → {request['to_plan']}{promo} · "
            f"적용일 {request['effective_date']} · 12개월 절감 {request['recalculated_saving_12m']:,}원(부가세 별도) · "
            f"접수 번호 {request['request_id']}")
    return {"text": text}


def submit(conn: sqlite3.Connection, user_id: str, role: str, quote_sha256: str, invoice_confirmed: bool,
           today: date, app_db: Path = history.APP_DB_PATH, audit_path: Path = audit.AUDIT_PATH,
           request_id: str | None = None) -> dict:
    """변경 접수(CHG-01, 04, 06). 결제 권한자가 확인 화면에서 누를 때만 기록된다."""
    code = documents._identity(conn, user_id)["code"]
    if role != ROLE_BILLING_ADMIN:
        audit.record("change_denied", audit_path, code=code, role=role)
        raise PermissionError("요금제 변경은 결제 권한자만 할 수 있습니다. 결제 권한자에게 요청해 주세요.")
    if not invoice_confirmed:
        raise ChangeError("세금계산서 발행 정보를 확인해 주세요.")
    fresh = prepare(conn, user_id, today)
    if not fresh["changeable"]:
        raise ChangeError("지금 요금제가 가장 유리해서 바꿀 것이 없습니다.")
    if fresh["quote_sha256"] != quote_sha256:
        raise QuoteChanged(fresh)

    with closing(history.connect(app_db)) as app:
        existing = _open_request(app, code)
        if existing:
            if existing["quote_sha256"] == quote_sha256:
                return {"request": existing, "duplicate": True, "sales_message": None}
            raise ChangeError(f"이미 접수된 변경({existing['request_id']})이 있습니다. 취소한 뒤 다시 신청해 주세요.")
        request = {
            "request_id": request_id or _new_request_id(today),
            "code": code,
            "requested_at": today.isoformat(),
            "requested_by_role": role,
            "from_plan": fresh["from_plan"]["plan_id"],
            "to_plan": fresh["to_plan"]["plan_id"],
            "promo_id": (fresh["promotion"] or {}).get("promo_id"),
            "effective_date": fresh["effective_date"],
            "cancel_deadline": fresh["cancel_deadline"],
            "status": STATUS_OPEN,
            "recalculated_saving_12m": fresh["saving_12m"]["supply"],
            "quote_sha256": quote_sha256,
            "cancelled_at": None,
            "applied_at": None,
        }
        try:
            app.execute(f"INSERT INTO change_requests ({', '.join(request)}) VALUES ({', '.join('?' * len(request))})",
                        tuple(request.values()))
        except sqlite3.IntegrityError:  # 같은 순간에 두 번 눌러 먼저 들어간 접수가 있음
            app.rollback()
            existing = _open_request(app, code)
            return {"request": existing, "duplicate": True, "sales_message": None}
        app.execute(
            "INSERT INTO recommendation_history (run_id, code, analyzed_at, action, plan_id, promo_id, saving_monthly,"
            " saving_12m, stability, model_id, outcome, outcome_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (f"self-{request['request_id']}", code, today.isoformat(), fresh["action"], request["to_plan"],
             request["promo_id"], fresh["saving_monthly"], request["recalculated_saving_12m"], fresh["stability"],
             "셀프 수락", today.isoformat()),
        )
        app.commit()
    audit.record("change_requested", audit_path, code=code, request_id=request["request_id"], role=role,
                 from_plan=request["from_plan"], to_plan=request["to_plan"], promo_id=request["promo_id"],
                 effective_date=request["effective_date"], quote_sha256=quote_sha256)
    return {"request": request, "duplicate": False, "sales_message": sales_message(request, "requested")}


def cancel(conn: sqlite3.Connection, user_id: str, request_id: str, role: str, today: date,
           app_db: Path = history.APP_DB_PATH, audit_path: Path = audit.AUDIT_PATH) -> dict:
    """적용일 전까지 취소(CHG-05). 결제 권한자만, 본인 고객의 접수만."""
    code = documents._identity(conn, user_id)["code"]
    if role != ROLE_BILLING_ADMIN:
        audit.record("change_cancel_denied", audit_path, code=code, role=role)
        raise PermissionError("변경 취소는 결제 권한자만 할 수 있습니다.")
    with closing(history.connect(app_db)) as app:
        row = app.execute("SELECT * FROM change_requests WHERE request_id = ? AND code = ?", (request_id, code)).fetchone()
        if row is None:
            raise ChangeError("해당 접수를 찾을 수 없습니다.")
        request = dict(row)
        if request["status"] != STATUS_OPEN:
            raise ChangeError(f"이미 {request['status']}된 접수라 취소할 수 없습니다.")
        if today > date.fromisoformat(request["cancel_deadline"]):
            raise ChangeError(f"취소 기한({request['cancel_deadline']})이 지났습니다.")
        app.execute("UPDATE change_requests SET status = ?, cancelled_at = ? WHERE request_id = ?",
                    (STATUS_CANCELLED, today.isoformat(), request_id))
        app.execute("UPDATE recommendation_history SET outcome = '취소', outcome_at = ? WHERE run_id = ?",
                    (today.isoformat(), f"self-{request_id}"))
        app.commit()
    request.update(status=STATUS_CANCELLED, cancelled_at=today.isoformat())
    audit.record("change_cancelled", audit_path, code=code, request_id=request_id, role=role)
    return {"request": request, "sales_message": sales_message(request, "cancelled")}


def apply_due(today: date, app_db: Path = history.APP_DB_PATH, audit_path: Path = audit.AUDIT_PATH) -> list[str]:
    """적용일이 된 접수를 적용 상태로 바꾼다. 시연에서는 이 표가 모의 청구 시스템이다."""
    with closing(history.connect(app_db)) as app:
        due = [row["request_id"] for row in app.execute(
            "SELECT request_id FROM change_requests WHERE status = ? AND effective_date <= ?",
            (STATUS_OPEN, today.isoformat()))]
        app.executemany("UPDATE change_requests SET status = ?, applied_at = ? WHERE request_id = ?",
                        [(STATUS_APPLIED, today.isoformat(), request_id) for request_id in due])
        app.commit()
    for request_id in due:
        audit.record("change_applied", audit_path, request_id=request_id)
    return due


def confirmation_change(request: dict) -> dict:
    """변경 확인서(DOC-01)에 넣을 접수 정보."""
    return {"request_id": request["request_id"], "effective_date": request["effective_date"],
            "cancel_deadline": request["cancel_deadline"]}

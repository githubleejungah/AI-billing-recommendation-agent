"""상담 연결(ESC-01~04). AI를 쓰지 않는다.

- 접수의 원본은 앱 저장소 `tickets` 표다(ESC-01). 고객에게는 접수 번호와 답변 예정 시간을 보여준다.
- 사내 담당자 채널(Slack)에는 접수 번호, 가칭 코드, 넘어온 이유, 가린 질문 요약만 보낸다(ESC-02).
- 질문은 가린 형태로만 저장한다(ASK-05, 명세 ASK "T12 구현 세부").
- 담당자 답변은 고객 화면에 보이고 기록된다(ESC-03). FAQ 후보 표시는 승인 전까지 지식 문서에 들어가지 않는다(ESC-04).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

import audit

REASONS = ("위험 주제", "이상한 요청", "근거 없음", "검증 실패", "AI 오류", "고객 요청")
RECEIVED, ANSWERED = "접수", "답변 완료"
ANSWER_ETA = "영업일 1일 이내"
SUMMARY_CHARS = 80
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output" / "tickets"


class TicketError(Exception):
    pass


def new_ticket_id(now: datetime) -> str:
    # 일련번호를 쓰지 않는다(하루 접수 건수가 드러나지 않게, T11과 같은 방식)
    return f"Q-{now:%Y%m%d}-{secrets.token_hex(3).upper()}"


def staff_message(ticket: dict) -> dict:
    """사내 담당자 채널 메시지(ESC-02). 가칭 코드와 가린 질문 요약만."""
    question = ticket["question_masked"]
    summary = question if len(question) <= SUMMARY_CHARS else question[:SUMMARY_CHARS] + "…"
    title = f"상담 접수 {ticket['ticket_id']} · 고객 {ticket['code']} · {ticket['reason']}"
    return {"text": title, "blocks": [
        {"type": "header", "text": {"type": "plain_text", "text": title}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"질문 요약(개인정보 가림): {summary}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"답변 예정: {ANSWER_ETA} · 담당자 화면에서 답변"}]},
    ]}


def open_ticket(app: sqlite3.Connection, code: str, question_masked: str, reason: str, now: datetime, *,
                audit_path: Path = audit.AUDIT_PATH, output_dir: Path = OUTPUT_DIR, sender=None) -> dict:
    """접수하고 사내 채널 메시지를 만든다(보낼 주소가 있으면 보낸다, NTF-02). 고객에게 보여줄 값을 돌려준다."""
    if reason not in REASONS:
        raise TicketError(f"알 수 없는 연결 이유: {reason}")
    ticket = {"ticket_id": new_ticket_id(now), "code": code, "created_at": now.isoformat(timespec="seconds"),
              "question_masked": question_masked, "reason": reason, "status": RECEIVED}
    app.execute("INSERT INTO tickets (ticket_id, code, created_at, question_masked, reason, status)"
                " VALUES (?, ?, ?, ?, ?, ?)", tuple(ticket.values()))
    app.commit()
    message = staff_message(ticket)
    path = output_dir / f"{ticket['ticket_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(message, ensure_ascii=False, indent=2), encoding="utf-8")
    delivery = "주소 없음(JSON만 저장)"
    if sender is not None:
        digest = hashlib.sha256(json.dumps(message, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        delivery = sender(message, digest, ticket["ticket_id"])
    audit.record("ticket_opened", audit_path, ticket_id=ticket["ticket_id"], code=code, reason=reason,
                 slack=delivery)
    return {**ticket, "eta": ANSWER_ETA, "slack": delivery, "message_path": path}


def answer_ticket(app: sqlite3.Connection, ticket_id: str, answer: str, now: datetime, *, faq_candidate: bool = False,
                  audit_path: Path = audit.AUDIT_PATH) -> dict:
    """담당자 답변(ESC-03). 답변은 담당자가 쓴 글이며 고객 화면에 그대로 보인다."""
    if not answer.strip():
        raise TicketError("답변이 비어 있습니다")
    row = app.execute("SELECT status, code FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
    if row is None:
        raise TicketError("없는 접수 번호입니다")
    if row[0] == ANSWERED:
        raise TicketError("이미 답변한 접수입니다")
    answered_at = now.isoformat(timespec="seconds")
    app.execute("UPDATE tickets SET status = ?, answer = ?, answered_at = ?, faq_candidate = ? WHERE ticket_id = ?",
                (ANSWERED, answer.strip(), answered_at, int(faq_candidate), ticket_id))
    app.commit()
    audit.record("ticket_answered", audit_path, ticket_id=ticket_id, code=row[1], faq_candidate=faq_candidate)
    return {"ticket_id": ticket_id, "status": ANSWERED, "answered_at": answered_at}


def mark_faq(app: sqlite3.Connection, ticket_id: str, *, audit_path: Path = audit.AUDIT_PATH) -> None:
    """FAQ 후보 표시(ESC-04). 지식 문서에는 넣지 않는다(승인 뒤 사람이 반영, OPS-03)."""
    if app.execute("UPDATE tickets SET faq_candidate = 1 WHERE ticket_id = ?", (ticket_id,)).rowcount == 0:
        raise TicketError("없는 접수 번호입니다")
    app.commit()
    audit.record("faq_candidate", audit_path, ticket_id=ticket_id)


def customer_tickets(app: sqlite3.Connection, code: str) -> list[dict]:
    """고객 화면에 보일 이 고객의 접수와 답변. 다른 고객의 접수는 보이지 않는다."""
    rows = app.execute("SELECT ticket_id, created_at, question_masked, reason, status, answer, answered_at"
                       " FROM tickets WHERE code = ? ORDER BY created_at", (code,)).fetchall()
    return [dict(r) for r in rows]


def staff_queue(app: sqlite3.Connection) -> list[dict]:
    """담당자 화면의 접수 목록(답변 전 먼저)."""
    rows = app.execute("SELECT * FROM tickets ORDER BY status = ?, created_at", (ANSWERED,)).fetchall()
    return [dict(r) for r in rows]

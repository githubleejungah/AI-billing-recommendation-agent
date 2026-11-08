"""고객 화면·담당자 화면이 함께 쓰는 화면 로직(UI-02~10, SEC-16). Streamlit 없이 테스트한다.

계산·문구는 만들지 않는다. 이미 있는 모듈(simulator, documents, change, chat, escalation, batch, agent)을
부르고, 화면에서만 필요한 일(역할별 버튼, 인용 나누기, 근거 모달 값, 실명 조회, 반응 기록)만 여기 둔다.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import agent
import audit
import data_access
import escalation
import history
import report

ROOT = Path(__file__).resolve().parent.parent
SCREEN_CUSTOMER, SCREEN_STAFF = "customer", "staff"
ROLE_ADMIN, ROLE_MEMBER, ROLE_STAFF = "결제 권한자", "일반 사용자", "영업 담당자"
REACTIONS = ("수락", "거절", "보류")  # UI-08. 셀프 수락·취소는 셀프 변경이 남긴다
DEMO_CUSTOMERS = ("user_002", "user_004")  # 시연 시나리오 고객(tasks.md)

# 역할마다 보이는 버튼(UI-09). AI는 요금제 변경을 실행하지 않는다: 변경·취소는 결제 권한자만.
ACTIONS = {
    ROLE_ADMIN: {"documents", "change", "cancel", "chat", "human"},
    ROLE_MEMBER: {"documents", "request_admin", "chat", "human"},
    ROLE_STAFF: {"selection", "analyze", "reveal", "reaction", "answer", "faq", "metrics"},
}


class EvidenceError(Exception):
    pass


@dataclass
class Config:
    """화면이 쓰는 경로와 날짜. 테스트는 임시 폴더로 바꾼다(실제 data/app.db로 선별을 돌리지 않게)."""
    billing_db: Path = data_access.DB_PATH
    app_db: Path = history.APP_DB_PATH
    audit_path: Path = audit.AUDIT_PATH
    output_dir: Path = ROOT / "output"
    today: Callable[[], date] = date.today
    now: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))

    @property
    def ticket_dir(self) -> Path:
        return self.output_dir / "tickets"

    @property
    def runs_dir(self) -> Path:
        return self.output_dir / "runs"


def actions_for(role: str) -> set[str]:
    if role not in ACTIONS:
        raise ValueError(f"알 수 없는 역할: {role}")
    return ACTIONS[role]


def make_client() -> agent.ClaudeClient | None:
    """실제 Claude 클라이언트. 키가 없으면 None(상담은 담당자 연결로 넘어가고, 분석 버튼은 안내만 한다)."""
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return agent.AnthropicClient(key) if key else None


# --- 근거 모달(UI-10) ---

def split_citations(text: str) -> list[tuple[str, str | None]]:
    """본문을 (글, 조각 ID) 조각으로 나눈다. 인용 자리는 (원문, 조각 ID), 나머지는 (글, None)."""
    parts, last = [], 0
    for match in report.CITATION.finditer(text):
        if match.start() > last:
            parts.append((text[last:match.start()], None))
        parts.append((match.group(0), match.group(1)))
        last = match.end()
    if last < len(text):
        parts.append((text[last:], None))
    return parts


def cited_ids(text: str) -> list[str]:
    return list(dict.fromkeys(report.CITATION.findall(text)))


def evidence(chunk_id: str, retrieved: dict[str, dict], screen: str) -> dict:
    """모달에 보일 조각. 그 실행에서 검색된 조각만 열고 다시 검색하지 않는다. 고객 화면은 고객용 조각만."""
    item = retrieved.get(chunk_id)
    if item is None:
        raise EvidenceError("이 답변에서 검색된 근거가 아닙니다.")
    audience = item.get("audience", "customer")
    if screen == SCREEN_CUSTOMER and audience != "customer":
        raise EvidenceError("사내 문서라 고객 화면에서는 열 수 없습니다.")
    not_eligible = [p for p in item.get("promotions", []) if p.get("status") == "대상 아님"]
    return {"chunk_id": chunk_id, "title": item["title"], "section": item["section"], "text": item["text"],
            "audience": audience, "not_eligible": not_eligible}


# --- 담당자: 실명 조회(SEC-16), 반응 기록(UI-08) ---

def reveal_name(conn: sqlite3.Connection, code: str, audit_path: Path = audit.AUDIT_PATH) -> str:
    """가칭 코드 → 실제 이름. 조회할 때마다 감사 기록(시각, PC 사용자, 코드)을 남긴다. 이름은 기록하지 않는다."""
    by_code = {c: u for u, c in data_access.code_map(conn).items()}
    if code not in by_code:
        raise LookupError("없는 가칭 코드입니다.")
    name = data_access.load_identity(conn, by_code[code])["name"]
    audit.record("name_lookup", audit_path, code=code, screen=SCREEN_STAFF)
    return name


def latest_recommendation(app: sqlite3.Connection, code: str) -> dict | None:
    row = app.execute("SELECT * FROM recommendation_history WHERE code = ? ORDER BY analyzed_at DESC, run_id DESC"
                      " LIMIT 1", (code,)).fetchone()
    return dict(row) if row else None


def record_reaction(app_db: Path, code: str, outcome: str, today: date,
                    audit_path: Path = audit.AUDIT_PATH) -> dict:
    """가장 최근 추천에 고객 반응을 적는다. 추천 이력(SEC-15)과 감사 기록에 남는다."""
    if outcome not in REACTIONS:
        raise ValueError(f"반응은 {', '.join(REACTIONS)} 중 하나입니다.")
    with closing(history.connect(app_db)) as app:
        rec = latest_recommendation(app, code)
        if rec is None:
            raise LookupError("이 고객의 추천 이력이 없습니다. 먼저 분석하거나 셀프 변경 기록을 확인하세요.")
        app.execute("UPDATE recommendation_history SET outcome = ?, outcome_at = ? WHERE run_id = ?",
                    (outcome, today.isoformat(), rec["run_id"]))
        app.commit()
    audit.record("reaction_recorded", audit_path, code=code, run_id=rec["run_id"], outcome=outcome)
    return {**rec, "outcome": outcome, "outcome_at": today.isoformat()}


# --- 조회 ---

def customer_notices(app_db: Path, code: str) -> list[dict]:
    """고객 화면 알림 목록(최근 것 먼저)."""
    with closing(history.connect(app_db)) as app:
        rows = app.execute("SELECT * FROM notifications WHERE code = ? ORDER BY created_at DESC", (code,)).fetchall()
    return [dict(r) for r in rows]


def open_change(app_db: Path, code: str) -> dict | None:
    with closing(history.connect(app_db)) as app:
        row = app.execute("SELECT * FROM change_requests WHERE code = ? AND status = '접수'", (code,)).fetchone()
    return dict(row) if row else None


def tickets_for(app_db: Path, code: str | None = None) -> list[dict]:
    """code가 있으면 그 고객의 접수(고객 화면), 없으면 담당자 목록. 질문은 이미 가린 글이다(ASK-05)."""
    with closing(history.connect(app_db)) as app:
        return escalation.customer_tickets(app, code) if code else escalation.staff_queue(app)


def sent_records(audit_entries: list[dict], run_id: str) -> list[dict]:
    """그 실행에서 Claude로 나간 요청의 기록(해시·크기·종류·토큰). 내용은 없다(SEC-11)."""
    return [e for e in audit_entries if e.get("event") == "external_send" and e.get("run_id") == run_id]


def recheck_draft(edited: str, sim: dict, retrieved: dict[str, dict], registry: dict | None = None) -> list[str]:
    """UI-06 시연: 고친 초안을 다시 검증만 한다. 저장·전송하지 않는다."""
    return report.check_draft(edited, sim, retrieved, registry)

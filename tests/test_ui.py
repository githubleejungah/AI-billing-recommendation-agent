"""화면(T13) 검증: 화면 로직(UI-06~10, SEC-16), 운영 지표(OPS-02), Streamlit 화면 흐름(streamlit.testing).

가짜 Claude로 돈다(API 키 없음). 경로는 임시 폴더라 실제 data/app.db와 logs/를 건드리지 않는다.
금액 기대값은 손 계산 값이다(test_simulator.py): user_002 12개월 절감 공급가액 598,000원,
세액 59,800원(10%), 합계 657,800원.
"""

from __future__ import annotations

import json
import sys
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "database"))

import audit  # noqa: E402
import history  # noqa: E402
import init_db  # noqa: E402
import metrics  # noqa: E402
import simulator  # noqa: E402
import ui_logic  # noqa: E402
from test_agent import FakeClient as AgentClient, happy_script  # noqa: E402
from test_chat import FakeClient as ChatClient, cite_first  # noqa: E402

TODAY = date(2026, 11, 7)
NOW = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
CODE_002 = "UQM-9GU"  # user_002의 가칭 코드(test_agent.py)


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "billing_mock.db"
    init_db.build(path)
    return path


@pytest.fixture()
def conn(db):
    with closing(simulator.connect(db)) as connection:
        yield connection


@pytest.fixture()
def cfg(db, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    return ui_logic.Config(billing_db=db, app_db=tmp_path / "app.db", audit_path=tmp_path / "audit.jsonl",
                           output_dir=tmp_path / "output", today=lambda: TODAY, now=lambda: NOW)


# --- 화면 로직 ---

def test_buttons_depend_on_role():
    assert "change" in ui_logic.actions_for(ui_logic.ROLE_ADMIN)
    member = ui_logic.actions_for(ui_logic.ROLE_MEMBER)
    assert "change" not in member and "cancel" not in member and "request_admin" in member
    assert {"human", "chat"} <= member  # 담당자 연결은 늘 보인다
    staff = ui_logic.actions_for(ui_logic.ROLE_STAFF)
    assert {"reveal", "reaction", "analyze"} <= staff and "change" not in staff
    with pytest.raises(ValueError):
        ui_logic.actions_for("관리자")


RETRIEVED = {
    "promo_pro_upgrade#1": {"chunk_id": "promo_pro_upgrade#1", "title": "프로 전환 할인", "section": "대상",
                            "text": "가입 6개월 이상", "audience": "customer",
                            "promotions": [{"promo_id": "PROMO_PRO", "status": "대상 아님", "reason": "가입 기간 미달"}]},
    "guide_upgrade#2": {"chunk_id": "guide_upgrade#2", "title": "상향 응대", "section": "설명 순서",
                        "text": "사용량부터", "audience": "internal", "promotions": []},
}


def test_citations_split_into_text_and_ids():
    parts = ui_logic.split_citations("할인 안내입니다. [근거: promo_pro_upgrade#1] 끝")
    assert parts == [("할인 안내입니다. ", None), ("[근거: promo_pro_upgrade#1]", "promo_pro_upgrade#1"), (" 끝", None)]
    assert ui_logic.cited_ids("[근거: a#1] [근거: a#1] [근거: b#2]") == ["a#1", "b#2"]


def test_evidence_opens_only_retrieved_chunks():
    item = ui_logic.evidence("promo_pro_upgrade#1", RETRIEVED, ui_logic.SCREEN_CUSTOMER)
    assert item["text"] == "가입 6개월 이상" and item["not_eligible"][0]["reason"] == "가입 기간 미달"
    with pytest.raises(ui_logic.EvidenceError):  # 이 실행에서 검색되지 않은 조각(다시 검색하지 않음)
        ui_logic.evidence("plan_pro#1", RETRIEVED, ui_logic.SCREEN_STAFF)


def test_customer_screen_refuses_internal_chunks():
    with pytest.raises(ui_logic.EvidenceError, match="사내 문서"):
        ui_logic.evidence("guide_upgrade#2", RETRIEVED, ui_logic.SCREEN_CUSTOMER)
    assert ui_logic.evidence("guide_upgrade#2", RETRIEVED, ui_logic.SCREEN_STAFF)["audience"] == "internal"


def test_name_lookup_is_audited_without_the_name(conn, cfg):
    name = ui_logic.reveal_name(conn, CODE_002, cfg.audit_path)
    assert name == "(주)베타소프트"
    entry = audit.read(cfg.audit_path)[-1]
    assert entry["event"] == "name_lookup" and entry["code"] == CODE_002 and entry["pc_user"]
    assert "베타소프트" not in cfg.audit_path.read_text(encoding="utf-8")
    with pytest.raises(LookupError):
        ui_logic.reveal_name(conn, "AAA-AAA", cfg.audit_path)


def test_reaction_updates_latest_recommendation_and_audit(cfg):
    with closing(history.connect(cfg.app_db)) as app:
        app.executemany("INSERT INTO recommendation_history (run_id, code, analyzed_at) VALUES (?, ?, ?)",
                        [("r1", CODE_002, "2026-11-01T00:00:00"), ("r2", CODE_002, "2026-11-05T00:00:00")])
        app.commit()
    ui_logic.record_reaction(cfg.app_db, CODE_002, "보류", TODAY, cfg.audit_path)
    with closing(history.connect(cfg.app_db)) as app:
        rows = {r["run_id"]: (r["outcome"], r["outcome_at"]) for r in app.execute("SELECT * FROM recommendation_history")}
    assert rows == {"r1": ("미기록", None), "r2": ("보류", "2026-11-07")}
    assert audit.read(cfg.audit_path)[-1] | {"at": None} == {
        "at": None, "event": "reaction_recorded", "pc_user": audit.read(cfg.audit_path)[-1]["pc_user"],
        "code": CODE_002, "run_id": "r2", "outcome": "보류"}
    with pytest.raises(ValueError):
        ui_logic.record_reaction(cfg.app_db, CODE_002, "셀프 수락", TODAY, cfg.audit_path)
    with pytest.raises(LookupError):
        ui_logic.record_reaction(cfg.app_db, "AAA-AAA", "수락", TODAY, cfg.audit_path)


def test_number_written_into_draft_is_rejected(conn):
    sim = simulator.simulate(conn, "user_002")
    ok = "## 추천\n{rec_plan} 요금제를 권합니다. [근거: promo_pro_upgrade#1]"
    edited = ok.replace("{rec_plan} 요금제를", "{rec_plan} 요금제를 월 50,000원에")
    assert any("숫자" in p or "50,000" in p for p in ui_logic.recheck_draft(edited, sim, RETRIEVED))


# --- 운영 지표(OPS-02) ---

def test_monthly_metrics_hand_calculated(tmp_path):
    with closing(history.connect(tmp_path / "app.db")) as app:
        app.executemany("INSERT INTO notifications (notification_id, code, created_at, valid_until, status)"
                        " VALUES (?, 'X', ?, '2026-12-01', '보냄')",
                        [("n1", "2026-11-01"), ("n2", "2026-11-01"), ("n3", "2026-11-02"), ("n4", "2026-11-03"),
                         ("n0", "2026-10-01")])
        app.executemany("INSERT INTO recommendation_history (run_id, code, analyzed_at, outcome, outcome_at)"
                        " VALUES (?, 'X', '2026-11-01', ?, ?)",
                        [("a", "셀프 수락", "2026-11-07"), ("b", "수락", "2026-11-08"), ("c", "거절", "2026-11-08")])
        app.executemany("INSERT INTO change_requests (request_id, code, requested_at, requested_by_role, status)"
                        " VALUES (?, ?, '2026-11-07', '결제 권한자', ?)", [("c1", "X", "취소"), ("c2", "Y", "접수")])
        app.executemany("INSERT INTO tickets (ticket_id, code, created_at, question_masked, reason, status, answered_at)"
                        " VALUES (?, 'X', ?, 'q', ?, '접수', ?)",
                        [("t1", "2026-11-07T01:00:00+00:00", "위험 주제", None),
                         ("t2", "2026-11-07T01:00:00+00:00", "고객 요청", None),
                         ("t3", "2026-11-07T01:00:00+00:00", "근거 없음", "2026-11-07T03:00:00+00:00")])
        app.execute("INSERT INTO quality_checks VALUES ('q1', '2026-11-01T00:00:00', 203, 199, 0.98, '{}', '[]', '[]', 0)")
        app.commit()
        audit_entries = [
            *({"at": "2026-11-07T01:00:00+00:00", "event": "chat"} for _ in range(3)),
            {"at": "2026-11-07T01:00:00+00:00", "event": "external_send", "result": "통과", "model": "claude-sonnet-5-5",
             "input_tokens": 1000, "output_tokens": 200},
            {"at": "2026-11-07T01:00:00+00:00", "event": "external_send", "result": "통과", "model": "claude-sonnet-5-5",
             "input_tokens": 500, "output_tokens": 100},
            {"at": "2026-11-07T01:00:00+00:00", "event": "external_send", "result": "차단", "model": "claude-sonnet-5-5"},
            {"at": "2026-10-07T01:00:00+00:00", "event": "chat"},
        ]
        runs = [
            {"run_id": "20261107-090000-aaaaaa", "status": "완료", "validation": [{"attempt": 1, "kinds": ["숫자"]}],
             "steps": [{"seconds": 1.5}, {"seconds": 2.5}]},
            {"run_id": "20261107-100000-bbbbbb", "status": "완료", "validation": [{"attempt": 1, "kinds": []}],
             "steps": [{"seconds": 2.0}]},
            {"run_id": "20261007-100000-cccccc", "status": "완료", "validation": [{"attempt": 1, "kinds": ["숫자"]}],
             "steps": [{"seconds": 9.0}]},
        ]
        m = metrics.compute(app, audit_entries, runs, "2026-11")
    # 알림 4건, 수락 2건(수락 + 셀프 수락) → 50%. 셀프 변경 2건 중 취소 1건 → 50%.
    assert (m["notices"], m["accepted"], m["acceptance_rate"]) == (4, 2, 0.5)
    assert m["self_change_cancel_rate"] == 0.5
    # 상담 질문 = 상담 기록 3 + 담당자 연결 버튼 1 = 4, 연결 3건 → 75%, 이유별 각 1/4
    assert (m["questions"], m["escalation_rate"]) == (4, 0.75)
    assert m["escalation_by_reason"] == {"고객 요청": 0.25, "근거 없음": 0.25, "위험 주제": 0.25}
    # 리포트 2건 중 첫 시도 실패 1건 → 50%, 평균 시간 (4.0 + 2.0) / 2 = 3.0초. 10월 실행은 빠짐
    assert (m["reports"], m["first_try_fail_rate"], m["report_seconds_avg"]) == (2, 0.5, 3.0)
    assert m["quality_pass_rate"] == 0.98 and m["ticket_answer_hours_avg"] == 2.0
    # 토큰 1,500/300. 비용 = (1500×3 + 300×15) / 1,000,000 = 0.009달러(가정 단가)
    assert (m["api_calls"], m["input_tokens"], m["output_tokens"], m["cost_usd"]) == (2, 1500, 300, 0.009)


def test_metrics_empty_month_has_no_ratios(tmp_path):
    with closing(history.connect(tmp_path / "app.db")) as app:
        m = metrics.compute(app, [], [], "2026-11")
    assert m["acceptance_rate"] is None and m["escalation_rate"] is None and m["cost_usd"] == 0


# --- 화면(streamlit.testing) ---

def app(name: str, cfg, client=None) -> AppTest:
    at = AppTest.from_file(str(ROOT / "src" / name), default_timeout=120)
    at.session_state["cfg"] = cfg
    if client is not None:
        at.session_state["client"] = client
    return at.run()


def texts(at: AppTest) -> str:
    return "\n".join(str(e.value) for kind in ("markdown", "info", "success", "warning", "error", "caption", "text")
                     for e in getattr(at, kind))


def keys(at: AppTest) -> set[str]:
    return {b.key for b in at.button}


def test_member_sees_request_message_instead_of_change_button(cfg):
    at = app("app_customer.py", cfg)
    assert not at.exception
    assert "prepare_change" in keys(at) and "human" in keys(at)
    assert "598,000원" in str([t.value for t in at.table])  # 12개월 절감 공급가액(손 계산)
    at.radio(key="role").set_value(ui_logic.ROLE_MEMBER).run()
    assert "prepare_change" not in keys(at) and "submit_change" not in keys(at)
    assert "결제 권한자에게 변경을 요청" in texts(at)
    assert "human" in keys(at)  # 담당자 연결 버튼은 역할과 관계없이 보인다


def test_self_change_submit_document_and_cancel(cfg):
    at = app("app_customer.py", cfg)
    at.button(key="prepare_change").click().run()
    table = str([t.value for t in at.table])
    assert "598,000원" in table and "59,800원" in table and "657,800원" in table  # 공급가액·세액·합계
    at.checkbox(key="invoice_ok").check().run()
    at.button(key="submit_change").click().run()
    assert "접수했습니다. 접수 번호 CR-20261107-" in texts(at)
    at.button(key="issue_confirmation").click().run()
    assert any(b.label == "PDF" for b in at.get("download_button"))
    at.button(key="cancel_change").click().run()
    assert "접수를 취소했습니다" in texts(at)
    events = [e["event"] for e in audit.read(cfg.audit_path)]
    assert events[:3] == ["change_requested", "document_issued", "change_cancelled"]


def test_chat_answer_keeps_evidence_and_human_button_opens_ticket(cfg):
    client = ChatClient(cite_first)
    at = app("app_customer.py", cfg, client)
    at.selectbox(key="user_id").set_value("user_004").run()
    assert "이 상담은 AI가 답합니다" in texts(at)
    at.text_input(key="question:user_004").input("프로 전환 할인 받을 수 있나요?").run()
    at.button(key="ask").click().run()
    assert len(client.requests) == 1
    reply = at.session_state["chat:user_004"][-1]["reply"]
    assert reply.answer and reply.citations and set(reply.citations) <= set(reply.retrieved)
    assert all(v["audience"] == "customer" for v in reply.retrieved.values())
    assert any(b.key.startswith("chat1:") for b in at.button)  # 인용마다 근거 버튼
    at.radio(key="role").set_value(ui_logic.ROLE_MEMBER).run()  # 다른 조작: 다시 부르지 않음
    assert len(client.requests) == 1
    at.button(key="human").click().run()
    assert "담당자에게 연결했습니다. 접수 번호 Q-20261107-" in texts(at)


def test_staff_selection_tickets_lookup_and_reaction(cfg, conn):
    at = app("app_staff.py", cfg)
    assert not at.exception
    at.button(key="run_selection").click().run()
    summary = texts(at)
    assert "월간 선별 2026-11-01" in summary and CODE_002 in summary and "베타소프트" not in summary
    at.text_input(key="lookup_code").input(CODE_002).run()
    at.button(key="reveal").click().run()
    assert f"{CODE_002} = (주)베타소프트" in texts(at)
    assert audit.read(cfg.audit_path)[-1]["event"] == "name_lookup"


def test_analyze_only_on_button_and_session_keeps_result(cfg):
    client = AgentClient(happy_script())
    at = app("app_staff.py", cfg, client)
    assert client.requests == []  # 화면을 열기만 해서는 부르지 않는다
    at.button(key="analyze").click().run()
    assert not at.exception
    calls = len(client.requests)
    assert calls == 3 and "(주)베타소프트" in texts(at)
    assert any(b.key.startswith(f"rep:{CODE_002}:") for b in at.button)  # 리포트 인용 → 근거 버튼
    at.text_input(key="metrics_month").input("2026-10").run()
    at.button(key=f"recheck:{CODE_002}").click().run()
    assert len(client.requests) == calls  # 다른 조작으로는 다시 부르지 않는다(UI-07)
    sent = [e for e in audit.read(cfg.audit_path) if e["event"] == "external_send"]
    assert sent and all(e["result"] == "통과" for e in sent)
    card = at.session_state[f"report:{CODE_002}"]["card"]
    assert "베타소프트" not in card and "user_002" not in card and CODE_002 not in card
    assert json.dumps(sent, ensure_ascii=False).count("베타소프트") == 0

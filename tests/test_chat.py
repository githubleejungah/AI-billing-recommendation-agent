"""AI 상담과 상담 연결 검증(ASK-01~06, ESC-01~04, EVAL-06). 가짜 Claude로 돈다(API 키 없음).

금액 기대값은 손 계산 값이다(test_simulator.py): user_002 현재 월 124,500원, 12개월 유지 1,486,000원,
프로 + 전환 할인 888,000원, 절감 598,000원(40%).
"""

from __future__ import annotations

import copy
import json
import random
import re
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "database"))

import agent  # noqa: E402
import audit  # noqa: E402
import chat  # noqa: E402
import escalation  # noqa: E402
import history  # noqa: E402
import init_db  # noqa: E402
import policy  # noqa: E402
import simulator  # noqa: E402

MODEL = "claude-sonnet-5-5"
NOW = datetime(2026, 11, 8, 2, 0, tzinfo=timezone.utc)
CHUNK = re.compile(r'<문서 조각 id="([^"]+)"')
EVAL = json.loads((ROOT / "eval" / "customer_questions.json").read_text(encoding="utf-8"))["questions"]


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "billing_mock.db"
    init_db.build(path)
    connection = simulator.connect(path)
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def pol():
    return policy.load()


@pytest.fixture()
def paths(tmp_path):
    return {"app_db": tmp_path / "app.db", "audit_path": tmp_path / "a.jsonl", "ticket_dir": tmp_path / "tickets"}


class FakeClient:
    def __init__(self, answer):
        self.answer = answer  # 문자열, 요청을 받아 문자열을 만드는 함수, 또는 예외
        self.requests = []

    def create(self, request):
        self.requests.append(copy.deepcopy(request))
        if isinstance(self.answer, Exception):
            raise self.answer
        text = self.answer(request) if callable(self.answer) else self.answer
        return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 800, "output_tokens": 120}}


def cite_first(request) -> str:
    first = CHUNK.findall(request["messages"][0]["content"])[0]
    return f"{{current_plan}} 요금제를 쓰고 계십니다. 안내 문서 기준으로 설명드립니다. [근거: {first}]"


def ask(conn, pol, paths, user_id, question, answer=cite_first):
    client = FakeClient(answer)
    reply = chat.ask(conn, user_id, question, client=client, model=MODEL, policy=pol, now=NOW,
                     sleep=lambda s: None, rng=random.Random(0), **paths)
    return reply, client


# --- 평가 세트(EVAL-06) ---

@pytest.mark.parametrize("item", EVAL, ids=[q["id"] for q in EVAL])
def test_customer_question_set(conn, pol, paths, item):
    reply, client = ask(conn, pol, paths, item["user_id"], item["question"])
    if item["expect"] == "escalate":
        assert reply.ticket and reply.ticket["reason"] == item["reason"], reply
        assert reply.answer is None
        if item["reason"] in ("위험 주제", "이상한 요청"):
            assert client.requests == []  # 위험 주제·이상한 요청은 Claude를 부르지 않는다(ASK-02)
    elif item["expect"] == "code_answer":
        assert reply.route == "금액" and reply.answer and client.requests == []
    else:
        assert reply.route == "조건" and reply.answer and reply.ticket is None
        sent = CHUNK.findall(client.requests[0]["messages"][0]["content"])
        assert set(item["answer_chunks"]) & set(sent), sent  # 정답 조각이 Claude에 간 조각(상위 3개) 안에 있음


def test_no_must_escalate_question_was_answered_by_ai(conn, pol, paths):
    answered = []
    for item in (q for q in EVAL if q["expect"] == "escalate"):
        reply, _ = ask(conn, pol, paths, item["user_id"], item["question"])
        if reply.answer is not None:
            answered.append(item["id"])
    assert answered == []  # 성공 기준: 넘겨야 할 질문을 AI가 답한 건수 0


# --- 금액 질문(ASK-02) ---

def test_amount_answer_uses_simulator_values(conn, pol, paths):
    reply, client = ask(conn, pol, paths, "user_002", "다음 달 요금 얼마 나와요?")
    assert client.requests == []
    assert reply.answer.startswith("지금 베이직 요금제의 예상 월 청구액은 최근 사용량 기준 124,500원이고, "
                                   "앞으로 12개월 동안 1,486,000원입니다(부가세 별도).")
    assert "프로 요금제로 바꾸면서 이벤트 '프로 전환 3개월 30% 할인'을 받으면 12개월 동안 888,000원으로 " \
           "598,000원(40%)을 아낄 수 있습니다." in reply.answer
    keep, _ = ask(conn, pol, paths, "user_001", "청구액이 얼마예요?")
    assert "지금 요금제가 가장 경제적입니다" in keep.answer


# --- 조건 질문의 AI 답변과 검증(ASK-02, 03, 06) ---

def test_condition_answer_is_filled_and_cited(conn, pol, paths):
    reply, client = ask(conn, pol, paths, "user_004", "프로 전환 할인 받을 수 있나요?")
    assert reply.answer.startswith("베이직 요금제를 쓰고 계십니다.")  # 자리표시자를 코드가 채움
    assert reply.citations and reply.citations[0] in CHUNK.findall(client.requests[0]["messages"][0]["content"])
    body = client.requests[0]["messages"][0]["content"]
    assert "PROMO_PRO_UPGRADE: 대상 아님(가입 기간 조건 미달)" in body  # 이유의 종류만 간다(가입 개월 수는 가지 않음)
    assert "현재 3개월" not in body
    assert not re.search(r'id="guide_', body)  # 내부용 응대 가이드는 고객 상담에 쓰지 않는다(KNW-07)


@pytest.mark.parametrize("answer, reason", [
    ("할인율은 30%입니다. [근거: promo_pro_upgrade#2]", "검증 실패"),           # AI가 쓴 숫자
    ("조건을 갖추면 받을 수 있습니다.", "검증 실패"),                            # 인용 없음
    ("조건을 갖추면 받을 수 있습니다. [근거: plan_basic#9]", "검증 실패"),       # 검색하지 않은 조각
    ("필요한 가입 기간은 {promo_min_tenure}입니다. [근거: promo_pro_upgrade#2]", "검증 실패"),  # 이 고객에게 값이 없는 키
    ("삼만 원을 아낍니다. [근거: promo_pro_upgrade#2]", "검증 실패"),           # 한글 금액
    (chat.NO_ANSWER, "근거 없음"),
    (agent.ClaudeAPIError("server", False), "AI 오류"),
])
def test_bad_or_missing_answers_go_to_staff(conn, pol, paths, answer, reason):
    reply, client = ask(conn, pol, paths, "user_004", "프로 전환 할인 받을 수 있나요?", answer)
    assert reply.answer is None and reply.ticket["reason"] == reason
    assert len(client.requests) == 1  # 다시 쓰게 하지 않고 바로 사람에게 넘긴다


def test_no_search_result_goes_to_staff_without_ai(conn, pol, paths):
    reply, client = ask(conn, pol, paths, "user_002", "회사 근처 맛집 추천해 줘")
    assert reply.ticket["reason"] == "근거 없음" and client.requests == []


# --- 개인정보 가림(ASK-05)과 전송 기록 ---

def test_personal_info_is_masked_before_claude_slack_and_storage(conn, pol, paths):
    # 질문이 길면 검색 범위 기준(조각에 든 글자 묶음 25%)을 넘지 못해 연결로 간다. 짧게 쓴 질문으로 시험한다(T7에서 개선).
    question = "(주)베타소프트 요금제 바꾸면 언제부터 적용돼요? 연락은 010-1234-5678"
    sent = []
    reply = chat.ask(conn, "user_002", question, client=FakeClient(cite_first), model=MODEL, policy=pol, now=NOW,
                     sleep=lambda s: None, rng=random.Random(0), **paths)
    assert reply.answer
    risky = "카드 이중 결제됐어요. 제 메일 kim@example.com, 사업자번호 123-45-67890"
    ticket = chat.ask(conn, "user_002", risky, client=FakeClient(cite_first), model=MODEL, policy=pol, now=NOW,
                      sender=lambda m, d, t: sent.append(m) or "보냄", **paths).ticket
    with closing(history.connect(paths["app_db"])) as app:
        stored = app.execute("SELECT question_masked FROM tickets WHERE ticket_id = ?", (ticket["ticket_id"],)).fetchone()[0]
    everything = stored + json.dumps(sent, ensure_ascii=False) + paths["audit_path"].read_text(encoding="utf-8")
    for raw in ("kim@example.com", "123-45-67890", "010-1234-5678", "베타소프트"):
        assert raw not in everything
    assert "[가림]" in stored and ticket["slack"] == "보냄"


def test_claude_request_carries_only_masked_question(conn, pol, paths):
    question = "(주)베타소프트인데 요금제 바꾸면 언제부터 적용돼요? 연락은 010-1234-5678"
    reply, client = ask(conn, pol, paths, "user_002", question)
    body = json.dumps(client.requests[0], ensure_ascii=False)
    assert "베타소프트" not in body and "010-1234-5678" not in body and "user_002" not in body
    events = [e for e in audit.read(paths["audit_path"]) if e["event"] == "external_send"]
    assert events[-1]["result"] == "통과" and events[-1]["kinds"] == ["question", "documents"]


# --- 상담 연결(ESC-01~04, ASK-04) ---

def test_ticket_flow_answer_and_faq(conn, pol, paths):
    first = chat.request_human(conn, "user_004", "직접 통화하고 싶어요", policy=pol, now=NOW, **paths)
    assert first.ticket["reason"] == "고객 요청" and first.ticket["eta"] == "영업일 1일 이내"
    assert re.fullmatch(r"Q-20261108-[0-9A-F]{6}", first.ticket["ticket_id"])
    message = json.loads(first.ticket["message_path"].read_text(encoding="utf-8"))
    assert first.ticket["code"] in message["text"] and "델타랩" not in json.dumps(message, ensure_ascii=False)
    other = chat.request_human(conn, "user_002", "문의드려요", policy=pol, now=NOW, **paths)
    with closing(history.connect(paths["app_db"])) as app:
        assert [t["ticket_id"] for t in escalation.staff_queue(app)][:2]
        escalation.answer_ticket(app, first.ticket["ticket_id"], "내일 오전에 전화드리겠습니다.", NOW,
                                 faq_candidate=True, audit_path=paths["audit_path"])
        with pytest.raises(escalation.TicketError, match="이미 답변"):
            escalation.answer_ticket(app, first.ticket["ticket_id"], "다시", NOW, audit_path=paths["audit_path"])
        mine = escalation.customer_tickets(app, first.ticket["code"])
        assert [(t["status"], t["answer"]) for t in mine] == [("답변 완료", "내일 오전에 전화드리겠습니다.")]
        assert other.ticket["ticket_id"] not in {t["ticket_id"] for t in mine}  # 다른 고객 접수는 안 보임
        assert escalation.staff_queue(app)[0]["ticket_id"] == other.ticket["ticket_id"]  # 답변 전이 먼저
        faq = app.execute("SELECT faq_candidate FROM tickets WHERE ticket_id = ?", (first.ticket["ticket_id"],)).fetchone()[0]
    assert faq == 1
    events = [e["event"] for e in audit.read(paths["audit_path"])]
    assert events.count("ticket_opened") == 2 and "ticket_answered" in events


def test_disclosure_and_model_check(conn, pol, paths):
    assert "AI가 답합니다" in chat.DISCLOSURE and "담당자 연결" in chat.DISCLOSURE  # ASK-01, 04
    with pytest.raises(policy.PolicyError):
        chat.ask(conn, "user_002", "요금 얼마예요?", client=None, model="other-model", policy=pol, **paths)

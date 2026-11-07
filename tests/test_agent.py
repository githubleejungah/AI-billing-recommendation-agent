"""리포트 파이프라인 검증(AGT-01~06, SEC-01, 03~06, 10, 12, 15, NTF-01~04). API 키 없이 가짜 Claude로 돈다.

가짜 클라이언트는 정해 둔 순서로 응답한다. 응답은 고정 dict이거나, 받은 요청을 보고 만드는 함수다
(예: 이번에 검색된 조각 ID를 읽어 인용하는 초안). 받은 요청은 깊은 복사로 남겨 무엇이 나갔는지 확인한다.
"""

from __future__ import annotations

import copy
import json
import os
import random
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "database"))

import agent  # noqa: E402
import audit  # noqa: E402
import guard  # noqa: E402
import history  # noqa: E402
import init_db  # noqa: E402
import policy  # noqa: E402
import report  # noqa: E402
import simulator  # noqa: E402

MODEL = "claude-sonnet-5-5"
NOW = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
CHUNK = re.compile(r'<문서 조각 id="([^"]+)"')


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("db") / "billing_mock.db"
    init_db.build(db_path)
    connection = simulator.connect(db_path)
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def pol():
    return policy.load()


@pytest.fixture()
def paths(tmp_path):
    return {"output_dir": tmp_path / "output", "app_db": tmp_path / "app.db",
            "audit_path": tmp_path / "audit.jsonl", "sent_log_path": tmp_path / "sent_log.json"}


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def create(self, request):
        self.requests.append(copy.deepcopy(request))
        if not self.script:
            raise AssertionError("가짜 응답이 모자랍니다")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(request) if callable(item) else item


def search(query, call_id="toolu_01"):
    return {"content": [{"type": "tool_use", "id": call_id, "name": "search_knowledge", "input": {"query": query}}],
            "stop_reason": "tool_use", "usage": {"input_tokens": 900, "output_tokens": 40}}


def text(body):
    return {"content": [{"type": "text", "text": body}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 1500, "output_tokens": 700}}


def retrieved_ids(request):
    found = []
    for message in request["messages"]:
        if isinstance(message["content"], list):
            for block in message["content"]:
                if block.get("type") == "tool_result":
                    found += CHUNK.findall(block["content"])
    return found


def good_draft(request, extra=""):
    ids = retrieved_ids(request)
    promo = next(i for i in ids if i.startswith("promo_pro_upgrade"))
    guide = next(i for i in ids if i.startswith("guide_upgrade"))
    return text(f"""## 현재 과금 분석
C1 고객은 {{current_plan}} 요금제로 매달 약 {{current_monthly}} 정도 나갑니다. 최근 평균 API 호출은 {{avg_api_calls}}, 스토리지는 {{avg_storage_gb}}로 기본 제공량을 크게 넘습니다.{extra}

## 추천
{{rec_plan}} 요금제와 {{promo_name}} 이벤트를 권합니다. {{stability}} [근거: {promo}]

## 절감액
월 {{saving_monthly}}, 비교 기간 {{horizon_months}} 동안 {{saving_12m}} 정도를 아낄 수 있습니다.

## 응대 안내
상향의 이유를 사용량 기준으로 먼저 설명합니다. [근거: {guide}]
""")


def happy_script():
    return [search("프로 전환 이벤트", "toolu_01A"), search("상향 응대 가이드", "toolu_01B"), good_draft]


def run(conn, pol, paths, client, user_id="user_002", **kwargs):
    kwargs.setdefault("sleep", lambda seconds: None)
    return agent.run(user_id, client=client, model=MODEL, conn=conn, policy=pol, now=NOW,
                     rng=random.Random(0), **paths, **kwargs)


# --- 정상 흐름 ---

def test_happy_path_produces_report_slack_history_and_log(conn, pol, paths):
    client = FakeClient(happy_script())
    result = run(conn, pol, paths, client)
    assert result.status == "완료"

    report_text = result.report_path.read_text(encoding="utf-8")
    assert report_text.startswith("# 요금제 추천 리포트: (주)베타소프트 (UQM-9GU)")
    assert "129,000원" in report_text and "660,000원" in report_text  # 코드가 채운 값
    assert not re.search(r"(?<![A-Za-z0-9])C1(?!\d)", report_text)  # 별칭은 이름으로 바뀜

    slack = json.dumps(json.loads(result.slack_path.read_text(encoding="utf-8")), ensure_ascii=False)
    assert "UQM-9GU" in slack and "베타소프트" not in slack and "user_002" not in slack

    with history.connect(paths["app_db"]) as app:
        row = dict(app.execute("SELECT * FROM recommendation_history").fetchone())
    assert row["code"] == "UQM-9GU" and row["action"] == "upgrade" and row["plan_id"] == "PLAN_PRO"
    assert row["saving_12m"] == 660000 and row["model_id"] == MODEL and row["outcome"] == "미기록"

    log = json.loads(result.run_log_path.read_text(encoding="utf-8"))
    assert [s["step"] for s in log["steps"]] == list(agent.STEPS)
    assert log["model"] == MODEL and log["status"] == "완료" and log["search_count"] == 2
    assert all(call["attempts"] == 1 for call in log["calls"])


def test_requests_carry_no_identity_and_follow_the_rules(conn, pol, paths):
    client = FakeClient(happy_script())
    run(conn, pol, paths, client)
    assert len(client.requests) == 3
    for request in client.requests:
        body = json.dumps(request, ensure_ascii=False)
        assert "user_002" not in body and "베타소프트" not in body and "UQM-9GU" not in body
        assert "129000" not in body and "129,000" not in body and "42000" not in body
        assert request["model"] == MODEL
        assert [tool["name"] for tool in request["tools"]] == ["search_knowledge"]  # 도구는 하나(AGT-01)
        assert agent.INJECTION_RULE in request["system"]  # SEC-06
    first = client.requests[0]["messages"][0]["content"]
    assert first.startswith("[상황 카드]\n고객: C1") and "{saving_12m}" in first
    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "toolu_01A"
    assert tool_result["content"].startswith('<문서 조각 id="') and "</문서 조각>" in tool_result["content"]


def test_every_request_is_audited_with_hash_and_tokens(conn, pol, paths):
    client = FakeClient(happy_script())
    result = run(conn, pol, paths, client)
    entries = audit.read(paths["audit_path"])
    events = [e["event"] for e in entries]
    assert events[0] == "analysis_requested" and events.count("external_send") == 3
    sends = [e for e in entries if e["event"] == "external_send"]
    assert all(e["result"] == "통과" and e["run_id"] == result.run_id for e in sends)
    assert sends[0]["kinds"] == ["card"] and sends[1]["kinds"] == ["card", "documents"]
    assert sends[-1]["input_tokens"] == 1500
    assert [e["result"] for e in entries if e["event"] == "validation"] == ["통과"]
    text_all = paths["audit_path"].read_text(encoding="utf-8")
    assert "베타소프트" not in text_all and "근거" not in text_all  # 이름·본문 없음


def onboarding_draft(request):
    guide = next(i for i in retrieved_ids(request) if i.startswith("guide_no_usage"))
    return text(f"""## 현재 과금 분석
C1 고객은 {{current_plan}} 요금제이며 이용 기록이 없어 요금제를 추천하지 않습니다.

## 응대 안내
시작 안내 자료를 보내고 온보딩 상담을 제안합니다. 처음 쓸 기능 한 가지를 안내하고 다시 연락할 시점을 정합니다. [근거: {guide}]""")


def test_no_usage_customer_searches_guide(conn, pol, paths):
    """(v3.5, REP-03·05) 이용 기록이 없는 고객도 응대 가이드를 검색해 첫 사용 안내를 인용한다."""
    client = FakeClient([search("이용 기록이 없는 고객 첫 사용 지원"), onboarding_draft])
    result = run(conn, pol, paths, client, user_id="user_010")
    assert result.status == "완료"
    body = result.report_path.read_text(encoding="utf-8")
    assert "이용 기록이 없어" in body and "온보딩 상담" in body and "[근거: guide_no_usage#" in body
    assert "상태: 이용 기록 없음" in client.requests[0]["messages"][0]["content"]
    assert "첫 사용 지원" in client.requests[0]["system"]  # 작성 지시에 첫 사용 안내가 들어감
    log = json.loads(result.run_log_path.read_text(encoding="utf-8"))
    assert log["search_count"] == 1
    with history.connect(paths["app_db"]) as app:
        assert app.execute("SELECT action FROM recommendation_history").fetchone()[0] == "no_usage"


def test_no_usage_notice_without_citation_is_rewritten_then_rejected(conn, pol, paths):
    """(v3.5) 근거 인용 없는 안내는 다시 쓰기를 거쳐도 고치지 않으면 내보내지 않는다(REP-05, AGT-05)."""
    bare = text("## 응대 안내\nC1 고객은 이용 기록이 없어 요금제를 추천하지 않습니다. {current_plan} 요금제입니다.")
    client = FakeClient([bare, bare])
    result = run(conn, pol, paths, client, user_id="user_010")
    assert result.status == "거부" and result.report_path is None
    assert any("근거 인용이 없음" in p for p in result.problems)


# --- 검색 제한(AGT-01) ---

def test_search_limit_stops_the_run(conn, pol, paths):
    client = FakeClient([search(f"검색 {n}", f"toolu_{n}") for n in range(6)])
    result = run(conn, pol, paths, client)
    assert result.status == "오류" and result.report_path is None
    log = json.loads(result.run_log_path.read_text(encoding="utf-8"))
    assert log["error_kind"] == "search_limit" and log["search_count"] == 5
    assert not (paths["output_dir"] / "reports").exists()
    sent = json.dumps(client.requests[-1]["messages"], ensure_ascii=False)
    assert "검색 [숫자]" in sent and "검색 4" not in sent  # 지어낸 숫자는 가려서 다시 보냄


# --- 호출 견고성(AGT-03) ---

def test_retryable_errors_are_retried_with_backoff(conn, pol, paths):
    slept = []
    script = [agent.ClaudeAPIError("rate_limit", True, 429), agent.ClaudeAPIError("server", True, 529)]
    client = FakeClient(script + happy_script())
    result = run(conn, pol, paths, client, sleep=slept.append)
    assert result.status == "완료"
    assert len(slept) == 2 and 1 <= slept[0] < 1.5 and 2 <= slept[1] < 2.5
    log = json.loads(result.run_log_path.read_text(encoding="utf-8"))
    assert log["calls"][0] == {**log["calls"][0], "attempts": 3, "errors": ["rate_limit", "server"]}


def test_retries_stop_after_two(conn, pol, paths):
    client = FakeClient([agent.ClaudeAPIError("timeout", True)] * 3)
    result = run(conn, pol, paths, client)
    assert result.status == "오류" and result.report_path is None
    log = json.loads(result.run_log_path.read_text(encoding="utf-8"))
    assert log["error_kind"] == "api_timeout" and log["calls"][-1]["attempts"] == 3


def test_auth_error_is_not_retried(conn, pol, paths):
    slept = []
    client = FakeClient([agent.ClaudeAPIError("auth", False, 401)])
    result = run(conn, pol, paths, client, sleep=slept.append)
    assert result.status == "오류" and slept == [] and len(client.requests) == 1


def test_truncated_answer_is_not_published(conn, pol, paths):
    client = FakeClient([search("프로 전환 이벤트"), {"content": [{"type": "text", "text": "## 현재 과금"}],
                                                   "stop_reason": "max_tokens", "usage": {}}])
    result = run(conn, pol, paths, client)
    assert result.status == "오류" and result.report_path is None


class _Status(Exception):
    def __init__(self, status_code):
        self.status_code = status_code


class APITimeoutError(Exception):
    pass


class APIConnectionError(Exception):
    pass


@pytest.mark.parametrize("error, kind, retryable", [
    (_Status(429), "rate_limit", True),
    (_Status(500), "server", True),
    (_Status(529), "server", True),
    (APITimeoutError(), "timeout", True),
    (APIConnectionError(), "network", True),
    (_Status(400), "bad_request", False),
    (_Status(401), "auth", False),
    (_Status(403), "auth", False),
    (ValueError("요청 본문"), "unknown", False),
])
def test_classify_error(error, kind, retryable):
    classified = agent.classify_error(error)
    assert (classified.kind, classified.retryable) == (kind, retryable)
    assert "요청 본문" not in str(classified)  # 오류 원문은 담지 않는다


# --- 검증과 다시 쓰기(AGT-05) ---

def test_rewrite_once_after_validation_failure(conn, pol, paths):
    bad = lambda request: good_draft(request, extra=" 지난달 사용량은 42000회였습니다.")  # noqa: E731
    client = FakeClient(happy_script()[:2] + [bad, good_draft])
    result = run(conn, pol, paths, client)
    assert result.status == "완료"
    rewrite = client.requests[-1]
    body = json.dumps(rewrite, ensure_ascii=False)
    assert "검증에서 아래 문제가 나왔습니다" in body
    assert "42000" not in body and "[숫자]" in body  # 지어낸 숫자를 다시 내보내지 않음
    entries = [e for e in audit.read(paths["audit_path"]) if e["event"] == "external_send"]
    assert entries[-1]["kinds"] == ["card", "documents", "rewrite"]
    validation = [e for e in audit.read(paths["audit_path"]) if e["event"] == "validation"][0]
    assert validation["rewrites"] == 1 and validation["result"] == "통과"


def test_second_failure_rejects_without_publishing(conn, pol, paths):
    bad = lambda request: good_draft(request, extra=" 사용량이 4.2배입니다.")  # noqa: E731
    client = FakeClient(happy_script()[:2] + [bad, bad])
    result = run(conn, pol, paths, client)
    assert result.status == "거부" and result.report_path is None and result.slack_path is None
    assert any("자리표시자 바깥에 숫자" in p for p in result.problems)
    assert not paths["app_db"].exists() or not history.connect(paths["app_db"]).execute(
        "SELECT COUNT(*) FROM recommendation_history").fetchone()[0]
    assert [e["result"] for e in audit.read(paths["audit_path"]) if e["event"] == "validation"] == ["거부"]


def test_unretrieved_citation_is_rejected(conn, pol, paths):
    def cites_unsearched(request):
        return text(good_draft(request)["content"][0]["text"].replace("[근거: guide_upgrade", "[근거: guide_churn_risk"))
    client = FakeClient(happy_script()[:2] + [cites_unsearched, cites_unsearched])
    result = run(conn, pol, paths, client)
    assert result.status == "거부"
    assert any("검색하지 않은 조각" in p for p in result.problems)


# --- 보안 장치 ---

def test_guard_block_means_nothing_is_sent(conn, pol, paths, monkeypatch):
    real = guard.situation_card
    monkeypatch.setattr(guard, "situation_card", lambda *a, **k: real(*a, **k) + "\n메모: (주)베타소프트")
    client = FakeClient(happy_script())
    result = run(conn, pol, paths, client)
    assert result.status == "오류" and client.requests == []
    log = json.loads(result.run_log_path.read_text(encoding="utf-8"))
    assert log["error_kind"] == "guard_blocked"
    blocked = [e for e in audit.read(paths["audit_path"]) if e.get("result") == "차단"]
    assert blocked and blocked[0]["rules"] == ["규칙 1 고객 이름"]


def test_model_outside_list_does_nothing(conn, pol, paths):
    client = FakeClient(happy_script())
    with pytest.raises(policy.PolicyError):
        agent.run("user_002", client=client, model="claude-opus-5-5", conn=conn, policy=pol, **paths)
    assert client.requests == [] and not paths["audit_path"].exists()


def test_run_log_has_no_secrets_or_names(conn, pol, paths, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only-api-key-value")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://example.invalid/test-webhook")
    result = run(conn, pol, paths, FakeClient(happy_script()))
    for path in paths["output_dir"].rglob("*.json"):
        body = path.read_text(encoding="utf-8")
        assert "test-only-api-key-value" not in body and "test-webhook" not in body
    log = result.run_log_path.read_text(encoding="utf-8")
    assert "베타소프트" not in log and "user_002" not in log


def test_mask_free_digits_keeps_placeholders_citations_and_alias():
    masked = agent.mask_free_digits("C1 고객 {saving_12m} [근거: promo_pro_upgrade#1] 42,000회 2~5배")
    assert masked == "C1 고객 {saving_12m} [근거: promo_pro_upgrade#1] [숫자]회 [숫자]~[숫자]배"


# --- Slack 전송(NTF-01~04) ---

def test_send_requires_flag_and_webhook(conn, pol, paths):
    posted = []
    run(conn, pol, paths, FakeClient(happy_script()), send=True, webhook_url=None,
        poster=lambda url, payload: posted.append(payload))
    run(conn, pol, paths, FakeClient(happy_script()), send=False, webhook_url="https://example.invalid/test-webhook",
        poster=lambda url, payload: posted.append(payload))
    assert posted == []
    assert len(list((paths["output_dir"] / "slack").glob("*.json"))) == 2  # JSON은 늘 저장(NTF-01)


def test_same_report_is_sent_once(conn, pol, paths):
    posted = []
    poster = lambda url, payload: posted.append(payload) or 200  # noqa: E731
    first = run(conn, pol, paths, FakeClient(happy_script()), send=True, webhook_url="https://example.invalid/test-webhook",
                poster=poster)
    second = run(conn, pol, paths, FakeClient(happy_script()), send=True, webhook_url="https://example.invalid/test-webhook",
                 poster=poster)
    assert first.sent and not second.sent and len(posted) == 1
    assert "UQM-9GU" in json.dumps(posted[0], ensure_ascii=False)
    sent_log = json.loads(paths["sent_log_path"].read_text(encoding="utf-8"))
    assert len(sent_log) == 1 and "test-webhook" not in paths["sent_log_path"].read_text(encoding="utf-8")
    assert [e["event"] for e in audit.read(paths["audit_path"])].count("slack_sent") == 1


def test_rejected_report_is_never_sent(conn, pol, paths):
    posted = []
    bad = lambda request: good_draft(request, extra=" 4.2배")  # noqa: E731
    run(conn, pol, paths, FakeClient(happy_script()[:2] + [bad, bad]), send=True,
        webhook_url="https://example.invalid/test-webhook", poster=lambda url, payload: posted.append(payload))
    assert posted == []


# --- 보관 기간 정리(SEC-10, 15) ---

def test_purge_removes_expired_files_and_history(conn, pol, paths):
    run(conn, pol, paths, FakeClient(happy_script()))
    out = paths["output_dir"]
    old = time.time() - 100 * 86400
    for folder in ("reports", "slack", "runs"):
        stale = out / folder / "old.json"
        stale.write_text("{}", encoding="utf-8")
        os.utime(stale, (old, old))
    with history.connect(paths["app_db"]) as app:
        app.execute("INSERT INTO recommendation_history (run_id, code, analyzed_at, contract_ended_at) "
                    "VALUES ('old-run', 'AAA-AAA', '2024-01-01', '2025-06-30')")
        app.execute("INSERT INTO recommendation_history (run_id, code, analyzed_at, contract_ended_at) "
                    "VALUES ('recent-end', 'CCC-CCC', '2026-01-01', '2026-06-30')")
        app.commit()
    counts = agent.purge(date(2026, 11, 7), pol, out, paths["app_db"], paths["audit_path"])
    assert counts == {"reports": 1, "slack": 1, "runs": 1, "recommendation_history": 1}
    assert len(list((out / "reports").glob("*"))) == 1  # 새 리포트는 남음
    with history.connect(paths["app_db"]) as app:
        left = {row[0] for row in app.execute("SELECT run_id FROM recommendation_history")}
    assert "old-run" not in left and "recent-end" in left and len(left) == 2
    entry = audit.read(paths["audit_path"])[-1]
    assert entry["event"] == "purge" and entry["reports"] == 1 and entry["base_date"] == "2026-11-07"


# --- 명령줄 ---

def test_cli_refuses_without_api_key(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(agent, "ROOT", tmp_path)  # 실제 .env를 읽지 않게
    monkeypatch.setattr(audit, "AUDIT_PATH", tmp_path / "audit.jsonl")
    assert agent.main(["user_002"]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert [e["event"] for e in audit.read(tmp_path / "audit.jsonl")] == ["policy_loaded"]


def test_cli_refuses_model_outside_list(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "ROOT", tmp_path)
    monkeypatch.setattr(audit, "AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-5-5")
    with pytest.raises(policy.PolicyError):
        agent.main(["user_002"])

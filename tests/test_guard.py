"""외부 AI 전송 보안 장치 검증(SEC-05, 07, 08, 11, 13, 14, ASK-05).

- 정책 파일: 승인 정보가 비면 실행하지 않음, 값 검사, 해시 기록과 "정책 변경" 기록.
- 상황 카드: 범주와 자리표시자 이름만. 203명 전원의 카드에 이름·ID·고객 수치·PC 전용 값이 없음.
- 질문 가림: 형식별 개인정보 예시가 모두 가려지고 숫자가 남지 않음.
- 전송 전 검사: 규칙 1~5와 허용 모델이 각각 차단하고, 감사 기록에 걸린 내용 없이 규칙만 남음.
"""

from __future__ import annotations

import json
import re
import sys
from fractions import Fraction
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "database"))

import audit  # noqa: E402
import data_access  # noqa: E402
import guard  # noqa: E402
import init_db  # noqa: E402
import knowledge  # noqa: E402
import mcp_server  # noqa: E402
import policy  # noqa: E402
import report  # noqa: E402
import simulator  # noqa: E402

MODEL = "claude-sonnet-5-5"
ALL_USERS = [f"user_{n:03d}" for n in range(1, 204)]
POLICY_TEXT = policy.POLICY_PATH.read_text(encoding="utf-8")


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


@pytest.fixture(scope="module")
def registry():
    return report.load_registry()


@pytest.fixture(scope="module")
def sims(conn):
    return {user_id: simulator.simulate(conn, user_id) for user_id in ALL_USERS}


@pytest.fixture(scope="module")
def screen(conn, pol, registry):
    return guard.Screen.build(conn, pol, registry)


def _policy_file(tmp_path, old: str, new: str) -> Path:
    assert old in POLICY_TEXT
    path = tmp_path / "data_policy.toml"
    path.write_text(POLICY_TEXT.replace(old, new, 1), encoding="utf-8")
    return path


def _request(card: str, extra: str = "") -> dict:
    return {"model": MODEL, "system": "요금제 리포트를 씁니다. 숫자 대신 자리표시자만 씁니다.",
            "messages": [{"role": "user", "content": card + extra}]}


# --- 정책 파일(SEC-14) ---

def test_policy_loads_with_hash_and_sections(pol):
    assert re.fullmatch(r"[0-9a-f]{64}", pol.sha256)
    assert pol.allowed_models == (MODEL,)
    assert pol.overage_multiples == (1, 2, 5)
    assert pol.retention_days["audit"] == 730
    assert pol.selection["max_alerts_per_month"] > 0
    assert pol.peak_alerts["first_notice_days_before"] == 45
    labels = {topic["label"] for topic in pol.risk_topics.values()}
    assert labels == {"해지·위약금", "환불", "계약 예외", "분쟁·법적 문제", "다른 고객 정보", "결제 오류", "개인정보 변경"}


@pytest.mark.parametrize("key", ["approved_by", "approved_at", "reason"])
def test_policy_refuses_to_run_without_approval(tmp_path, key):
    line = next(line for line in POLICY_TEXT.splitlines() if line.startswith(f"{key} ="))
    path = _policy_file(tmp_path, line, f'{key} = ""')
    with pytest.raises(policy.PolicyError, match="승인 정보"):
        policy.load(path)


@pytest.mark.parametrize("old, new, message", [
    ("send_customer_numbers = false", "send_customer_numbers = true", "false만"),
    ("send_ratios = false", "send_ratios = true", "false만"),
    ('allowed_models = ["claude-sonnet-5-5"]', "allowed_models = []", "allowed_models"),
    ("overage_multiples = [1, 2, 5]", "overage_multiples = [1, 5, 2]", "순서"),
    ("overage_multiples = [1, 2, 5]", "overage_multiples = [2, 5]", "1\\(기본 제공량\\)"),
    ("saving_pct = [10, 50]", "saving_pct = [10, 120]", "100보다"),
    ("stability_change_pct = 20", "stability_change_pct = 0", "1~99"),
    ("audit = 730", "audit = -1", "retention_days.audit"),
    ("exclude_low_data = true", 'exclude_low_data = "yes"', "exclude_low_data"),
    ('keywords = ["환불", "돌려받", "반환"]', "keywords = []", "risk_topics.refund"),
    ('approved_at = "2026-11-07"', 'approved_at = "11/07"', "YYYY-MM-DD"),
])
def test_policy_rejects_bad_values(tmp_path, old, new, message):
    with pytest.raises(policy.PolicyError, match=message):
        policy.load(_policy_file(tmp_path, old, new))


def test_policy_records_hash_and_change(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    first = policy.load()
    assert [e["event"] for e in policy.record_start(first, audit_path)] == ["policy_loaded"]
    assert [e["event"] for e in policy.record_start(first, audit_path)] == ["policy_loaded"]  # 같으면 변경 아님

    changed = policy.load(_policy_file(tmp_path, 'reason = "초기 정책"', 'reason = "절감률 하한 조정"'))
    entries = policy.record_start(changed, audit_path)
    assert [e["event"] for e in entries] == ["policy_changed", "policy_loaded"]
    assert entries[0]["before_sha256"] == first.sha256
    assert entries[0]["after_sha256"] == changed.sha256
    assert entries[0]["approved_by"] == "정보보호책임자" and entries[0]["reason"] == "절감률 하한 조정"


def test_policy_matches_simulator_constant(pol):
    assert pol.stability_change_pct == simulator.STABILITY_CHANGE_PCT


def test_check_model(pol):
    guard.check_model(pol, MODEL)
    with pytest.raises(policy.PolicyError, match="허용 모델"):
        guard.check_model(pol, "claude-opus-5-5")


# --- 별칭(SEC-05) ---

def test_aliases_are_per_run():
    run = guard.Aliases()
    assert run.alias("user_002") == "C1"
    assert run.alias("user_007") == "C2"
    assert run.alias("user_002") == "C1"
    assert run.user_id("C2") == "user_007"
    with pytest.raises(guard.GuardError):
        run.user_id("C3")
    assert guard.Aliases().alias("user_007") == "C1"  # 새 실행은 새로 센다


# --- 범주 ---

@pytest.mark.parametrize("used, expected", [
    (10000, "기본 제공량 이내"),
    (10001, "기본 제공량의 2배 미만"),
    (20000, "기본 제공량의 2~5배"),
    (49999, "기본 제공량의 2~5배"),
    (50000, "기본 제공량의 5배 이상"),
])
def test_overage_category_bounds(used, expected):
    assert guard.overage_category(used, 10000, (Fraction(1), Fraction(2), Fraction(5))) == expected


def test_overage_category_single_band():
    assert guard.overage_category(11, 10, (Fraction(1),)) == "기본 제공량 초과"


@pytest.mark.parametrize("saving, total, expected", [
    (0, 1000, "없음"),
    (99, 1000, "10% 미만"),
    (100, 1000, "10~50%"),
    (499, 1000, "10~50%"),
    (500, 1000, "50% 이상"),
])
def test_saving_category_bounds(saving, total, expected):
    assert guard.saving_category(saving, total, (Fraction(10), Fraction(50))) == expected


# --- 상황 카드(SEC-07) ---

def test_card_for_user_002_matches_security_example(sims, pol, registry):
    card = guard.situation_card(sims["user_002"], "C1", pol, registry)
    for line in (
        "고객: C1",
        '현재 요금제: 베이직 → 추천: 프로 (상향) + 이벤트 "프로 전환 3개월 30% 할인"',
        "초과 정도(최근 사용량 기준): API 사용량은 기본 제공량의 2~5배, 스토리지 사용량은 기본 제공량의 2~5배",
        "절감 규모: 10~50%",
        "추천 안정성: 안정 (사용량 ±20% 기준)",
        "이탈 위험: 없음 / 기록 부족: 없음",
        '  - "베이직 2년 이상 고객 2개월 1만 원 할인": 가입 기간 조건 미달',
        '  - "여름 프로 50% 할인(종료)": 이벤트 기간 아님',
        "  {saving_12m} 12개월 절감액",
    ):
        assert line in card.splitlines()


def test_card_for_seasonal_and_keep_customers(sims, pol, registry):
    card = guard.situation_card(sims["user_201"], "C1", pol, registry)
    assert "예측 방법: 계절 반영" in card
    assert "성수기: 있음(증가 2배 이상) / 다가오는 성수기: 있음" in card
    assert "{peak_deadline}" in card
    keep = guard.situation_card(sims["user_202"], "C1", pol, registry)
    assert '추천: 현재 요금제 유지 + 이벤트 "베이직 2년 이상 고객 2개월 1만 원 할인"' in keep
    none = guard.situation_card(sims["user_203"], "C1", pol, registry)
    assert "성수기: 없음 / 다가오는 성수기: 없음" in none


def test_card_for_no_usage_customer(sims, pol, registry):
    card = guard.situation_card(sims["user_010"], "C1", pol, registry)
    assert "상태: 이용 기록 없음" in card
    assert "절감 규모" not in card


def test_cards_never_carry_identity_or_customer_numbers(conn, sims, pol, registry, screen):
    names = data_access.customer_names(conn)
    codes = data_access.customer_codes(conn)
    for user_id, sim in sims.items():
        card = guard.situation_card(sim, "C1", pol, registry)
        assert screen.check(_request(card)) == [], user_id
        assert user_id not in card and not any(code in card for code in codes)
        assert not any(name in card for name in names)
        values = report.values(sim, registry)
        # 공개된 고정 문구(자리표시자 설명, 이벤트 이름, 예측 방법 "3개월 평균")는 고객 값이 아니다.
        # (T3d) 남은 개월 수 "2개월"·"12개월"이 이런 문구와 우연히 겹쳐서 빼고 본다.
        variable = card.split("쓸 수 있는 자리표시자")[0]
        for fixed in [p.promo_name for p in data_access.load_promotions(conn)] + ["3개월 평균"]:
            variable = variable.replace(fixed, "")
        for key, spec in registry["keys"].items():
            if values[key] is None:
                continue
            if spec.get("local_only") or spec["format"] in ("won", "date"):
                # 앞이 숫자가 아닐 때만 같은 값으로 본다("12개월" 안의 "2개월"은 다른 값)
                assert not re.search(r"(?<![\d.,])" + re.escape(values[key]), variable), (user_id, key)
        assert not re.search(r"현재 \d+개월", card)  # 가입 기간이 든 원래 이유 문구가 없음


def test_local_only_values_stay_on_pc(sims, pol, registry):
    sim = sims["user_201"]
    card = guard.situation_card(sim, "C1", pol, registry)
    upcoming = sim["seasonality"]["upcoming"]
    for value in (upcoming["label"], upcoming["deadline"], f"{upcoming['current_bill']:,}", "5.5"):
        assert value not in card


def test_card_refuses_unknown_values(sims, pol, registry):
    sim = json.loads(json.dumps(sims["user_002"]))
    sim["options"][0]["skipped_promotions"][0]["reason"] = "새로운 이유(현재 19개월)"
    with pytest.raises(guard.GuardError, match="이유"):
        guard.situation_card(sim, "C1", pol, registry)
    sim = json.loads(json.dumps(sims["user_002"]))
    sim["recommendation"]["action"] = "cancel"
    with pytest.raises(guard.GuardError, match="추천 종류"):
        guard.situation_card(sim, "C1", pol, registry)
    sim = json.loads(json.dumps(sims["user_002"]))
    sim["stability"]["change_pct"] = 30
    with pytest.raises(guard.GuardError, match="정책 파일"):
        guard.situation_card(sim, "C1", pol, registry)


def test_mcp_server_uses_same_reason_table():
    assert mcp_server.REASON_CATEGORIES is guard.REASON_CATEGORIES


# --- 질문 가림(ASK-05) ---

@pytest.mark.parametrize("question", [
    "제 번호 010-1234-5678로 연락 주세요",
    "사무실 02-123-4567로 전화 주세요",
    "메일은 kim.manager@beta-soft.co.kr 입니다",
    "사업자등록번호 123-45-67890 기준으로 봐주세요",
    "주민번호 900101-1234567 확인 부탁드립니다",
    "환불 계좌 110-123-456789 로 보내주세요",
    "(주)베타소프트 담당자입니다",
    "㈜베타소프트 요금 문의",
    "베타소프트에서 문의드립니다",
    "지난달 129,000원이 나왔는데 30만원 넘으면 어떻게 되나요",
])
def test_mask_question_hides_each_format(conn, question):
    masked = guard.mask_question(question, data_access.customer_names(conn))
    assert not re.search(r"\d", masked)
    assert "베타소프트" not in masked and "@" not in masked
    assert "[가림]" in masked or "[숫자]" in masked


def test_mask_keeps_ordinary_words(conn):
    masked = guard.mask_question("프로 요금제로 바꾸면 이벤트를 받을 수 있나요?", data_access.customer_names(conn))
    assert masked == "프로 요금제로 바꾸면 이벤트를 받을 수 있나요?"


# --- 전송 전 검사(SEC-08) ---

@pytest.fixture()
def card(sims, pol, registry):
    return guard.situation_card(sims["user_002"], "C1", pol, registry)


def test_clean_request_passes(screen, card):
    request = _request(card)
    request["messages"].append({"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_01XYZ987654", "name": "search_knowledge", "input": {"query": "프로 전환"}}]})
    request["messages"].append({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_01XYZ987654", "content": "<문서>프로 전환 3개월 30% 할인</문서>"}]})
    assert screen.check(request) == []


def test_knowledge_documents_pass_the_screen(screen):
    for path in sorted(knowledge.KNOWLEDGE_DIR.glob("*.md")):
        assert screen.check({"content": path.read_text(encoding="utf-8")}) == [], path.name


@pytest.mark.parametrize("extra, rule", [
    ("\n참고: (주)감마테크", "규칙 1"),          # 분석 중이 아닌 다른 고객 이름도 막는다
    ("\n참고: 델타랩", "규칙 1"),
    ("\n고객 user_002", "규칙 2"),
    ("\n코드 UQM-9GU", "규칙 2"),
    ("\n최근 API 42000회", "규칙 3"),
    ("\n가입 19개월", "규칙 3"),
    ("\n이용량 4.2배", "규칙 3"),
    ("\n연락처 kim@beta.co.kr", "규칙 4"),
    ("\n연락처 010-1234-5678", "규칙 4"),
])
def test_each_rule_blocks(screen, card, extra, rule):
    hits = screen.check(_request(card, extra))
    assert any(hit.startswith(rule) for hit in hits), hits


def test_unmasked_question_is_blocked_and_masked_passes(screen, card):
    raw = "프로로 바꾸면 29만원보다 싸지나요?"
    blocked = screen.check(_request(card, "\n고객 질문: " + raw), raw_questions=[raw])
    assert "규칙 5 가림 전 질문 원문" in blocked
    masked = screen.mask(raw)
    assert screen.check(_request(card, "\n고객 질문: " + masked), raw_questions=[raw]) == []


def test_preflight_blocks_and_audits_without_content(tmp_path, screen, pol, card):
    audit_path = tmp_path / "audit.jsonl"
    with pytest.raises(guard.GuardBlocked) as caught:
        guard.preflight(_request(card, "\n(주)베타소프트 42000회"), screen=screen, policy=pol, run_id="run-1",
                        kinds=["card"], audit_path=audit_path)
    assert "베타소프트" not in str(caught.value) and "42000" not in str(caught.value)
    text = audit_path.read_text(encoding="utf-8")
    assert "베타소프트" not in text and "42000" not in text
    entry = audit.read(audit_path)[-1]
    assert entry["event"] == "external_send" and entry["result"] == "차단"
    assert entry["rules"] == ["규칙 1 고객 이름", "규칙 3 허용 목록 밖 숫자"]
    assert re.fullmatch(r"[0-9a-f]{64}", entry["request_sha256"]) and entry["request_bytes"] > 0


def test_preflight_blocks_model_outside_list(tmp_path, screen, pol, card):
    request = {**_request(card), "model": "claude-opus-5-5"}
    with pytest.raises(guard.GuardBlocked) as caught:
        guard.preflight(request, screen=screen, policy=pol, run_id="run-1", kinds=["card"],
                        audit_path=tmp_path / "audit.jsonl")
    assert caught.value.rules == ["SEC-13 허용 모델 밖"]


def test_preflight_pass_and_record_sent(tmp_path, screen, pol, card):
    audit_path = tmp_path / "audit.jsonl"
    request = _request(card)
    ticket = guard.preflight(request, screen=screen, policy=pol, run_id="run-2", kinds=["card", "documents"],
                             audit_path=audit_path)
    assert not audit_path.exists()  # 통과는 호출이 끝난 뒤 토큰과 함께 한 줄로 남긴다
    body = guard.canonical(request)
    assert ticket["request_bytes"] == len(body)
    entry = guard.record_sent(ticket, audit_path, input_tokens=1200, output_tokens=800)
    assert entry["result"] == "통과" and entry["kinds"] == ["card", "documents"] and entry["model"] == MODEL
    assert entry["input_tokens"] == 1200
    assert "베이직" not in audit_path.read_text(encoding="utf-8")  # 본문은 남기지 않는다


def test_customer_name_loader_covers_everyone(conn):
    assert len(data_access.customer_names(conn)) == len(ALL_USERS)
    assert len(set(data_access.customer_codes(conn))) == len(ALL_USERS)

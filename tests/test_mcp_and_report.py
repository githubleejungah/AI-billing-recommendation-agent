"""MCP 서버 도구와 리포트 검증 테스트. API 키 없이 돈다."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import closing
from pathlib import Path

import pytest
from mcp import Client

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "database"))

import init_db  # noqa: E402
import mcp_server  # noqa: E402
import report  # noqa: E402
import simulator  # noqa: E402


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "billing_mock.db"
    init_db.build(path)
    return path


def call(db_path, name, arguments=None):
    async def run():
        async with Client(mcp_server.build_server(db_path)) as client:
            tools = await client.list_tools()
            result = await client.call_tool(name, arguments or {})
            return [tool.name for tool in tools.tools], result
    return asyncio.run(run())


def simulation(db_path, user_id):
    with closing(simulator.connect(db_path)) as conn:
        return simulator.simulate(conn, user_id)


# --- MCP 서버 ---

def test_only_fixed_read_tools(db_path):
    names, _ = call(db_path, "list_plans")
    assert sorted(names) == ["get_customer_usage", "list_plans", "list_promotions", "search_knowledge", "simulate_plans"]


def test_simulate_tool_matches_simulator_without_name(db_path):
    _, result = call(db_path, "simulate_plans", {"user_id": "user_002"})
    data = json.loads(result.content[0].text)
    expected = simulation(db_path, "user_002")
    assert "name" not in data
    assert data["recommendation"] == expected["recommendation"]


def test_customer_tool_hides_name(db_path):
    _, result = call(db_path, "get_customer_usage", {"user_id": "user_002"})
    data = json.loads(result.content[0].text)
    assert "name" not in data and len(data["usage_history"]) == 6
    assert "베타소프트" not in result.content[0].text


def test_unknown_customer_tool_result(db_path):
    _, result = call(db_path, "simulate_plans", {"user_id": "user_999"})
    assert json.loads(result.content[0].text)["error"] == "customer_not_found"


@pytest.mark.parametrize("tool", ["simulate_plans", "get_customer_usage", "search_knowledge"])
def test_rejects_malformed_user_id(db_path, tool):
    # 형식이 틀린 ID는 DB에 닿기 전에 막고, 이유가 담긴 JSON 오류로 돌려준다. 받은 입력값은 되돌려 보내지 않는다.
    arguments = {"user_id": "user_002' OR 1=1 --"}
    if tool == "search_knowledge":
        arguments["query"] = "프로 전환 할인"
    _, result = call(db_path, tool, arguments)
    assert not result.is_error
    data = json.loads(result.content[0].text)
    assert data["error"] == "invalid_user_id"
    assert "OR 1=1" not in result.content[0].text


# --- 리포트 검증(REP-01~05) ---
# Claude는 숫자 대신 자리표시자를 쓰고, 코드가 검증한 뒤 채운다. 기대값은 user_002의 손 계산 값이다
# (베이직 월 129,000원, 프로 + 전환 3개월 30% 할인, 12개월 절감 660,000원(43%)).

GOOD_002 = """## 1. 현재 과금 분석
현재 {current_plan} 요금제로 월 {current_monthly}이 예상됩니다. 최근 {forecast_months} 평균 API {avg_api_calls}, 스토리지 {avg_storage_gb}를 사용해
기본 제공량({included_api_calls}, {included_storage_gb})을 크게 넘습니다.
## 2. 추천
{rec_plan} 요금제로 바꾸고 {promo_name}을 받으세요. {stability}. [근거: promo_pro_upgrade#4]
## 3. 절감액
월 {saving_monthly}, {horizon_months} 기준 {saving_12m}({saving_pct})을 절감합니다.
## 4. 응대 안내
먼저 초과 원인을 보여 주고 절감액을 설명합니다. [근거: guide_upgrade#1]
"""


def search(db_path, query, user_id):
    _, result = call(db_path, "search_knowledge", {"query": query, "top_k": 3, "user_id": user_id})
    return {item["chunk_id"]: item for item in json.loads(result.content[0].text)["results"]}


@pytest.fixture(scope="module")
def sim_002(db_path):
    return simulation(db_path, "user_002")


@pytest.fixture(scope="module")
def retrieved_002(db_path):
    found = {}
    for query in ("프로 전환 할인 조건", "상향 추천 응대 설명 순서", "베이직 장기 고객 할인"):
        found.update(search(db_path, query, "user_002"))
    return found


def test_good_draft_is_filled_with_simulator_values(sim_002, retrieved_002):
    text = report.verify_and_fill(GOOD_002, sim_002, retrieved_002)
    assert "베이직 요금제로 월 129,000원이 예상됩니다" in text
    assert "최근 3개월 평균 API 42,000회, 스토리지 120GB" in text
    assert "프로 요금제로 바꾸고 프로 전환 3개월 30% 할인을 받으세요" in text
    assert "월 49,000원, 12개월 기준 660,000원(43%)을 절감합니다" in text
    assert "사용량이 20% 줄거나 늘어도 같은 추천입니다" in text
    assert "{" not in text


# 아키텍처 검토(2026-11-07)의 실험 4가지: 예전 방식(숫자 대조)에서는 통과하거나 잘못 거부되던 경우다.
@pytest.mark.parametrize("bad, reason", [
    ("프로는 330,000원, 현재는 80,000원입니다.", "자리표시자 바깥에 숫자"),   # ① 금액 자리 바꿔치기
    ("요금이 12% 줄어듭니다.", "자리표시자 바깥에 숫자"),                     # ② 지어낸 작은 비율
    ("약 307만 원을 아낍니다.", "자리표시자 바깥에 숫자"),                    # ③ 한국어 금액 표기
    ("월 요금은 38% 줄어듭니다.", "자리표시자 바깥에 숫자"),                  # ④ 모델이 직접 계산한 비율
    ("삼십만 원 정도 줄어듭니다.", "한글로 쓴 금액"),
])
def test_numbers_written_by_ai_are_rejected(sim_002, retrieved_002, bad, reason):
    problems = report.check_draft(GOOD_002 + bad + "\n", sim_002, retrieved_002)
    assert any(reason in p for p in problems), problems


def test_unknown_and_empty_keys_are_rejected(db_path, sim_002, retrieved_002):
    problems = report.check_draft(GOOD_002 + "{discount_rate}\n", sim_002, retrieved_002)
    assert any("등록되지 않은 자리표시자: discount_rate" in p for p in problems)
    # user_007은 이벤트가 없다 → {promo_name}을 쓰면 값이 없는 키
    sim_007 = simulation(db_path, "user_007")
    problems = report.check_draft(GOOD_002, sim_007, retrieved_002)
    assert any("값이 없는 자리표시자" in p and "promo_name" in p for p in problems)


def test_unit_after_key_is_rejected(sim_002, retrieved_002):
    problems = report.check_draft(GOOD_002 + "{saving_12m}원\n", sim_002, retrieved_002)
    assert any("단위" in p for p in problems)


@pytest.mark.parametrize("remove, expected", [
    ("{promo_name}", "{promo_name}"),
    ("{saving_12m}", "{saving_12m}"),
    ("{current_monthly}", "{current_monthly}"),
])
def test_missing_required_key_is_rejected(sim_002, retrieved_002, remove, expected):
    problems = report.check_draft(GOOD_002.replace(remove, "그만큼"), sim_002, retrieved_002)
    assert any(expected in p for p in problems), problems


def test_usage_evidence_is_required(sim_002, retrieved_002):
    draft = GOOD_002.replace("API {avg_api_calls}, 스토리지 {avg_storage_gb}를", "많이")
    assert any("근거 사용량" in p for p in report.check_draft(draft, sim_002, retrieved_002))


def test_list_numbers_and_alias_are_allowed(sim_002, retrieved_002):
    draft = GOOD_002 + "1. 고객 C1에게 먼저 연락합니다.\n"
    assert report.check_draft(draft, sim_002, retrieved_002) == []


# --- 인용(REP-04, 05) ---

def test_citation_must_be_retrieved(sim_002, retrieved_002):
    draft = GOOD_002.replace("[근거: guide_upgrade#1]", "[근거: guide_downgrade#1]")
    problems = report.check_draft(draft, sim_002, retrieved_002)
    assert any("검색하지 않은 조각 인용: guide_downgrade#1" in p for p in problems)


def test_ineligible_promotion_cannot_back_recommendation(sim_002, retrieved_002):
    draft = GOOD_002.replace("[근거: promo_pro_upgrade#4]", "[근거: promo_basic_loyal#2]")
    problems = report.check_draft(draft, sim_002, retrieved_002)
    assert any("받을 수 없는 이벤트의 조각을 추천 근거로 인용" in p for p in problems)
    # 응대 안내에서 "왜 이 이벤트는 안 되는지" 설명하는 데는 쓸 수 있다
    ok = GOOD_002.replace("[근거: guide_upgrade#1]", "[근거: guide_upgrade#1] [근거: promo_basic_loyal#2]")
    assert report.check_draft(ok, sim_002, retrieved_002) == []


@pytest.mark.parametrize("citation, section", [("[근거: promo_pro_upgrade#4]", "추천"), ("[근거: guide_upgrade#1]", "응대 안내")])
def test_each_section_needs_a_citation(sim_002, retrieved_002, citation, section):
    problems = report.check_draft(GOOD_002.replace(citation, ""), sim_002, retrieved_002)
    assert any(f"'{section}' 구역에 근거 인용이 없음" in p for p in problems)


def test_rejected_draft_raises(sim_002, retrieved_002):
    with pytest.raises(report.ReportRejected) as error:
        report.verify_and_fill(GOOD_002 + "700,000원\n", sim_002, retrieved_002)
    assert error.value.problems


def test_second_line_catches_bad_fill(sim_002, retrieved_002, monkeypatch):
    """채우는 코드에 버그가 있어 시뮬레이터에 없는 숫자가 들어가도 내보내지 않는다(두 번째 방어선)."""
    monkeypatch.setattr(report, "format_value", lambda value, fmt: "777,777원" if fmt == "won" else str(value))
    with pytest.raises(report.ReportRejected) as error:
        report.verify_and_fill(GOOD_002, sim_002, retrieved_002)
    assert any("777777" in p for p in error.value.problems)


def test_number_formats_are_normalized():
    assert report.numbers_in("1,234원, 120.0GB, 12.50%, user_002, 2026-10") == ["1234", "120", "12.5", "2026", "10"]


def test_no_usage_report(db_path):
    sim = simulation(db_path, "user_010")
    assert report.check_draft("이용 기록이 없어 추천할 수 없습니다. 현재 {current_plan} 요금제입니다.", sim, {}) == []
    assert report.check_draft("{rec_plan} 요금제를 추천합니다.", sim, {})


# --- 자리표시자 등록 파일(5.3) ---

def test_registry_matches_simulator_facts(db_path):
    registry = report.load_registry()
    known = set()
    for user_id in ("user_002", "user_007", "user_015", "user_201", "user_010"):
        known |= set(report.facts(simulation(db_path, user_id)))
    missing = [name for name, spec in registry["keys"].items() if spec["source"] not in known]
    assert missing == []


def test_registry_keys_from_plan(db_path):
    keys = set(report.load_registry()["keys"])
    assert {"current_plan", "current_monthly", "rec_plan", "promo_name", "saving_12m", "skipped_promo_reasons",
            "peak_months", "forecast_method", "stability"} <= keys
    local = {k for k, spec in report.load_registry()["keys"].items() if spec.get("local_only")}
    assert {"skipped_promo_reasons", "peak_months", "peak_evidence"} <= local


def test_bad_registry_is_refused(tmp_path):
    path = tmp_path / "placeholders.toml"
    path.write_text('[keys.x]\nsource = "current.total"\nformat = "yen"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        report.load_registry(path)


def test_seasonal_values(db_path):
    values = report.values(simulation(db_path, "user_201"), report.load_registry())
    assert values["peak_months"] == "1~3월, 5월"
    assert values["peak_deadline"] == "2026-12-31"
    assert values["peak_bill_current"] == "855,000원"
    assert values["peak_evidence"] == "2025-01 평소의 5.0배, 2026-01 5.5배"
    assert values["forecast_method"] == "계절 반영"


# --- Slack ---

def test_slack_message_uses_code_not_name(db_path, sim_002, retrieved_002):
    text = report.verify_and_fill(GOOD_002, sim_002, retrieved_002)
    message = report.slack_message(text, sim_002, "UQM-9GU")
    assert message["blocks"][0]["type"] == "header"
    dumped = json.dumps(message, ensure_ascii=False)
    assert "UQM-9GU" in dumped and "베타소프트" not in dumped and "user_002" not in dumped
    assert "660,000원" in dumped and "**" not in dumped
    assert all(len(block.get("text", {}).get("text", "")) <= 3000 for block in message["blocks"])

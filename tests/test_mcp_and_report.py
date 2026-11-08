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
# (T3d 장기 혜택 반영: 베이직 월 124,500원(2단계 제공 11,000회·55GB), 프로 + 전환 3개월 30% 할인,
#  월 절감 43,833원, 12개월 절감 598,000원(40%). 계산식은 test_simulator.py).

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
    assert "베이직 요금제로 월 124,500원이 예상됩니다" in text
    assert "최근 3개월 평균 API 42,000회, 스토리지 120GB" in text
    assert "기본 제공량(11,000회, 55GB)" in text  # 지금 받는 장기 혜택(2단계) 반영
    assert "프로 요금제로 바꾸고 프로 전환 3개월 30% 할인을 받으세요" in text
    assert "월 43,833원, 12개월 기준 598,000원(40%)을 절감합니다" in text
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
    found = search(db_path, "이용 기록이 없는 고객 첫 사용 지원", "user_010")
    guide = next(i for i in found if i.startswith("guide_no_usage"))
    good = f"이용 기록이 없어 추천할 수 없습니다. 현재 {{current_plan}} 요금제입니다.\n## 응대 안내\n온보딩 상담을 제안합니다. [근거: {guide}]"
    assert report.check_draft(good, sim, found) == []
    assert report.check_draft("{rec_plan} 요금제를 추천합니다.", sim, {})


def test_no_usage_report_needs_onboarding_citation(db_path):
    """(v3.5, REP-03·05) 이용 기록이 없는 고객의 응대 안내에는 검색한 근거 인용이 있어야 하고, 이벤트 문서는 인용하지 않는다."""
    sim = simulation(db_path, "user_010")
    found = search(db_path, "이용 기록이 없는 고객 첫 사용 지원", "user_010")
    found.update(search(db_path, "프로 전환 할인 조건", "user_010"))
    guide = next(i for i in found if i.startswith("guide_no_usage"))
    promo = next(i for i in found if i.startswith("promo_"))
    head = "이용 기록이 없어 추천하지 않습니다. 현재 {current_plan} 요금제입니다.\n## 응대 안내\n"

    no_citation = report.check_draft(head + "온보딩 상담을 제안합니다.", sim, found)
    assert any("'응대 안내' 구역에 근거 인용이 없음" in p for p in no_citation)
    not_searched = report.check_draft(head + "온보딩 상담을 제안합니다. [근거: guide_no_usage#9]", sim, found)
    assert any("검색하지 않은 조각" in p for p in not_searched)
    with_promo = report.check_draft(head + f"온보딩 상담을 제안합니다. [근거: {guide}] [근거: {promo}]", sim, found)
    assert any("이벤트 문서를 인용" in p for p in with_promo)
    assert report.check_draft(head + f"온보딩 상담을 제안합니다. [근거: {guide}]", sim, found) == []


def test_no_usage_report_can_mention_horizon(db_path):
    """(v3.4, T6에서 발견) 이용 기록이 없는 고객도 {horizon_months}를 쓰면 두 번째 방어선을 통과해야 한다.
    비교 기간 12개월은 시뮬레이터 결과(horizon_months)에서 오므로 채운 "12"가 허용된다."""
    sim = simulation(db_path, "user_010")
    assert sim["status"] == "no_usage" and sim["horizon_months"] == 12
    found = search(db_path, "이용 기록이 없는 고객 첫 사용 지원", "user_010")
    guide = next(i for i in found if i.startswith("guide_no_usage"))
    tail = f"\n## 응대 안내\n온보딩 상담을 제안합니다. [근거: {guide}]"  # (v3.5) 응대 안내 인용 필수
    text = report.verify_and_fill("이용 기록이 없어 {horizon_months} 비교를 하지 않습니다. 현재 {current_plan} 요금제입니다."
                                  + tail, sim, found)
    assert text.startswith("이용 기록이 없어 12개월 비교를 하지 않습니다. 현재 베이직 요금제입니다.")


# --- 자리표시자 등록 파일(5.3) ---

# --- 이탈 위험 고객의 장기 이용 혜택 안내(REP-06, v3.5) ---

CHURN_DRAFT = """## 1. 현재 과금 분석
현재 {current_plan} 요금제로 월 {current_monthly} 수준이며 최근 평균 API {avg_api_calls} 정도를 씁니다.
## 2. 추천
{rec_plan} 요금제를 그대로 쓰는 것이 맞습니다. [근거: {keep}]
## 3. 응대 안내
최근 접속이 크게 줄었습니다. 먼저 불편한 점을 묻습니다. 장기 이용 혜택은 {loyalty_tier} 입니다.{extra} [근거: {churn}]
"""


@pytest.fixture(scope="module")
def churn_case(db_path):
    sim = simulation(db_path, "user_015")  # 이탈 위험, 가입 44개월(최고 단계)
    found = {}
    for query in ("유지 추천 응대", "이탈 위험 고객 응대"):
        found.update(search(db_path, query, "user_015"))
    keep = next(i for i in found if i.startswith("guide_keep"))
    churn = next(i for i in found if i.startswith("guide_churn_risk"))
    return sim, found, {"keep": keep, "churn": churn}


def _churn_draft(ids, extra="", where="guide"):
    draft = CHURN_DRAFT.replace("{keep}", ids["keep"]).replace("{churn}", ids["churn"]).replace("{extra}", extra)
    if where == "recommendation":  # 혜택 안내를 추천 구역으로 옮긴 초안
        draft = draft.replace(" 장기 이용 혜택은 {loyalty_tier} 입니다.", "").replace(
            "맞습니다. [근거", "맞습니다. 장기 이용 혜택은 {loyalty_tier} 입니다. [근거")
    return draft


def test_loyalty_values_for_each_customer(db_path, churn_case):
    registry = report.load_registry()
    top = report.values(churn_case[0], registry)
    assert (top["loyalty_tier"], top["loyalty_next_months"], top["loyalty_next_tier"]) == (
        "3단계(기본 제공량 15% 추가, 최고 단계)", None, None)
    mid = report.values(simulation(db_path, "user_002"), registry)  # 19개월, 5개월 뒤 3단계
    assert (mid["loyalty_tier"], mid["loyalty_next_months"], mid["loyalty_next_tier"]) == (
        "2단계(기본 제공량 10% 추가)", "5개월", "3단계(기본 제공량 15% 추가)")
    new = report.values(simulation(db_path, "user_004"), registry)  # 3개월
    assert (new["loyalty_tier"], new["loyalty_next_months"]) == ("아직 없음(첫 단계는 가입 6개월부터)", "3개월")


def test_churn_risk_report_has_loyalty_placeholders(db_path, churn_case):
    sim, found, ids = churn_case
    registry = report.load_registry()
    # 최고 단계라 남은 개월 수는 값이 없어 필수가 아니다. 이탈 위험이 아닌 고객에게는 필수가 아니다.
    assert "loyalty_tier" in report.required_keys(sim, registry)
    assert "loyalty_next_months" not in report.required_keys(sim, registry)
    assert not {"loyalty_tier", "loyalty_next_months"} & set(report.required_keys(simulation(db_path, "user_002"), registry))

    text = report.verify_and_fill(_churn_draft(ids), sim, found)
    assert "장기 이용 혜택은 3단계(기본 제공량 15% 추가, 최고 단계) 입니다." in text

    missing = _churn_draft(ids).replace(" 장기 이용 혜택은 {loyalty_tier} 입니다.", "")
    assert "필수 자리표시자 없음: {loyalty_tier}" in report.check_draft(missing, sim, found)
    wrong_place = _churn_draft(ids, where="recommendation")
    assert any("응대 안내" in p and "REP-06" in p for p in report.check_draft(wrong_place, sim, found))


def test_churn_risk_below_top_tier_needs_remaining_months(db_path, churn_case):
    sim, found, ids = churn_case
    registry = report.load_registry()
    mid = json.loads(json.dumps(sim))
    mid["loyalty"] = simulation(db_path, "user_002")["loyalty"]  # 이탈 위험인데 아직 2단계인 경우
    assert {"loyalty_tier", "loyalty_next_months"} <= set(report.required_keys(mid, registry))
    assert "필수 자리표시자 없음: {loyalty_next_months}" in report.check_draft(_churn_draft(ids), mid, found)
    text = report.verify_and_fill(_churn_draft(ids, extra=" 다음 단계까지 {loyalty_next_months} 남았습니다."), mid, found)
    assert "다음 단계까지 5개월 남았습니다." in text


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
    # 1~3월 예상 110,000회·160GB(성장률 1.0), 3단계 제공 11,500회·57GB:
    # 30,000 + ⌈98.5⌉×2,000 + 103×500 = 279,500 × 3 = 838,500
    assert values["peak_bill_current"] == "838,500원"
    assert values["peak_evidence"] == "2025-01 평소의 5.0배, 2026-01 5.5배"
    assert values["forecast_method"] == "계절 반영"


# --- Slack ---

def test_slack_message_uses_code_not_name(db_path, sim_002, retrieved_002):
    text = report.verify_and_fill(GOOD_002, sim_002, retrieved_002)
    message = report.slack_message(text, sim_002, "UQM-9GU")
    assert message["blocks"][0]["type"] == "header"
    dumped = json.dumps(message, ensure_ascii=False)
    assert "UQM-9GU" in dumped and "베타소프트" not in dumped and "user_002" not in dumped
    assert "598,000원" in dumped and "**" not in dumped
    assert all(len(block.get("text", {}).get("text", "")) <= 3000 for block in message["blocks"])

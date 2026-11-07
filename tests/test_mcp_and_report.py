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


# --- 리포트 검증 ---

GOOD_002 = """## 1. 현재 과금 분석
현재 베이직 요금제로 월 129,000원이 예상됩니다. 최근 3개월 평균 API 42,000회, 스토리지 120GB를 사용해
기본 제공량(10,000회, 50GB)을 크게 넘습니다.
## 2. 추천
프로 요금제로 바꾸고 프로 전환 3개월 30% 할인을 받으세요. 월 80,000원이 됩니다.
## 3. 절감액
12개월 기준 660,000원(43%)을 절감합니다.
"""


@pytest.fixture(scope="module")
def outputs_002(db_path):
    _, sim = call(db_path, "simulate_plans", {"user_id": "user_002"})
    _, plans = call(db_path, "list_plans")
    return [sim.content[0].text, plans.content[0].text]


def test_good_report_passes(db_path, outputs_002):
    assert report.check_report(GOOD_002, outputs_002, simulation(db_path, "user_002")) == []


def test_invented_number_is_rejected(db_path, outputs_002):
    bad = GOOD_002.replace("660,000원", "700,000원")
    with pytest.raises(report.ReportRejected) as error:
        report.verify_report(bad, outputs_002, simulation(db_path, "user_002"))
    assert any("700000" in problem for problem in error.value.problems)


def test_report_computed_percentage_is_rejected(db_path, outputs_002):
    # 비율을 모델이 직접 계산하면(예: 월 절감률 49,000/129,000 ≈ 38%) 도구 결과에 없으므로 막는다.
    bad = GOOD_002 + "월 요금은 38% 줄어듭니다.\n"
    assert report.check_report(bad, outputs_002, simulation(db_path, "user_002"))


def test_missing_required_item_is_rejected(db_path, outputs_002):
    bad = GOOD_002.replace("프로 전환 3개월 30% 할인", "전환 할인")
    problems = report.check_report(bad, outputs_002, simulation(db_path, "user_002"))
    assert any("이벤트" in problem for problem in problems)


def test_number_formats_are_normalized():
    assert report.numbers_in("1,234원, 120.0GB, 12.50%, user_002, 2026-10") == ["1234", "120", "12.5", "2026", "10"]


def test_no_usage_report(db_path):
    sim = simulation(db_path, "user_010")
    assert report.check_report("이용 기록이 없어 추천할 수 없습니다.", [json.dumps(sim)], sim) == []
    assert report.check_report("프로 요금제를 추천합니다.", [json.dumps(sim)], sim)


# --- Slack ---

def test_slack_message_shape(db_path):
    message = report.slack_message(GOOD_002, simulation(db_path, "user_002"), "(주)베타소프트")
    assert message["blocks"][0]["type"] == "header"
    text = json.dumps(message, ensure_ascii=False)
    assert "660,000원" in text and "**" not in text
    assert all(len(block.get("text", {}).get("text", "")) <= 3000 for block in message["blocks"])

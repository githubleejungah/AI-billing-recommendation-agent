"""지식 문서 검색 테스트(T2). KNW-01, 02, 04, 06."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp import Client

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "database"))

import init_db  # noqa: E402
import knowledge  # noqa: E402
import mcp_server  # noqa: E402


@pytest.fixture(scope="module")
def kb():
    return knowledge.KnowledgeBase()


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "billing_mock.db"
    init_db.build(path)
    return path


def search_tool(db_path, arguments):
    async def run():
        async with Client(mcp_server.build_server(db_path)) as client:
            result = await client.call_tool("search_knowledge", arguments)
            return json.loads(result.content[0].text)
    return asyncio.run(run())


# --- 조각과 색인 ---

def test_chunks_follow_section_headings(kb):
    ids = [c.chunk_id for c in kb.chunks]
    assert len(ids) == len(set(ids)) == 46
    first = next(c for c in kb.chunks if c.chunk_id == "promo_pro_upgrade#2")
    assert first.section == "받을 수 있는 조건"


def test_korean_is_indexed_as_two_letter_pieces():
    assert knowledge.tokenize("전환 할인") == ["전환", "할인"]
    assert knowledge.tokenize("엔터프라이즈 SLA") == ["엔터", "터프", "프라", "라이", "이즈", "sla"]
    assert "떻게" not in knowledge.tokenize("어떻게 해야 하나요")


# --- KNW-02 재현성 ---

def test_same_query_same_order(kb):
    query = "할인 기간이 끝나면 요금"
    first = [(c.chunk_id, s) for c, s in kb.search(query, 5)]
    assert first and all(first == [(c.chunk_id, s) for c, s in kb.search(query, 5)] for _ in range(3))


def test_ties_are_broken_by_chunk_id(kb):
    results = kb.search("기본 제공량과 초과 요금 방식", 5)
    scores = [s for _, s in results]
    assert scores == sorted(scores, reverse=True)
    for (a, sa), (b, sb) in zip(results, results[1:]):
        if sa == sb:
            assert a.chunk_id < b.chunk_id


# --- KNW-01 개수 제한 ---

def test_top_k_limits(kb):
    assert len(kb.search("요금제", 5)) <= 5
    with pytest.raises(ValueError):
        kb.search("요금제", 6)
    assert search_tool(ROOT / "database" / "unused.db", {"query": "요금제", "top_k": 9})["error"] == "invalid_top_k"


# --- KNW-04 빈 결과 ---

@pytest.mark.parametrize("query", ["사내 식당 이번 주 메뉴 알려줘", "노트북 비밀번호를 잊어버렸을 때 어떻게 해",
                                   "회의실 예약은 어디서 해", "다음 분기 주식 시장 전망", "", "!!!"])
def test_unrelated_query_returns_nothing(kb, query):
    assert kb.search(query) == []


# --- 도구 결과 ---

def test_tool_result_has_no_scores_or_numbers(db_path):
    data = search_tool(db_path, {"query": "프로 전환 할인 조건"})
    assert data["results"] and data["results"][0]["chunk_id"].startswith("promo_pro_upgrade#")
    assert set(data["results"][0]) == {"chunk_id", "title", "section", "audience", "text", "promotions"}
    assert "score" not in json.dumps(data)


# --- KNW-06 대상 여부 표시 ---

def status_of(data, chunk_prefix, promo_id):
    chunk = next(r for r in data["results"] if r["chunk_id"].startswith(chunk_prefix))
    return next(p for p in chunk["promotions"] if p["promo_id"] == promo_id)


def test_short_tenure_customer_is_marked_not_eligible(db_path):
    # user_004: 가입 3개월이라 프로 전환 할인 대상이 아니다. 이유는 범주로만, 가입 기간 숫자는 없이.
    data = search_tool(db_path, {"query": "프로 전환 할인 조건", "user_id": "user_004"})
    status = status_of(data, "promo_pro_upgrade#", "PROMO_PRO_UPGRADE")
    assert status == {"promo_id": "PROMO_PRO_UPGRADE", "status": "대상 아님", "reason": "가입 기간 조건 미달"}
    assert "3개월" not in json.dumps(data, ensure_ascii=False)


def test_eligible_customer_is_marked(db_path):
    data = search_tool(db_path, {"query": "프로 전환 할인 조건", "user_id": "user_002"})
    assert status_of(data, "promo_pro_upgrade#", "PROMO_PRO_UPGRADE")["status"] == "대상"


def test_ended_promotion_is_marked(db_path):
    data = search_tool(db_path, {"query": "여름 프로 할인 지금 신청", "user_id": "user_002"})
    assert status_of(data, "promo_pro_summer#", "PROMO_PRO_SUMMER") == {
        "promo_id": "PROMO_PRO_SUMMER", "status": "대상 아님", "reason": "이벤트 기간 아님"}


def test_upgrade_only_promotion_for_current_plan(db_path):
    # user_003은 이미 프로라서 프로 전환 할인은 상위 전환이 아니다.
    data = search_tool(db_path, {"query": "프로 전환 할인 조건", "user_id": "user_003"})
    assert status_of(data, "promo_pro_upgrade#", "PROMO_PRO_UPGRADE")["reason"] == "상위 전환에만 적용"


def test_no_usage_customer_status_is_unknown(db_path):
    data = search_tool(db_path, {"query": "프로 전환 할인 조건", "user_id": "user_010"})
    assert status_of(data, "promo_pro_upgrade#", "PROMO_PRO_UPGRADE")["status"] == "알 수 없음"


def test_unknown_customer(db_path):
    assert search_tool(db_path, {"query": "할인", "user_id": "user_999"})["error"] == "customer_not_found"


# --- KNW-07 문서 대상 구분 ---

def test_guides_are_internal_and_others_customer():
    docs = knowledge.KnowledgeBase().documents()
    for doc_id, document in docs.items():
        expected = "internal" if doc_id.startswith("guide_") else "customer"
        assert document.audience == expected, doc_id


def test_customer_search_never_returns_internal_documents():
    kb = knowledge.KnowledgeBase()
    for query in ("상향 추천 응대 설명 순서", "이탈 위험 고객 응대", "프로 전환 할인 조건", "하향 변경 유의사항"):
        for chunk, _score in kb.search(query, 5, audience="customer"):
            assert chunk.document.audience == "customer", (query, chunk.chunk_id)
    assert any(c.document.audience == "internal" for c, _ in kb.search("상향 추천 응대 설명 순서", 5))


def test_missing_audience_is_refused(tmp_path):
    (tmp_path / "x.md").write_text("---\ndoc_id: x\ntitle: 제목\n---\n\n## 소제목\n본문\n", encoding="utf-8")
    with pytest.raises(ValueError):
        knowledge.KnowledgeBase(tmp_path)


def test_tool_marks_audience(db_path):
    data = search_tool(db_path, {"query": "상향 추천 응대 설명 순서"})
    assert {r["audience"] for r in data["results"]} == {"internal"}

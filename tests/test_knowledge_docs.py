"""지식 문서 테스트(T1). 금액·할인율이 없는지(KNW-03), 메타데이터가 DB와 맞는지(KNW-05) 확인한다.

문서 머리말은 검색기와 같은 파서(src/knowledge.py)로 읽는다.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE = ROOT / "knowledge"
sys.path.insert(0, str(ROOT / "database"))
sys.path.insert(0, str(ROOT / "src"))

import init_db  # noqa: E402
import knowledge  # noqa: E402

# 금액: 숫자 뒤에 원/만 원/천 원, 또는 ₩. 할인율: 숫자 뒤에 %.
AMOUNT = re.compile(r"\d[\d,]*\s*(?:만\s*|천\s*)?원|₩")
RATE = re.compile(r"\d+(?:\.\d+)?\s*%")


def parse(path: Path) -> tuple[dict, str]:
    document, body = knowledge.parse_document(path)
    meta = {"doc_id": document.doc_id, "title": document.title,
            "plan_ids": list(document.plan_ids), "promo_ids": list(document.promo_ids)}
    return meta, body


def documents() -> dict[str, tuple[dict, str]]:
    return {path.stem: parse(path) for path in sorted(KNOWLEDGE.glob("*.md"))}


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "billing_mock.db"
    init_db.build(path)
    with sqlite3.connect(path) as conn:
        plans = {row[0] for row in conn.execute("SELECT plan_id FROM plans")}
        promos = {row[0] for row in conn.execute("SELECT promo_id FROM promotions")}
    return plans, promos


def test_fourteen_documents_with_metadata_and_sections():
    docs = documents()
    assert len(docs) == 14  # (v3.5) 장기 이용 혜택 문서 추가(LOY-04)
    for stem, (meta, body) in docs.items():
        assert meta["doc_id"] == stem
        assert meta["title"]
        assert len(re.findall(r"^## ", body, re.M)) >= 2, f"{stem}: 소제목(검색 조각)이 2개 미만"


# --- KNW-03 ---

def test_amount_and_rate_patterns_catch_examples():
    for sample in ["월 80,000원", "1만 원 할인", "5 천원", "₩3000"]:
        assert AMOUNT.search(sample), sample
    assert RATE.search("30% 할인") and RATE.search("12.5 %")
    assert not AMOUNT.search("원인을 설명합니다") and not AMOUNT.search("기본 제공량")


def test_knowledge_has_no_amounts_or_rates():
    found = {}
    for stem, (meta, body) in documents().items():
        hits = AMOUNT.findall(body) + RATE.findall(body) + AMOUNT.findall(meta["title"]) + RATE.findall(meta["title"])
        if hits:
            found[stem] = hits
    assert found == {}


# --- KNW-05 ---

def test_metadata_ids_exist_in_database(db):
    plans, promos = db
    for stem, (meta, _) in documents().items():
        assert set(meta["plan_ids"]) <= plans, stem
        assert set(meta["promo_ids"]) <= promos, stem


def test_every_plan_and_promotion_has_a_document(db):
    plans, promos = db
    docs = documents().values()
    assert plans <= {p for meta, _ in docs for p in meta["plan_ids"]}
    assert promos <= {p for meta, _ in docs for p in meta["promo_ids"]}


# --- EVAL-02 평가 세트 ---

def test_retrieval_set_targets_existing_documents():
    data = json.loads((ROOT / "eval" / "retrieval_set.json").read_text(encoding="utf-8"))
    questions = data["questions"]
    assert sum(q["scored"] for q in questions) >= 20
    stems = set(documents())
    for q in questions:
        assert set(q["target_docs"]) <= stems, q["id"]
        assert bool(q["target_docs"]) == q["scored"], q["id"]


def test_holdout_set_is_separate_and_points_to_real_chunks():
    """확인용 질문(EVAL-05, 2026-11-08)은 10개 이상이고, 조정용 세트와 겹치지 않으며, 정답 조각이 실제로 있다."""
    import knowledge

    holdout = json.loads((ROOT / "eval" / "retrieval_holdout.json").read_text(encoding="utf-8"))["questions"]
    tuning = json.loads((ROOT / "eval" / "retrieval_set.json").read_text(encoding="utf-8"))["questions"]
    assert sum(q["scored"] for q in holdout) >= 10
    assert not {q["question"] for q in holdout} & {q["question"] for q in tuning}
    chunk_ids = {c.chunk_id for c in knowledge.KnowledgeBase().chunks}
    for q in holdout:
        assert set(q["answer_chunks"]) <= chunk_ids, q["id"]
        assert bool(q["answer_chunks"]) == q["scored"], q["id"]

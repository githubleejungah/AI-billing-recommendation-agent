"""평가 실행기 검증(EVAL-01, 02, 06, OPS-04). API 없이 도는 부분만 시험한다."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "database"))
sys.path.insert(0, str(ROOT / "eval"))

import init_db  # noqa: E402
import knowledge  # noqa: E402
import run_eval  # noqa: E402


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    db = tmp_path_factory.mktemp("db") / "billing_mock.db"
    init_db.build(db)
    return run_eval.offline_report(db)


def test_scenario_answers_match_simulator(report):
    assert report["scenario"] == {"correct": 20, "total": 20, "wrong": []}  # 성공 기준: 20명 모두 정답


def test_customer_questions_are_routed(report):
    c = report["customer"]
    assert (c["must_escalate"], c["ai_answered_must_escalate"]) == (12, 0)  # 성공 기준: 0건
    assert (c["code_answer_ok"], c["ai_chunk_hit"]) == (3, 5) and c["problems"] == []


def test_search_numbers_and_synonym_decision(report):
    # 2026-11-08 기록: 사전 없이 조정용 16/20·확인용 7/14, 사전을 쓰면 18/20·8/14지만
    # 확인용 빈 결과 질문 하나가 결과를 내서(1/2 → 0/2) 규칙상 채택하지 않음
    assert report["synonym_comparison"] == {
        "without": {"tuning": {"top3": 16, "empty_ok": 2}, "holdout": {"top3": 7, "empty_ok": 1}},
        "with": {"tuning": {"top3": 18, "empty_ok": 2}, "holdout": {"top3": 8, "empty_ok": 0}},
    }
    assert report["adopt_by_rule"] is False and knowledge.SYNONYMS_ADOPTED is False


def test_current_results_meet_the_baseline(report):
    assert run_eval.check(report) == []


def test_check_reports_a_drop(report):
    worse = {**report, "search": {**report["search"], "holdout": {**report["search"]["holdout"], "top3": 6}}}
    assert run_eval.check(worse) == ["검색 holdout top3: 6 < 기준 7"]


def test_synonyms_only_change_the_query():
    synonyms = knowledge.load_synonyms()
    assert knowledge.normalize_query("요금 플랜 이용 내역", synonyms) == "요금제 이용 기록"
    assert synonyms[0][0] == "다운그레이드"  # 긴 말부터 바꾼다

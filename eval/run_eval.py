"""평가 실행(EVAL-01~06, OPS-03·04). 결과는 eval/results/에 남긴다.

실행:
  python eval/run_eval.py              # API 없이 도는 평가(검색, 시나리오 정답, 고객 질문 분류)
  python eval/run_eval.py --check      # 위 결과가 기준(eval/baseline.json)보다 떨어지면 실패(CI용, OPS-04)
  python eval/run_eval.py --live       # 실제 API: 시나리오 20명 + 무작위 10명 리포트, 고객 조건 질문(비용 발생)

API 없이 도는 평가는 같은 입력이면 같은 결과다. --live 결과는 날짜별 파일로 남겨 이전 결과와 비교한다.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from contextlib import closing
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import chat  # noqa: E402
import data_access  # noqa: E402
import knowledge  # noqa: E402
import policy as policy_module  # noqa: E402
import simulator  # noqa: E402

EVAL_DIR = ROOT / "eval"
RESULTS_DIR = EVAL_DIR / "results"
BASELINE = EVAL_DIR / "baseline.json"
RANDOM_SEED = 2026  # EVAL-05 무작위 고객 10명(시드 고정)
SEARCH_SETS = {"tuning": "retrieval_set.json", "holdout": "retrieval_holdout.json"}


def _load(name: str) -> dict:
    return json.loads((EVAL_DIR / name).read_text(encoding="utf-8"))


def retrieval(kb: knowledge.KnowledgeBase, name: str, top_k: int = 3) -> dict:
    """EVAL-02: 정답 조각이 상위 3개 안에 든 비율과 빈 결과 질문(KNW-04)."""
    data = _load(name)
    hits, scored, empty_ok, empty_total, misses = 0, 0, 0, 0, []
    for q in data["questions"]:
        found = [c.chunk_id for c, _ in kb.search(q["question"], top_k)]
        if q["scored"]:
            scored += 1
            if set(found) & set(q["answer_chunks"]):
                hits += 1
            else:
                misses.append(q["id"])
        else:
            empty_total += 1
            empty_ok += not found
    return {"top3": hits, "scored": scored, "rate": round(hits / scored, 3), "empty_ok": empty_ok,
            "empty_total": empty_total, "misses": misses}


def scenario_accuracy(conn) -> dict:
    """EVAL-01(코드 부분): 시뮬레이터 추천이 시나리오 정답과 같은가. 리포트의 숫자·요금제는 이 값으로 채워진다."""
    answers = _load("scenario_answers.json")["answers"]
    wrong = []
    for user_id, expected in answers.items():
        sim = simulator.simulate(conn, user_id)
        rec = sim["recommendation"]
        got = ({"action": "no_usage", "plan_id": None, "promo_id": None} if rec is None
               else {"action": rec["action"], "plan_id": rec["plan_id"], "promo_id": rec["promo_id"]})
        if got != expected:
            wrong.append({"user_id": user_id, "expected": expected, "got": got})
    return {"correct": len(answers) - len(wrong), "total": len(answers), "wrong": wrong}


def customer_routing(conn, pol, kb: knowledge.KnowledgeBase) -> dict:
    """EVAL-06(코드 부분): 넘겨야 할 질문이 코드 규칙에서 넘어가는가, AI 질문의 정답 조각이 Claude에 가는가."""
    questions = _load("customer_questions.json")["questions"]
    must, must_ok, ai, ai_hit, code, code_ok, problems = 0, 0, 0, 0, 0, 0, []
    for q in questions:
        route, _ = chat.classify(q["question"], pol)
        masked = q["question"]
        found = [c.chunk_id for c, _ in kb.search(masked, chat.SEARCH_TOP_K, audience="customer")]
        sim = simulator.simulate(conn, q["user_id"])
        if q["expect"] == "escalate":
            must += 1
            # 코드가 넘기는 경우: 위험 주제·이상한 요청, 금액인데 계산 불가, 조건인데 검색 결과 없음
            escalated = (route in (chat.RISK, chat.ODD) or (route == chat.AMOUNT and sim["status"] != "ok")
                         or (route == chat.CONDITION and not found))
            must_ok += escalated
            if not escalated:
                problems.append(q["id"])
        elif q["expect"] == "code_answer":
            code += 1
            code_ok += route == chat.AMOUNT and sim["status"] == "ok"
        else:
            ai += 1
            if route == chat.CONDITION and set(found) & set(q["answer_chunks"]):
                ai_hit += 1
            else:
                problems.append(q["id"])
    return {"must_escalate": must, "escalated_by_code": must_ok, "ai_answered_must_escalate": must - must_ok,
            "code_answer": code, "code_answer_ok": code_ok, "ai_questions": ai, "ai_chunk_hit": ai_hit,
            "problems": problems}


def offline(synonyms: bool, db_path: Path | None = None) -> dict:
    kb = knowledge.KnowledgeBase(synonyms=knowledge.load_synonyms() if synonyms else None)
    pol = policy_module.load()
    with closing(data_access.connect(db_path) if db_path else data_access.connect()) as conn:
        return {
            "search": {key: retrieval(kb, name) for key, name in SEARCH_SETS.items()},
            "scenario": scenario_accuracy(conn),
            "customer": customer_routing(conn, pol, kb),
        }


def compare_synonyms(db_path: Path | None = None) -> dict:
    """업무 용어 사전 채택 판단(KNW-08과 같은 원칙): 조정용에서 오르고 확인용에서 떨어지지 않아야 채택."""
    off, on = offline(False, db_path), offline(True, db_path)
    tuning_up = on["search"]["tuning"]["top3"] > off["search"]["tuning"]["top3"]
    holdout_ok = on["search"]["holdout"]["top3"] >= off["search"]["holdout"]["top3"]
    empties_ok = all(on["search"][k]["empty_ok"] >= off["search"][k]["empty_ok"] for k in SEARCH_SETS)
    return {"without": off, "with": on, "adopt": tuning_up and holdout_ok and empties_ok}


def offline_report(db_path: Path | None = None) -> dict:
    """API 없는 평가 결과. 지금 쓰는 검색 설정(사전 채택 여부)의 값을 본 결과로 둔다."""
    comparison = compare_synonyms(db_path)
    adopted = knowledge.SYNONYMS_ADOPTED
    summary = {k: {n: {"top3": comparison[k]["search"][n]["top3"], "empty_ok": comparison[k]["search"][n]["empty_ok"]}
                   for n in SEARCH_SETS} for k in ("without", "with")}
    return {"synonyms_adopted": adopted, "synonym_comparison": summary, "adopt_by_rule": comparison["adopt"],
            **comparison["with" if adopted else "without"]}


def check(result: dict) -> list[str]:
    """기준보다 떨어진 항목(OPS-04). 기준 파일은 사람이 검토하고 갱신한다."""
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    drops = []
    for key in SEARCH_SETS:
        for metric in ("top3", "empty_ok"):
            if result["search"][key][metric] < base["search"][key][metric]:
                drops.append(f"검색 {key} {metric}: {result['search'][key][metric]} < 기준 {base['search'][key][metric]}")
    if result["scenario"]["correct"] < base["scenario"]["correct"]:
        drops.append(f"시나리오 정답 {result['scenario']['correct']} < 기준 {base['scenario']['correct']}")
    if result["customer"]["ai_answered_must_escalate"] > 0:
        drops.append(f"넘겨야 할 질문을 넘기지 않음: {result['customer']['problems']}")
    return drops


def live(model: str | None = None) -> dict:  # pragma: no cover - 실제 API 비용이 들어 테스트에서 돌리지 않는다
    """EVAL-01, 03, 05(실제 API): 시나리오 20명 + 무작위 10명 리포트. 첫 시도·최종 통과율, 시간, 토큰, 모델 ID."""
    import agent

    pol = policy_module.load()
    model = model or os.environ.get("CLAUDE_MODEL", "").strip() or pol.allowed_models[0]
    client = agent.AnthropicClient(os.environ["ANTHROPIC_API_KEY"])
    answers = _load("scenario_answers.json")["answers"]
    out_dir = RESULTS_DIR / f"live_{date.today():%Y%m%d}"
    with closing(data_access.connect()) as conn:
        others = [u for u in data_access.customer_ids(conn) if u not in answers]
        groups = {"scenario": list(answers), "random": random.Random(RANDOM_SEED).sample(others, 10)}
        rows = []
        for group, users in groups.items():
            for user_id in users:
                started = time.monotonic()
                result = agent.run(user_id, client=client, model=model, conn=conn, policy=pol,
                                   output_dir=out_dir, app_db=out_dir / "app.db", audit_path=out_dir / "audit.jsonl",
                                   sent_log_path=out_dir / "sent.json")
                log = json.loads(result.run_log_path.read_text(encoding="utf-8"))
                first = log["validation"][0]["kinds"] if log.get("validation") else None
                rows.append({"group": group, "user_id": user_id, "status": result.status,
                             "first_pass": first == [], "first_kinds": first,
                             "seconds": round(time.monotonic() - started, 1),
                             "tokens": sum((c.get("input_tokens") or 0) + (c.get("output_tokens") or 0) for c in log["calls"]),
                             "searches": log["search_count"]})
    summary = {}
    for group in groups:
        g = [r for r in rows if r["group"] == group]
        summary[group] = {"n": len(g), "final_pass": sum(r["status"] == "완료" for r in g),
                          "first_pass": sum(bool(r["first_pass"]) for r in g),
                          "max_seconds": max(r["seconds"] for r in g), "tokens": sum(r["tokens"] for r in g)}
    return {"model": model, "summary": summary, "rows": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="평가 실행")
    parser.add_argument("--check", action="store_true", help="기준보다 떨어지면 실패(CI)")
    parser.add_argument("--live", action="store_true", help="실제 API로 리포트 평가(비용 발생)")
    args = parser.parse_args(argv)
    if args.live:
        try:
            from dotenv import load_dotenv

            load_dotenv(ROOT / ".env")
        except ImportError:
            pass
        result = live()
        name = f"live_{date.today():%Y%m%d}.json"
    else:
        result = offline_report()
        name = f"offline_{date.today():%Y%m%d}.json"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / name).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.check and not args.live:
        drops = check(result)
        for drop in drops:
            print(f"기준 미달: {drop}", file=sys.stderr)
        return 1 if drops else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

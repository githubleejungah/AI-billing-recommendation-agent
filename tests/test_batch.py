"""월간 선별 검증(PRO-01~11). 선별일 2026-11-01(기준일). AI를 부르지 않는다.

기대값은 손 계산 값을 쓴다(test_simulator.py, test_seasonality.py).
- user_002: 절감 추천, 12개월 598,000원(40%). 유효기간 = min(2026-11-01 + 30일, 이벤트 마감 2026-12-31) = 2026-12-01
- user_201: 성수기 1~3월. 첫날 2027-01-01 − 45일 = 2026-11-17(1차), 신청 마감 2026-12-31, 마감 전 알림 2026-12-24.
  프로 + 할인이 안정적으로 유리 → 변경 권유. 같은 달 절감 추천(296,000원)과 합친다(PRO-09).
- user_202: 성수기 1~2월, 유지가 유리 → 예산 안내, 1차 2026-11-17. 추가 비용 156,000원 ≥ 10만 원
- user_203: 성수기 아님 → 성수기 알림 없음. 유지 + 이벤트 절감 20,000원은 하한(10만 원) 미만 → 알림 없음
- user_015: 이탈 위험 → 담당자 연락만. user_010: 이용 기록 없음 → 담당자 연락(첫 사용 지원)
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "database"))

import audit  # noqa: E402
import batch  # noqa: E402
import data_access  # noqa: E402
import history  # noqa: E402
import init_db  # noqa: E402
import policy  # noqa: E402
import simulator  # noqa: E402

NOV, DEC = date(2026, 11, 1), date(2026, 12, 1)
NOW = datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "billing_mock.db"
    init_db.build(path)
    with closing(sqlite3.connect(path)) as raw:
        # 12월 선별을 시험하려면 2026-11 기록이 있어야 24개월 성수기 판정이 된다(가상 데이터는 2026-10까지).
        # 평소 달 값을 그대로 넣는다: user_201 20,000회·45GB, user_202 8,000회·30GB
        raw.executemany("INSERT INTO usage_logs (user_id, target_month, api_calls_used, storage_gb_used, active_days)"
                        " VALUES (?, '2026-11', ?, ?, 20)", [("user_201", 20000, 45.0), ("user_202", 10000, 30.0)])
        raw.commit()
    return path


@pytest.fixture()
def conn(db):
    connection = simulator.connect(db)
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def pol():
    return policy.load()


@pytest.fixture()
def paths(tmp_path):
    return {"app_db": tmp_path / "app.db", "output_dir": tmp_path / "out", "audit_path": tmp_path / "a.jsonl"}


@pytest.fixture()
def code(conn):
    codes = data_access.code_map(conn)
    return lambda user_id: codes[user_id]


def run(conn, pol, paths, as_of=NOV, **kwargs):
    return batch.run(conn, pol, as_of, now=NOW, **paths, **kwargs)


def notice_of(done, code):
    return next((n for n in done["result"]["notices"] if n.code == code), None)


def preview_of(done, code):
    return next(p for p in done["previews"] if p["code"] == code)


def with_selection(pol, **changes):
    raw = json.loads(json.dumps(pol.raw))
    raw["selection"].update(changes)
    return dataclasses.replace(pol, raw=raw)


# --- 절감 추천(PRO-01~03) ---

def test_saving_notice_for_user_002(conn, pol, paths, code):
    done = run(conn, pol, paths)
    n = notice_of(done, code("user_002"))
    assert (n.kind, n.priority, n.send_on) == ("절감 추천", 598000, NOV)
    p = preview_of(done, code("user_002"))
    assert p["valid_until"] == "2026-12-01"
    assert "598,000원(40%)" in p["text"] and "프로 전환 3개월 30% 할인" in p["text"] and "부가세 별도" in p["text"]
    assert "받지 않기" in p["text"]  # 수신거부 방법(PRO-03)
    assert "베타소프트" not in json.dumps(done["previews"], ensure_ascii=False)


def test_thresholds_come_from_policy(conn, pol, paths, tmp_path):
    base = {n.code for n in run(conn, pol, paths)["result"]["notices"] if "절감 추천" in [p.kind for p in n.parts]}
    other = {"app_db": tmp_path / "b.db", "output_dir": tmp_path / "o2", "audit_path": tmp_path / "b.jsonl"}
    strict = with_selection(pol, min_saving_12m=500000)
    fewer = {n.code for n in run(conn, strict, other)["result"]["notices"] if "절감 추천" in [p.kind for p in n.parts]}
    assert fewer < base and all(n.priority >= 500000 for n in run(conn, strict, {
        **other, "app_db": tmp_path / "c.db"})["result"]["notices"] if "절감 추천" in [p.kind for p in n.parts])


def test_monthly_limit_keeps_peak_notices_first(conn, pol, paths, code):
    small = with_selection(pol, max_alerts_per_month=3)
    done = run(conn, small, paths)
    limited = [n for n in done["result"]["notices"] if n.counts_toward_limit]
    assert len(limited) == 3
    # 마감이 있는 성수기 알림(user_201, user_202)이 먼저, 그다음 절감액이 가장 큰 고객
    assert [n.code for n in limited[:2]] == sorted([code("user_201"), code("user_202")],
                                                   key=lambda c: (c != code("user_201"), c))
    assert len(done["result"]["skipped"]["월 최대 수 초과(다음 달로)"]) > 0


def test_low_data_customer_is_excluded_by_policy(conn, pol, paths, code):
    # user_013: 기록 1개월(데이터 적음), 절감 116,000원(10.8%)
    assert notice_of(run(conn, pol, paths), code("user_013")) is None
    relaxed = with_selection(pol, exclude_low_data=False)
    paths = {**paths, "app_db": paths["app_db"].with_name("relaxed.db")}
    assert notice_of(run(conn, relaxed, paths), code("user_013")).priority == 116000


# --- 담당자 연락(PRO-02, REP-03) ---

def test_churn_risk_and_no_usage_go_to_staff_only(conn, pol, paths, code):
    done = run(conn, pol, paths)
    staff = {s["code"]: s["reason"] for s in done["result"]["staff"]}
    assert staff[code("user_015")] == "이탈 위험" and staff[code("user_010")] == "첫 사용 지원"
    assert notice_of(done, code("user_015")) is None and notice_of(done, code("user_010")) is None
    with closing(history.connect(paths["app_db"])) as app:
        rows = app.execute("SELECT status, parts FROM notifications WHERE kind = '담당자 연락'").fetchall()
    assert {tuple(r) for r in rows} >= {("사내", "이탈 위험"), ("사내", "첫 사용 지원")}


# --- 재알림 제한(PRO-05) ---

def test_same_month_rerun_does_not_repeat(conn, pol, paths, code):
    first = run(conn, pol, paths)
    second = run(conn, pol, paths)
    assert len(second["result"]["staff"]) == 0
    assert second["result"]["skipped"]["같은 추천 90일 안 재알림"]
    assert notice_of(second, code("user_002")) is None
    assert notice_of(second, code("user_201")) is None  # 1차 성수기 알림도 다시 보내지 않음
    assert notice_of(second, code("user_202")) is None
    assert len(first["result"]["notices"]) > len(second["result"]["notices"])


def test_rejected_recommendation_waits_until_next_quarter(conn, pol, paths, code):
    with closing(history.connect(paths["app_db"])) as app:
        app.execute("INSERT INTO recommendation_history (run_id, code, analyzed_at, plan_id, promo_id, outcome, outcome_at)"
                    " VALUES ('r1', ?, '2026-10-01', 'PLAN_PRO', 'PROMO_PRO_UPGRADE', '거절', '2026-10-15')",
                    (code("user_002"),))
        app.commit()
    done = run(conn, pol, paths)  # 2026-10-15 거절 → 같은 4분기(10~12월)인 11월에는 알리지 않음
    assert code("user_002") in done["result"]["skipped"]["거절한 추천(이번 분기)"]


def test_opt_out_customer_gets_nothing(conn, pol, paths, code):
    with closing(history.connect(paths["app_db"])) as app:
        app.execute("INSERT INTO opt_outs VALUES (?, '2026-10-01')", (code("user_002"),))
        app.commit()
    done = run(conn, pol, paths)
    assert notice_of(done, code("user_002")) is None
    assert code("user_002") in done["result"]["skipped"]["수신거부"]


# --- 성수기 알림(PRO-07~09) ---

def test_seasonal_customers(conn, pol, paths, code):
    done = run(conn, pol, paths)
    tax = notice_of(done, code("user_201"))
    assert (tax.kind, [p.kind for p in tax.parts], tax.send_on) == ("성수기 예고", ["절감 추천", "성수기 예고"],
                                                                     date(2026, 11, 17))
    peak = tax.main_part()
    assert (peak.stage, peak.peak_months, peak.saving_12m) == ("1차", "1~3월", 296000)
    text = preview_of(done, code("user_201"))["text"]
    assert "2025-01 평소의 5.0배, 2026-01 5.5배" in text and "838,500원" in text
    assert "유지 1,502,000원" in text and "1,206,000원" in text and "2026-12-31까지 신청" in text
    assert "[요금제 추천]" not in text  # 합친 알림에는 절감 추천 문장을 따로 넣지 않음

    shop = notice_of(done, code("user_202"))
    assert (shop.kind, shop.send_on, shop.priority) == ("예산 안내", date(2026, 11, 17), 0)
    assert "바꾸지 않아도" in preview_of(done, code("user_202"))["text"]
    assert "216,000원" in preview_of(done, code("user_202"))["text"]  # 1~2월 108,000 × 2

    assert notice_of(done, code("user_203")) is None  # 한 번만 튄 고객: 성수기 알림 없음


def test_reminder_in_december_only_after_first_notice(conn, pol, paths, code):
    run(conn, pol, paths)
    dec = run(conn, pol, paths, as_of=DEC)
    tax = notice_of(dec, code("user_201"))
    peak = next(p for p in tax.parts if p.kind == "성수기 예고")
    assert (peak.stage, peak.send_on, peak.peak_months) == ("마감 전", date(2026, 12, 24), "1~3월")
    shop = notice_of(dec, code("user_202"))
    assert (shop.main_part().stage, shop.send_on) == ("마감 전", date(2026, 12, 24))
    again = run(conn, pol, paths, as_of=DEC)  # 같은 묶음에는 두 번만(PRO-09)
    assert notice_of(again, code("user_201")) is None and notice_of(again, code("user_202")) is None


def test_no_reminder_after_change_request(conn, pol, paths, code):
    run(conn, pol, paths)
    with closing(history.connect(paths["app_db"])) as app:
        app.execute("INSERT INTO change_requests (request_id, code, requested_at, requested_by_role, status)"
                    " VALUES ('CR-1', ?, '2026-11-20', '결제 권한자', '접수')", (code("user_201"),))
        app.commit()
    dec = run(conn, pol, paths, as_of=DEC)
    assert notice_of(dec, code("user_201")) is None  # 변경을 신청한 고객에게 마감 전 알림 없음
    assert notice_of(dec, code("user_202")) is not None


# --- 장기 이용 혜택(PRO-10, 11) ---

def test_tier_notice_next_month_once(conn, pol, paths, code):
    # user_198: 가입 2026-06-25, 12월 1일에 5개월 → 2027-01부터 1단계(남은 1개월)
    nov = run(conn, pol, paths)
    assert notice_of(nov, code("user_198")) is None or "단계 도달" not in [p.kind for p in notice_of(nov, code("user_198")).parts]
    dec = run(conn, pol, paths, as_of=DEC)
    n = notice_of(dec, code("user_198"))
    assert "단계 도달" in [p.kind for p in n.parts]
    assert "2027-01부터 1단계" in preview_of(dec, code("user_198"))["text"]
    assert not n.counts_toward_limit or len(n.parts) > 1
    again = run(conn, pol, paths, as_of=DEC)
    assert notice_of(again, code("user_198")) is None


def test_loyalty_announcement_once_for_everyone(conn, paths, code):
    done = batch.announce_loyalty(conn, NOV, now=NOW, **paths)
    assert done["count"] == 203
    notices = {n["code"]: n["text"] for n in json.loads(done["path"].read_text(encoding="utf-8"))["notices"]}
    assert "이미 최고 단계" in notices[code("user_015")]
    assert "2단계(기본 제공량 10% 추가)" in notices[code("user_002")] and "2027-04부터 3단계" in notices[code("user_002")]
    assert "아직 단계가 없습니다" in notices[code("user_004")]
    with pytest.raises(ValueError, match="한 번만"):
        batch.announce_loyalty(conn, NOV, now=NOW, **paths)


# --- 저장과 사내 요약(PRO-04, 06, SEC-12) ---

def test_records_summary_and_audit_have_codes_only(conn, pol, paths, code):
    done = run(conn, pol, paths)
    with closing(history.connect(paths["app_db"])) as app:
        rows = [dict(r) for r in app.execute("SELECT * FROM notifications")]
    customer = [r for r in rows if r["kind"] != "담당자 연락"]
    assert {r["status"] for r in customer} == {"미리보기"}  # 고객 알림은 미리보기로만(PRO-04)
    tax = next(r for r in rows if r["code"] == code("user_201"))
    assert (tax["kind"], tax["parts"], tax["stage"], tax["send_on"], tax["peak_months"]) == (
        "성수기 예고", "절감 추천,성수기 예고", "1차", "2026-11-17", "1~3월")
    names = data_access.customer_names(conn)
    files = list((paths["output_dir"] / "selection").glob("*.json"))
    dumped = json.dumps([rows, done["summary"]], ensure_ascii=False) + "".join(f.read_text(encoding="utf-8") for f in files)
    assert not any(name in dumped for name in names) and "user_" not in dumped
    assert code("user_002") in done["summary"]["blocks"][1]["text"]["text"]
    assert done["slack"] == "주소 없음(JSON만 저장)"
    events = [e["event"] for e in audit.read(paths["audit_path"])]
    assert events == ["quality_check", "selection"]


def test_summary_is_sent_automatically_when_sender_exists(conn, pol, paths):
    sent = []
    done = run(conn, pol, paths, slack_sender=lambda message, digest, run_id: sent.append(message) or "보냄")
    assert done["slack"] == "보냄" and sent[0]["text"].startswith("월간 선별 2026-11-01")

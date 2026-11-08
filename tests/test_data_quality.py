"""데이터 품질 게이트 검증(DQ-01~04). 위반 기록은 임시 DB에 직접 넣어 만든다(원천 데이터는 깨끗하다)."""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "database"))

import batch  # noqa: E402
import data_quality  # noqa: E402
import history  # noqa: E402
import init_db  # noqa: E402
import policy  # noqa: E402
import simulator  # noqa: E402

AS_OF = date(2026, 11, 1)


def build(tmp_path, edit=None):
    path = tmp_path / "billing_mock.db"
    init_db.build(path)
    if edit:
        with closing(sqlite3.connect(path)) as raw:
            edit(raw)
            raw.commit()
    return simulator.connect(path)


def test_clean_data_passes_and_seasonal_spikes_are_only_flagged(tmp_path):
    with closing(build(tmp_path)) as conn:
        result = data_quality.check(conn, AS_OF)
    assert (result["total"], result["passed"], result["pass_rate"], result["stopped"]) == (203, 203, 1.0, False)
    assert result["excluded"] == {}
    # 성수기·일회성 급증은 직전 3개월 중앙값의 5배 이상이라 "확인 필요"지만 빼지 않는다(DQ-02)
    assert result["flagged"] == {"user_201": ["이상치"], "user_202": ["이상치"], "user_203": ["이상치"]}
    assert result["window"] == ["2024-11", "2026-10"]


def _break(raw):
    raw.execute("UPDATE usage_logs SET api_calls_used = -5 WHERE user_id = 'user_030' AND target_month = '2026-10'")
    raw.execute("UPDATE usage_logs SET active_days = 40 WHERE user_id = 'user_031' AND target_month = '2026-09'")
    raw.execute("UPDATE usage_logs SET api_calls_used = 'abc' WHERE user_id = 'user_032' AND target_month = '2026-08'")
    raw.execute("UPDATE usage_logs SET target_month = '2026-13' WHERE user_id = 'user_036' AND target_month = '2026-08'")
    # 중복: 고유 제약을 없앤 표로 바꾼 뒤 같은 달을 한 번 더 넣는다(실제 청구 시스템에는 제약이 없을 수 있음)
    raw.executescript("CREATE TABLE u2 AS SELECT * FROM usage_logs; DROP TABLE usage_logs; "
                      "ALTER TABLE u2 RENAME TO usage_logs;")
    raw.execute("INSERT INTO usage_logs (user_id, target_month, api_calls_used, storage_gb_used, active_days) "
                "VALUES ('user_033', '2026-10', 5000, 10.0, 20)")
    raw.execute("DELETE FROM usage_logs WHERE user_id = 'user_034' AND target_month = '2026-08'")  # 중간에 빠진 달


def test_each_rule_is_detected(tmp_path):
    with closing(build(tmp_path, _break)) as conn:
        result = data_quality.check(conn, AS_OF)
    assert result["excluded"] == {"user_030": ["범위"], "user_031": ["범위"], "user_032": ["형식"],
                                  "user_033": ["중복"], "user_036": ["형식"]}
    assert result["flagged"]["user_034"] == ["연속성"]  # 빼지 않고 표시만
    assert result["counts"] == {"형식": 2, "범위": 2, "중복": 1, "연속성": 1, "이상치": 3}
    assert (result["passed"], result["stopped"]) == (198, False)  # 198/203 = 97.5% ≥ 95%


def test_excluded_customers_are_left_out_of_selection(tmp_path):
    with closing(build(tmp_path, _break)) as conn:
        done = batch.run(conn, policy.load(), AS_OF, app_db=tmp_path / "app.db", output_dir=tmp_path / "out",
                         audit_path=tmp_path / "a.jsonl")
        codes = batch.data_access.code_map(conn)
    bad = {codes[u] for u in ("user_030", "user_031", "user_032", "user_033", "user_036")}
    assert set(done["result"]["skipped"]["데이터 품질 위반"]) == bad
    assert not bad & {n.code for n in done["result"]["notices"]}


def test_low_pass_rate_stops_the_whole_selection(tmp_path):
    def many(raw):  # 11명 위반 → 192/203 = 94.6% < 95%
        for number in range(30, 41):
            raw.execute("UPDATE usage_logs SET storage_gb_used = -1 WHERE user_id = ?", (f"user_{number:03d}",))

    with closing(build(tmp_path, many)) as conn:
        assert data_quality.check(conn, AS_OF)["stopped"] is True
        with pytest.raises(batch.SelectionStopped, match="94.6%"):
            batch.run(conn, policy.load(), AS_OF, app_db=tmp_path / "app.db", output_dir=tmp_path / "out",
                      audit_path=tmp_path / "a.jsonl")
        codes = batch.data_access.code_map(conn)
    with closing(history.connect(tmp_path / "app.db")) as app:
        row = dict(app.execute("SELECT * FROM quality_checks").fetchone())
        assert app.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0  # 알림을 만들지 않음
    assert row["stopped"] == 1 and row["passed"] == 192
    saved = json.loads(row["excluded_codes"])
    assert set(saved) == {codes[f"user_{n:03d}"] for n in range(30, 41)}  # 가칭 코드로만 저장(DQ-04)
    assert "user_" not in json.dumps(row, ensure_ascii=False)
    summary = list((tmp_path / "out" / "selection").glob("*_summary.json"))
    assert summary and "데이터 품질 점검에서 멈춤" in summary[0].read_text(encoding="utf-8")

"""데이터 품질 게이트(DQ-01~04). 월간 선별 전에 이용 기록을 점검한다. AI를 쓰지 않는다.

- 형식(필수 값, 자료형, 월 형식)·범위(0 이상, 활동일 0~31)·중복(고객·월) 위반 고객은 그달 선별에서 뺀다.
- 이상치(직전 3개월 중앙값의 5배 이상)는 빼지 않고 "확인 필요"로 표시한다. 성수기처럼 정상인 급증이 있기 때문이다.
- 연속성(최근 기록 중간에 빠진 달) 위반은 빼지 않는다. 그런 고객은 성수기를 판정하지 않는다(24개월이 채워지지 않음, SIM-07).
- 전체 통과 비율이 95% 미만이면 선별 전체를 멈춘다. 원천 데이터 문제일 가능성이 크기 때문이다.
- 결과에는 고객 ID를 쓰고, 저장할 때는 가칭 코드만 남긴다(DQ-04).
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from datetime import date
from fractions import Fraction

import data_access
import simulator

OUTLIER_MULTIPLE = 5            # 직전 3개월 중앙값의 몇 배 이상이면 이상치인가(DQ-01)
MIN_PASS_RATE = Fraction(95, 100)  # 이보다 낮으면 선별 전체를 멈춘다(DQ-03)
WINDOW_MONTHS = simulator.ANALYSIS_MONTHS
EXCLUDING = ("형식", "범위", "중복")   # 선별에서 빼는 위반(DQ-02)
FLAGGING = ("연속성", "이상치")         # 빼지 않고 표시만
MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
FIELDS = ("api_calls_used", "storage_gb_used", "active_days")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return _is_int(value) or isinstance(value, float)


def _row_problems(row: dict) -> list[str]:
    if not isinstance(row.get("target_month"), str) or not MONTH.match(row["target_month"]) \
            or any(row.get(f) is None for f in FIELDS) \
            or not _is_int(row["api_calls_used"]) or not _is_number(row["storage_gb_used"]) \
            or not _is_int(row["active_days"]):
        return ["형식"]
    if row["api_calls_used"] < 0 or row["storage_gb_used"] < 0 or not 0 <= row["active_days"] <= 31:
        return ["범위"]
    return []


def _next_month(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:])
    return f"{year + mon // 12}-{mon % 12 + 1:02d}"


def _has_gap(months: list[str]) -> bool:
    return any(_next_month(a) != b for a, b in zip(months, months[1:]))


def _outlier(rows: list[dict]) -> bool:
    for index in range(3, len(rows)):
        for key in ("api_calls_used", "storage_gb_used"):
            median = statistics.median(Fraction(str(r[key])) for r in rows[index - 3:index])
            if median > 0 and Fraction(str(rows[index][key])) >= OUTLIER_MULTIPLE * median:
                return True
    return False


def check(conn: sqlite3.Connection, as_of: date) -> dict:
    """기준일 전 24개월 기록을 점검한다. 결과의 excluded/flagged는 고객 ID → 위반 종류 목록이다."""
    window = simulator._months_before(as_of, WINDOW_MONTHS)
    in_window = set(window)
    customers = data_access.customer_ids(conn)
    by_user: dict[str, list[dict]] = {}
    for row in data_access.load_usage_rows(conn):
        month = row.get("target_month")
        malformed = not isinstance(month, str) or not MONTH.match(month)
        if malformed or month in in_window:  # 형식이 틀린 달은 기간과 상관없이 점검한다
            by_user.setdefault(row["user_id"], []).append(row)

    excluded: dict[str, set[str]] = {}
    flagged: dict[str, set[str]] = {}
    for user_id, user_rows in by_user.items():
        kinds = {kind for row in user_rows for kind in _row_problems(row)}
        months = [r["target_month"] for r in user_rows]
        if len(months) != len(set(months)):
            kinds.add("중복")
        if kinds:
            excluded[user_id] = kinds
            continue  # 형식이 틀린 기록으로는 연속성·이상치를 볼 수 없다
        if _has_gap(months):
            flagged.setdefault(user_id, set()).add("연속성")
        if _outlier(user_rows):
            flagged.setdefault(user_id, set()).add("이상치")

    total = len(customers)
    passed = total - len(excluded)
    counts = {kind: sum(kind in k for k in list(excluded.values()) + list(flagged.values()))
              for kind in EXCLUDING + FLAGGING}
    rate = Fraction(passed, total) if total else Fraction(0)
    return {
        "checked_at": as_of.isoformat(),
        "window": [window[0], window[-1]],
        "total": total,
        "passed": passed,
        "pass_rate": float(round(rate, 4)),
        "counts": counts,
        "excluded": {u: sorted(k) for u, k in sorted(excluded.items())},
        "flagged": {u: sorted(k) for u, k in sorted(flagged.items())},
        "stopped": rate < MIN_PASS_RATE,
    }


def save(app: sqlite3.Connection, run_id: str, result: dict, codes: dict[str, str]) -> None:
    """결과를 앱 저장소에 남긴다(DQ-04). 고객은 가칭 코드로만 남긴다."""
    app.execute(
        "INSERT OR REPLACE INTO quality_checks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, result["checked_at"], result["total"], result["passed"], result["pass_rate"],
         json.dumps(result["counts"], ensure_ascii=False),
         json.dumps({codes[u]: k for u, k in result["excluded"].items()}, ensure_ascii=False),
         json.dumps({codes[u]: k for u, k in result["flagged"].items()}, ensure_ascii=False),
         int(result["stopped"])),
    )
    app.commit()

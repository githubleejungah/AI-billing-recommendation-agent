"""가상 과금 데이터베이스(billing_mock.db)를 만든다.

실행: python database/init_db.py
- user_001~020: 평가용 시나리오 고객. 사용량을 고정해 정답을 손으로 계산할 수 있다.
- user_021~200: 규모를 위한 무작위 고객. 시드가 고정이라 매번 같은 데이터가 나온다.
  (명세 v3.0) 실제처럼 대부분은 지금 요금제가 맞도록 만든다. 요금제와 맞는 고객 75%, 넘치는 고객 15%,
  남는 고객 10%(베이직은 남을 수 없고 엔터프라이즈는 넘칠 수 없어 맞는 쪽으로 돌린다).
- user_201~203: (명세 v3.2) 성수기 시나리오 고객. 24개월 기록(2024-11~2026-10)을 표로 고정했다.
  기존 고객 200명의 데이터와 가칭 코드가 바뀌지 않도록 맨 뒤에 붙인다(무작위 값을 쓰지 않는다).
- loyalty_tiers: (명세 v3.5) 장기 이용 혜택 단계(가입 6개월·1년·2년, 기본 제공량 5%·10%·15% 추가).
- customer_codes: Slack 등 사내 공유용 고정 가칭 코드(SEC-12). 사용 데이터와 다른 시드로 만든다.
모든 이름과 수치는 가상이다.
"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "billing_mock.db"
MONTHS = ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
SEED = 42

SCHEMA = """
CREATE TABLE plans (
    plan_id TEXT PRIMARY KEY,
    plan_name TEXT NOT NULL,
    base_price INTEGER NOT NULL,
    included_api_calls INTEGER NOT NULL,
    included_storage_gb REAL NOT NULL,
    api_overage_per_1k INTEGER NOT NULL,
    storage_overage_per_gb INTEGER NOT NULL
);
CREATE TABLE users (
    user_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    current_plan_id TEXT NOT NULL REFERENCES plans (plan_id),
    signup_date TEXT NOT NULL
);
CREATE TABLE usage_logs (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users (user_id),
    target_month TEXT NOT NULL,
    api_calls_used INTEGER NOT NULL,
    storage_gb_used REAL NOT NULL,
    active_days INTEGER NOT NULL,
    UNIQUE (user_id, target_month)
);
CREATE TABLE promotions (
    promo_id TEXT PRIMARY KEY,
    promo_name TEXT NOT NULL,
    plan_id TEXT NOT NULL REFERENCES plans (plan_id),
    discount_type TEXT NOT NULL CHECK (discount_type IN ('percent', 'fixed')),
    discount_value INTEGER NOT NULL,
    duration_months INTEGER NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    min_tenure_months INTEGER NOT NULL,
    upgrade_only INTEGER NOT NULL
);
CREATE TABLE customer_codes (
    user_id TEXT PRIMARY KEY REFERENCES users (user_id),
    code TEXT NOT NULL UNIQUE
);
CREATE TABLE loyalty_tiers (
    tier INTEGER PRIMARY KEY,
    tier_name TEXT NOT NULL,
    min_tenure_months INTEGER NOT NULL UNIQUE,
    bonus_pct INTEGER NOT NULL CHECK (bonus_pct > 0)
);
"""

# 기본 요금과 기본 제공량은 처음 구상 그대로다. 초과 단가는 명세 v2.4(2026-11-07)에서 낮췄다.
# 금액은 모두 부가세 별도(공급가액)다(SIM-06).
PLANS = [
    ("PLAN_BASIC", "베이직", 30000, 10000, 50, 2000, 500),
    ("PLAN_PRO", "프로", 80000, 50000, 200, 1500, 300),
    ("PLAN_ENTERPRISE", "엔터프라이즈", 200000, 200000, 1000, 1000, 200),
]

PROMOTIONS = [
    ("PROMO_PRO_UPGRADE", "프로 전환 3개월 30% 할인", "PLAN_PRO", "percent", 30, 3, "2026-09-01", "2026-12-31", 6, 1),
    ("PROMO_ENT_UPGRADE", "엔터프라이즈 전환 6개월 5만 원 할인", "PLAN_ENTERPRISE", "fixed", 50000, 6, "2026-10-01", "2027-03-31", 12, 1),
    ("PROMO_BASIC_LOYAL", "베이직 2년 이상 고객 2개월 1만 원 할인", "PLAN_BASIC", "fixed", 10000, 2, "2026-01-01", "2026-12-31", 24, 0),
    ("PROMO_PRO_SUMMER", "여름 프로 50% 할인(종료)", "PLAN_PRO", "percent", 50, 3, "2026-06-01", "2026-08-31", 0, 0),
]

# (명세 v3.5) 장기 이용 혜택(LOY-01, 02, 결정 42). 가입 기간이 기준 이상이면 모든 요금제의 기본 제공량을
# bonus_pct만큼 늘린다(늘린 양은 내림). 이벤트와 따로 계산하므로 함께 받을 수 있다(LOY-03).
# (단계, 이름, 필요한 가입 개월 수, 기본 제공량 추가 %)
LOYALTY_TIERS = [
    (1, "1단계", 6, 5),
    (2, "2단계", 12, 10),
    (3, "3단계", 24, 15),
]

# (user_id, 이름, 요금제, 가입일, 최근 3개월 평균 API 호출, 평균 스토리지 GB, 기록 개월 수, 메모)
SCENARIOS = [
    ("user_001", "(주)알파컴퍼니", "PLAN_BASIC", "2025-01-15", 8500, 30, 6, "제공량 안 → 유지"),
    ("user_002", "(주)베타소프트", "PLAN_BASIC", "2025-03-20", 42000, 120, 6, "베이직 초과 폭탄 → 프로 + 전환 할인"),
    ("user_003", "(주)감마테크", "PLAN_PRO", "2024-11-10", 45000, 150, 6, "프로 제공량 안 → 유지"),
    # (T3d) 장기 이용 혜택으로 제공량이 늘어 33,000회에서는 베이직과 프로+할인이 같아져, 34,000회로 올렸다.
    ("user_004", "(주)델타랩", "PLAN_BASIC", "2026-08-01", 34000, 40, 3, "가입 3개월이라 전환 할인 대상 아님 → 유지"),
    ("user_005", "(주)엡실론", "PLAN_BASIC", "2025-01-01", 34000, 40, 6, "전환 할인을 받으면 프로가 더 쌈(장기 할인 대상은 아님)"),
    ("user_006", "(주)제타웍스", "PLAN_PRO", "2025-02-01", 6000, 20, 6, "적게 씀 → 베이직 하향"),
    ("user_007", "(주)에타시스템", "PLAN_ENTERPRISE", "2024-01-10", 60000, 150, 6, "엔터프라이즈 과잉 → 프로 하향"),
    ("user_008", "(주)세타데이터", "PLAN_PRO", "2023-06-01", 120000, 400, 6, "프로 초과 → 엔터프라이즈 + 전환 할인"),
    ("user_009", "(주)이오타", "PLAN_PRO", "2026-09-15", 120000, 400, 1, "가입 1개월, 할인 없이도 엔터프라이즈"),
    ("user_010", "(주)카파", "PLAN_BASIC", "2025-06-01", 0, 0, 0, "기록 없음 → 추천 불가"),
    ("user_011", "(주)람다스토리지", "PLAN_BASIC", "2025-01-01", 5000, 150, 6, "스토리지 초과 → 프로"),
    ("user_012", "(주)뮤그로스", "PLAN_PRO", "2024-03-01", 70000, 180, 6, "사용량 증가 중이지만 아직 프로 유지"),
    ("user_013", "(주)뉴스타트", "PLAN_BASIC", "2026-10-01", 40000, 40, 1, "기록 1개월 → 프로(데이터 적음 안내)"),
    ("user_014", "(주)크사이", "PLAN_BASIC", "2024-01-01", 9000, 45, 6, "유지 + 장기 고객 할인"),
    ("user_015", "(주)오미크론", "PLAN_ENTERPRISE", "2023-03-01", 150000, 800, 6, "유지, 활동 급감(이탈 위험)"),
    # (T3d) user_016·017은 장기 혜택 2단계(10%)로 늘어난 지금 제공량에 맞춰 경계값을 옮겼다.
    ("user_016", "(주)파이", "PLAN_PRO", "2025-05-01", 55000, 220, 6, "제공량과 정확히 같음 → 초과 없음"),
    ("user_017", "(주)로", "PLAN_BASIC", "2025-07-01", 11001, 30, 6, "1회 초과도 1,000회 단위로 올림"),
    ("user_018", "(주)시그마", "PLAN_BASIC", "2025-09-01", 17000, 40, 6, "종료된 이벤트는 무시 → 유지"),
    # (T3d) 비교 기간 중 혜택 단계가 바뀌어도 12개월 총액이 같도록 가입 3개월(전환 할인 없음)·33,000회·61.5GB로 바꿨다.
    ("user_019", "(주)타우", "PLAN_BASIC", "2026-08-01", 33000, 61.5, 6, "베이직과 프로 12개월 금액 같음 → 현재 유지"),
    # (T3d) 장기 이용 혜택 3단계로 프로 제공량이 늘어 95,000회·350GB에서는 프로 유지가 맞게 되어 올렸다.
    ("user_020", "(주)입실론", "PLAN_PRO", "2024-08-01", 100000, 380, 6, "프로 초과 → 엔터프라이즈 + 전환 할인"),
]

# (명세 v3.2) 성수기 시나리오. 기본값과 달마다 다른 값(월: (API 호출, 스토리지 GB))을 적는다.
SEASON_MONTHS = [f"{2024 + (10 + i) // 12}-{(10 + i) % 12 + 1:02d}" for i in range(24)]  # 2024-11 ~ 2026-10
SEASONAL = [
    ("user_201", "(주)하늘세무회계", "PLAN_BASIC", "2023-02-01", (20000, 45.0),
     {"2025-01": (100000, 150.0), "2025-02": (100000, 150.0), "2025-03": (100000, 150.0), "2025-05": (100000, 150.0),
      "2026-01": (110000, 160.0), "2026-02": (110000, 160.0), "2026-03": (110000, 160.0), "2026-05": (110000, 160.0)},
     "1~3월·5월 성수기(연말정산·종합소득세). 3개월 평균으로는 유지, 계절 반영하면 연중 프로가 유리"),
    ("user_202", "(주)온누리선물몰", "PLAN_BASIC", "2024-09-10", (8000, 30.0),
     {"2025-01": (32000, 30.0), "2025-02": (32000, 30.0), "2026-01": (40000, 30.0), "2026-02": (40000, 30.0),
      "2026-08": (10000, 30.0), "2026-09": (10000, 30.0), "2026-10": (10000, 30.0)},
     "설 명절 1~2월 성수기, API 성장률 1.25. 성수기가 짧아 유지가 유리(예산 안내)"),
    ("user_203", "(주)바람캠핑", "PLAN_BASIC", "2024-06-01", (9000, 30.0),
     {"2025-08": (60000, 30.0)},
     "2025-08에 한 번만 튐 → 성수기 아님"),
]


def _seasonal_rows() -> tuple[list[tuple], list[tuple]]:
    users, logs = [], []
    for user_id, name, plan_id, signup, usual, special, _memo in SEASONAL:
        users.append((user_id, name, plan_id, signup))
        for index, month in enumerate(SEASON_MONTHS):
            api, storage = special.get(month, usual)
            logs.append((user_id, month, api, storage, 20 + (int(user_id[-3:]) + index) % 5))
    return users, logs


COMPANY_WORDS = ["클라우드", "데이터", "소프트", "랩스", "웍스", "시스템", "테크", "네트웍스", "솔루션", "스튜디오"]
COMPANY_HEADS = ["한빛", "누리", "가온", "다온", "새솔", "온결", "하람", "미르", "라온", "도담", "바른", "푸른"]


def _scenario_logs(user_id: str, api: int, storage: float, months: int) -> list[tuple]:
    """최근 3개월 평균이 정확히 api, storage가 되도록 만든다."""
    rows = []
    recent = MONTHS[-months:] if months else []
    # 정확한 경계값이 필요한 고객은 흔들지 않는다.
    exact = user_id in {"user_016", "user_017", "user_019"}
    api_d = 0 if exact else (api // 20) // 100 * 100
    st_d = 0 if exact else round(storage * 0.05, 1)
    shape = {-3: -1, -2: 0, -1: 1}
    for index, month in enumerate(recent):
        offset = index - len(recent)
        if len(recent) < 3:
            month_api, month_st = api, storage
        elif offset >= -3:
            sign = shape[offset]
            month_api, month_st = api + sign * api_d, round(storage + sign * st_d, 1)
        else:
            # 3개월 이전 기록은 평균에 들어가지 않는다. 조금 낮게 둔다.
            month_api, month_st = int(api * 0.9), round(storage * 0.9, 1)
        if user_id == "user_012":
            # 증가 추세: 5월 30,000 → 10월 80,000. 최근 3개월 평균 70,000.
            month_api = 30000 + 10000 * MONTHS.index(month)
        rows.append((user_id, month, month_api, month_st, _active_days(user_id, MONTHS.index(month))))
    return rows


def _active_days(user_id: str, month_index: int) -> int:
    if user_id == "user_015":
        return [26, 25, 24, 12, 8, 5][month_index]
    return 20 + (int(user_id[-3:]) + month_index) % 5


# 요금제별 사용량 범위(API 호출, 스토리지 GB). 명세 v2.4 단가 기준으로 각 요금제가 가장 싼 구간을 잡았다.
FIT = {
    "PLAN_BASIC": ((1000, 20000), (5, 60)),
    "PLAN_PRO": ((40000, 90000), (60, 220)),
    "PLAN_ENTERPRISE": ((140000, 260000), (300, 1100)),
}
OVER = {  # 지금 요금제보다 많이 써서 상위가 유리한 고객. 경계 가까이부터 넓게 퍼뜨린다.
    "PLAN_BASIC": ((36000, 75000), (40, 120)),
    "PLAN_PRO": ((135000, 230000), (200, 500)),
}
UNDER = {  # 지금 요금제보다 적게 써서 하위가 유리한 고객
    "PLAN_PRO": ((3000, 30000), (5, 50)),
    "PLAN_ENTERPRISE": ((60000, 125000), (100, 300)),
}
CODE_ALPHABET = "ACDEFGHJKMNPQRTUVWXY34679"  # 헷갈리는 글자(0/O, 1/I/L, 2/Z, 5/S, 8/B)는 뺐다.


def _usage_profile(rng: random.Random, plan_id: str) -> tuple[int, float]:
    kind = rng.choices(["fit", "over", "under"], weights=[75, 15, 10])[0]
    table = {"over": OVER, "under": UNDER}.get(kind, FIT)
    (api_lo, api_hi), (st_lo, st_hi) = table.get(plan_id, FIT[plan_id])
    return rng.randint(api_lo, api_hi), round(rng.uniform(st_lo, st_hi), 1)


def _random_users(rng: random.Random) -> tuple[list[tuple], list[tuple]]:
    users, logs = [], []
    plan_ids = [plan[0] for plan in PLANS]
    for number in range(21, 201):
        user_id = f"user_{number:03d}"
        name = f"(주){rng.choice(COMPANY_HEADS)}{rng.choice(COMPANY_WORDS)}"
        plan_id = rng.choices(plan_ids, weights=[5, 4, 1])[0]
        signup = f"{rng.randint(2023, 2026)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        if signup > "2026-10-01":
            signup = "2026-04-01"
        users.append((user_id, name, plan_id, signup))
        if rng.random() < 0.03:
            continue  # 일부는 기록이 없다.
        api, storage = _usage_profile(rng, plan_id)
        trend = rng.uniform(-0.03, 0.05)
        base_days = rng.randint(8, 28)
        start = max(0, MONTHS.index(signup[:7]) if signup[:7] in MONTHS else 0)
        for index in range(start, len(MONTHS)):
            factor = (1 + trend) ** (index - len(MONTHS) + 2) * rng.uniform(0.95, 1.05)
            days = min(31, max(0, base_days + rng.randint(-3, 3)))
            logs.append((user_id, MONTHS[index], int(api * factor), round(storage * factor, 1), days))
    return users, logs


def _customer_codes(user_ids: list[str], rng: random.Random) -> list[tuple]:
    """고객마다 겹치지 않는 고정 가칭 코드(예: K7Q-2F9). 고객 ID나 순서와 관계없는 무작위 값이다."""
    codes, used = [], set()
    for user_id in user_ids:
        while True:
            raw = "".join(rng.choice(CODE_ALPHABET) for _ in range(6))
            code = f"{raw[:3]}-{raw[3:]}"
            if code not in used:
                used.add(code)
                codes.append((user_id, code))
                break
    return codes


def build(db_path: Path = DB_PATH) -> dict:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    users = [(s[0], s[1], s[2], s[3]) for s in SCENARIOS]
    logs = [row for s in SCENARIOS for row in _scenario_logs(s[0], s[4], s[5], s[6])]
    more_users, more_logs = _random_users(random.Random(SEED))
    users += more_users
    logs += more_logs
    season_users, season_logs = _seasonal_rows()  # 맨 뒤에 붙여 기존 고객의 가칭 코드가 바뀌지 않게 한다.
    users += season_users
    logs += season_logs
    codes = _customer_codes([u[0] for u in users], random.Random(SEED + 1000))

    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO plans VALUES (?, ?, ?, ?, ?, ?, ?)", PLANS)
        conn.executemany("INSERT INTO promotions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", PROMOTIONS)
        conn.executemany("INSERT INTO loyalty_tiers VALUES (?, ?, ?, ?)", LOYALTY_TIERS)
        conn.executemany("INSERT INTO users VALUES (?, ?, ?, ?)", users)
        conn.executemany("INSERT INTO customer_codes VALUES (?, ?)", codes)
        conn.executemany(
            "INSERT INTO usage_logs (user_id, target_month, api_calls_used, storage_gb_used, active_days)"
            " VALUES (?, ?, ?, ?, ?)",
            logs,
        )
    conn.close()
    return {"plans": len(PLANS), "promotions": len(PROMOTIONS), "loyalty_tiers": len(LOYALTY_TIERS), "users": len(users), "usage_logs": len(logs),
            "customer_codes": len(codes)}


if __name__ == "__main__":
    counts = build()
    print(f"생성 완료: {DB_PATH.name} " + ", ".join(f"{name} {count}" for name, count in counts.items()))

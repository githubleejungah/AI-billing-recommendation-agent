"""실행 설정(SIM-09). 코드에 고정하지 않고 환경 변수로 바꿀 수 있는 값.

BILLING_AS_OF: 분석 기준일(YYYY-MM-01). 이 날 전달까지의 이용 기록을 분석하고, 비교 기간은 이 달 다음 달부터다.
비워 두면 가상 데이터에 맞춘 기본값(2026-11-01)을 쓴다. 테스트는 이 값을 지우고 기준일을 명시한다.
"""

from __future__ import annotations

import os
from datetime import date

DEFAULT_AS_OF = date(2026, 11, 1)  # 가상 데이터의 마지막 달(2026-10) 다음 날
AS_OF_ENV = "BILLING_AS_OF"


def as_of() -> date:
    raw = os.environ.get(AS_OF_ENV, "").strip()
    if not raw:
        return DEFAULT_AS_OF
    try:
        value = date.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"{AS_OF_ENV}는 YYYY-MM-01 형식이어야 합니다: {raw!r}") from None
    if value.day != 1:
        raise ValueError(f"{AS_OF_ENV}는 달의 첫날이어야 합니다(청구 마감 뒤 분석): {raw!r}")
    return value

"""모든 테스트에 공통으로 적용하는 설정."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def fixed_as_of(monkeypatch):
    """PC에 BILLING_AS_OF가 설정되어 있어도 테스트는 기본 기준일(2026-11-01)로 돈다(SIM-09)."""
    monkeypatch.delenv("BILLING_AS_OF", raising=False)

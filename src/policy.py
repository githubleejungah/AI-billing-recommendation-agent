"""전송 정책 파일 읽기(SEC-14, security.md 5.1).

정책 파일은 외부 AI 전송 기준, 범주 구간, 허용 모델, 보관 기간, 선별 기준, 상담 위험 주제를 담는다.
승인 정보가 비어 있거나 값이 잘못되면 읽지 않고 오류를 낸다(잘못된 기준으로 돌지 않게).
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from pathlib import Path

import audit

POLICY_PATH = Path(__file__).resolve().parent.parent / "config" / "data_policy.toml"
RETENTION_KEYS = ("reports", "run_logs", "audit", "sent_log", "history_after_contract_end", "tickets_after_close")
SELECTION_KEYS = ("min_saving_12m", "min_saving_pct", "max_alerts_per_month", "renotify_days")
PEAK_ALERT_KEYS = ("first_notice_days_before", "reminder_days_before_deadline", "min_extra_cost")


class PolicyError(Exception):
    pass


@dataclass(frozen=True)
class Policy:
    raw: dict
    sha256: str
    path: Path

    @property
    def approval(self) -> dict:
        return self.raw["approval"]

    @property
    def allowed_models(self) -> tuple[str, ...]:
        return tuple(self.raw["external_ai"]["allowed_models"])

    @property
    def overage_multiples(self) -> tuple[Fraction, ...]:
        return tuple(Fraction(str(v)) for v in self.raw["categories"]["overage_multiples"])

    @property
    def saving_pct(self) -> tuple[Fraction, ...]:
        return tuple(Fraction(str(v)) for v in self.raw["categories"]["saving_pct"])

    @property
    def stability_change_pct(self) -> int:
        return self.raw["categories"]["stability_change_pct"]

    @property
    def peak_increase_multiples(self) -> tuple[Fraction, ...]:
        return tuple(Fraction(str(v)) for v in self.raw["categories"]["peak_increase_multiples"])

    @property
    def retention_days(self) -> dict[str, int]:
        return dict(self.raw["retention_days"])

    @property
    def selection(self) -> dict:
        return dict(self.raw["selection"])

    @property
    def peak_alerts(self) -> dict[str, int]:
        return dict(self.raw["peak_alerts"])

    @property
    def risk_topics(self) -> dict[str, dict]:
        return {key: dict(value) for key, value in self.raw["risk_topics"].items()}


def _increasing(values, name: str, low: Fraction | None = None, high: Fraction | None = None) -> None:
    if not isinstance(values, list) or not values:
        raise PolicyError(f"{name}: 비어 있지 않은 목록이어야 합니다")
    numbers = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PolicyError(f"{name}: 숫자만 쓸 수 있습니다")
        numbers.append(Fraction(str(value)))
    if any(b <= a for a, b in zip(numbers, numbers[1:])):
        raise PolicyError(f"{name}: 작은 값부터 커지는 순서여야 합니다")
    if low is not None and numbers[0] <= low:
        raise PolicyError(f"{name}: {low}보다 커야 합니다")
    if high is not None and numbers[-1] >= high:
        raise PolicyError(f"{name}: {high}보다 작아야 합니다")


def _positive_ints(section: dict, keys: tuple[str, ...], name: str) -> None:
    for key in keys:
        value = section.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise PolicyError(f"{name}.{key}: 0보다 큰 정수여야 합니다")


def validate(raw: dict) -> None:
    for section in ("approval", "external_ai", "categories", "retention_days", "selection", "peak_alerts",
                    "risk_topics"):
        if not isinstance(raw.get(section), dict):
            raise PolicyError(f"[{section}] 항목이 없습니다")

    approval = raw["approval"]
    for key in ("approved_by", "approved_at", "reason"):
        if not str(approval.get(key, "")).strip():
            raise PolicyError(f"승인 정보가 비어 있어 실행하지 않습니다: approval.{key}")
    try:
        date.fromisoformat(str(approval["approved_at"]))
    except ValueError:
        raise PolicyError("approval.approved_at은 YYYY-MM-DD 형식이어야 합니다") from None

    external = raw["external_ai"]
    for key in ("send_customer_numbers", "send_ratios"):
        if external.get(key) is not False:
            # 수치·비율을 보내는 카드 형식은 없다. 바꾸려면 보안 검토와 코드 변경이 함께 필요하다.
            raise PolicyError(f"external_ai.{key}는 false만 지원합니다")
    models = external.get("allowed_models")
    if not isinstance(models, list) or not models or not all(isinstance(m, str) and m.strip() for m in models):
        raise PolicyError("external_ai.allowed_models는 모델 ID 목록이어야 합니다")

    categories = raw["categories"]
    _increasing(categories.get("overage_multiples"), "categories.overage_multiples")
    if Fraction(str(categories["overage_multiples"][0])) != 1:
        raise PolicyError("categories.overage_multiples는 1(기본 제공량)로 시작해야 합니다")
    _increasing(categories.get("saving_pct"), "categories.saving_pct", low=Fraction(0), high=Fraction(100))
    _increasing(categories.get("peak_increase_multiples"), "categories.peak_increase_multiples", low=Fraction(1))
    change = categories.get("stability_change_pct")
    if isinstance(change, bool) or not isinstance(change, int) or not 0 < change < 100:
        raise PolicyError("categories.stability_change_pct는 1~99 사이 정수여야 합니다")

    _positive_ints(raw["retention_days"], RETENTION_KEYS, "retention_days")
    _positive_ints(raw["selection"], SELECTION_KEYS, "selection")
    if not isinstance(raw["selection"].get("exclude_low_data"), bool):
        raise PolicyError("selection.exclude_low_data는 true 또는 false여야 합니다")
    _positive_ints(raw["peak_alerts"], PEAK_ALERT_KEYS, "peak_alerts")

    if not raw["risk_topics"]:
        raise PolicyError("risk_topics가 비어 있습니다")
    for key, topic in raw["risk_topics"].items():
        if not isinstance(topic, dict) or not str(topic.get("label", "")).strip():
            raise PolicyError(f"risk_topics.{key}.label이 없습니다")
        keywords = topic.get("keywords")
        if not isinstance(keywords, list) or not keywords or not all(isinstance(k, str) and k.strip() for k in keywords):
            raise PolicyError(f"risk_topics.{key}.keywords는 비어 있지 않은 문자열 목록이어야 합니다")


def load(path: Path = POLICY_PATH) -> Policy:
    data = path.read_bytes()
    try:
        raw = tomllib.loads(data.decode("utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise PolicyError(f"정책 파일 형식 오류: {error}") from None
    validate(raw)
    return Policy(raw=raw, sha256=hashlib.sha256(data).hexdigest(), path=path)


def record_start(policy: Policy, audit_path: Path = audit.AUDIT_PATH) -> list[dict]:
    """시작할 때 정책 해시를 남긴다. 지난번 해시와 다르면 "정책 변경"을 따로 남긴다(security.md 9장)."""
    previous = None
    for entry in reversed(audit.read(audit_path)):
        if entry.get("event") == "policy_loaded":
            previous = entry.get("policy_sha256")
            break
    entries = []
    if previous is not None and previous != policy.sha256:
        entries.append(audit.record(
            "policy_changed", audit_path, before_sha256=previous, after_sha256=policy.sha256,
            approved_by=policy.approval["approved_by"], approved_at=str(policy.approval["approved_at"]),
            reason=policy.approval["reason"]))
    entries.append(audit.record("policy_loaded", audit_path, policy_sha256=policy.sha256))
    return entries

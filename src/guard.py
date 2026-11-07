"""외부 AI 전송 보안 장치(SEC-05, 07, 08, 11, 13, ASK-05, security.md 5·6·9장).

- 별칭: 실행마다 새로 정하는 고객 별칭(C1). 고객 ID와의 연결은 이 실행의 메모리 안에만 있다.
- 상황 카드: Claude가 받는 고객 정보의 전부. 범주와 자리표시자 이름만 담고 고객 수치는 담지 않는다.
- 질문 가림: 고객 질문의 개인정보 형식과 숫자를 Claude·Slack에 보내기 전에 가린다.
- 전송 전 검사: 보낼 요청 전체를 규칙 1~5와 허용 모델로 검사하고, 걸리면 보내지 않고 감사 기록을 남긴다.

1차 방어는 카드에 고객 수치를 넣지 않는 것이고, 전송 전 검사는 코드 실수에 대한 안전망(2차 방어)이다.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Iterable

import audit
import data_access
import knowledge
import policy as policy_module
import report
import simulator

# --- 범주 문구(security.md 5장) ---

ACTION_LABELS = {"upgrade": "상향", "downgrade": "하향", "keep": "유지", "keep_with_promotion": "유지"}  # 이벤트는 따로 붙인다
FORECAST_METHODS = ("3개월 평균", "계절 반영")
NOT_JUDGED = "판정 안 함(기록 부족)"
# 받을 수 없는 이벤트의 이유를 범주로 바꾼다. 원래 문구에는 고객의 가입 기간이 들어 있다.
REASON_CATEGORIES = (
    ("이용 ", "가입 기간 조건 미달"),
    ("상위 요금제로 바꿀 때만", "상위 전환에만 적용"),
    ("이벤트 기간 아님", "이벤트 기간 아님"),
)

# --- 검사 형식(security.md 6장, ASK-05) ---

USER_ID = re.compile(r"user_\d+")
CODE = re.compile(rf"(?<![A-Za-z0-9])[{re.escape('ACDEFGHJKMNPQRTUVWXY34679')}]{{3}}-"
                  rf"[{re.escape('ACDEFGHJKMNPQRTUVWXY34679')}]{{3}}(?![A-Za-z0-9])")
RRN = re.compile(r"(?<!\d)\d{6}\s?-\s?[1-8]\d{6}(?!\d)")  # 주민등록번호
BIZ = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{5}(?!\d)")  # 사업자등록번호
PHONE = re.compile(r"(?<!\d)(?:\+82[-\s]?)?0\d{1,2}[-\s.]?\d{3,4}[-\s.]?\d{4}(?!\d)")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ACCOUNT = re.compile(r"(?<!\d)\d{2,6}(?:-\d{2,8}){2,3}(?!\d)")  # 계좌번호(하이픈으로 나뉜 숫자 묶음)
DIGITS = re.compile(r"\d+(?:[.,]\d+)*")
MASK = "[가림]"
MASK_NUMBER = "[숫자]"
CORE_NAME_MIN = 3  # "(주)" 없이 쓴 이름은 3글자 이상만 찾는다. "로", "카파"는 보통 낱말과 겹친다.
NUMBER_EXEMPT_KEYS = {"model", "id", "tool_use_id"}  # 시스템이 붙이는 값. 숫자·연락처 형식 검사에서 뺀다.

RULES = {
    1: "규칙 1 고객 이름",
    2: "규칙 2 고객 ID·가칭 코드",
    3: "규칙 3 허용 목록 밖 숫자",
    4: "규칙 4 연락처·등록번호 형식",
    5: "규칙 5 가림 전 질문 원문",
    "model": "SEC-13 허용 모델 밖",
}


class GuardError(Exception):
    """상황 카드를 만들 수 없음(알 수 없는 값). 모르는 것은 보내지 않는다."""


class GuardBlocked(GuardError):
    """전송 전 검사에 걸림. 메시지에는 걸린 규칙만 있고 걸린 내용은 없다."""

    def __init__(self, rules: list[str]):
        super().__init__("외부 전송 차단: " + ", ".join(rules))
        self.rules = rules


# --- 별칭(SEC-05) ---

class Aliases:
    """실행마다 새로 만든다. 같은 실행 안에서는 같은 고객이 같은 별칭을 받는다."""

    def __init__(self):
        self._by_id: dict[str, str] = {}
        self._by_alias: dict[str, str] = {}

    def alias(self, user_id: str) -> str:
        if user_id not in self._by_id:
            alias = f"C{len(self._by_id) + 1}"
            self._by_id[user_id] = alias
            self._by_alias[alias] = user_id
        return self._by_id[user_id]

    def user_id(self, alias: str) -> str:
        if alias not in self._by_alias:
            raise GuardError("이 실행에서 만든 별칭이 아닙니다")
        return self._by_alias[alias]


# --- 범주 ---

def _fmt(value: Fraction) -> str:
    text = format((Decimal(value.numerator) / Decimal(value.denominator)).normalize(), "f")
    return text


def overage_category(used, included, multiples: tuple[Fraction, ...]) -> str:
    """기본 제공량 대비 초과 정도. 비율 자체는 내보내지 않는다(역산 방지)."""
    included = Fraction(str(included))
    if included <= 0:
        raise GuardError("기본 제공량이 0 이하입니다")
    ratio = Fraction(str(used)) / included
    if ratio <= multiples[0]:
        return "기본 제공량 이내"
    if len(multiples) == 1:
        return "기본 제공량 초과"
    if ratio < multiples[1]:
        return f"기본 제공량의 {_fmt(multiples[1])}배 미만"
    for low, high in zip(multiples[1:], multiples[2:]):
        if ratio < high:
            return f"기본 제공량의 {_fmt(low)}~{_fmt(high)}배"
    return f"기본 제공량의 {_fmt(multiples[-1])}배 이상"


def saving_category(saving_12m: int, current_total_12m: int, bands: tuple[Fraction, ...]) -> str:
    """절감 규모. 반올림한 %가 아니라 정확한 비율로 구간을 정한다."""
    if saving_12m <= 0 or current_total_12m <= 0:
        return "없음"
    pct = Fraction(saving_12m * 100, current_total_12m)
    if pct < bands[0]:
        return f"{_fmt(bands[0])}% 미만"
    for low, high in zip(bands, bands[1:]):
        if pct < high:
            return f"{_fmt(low)}~{_fmt(high)}%"
    return f"{_fmt(bands[-1])}% 이상"


def peak_category(season: dict, multiples: tuple[Fraction, ...]) -> str:
    """성수기 증가 정도. 성수기로 판정된 달 가운데 가장 작은 배수로 구간을 정한다(보수적으로)."""
    if season["status"] == "판정 안 함":
        return NOT_JUDGED
    if season["status"] != "있음":
        return "없음"
    ratios = [Fraction(o["ratio"]) for item in season["evidence"] for o in item["occurrences"]]
    if not ratios:
        raise GuardError("성수기 근거가 없습니다")
    smallest = min(ratios)
    base = simulator.SEASON_HIGH_RATIO
    if smallest < multiples[0]:
        return f"있음(증가 {_fmt(base)}~{_fmt(multiples[0])}배)"
    for low, high in zip(multiples, multiples[1:]):
        if smallest < high:
            return f"있음(증가 {_fmt(low)}~{_fmt(high)}배)"
    return f"있음(증가 {_fmt(multiples[-1])}배 이상)"


def stability_phrase(status: str, change_pct: int) -> str:
    return f"{status} (사용량 ±{change_pct}% 기준)"


def reason_category(reason: str) -> str:
    for prefix, category in REASON_CATEGORIES:
        if reason.startswith(prefix):
            return category
    raise GuardError("알 수 없는 이벤트 제외 이유입니다")


def category_phrases(policy: policy_module.Policy) -> list[str]:
    """카드에 나올 수 있는 고정 범주 문구 전체. 고객마다 바뀌지 않는 코드 상수라 그 숫자는 허용 목록에 든다."""
    multiples = policy.overage_multiples
    phrases = ["기본 제공량 이내", "기본 제공량 초과", NOT_JUDGED, *FORECAST_METHODS,
               stability_phrase("", policy.stability_change_pct)]
    if len(multiples) > 1:
        phrases.append(f"기본 제공량의 {_fmt(multiples[1])}배 미만")
        phrases += [f"기본 제공량의 {_fmt(a)}~{_fmt(b)}배" for a, b in zip(multiples[1:], multiples[2:])]
        phrases.append(f"기본 제공량의 {_fmt(multiples[-1])}배 이상")
    bands = policy.saving_pct
    phrases.append(f"{_fmt(bands[0])}% 미만")
    phrases += [f"{_fmt(a)}~{_fmt(b)}%" for a, b in zip(bands, bands[1:])]
    phrases.append(f"{_fmt(bands[-1])}% 이상")
    peaks = policy.peak_increase_multiples
    phrases.append(f"있음(증가 {_fmt(simulator.SEASON_HIGH_RATIO)}~{_fmt(peaks[0])}배)")
    phrases += [f"있음(증가 {_fmt(a)}~{_fmt(b)}배)" for a, b in zip(peaks, peaks[1:])]
    phrases.append(f"있음(증가 {_fmt(peaks[-1])}배 이상)")
    return phrases


# --- 상황 카드(SEC-07) ---

def _option(sim: dict, plan_id: str) -> dict:
    return next(o for o in sim["options"] if o["plan_id"] == plan_id)


def _yes_no(value) -> str:
    if value is None:
        return NOT_JUDGED
    return "있음" if value else "없음"


def situation_card(sim: dict, alias: str, policy: policy_module.Policy, registry: dict | None = None) -> str:
    """Claude에 보내는 고객 정보의 전부. 고객 수치·이름·ID·날짜는 넣지 않는다."""
    registry = registry or report.load_registry()
    lines = [f"고객: {alias}"]
    if sim["status"] == "no_usage":
        lines += ["상태: 이용 기록 없음", f"현재 요금제: {sim['current_plan_name']}"]
    elif sim["status"] == "ok":
        rec = sim["recommendation"]
        if rec["action"] not in ACTION_LABELS:
            raise GuardError("알 수 없는 추천 종류입니다")
        if sim["stability"]["change_pct"] != policy.stability_change_pct:
            raise GuardError("시뮬레이터의 안정성 변화폭이 정책 파일과 다릅니다")
        current = next(o for o in sim["options"] if o["is_current"])
        best = _option(sim, rec["plan_id"])
        target = (f"{rec['plan_name']} ({ACTION_LABELS[rec['action']]})"
                  if rec["action"] in ("upgrade", "downgrade") else f"현재 요금제 {ACTION_LABELS[rec['action']]}")
        if best["promotion"]:
            target += f" + 이벤트 \"{best['promotion']['promo_name']}\""
        forecast = sim["forecast"]
        if forecast["method"] not in FORECAST_METHODS:
            raise GuardError("알 수 없는 예측 방법입니다")
        multiples = policy.overage_multiples
        api = overage_category(forecast["api_calls"], current["included_api_calls"], multiples)
        storage = overage_category(forecast["storage_gb"], current["included_storage_gb"], multiples)
        season = sim["seasonality"]
        activity = sim["activity"]
        lines += [
            "상태: 분석 완료",
            f"현재 요금제: {sim['current_plan_name']} → 추천: {target}",
            f"예측 방법: {forecast['method']}",
            f"초과 정도(최근 사용량 기준): API 사용량은 {api}, 스토리지 사용량은 {storage}",
            f"절감 규모: {saving_category(rec['saving_12m'], rec['current_total_12m'], policy.saving_pct)}",
            f"추천 안정성: {stability_phrase(sim['stability']['status'], policy.stability_change_pct)}",
            f"이탈 위험: {_yes_no(None if activity is None else activity['churn_risk'])}"
            f" / 기록 부족: {_yes_no(rec['low_data'])}",
            f"성수기: {peak_category(season, policy.peak_increase_multiples)}"
            f" / 다가오는 성수기: {'있음' if season.get('upcoming') else '없음'}",
        ]
        skipped, seen = [], set()
        for option in (current, best):
            for item in option["skipped_promotions"]:
                if item["promo_id"] not in seen:
                    seen.add(item["promo_id"])
                    skipped.append(f"  - \"{item['promo_name']}\": {reason_category(item['reason'])}")
        lines.append("받을 수 없는 이벤트:" + ("" if skipped else " 없음"))
        lines += skipped
    else:
        raise GuardError("알 수 없는 분석 상태입니다")

    available = [name for name, value in report.values(sim, registry).items() if value is not None]
    lines.append("쓸 수 있는 자리표시자(값은 검증 뒤 코드가 채움):")
    lines += [f"  {{{name}}} {registry['keys'][name].get('description', '')}".rstrip() for name in available]
    return "\n".join(lines)


# --- 질문 가림(ASK-05) ---

def name_variants(names: Iterable[str]) -> list[str]:
    """고객 이름과 흔히 쓰는 변형. 긴 것부터 찾도록 정렬한다."""
    variants = set()
    for name in names:
        core = name
        for prefix in ("(주)", "㈜", "주식회사"):
            core = core.removeprefix(prefix)
        core = core.strip()
        variants |= {name, f"㈜{core}", f"(주){core}", f"주식회사 {core}", f"주식회사{core}"}
        if len(core) >= CORE_NAME_MIN:
            variants.add(core)
    return sorted(variants, key=len, reverse=True)


def mask_question(text: str, names: Iterable[str]) -> str:
    """개인정보 형식과 고객 이름은 [가림], 남은 숫자는 [숫자]로 바꾼다.

    금액 질문은 코드가 시뮬레이터 값으로 답하므로 질문 속 숫자를 밖으로 보낼 필요가 없다.
    """
    for pattern in (RRN, BIZ, PHONE, EMAIL, ACCOUNT):
        text = pattern.sub(MASK, text)
    for variant in name_variants(names):
        text = text.replace(variant, MASK)
    return DIGITS.sub(MASK_NUMBER, text)


# --- 전송 전 검사(SEC-08) ---

def _normalize(token: str) -> str:
    token = token.replace(",", "")
    try:
        return format(Decimal(token).normalize(), "f")
    except InvalidOperation:
        return token


def numbers_in(text: str) -> set[str]:
    return {_normalize(m.group()) for m in DIGITS.finditer(text)}


def _strings(value, key: str | None = None) -> Iterable[tuple[str | None, str]]:
    if isinstance(value, str):
        yield key, value
    elif isinstance(value, dict):
        for child_key, child in value.items():
            yield from _strings(child, child_key)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _strings(child, key)


def canonical(request: dict) -> bytes:
    return json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class Screen:
    """한 실행 동안 쓰는 검사 기준."""

    names: tuple[str, ...]  # 이름 변형 전부(긴 것부터)
    codes: frozenset[str]
    numbers: frozenset[str]

    @classmethod
    def build(cls, conn: sqlite3.Connection, policy: policy_module.Policy, registry: dict | None = None,
              constants: Iterable[str] = ()) -> "Screen":
        """허용 숫자: 요금제·이벤트·지식 문서의 숫자(상품 정보), 고정 범주 문구와 자리표시자 설명의 숫자,
        호출하는 코드가 넘기는 고정 문구(지시문, 도구 설명)의 숫자."""
        registry = registry or report.load_registry()
        texts = [json.dumps(p.__dict__, ensure_ascii=False) for p in data_access.load_plans(conn)]
        texts += [json.dumps(p.__dict__, ensure_ascii=False) for p in data_access.load_promotions(conn)]
        texts += [path.read_text(encoding="utf-8") for path in sorted(knowledge.KNOWLEDGE_DIR.glob("*.md"))]
        texts += category_phrases(policy)
        texts += [spec.get("description", "") for spec in registry["keys"].values()]
        texts += list(constants)
        numbers = set()
        for text in texts:
            numbers |= numbers_in(text)
        return cls(names=tuple(name_variants(data_access.customer_names(conn))),
                   codes=frozenset(data_access.customer_codes(conn)), numbers=frozenset(numbers))

    def mask(self, text: str) -> str:
        return mask_question(text, self.names)

    def check(self, request: dict, raw_questions: Iterable[str] = ()) -> list[str]:
        """걸린 규칙 목록. 걸린 내용 자체는 돌려주지 않는다(그것도 고객 정보일 수 있음)."""
        pairs = list(_strings(request))
        everything = "\n".join(text for _, text in pairs)
        checked = [text for key, text in pairs if key not in NUMBER_EXEMPT_KEYS]
        hits = set()
        if any(variant in everything for variant in self.names):
            hits.add(1)
        if USER_ID.search(everything) or CODE.search(everything) or any(c in everything for c in self.codes):
            hits.add(2)
        for text in checked:
            stripped = report.ALIAS.sub(" ", report.PLACEHOLDER.sub(" ", text))
            if numbers_in(stripped) - self.numbers:
                hits.add(3)
            if any(p.search(text) for p in (RRN, BIZ, PHONE, EMAIL)):
                hits.add(4)
        for question in raw_questions:
            question = question.strip()
            if question and self.mask(question) != question and question in everything:
                hits.add(5)
        return [RULES[number] for number in sorted(hits)]


def check_model(policy: policy_module.Policy, model: str) -> None:
    """허용 모델 목록 밖이면 실행하지 않는다(SEC-13)."""
    if model not in policy.allowed_models:
        raise policy_module.PolicyError(f"허용 모델 목록에 없는 모델입니다: {model}")


def preflight(request: dict, *, screen: Screen, policy: policy_module.Policy, run_id: str, kinds: list[str],
              raw_questions: Iterable[str] = (), audit_path: Path = audit.AUDIT_PATH) -> dict:
    """Claude API를 부르기 직전에 요청 전체를 검사한다. 걸리면 차단을 기록하고 GuardBlocked를 낸다.

    통과하면 전송 표(해시·크기·종류·모델)를 돌려준다. 호출이 끝나면 record_sent로 토큰과 함께 남긴다.
    """
    rules = [] if request.get("model") in policy.allowed_models else [RULES["model"]]
    rules += screen.check(request, raw_questions)
    body = canonical(request)
    ticket = {"run_id": run_id, "request_sha256": hashlib.sha256(body).hexdigest(), "request_bytes": len(body),
              "kinds": list(kinds), "model": request.get("model")}
    if rules:
        audit.record("external_send", audit_path, result="차단", rules=rules, **ticket)
        raise GuardBlocked(rules)
    return ticket


def record_sent(ticket: dict, audit_path: Path = audit.AUDIT_PATH, input_tokens: int | None = None,
                output_tokens: int | None = None) -> dict:
    return audit.record("external_send", audit_path, result="통과", rules=[], input_tokens=input_tokens,
                        output_tokens=output_tokens, **ticket)

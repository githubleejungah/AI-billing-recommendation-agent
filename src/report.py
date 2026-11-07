"""리포트 검증(REP-02)과 Slack 메시지 생성.

AI가 쓴 리포트의 숫자가 모두 도구 결과에 있는지 확인한다. 하나라도 없으면 리포트를 내보내지 않는다.
"""

from __future__ import annotations

import re

# 1,234 / 1,234.5 / 1234 / 12.5. 앞이 글자·숫자·밑줄·점이면 다른 숫자의 일부(user_002, 1.5.3)로 보고 건너뛴다.
NUMBER = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
# 목록 번호(1. 2.)와 개월 수처럼 흔한 작은 정수는 도구 결과와 관계없이 허용한다.
ALWAYS_ALLOWED = {str(n) for n in range(0, 13)}
SLACK_TEXT_LIMIT = 3000


class ReportRejected(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def _normalize(token: str) -> str:
    value = token.replace(",", "")
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value


def numbers_in(text: str) -> list[str]:
    return [_normalize(match.group()) for match in NUMBER.finditer(text)]


def allowed_numbers(tool_outputs: list[str]) -> set[str]:
    return ALWAYS_ALLOWED | {number for output in tool_outputs for number in numbers_in(output)}


def _won(amount: int) -> str:
    return f"{amount:,}"


def check_report(text: str, tool_outputs: list[str], simulation: dict) -> list[str]:
    """문제 목록을 돌려준다. 빈 목록이면 통과."""
    problems = []
    allowed = allowed_numbers(tool_outputs)
    unknown = sorted({n for n in numbers_in(text) if n not in allowed})
    if unknown:
        problems.append(f"도구 결과에 없는 숫자: {', '.join(unknown)}")

    if simulation.get("status") == "no_usage":
        if "기록" not in text:
            problems.append("이용 기록이 없다는 안내가 없음")
        return problems

    rec = simulation["recommendation"]
    current = next(o for o in simulation["options"] if o["is_current"])
    required = {
        "현재 월 청구액": _won(current["monthly"]["total"]),
        "추천 요금제": rec["plan_name"],
    }
    if rec["saving_12m"]:
        required["12개월 절감액"] = _won(rec["saving_12m"])
    if rec["promo_id"]:
        promo = next(o["promotion"] for o in simulation["options"] if o["plan_id"] == rec["plan_id"])
        required["이벤트"] = promo["promo_name"]
    for label, value in required.items():
        if value not in text:
            problems.append(f"{label}({value})이 리포트에 없음")
    return problems


def verify_report(text: str, tool_outputs: list[str], simulation: dict) -> str:
    problems = check_report(text, tool_outputs, simulation)
    if problems:
        raise ReportRejected(problems)
    return text


def slack_message(report_text: str, simulation: dict, customer_name: str) -> dict:
    """Slack Incoming Webhook에 보낼 Block Kit JSON. 보내는 일은 하지 않는다."""
    # Slack mrkdwn은 굵게를 *로 쓴다.
    body = re.sub(r"\*\*(.+?)\*\*", r"*\1*", report_text)
    body = re.sub(r"^#+\s*(.+)$", r"*\1*", body, flags=re.MULTILINE)
    if len(body) > SLACK_TEXT_LIMIT:
        body = body[: SLACK_TEXT_LIMIT - 1] + "…"
    title = f"요금제 추천 리포트: {customer_name} ({simulation['user_id']})"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": title[:150]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
    ]
    rec = simulation.get("recommendation")
    if rec:
        blocks.insert(1, {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*현재*\n{simulation['current_plan_name']}"},
            {"type": "mrkdwn", "text": f"*추천*\n{rec['plan_name']}"},
            {"type": "mrkdwn", "text": f"*12개월 절감*\n{_won(rec['saving_12m'])}원"},
            {"type": "mrkdwn", "text": f"*이벤트*\n{rec['promo_id'] or '없음'}"},
        ]})
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": f"분석 기준일 {simulation['as_of']} · 금액은 시뮬레이터 계산값"},
    ]})
    return {"text": title, "blocks": blocks}

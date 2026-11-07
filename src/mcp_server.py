"""과금 DB 조회·시뮬레이션·지식 문서 검색 도구를 MCP로 제공하는 서버.

실행(stdio): python src/mcp_server.py
- 정해진 읽기 전용 도구만 있다. SQL을 받아 실행하는 도구는 두지 않는다(RET-02).
- 고객 이름은 내보내지 않는다. 이름은 리포트를 검증한 뒤 코드가 붙인다(SEC-03).
- 잘못된 입력은 예외 대신 이유가 담긴 JSON 오류로 돌려준다. 받은 입력값은 되돌려 보내지 않는다.
- 이 서버를 Claude Code에서 직접 쓰는 것은 가상 데이터로 개발할 때만이다(security.md 3.1).
"""

from __future__ import annotations

import json
import re
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer  # noqa: E402

import knowledge  # noqa: E402
import simulator  # noqa: E402

USER_ID = re.compile(r"user_\d{3}")
INVALID_USER_ID = {"error": "invalid_user_id", "message": "user_id는 user_001 형식이어야 합니다."}

# 받을 수 없는 이벤트의 이유를 범주로 바꾼다. 원래 문구에는 고객의 가입 기간이 들어 있어
# 외부로 나가는 결과에는 범주만 쓴다(security.md 5장).
REASON_CATEGORIES = (
    ("이용 ", "가입 기간 조건 미달"),
    ("상위 요금제로 바꿀 때만", "상위 전환에만 적용"),
    ("이벤트 기간 아님", "이벤트 기간 아님"),
)


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False)


def _valid(user_id: str) -> bool:
    return bool(USER_ID.fullmatch(user_id))


def _without_name(result: dict) -> dict:
    return {key: value for key, value in result.items() if key != "name"}


def reason_category(reason: str) -> str:
    for prefix, category in REASON_CATEGORIES:
        if reason.startswith(prefix):
            return category
    return "조건 미달"


def promotion_status(simulation: dict) -> dict[str, dict]:
    """이 고객 기준 이벤트별 대상 여부. 기록이 없는 고객은 비교를 하지 않으므로 알 수 없음."""
    if simulation["status"] != "ok":
        return {}
    status = {}
    for option in simulation["options"]:
        for skipped in option["skipped_promotions"]:
            status[skipped["promo_id"]] = {"eligible": False, "reason": reason_category(skipped["reason"])}
        if option["promotion"]:
            status.setdefault(option["promotion"]["promo_id"], {"eligible": True, "reason": None})
    return status


def chunk_eligibility(chunk: knowledge.Chunk, status: dict[str, dict] | None) -> dict:
    """조각의 문서가 다루는 이벤트별 대상 여부(KNW-06). 이벤트를 다루지 않는 문서면 빈 목록."""
    promos = []
    for promo_id in chunk.document.promo_ids:
        if status is None:
            promos.append({"promo_id": promo_id, "status": "고객 지정 안 함"})
        elif promo_id not in status:
            promos.append({"promo_id": promo_id, "status": "알 수 없음"})
        elif status[promo_id]["eligible"]:
            promos.append({"promo_id": promo_id, "status": "대상"})
        else:
            promos.append({"promo_id": promo_id, "status": "대상 아님", "reason": status[promo_id]["reason"]})
    return {"promotions": promos}


def build_server(db_path: Path = simulator.DB_PATH, knowledge_dir: Path = knowledge.KNOWLEDGE_DIR) -> MCPServer:
    server = MCPServer("billing-tools")
    kb = knowledge.KnowledgeBase(knowledge_dir)

    @server.tool(description="요금제 목록과 기본 제공량, 초과 단가를 돌려준다.")
    def list_plans() -> str:
        with closing(simulator.connect(db_path)) as conn:
            return _json([plan.__dict__ for plan in simulator.load_plans(conn)])

    @server.tool(description="이벤트 상품 목록과 적용 조건을 돌려준다.")
    def list_promotions() -> str:
        with closing(simulator.connect(db_path)) as conn:
            return _json([promo.__dict__ for promo in simulator.load_promotions(conn)])

    @server.tool(description="고객의 현재 요금제, 가입일, 월별 이용 기록(최대 6개월)을 돌려준다.")
    def get_customer_usage(user_id: str) -> str:
        if not _valid(user_id):
            return _json(INVALID_USER_ID)
        with closing(simulator.connect(db_path)) as conn:
            try:
                customer = simulator.load_customer(conn, user_id)
            except simulator.CustomerNotFound:
                return _json({"error": "customer_not_found", "user_id": user_id})
            customer.pop("name")
            return _json({**customer, "usage_history": simulator.load_usage(conn, user_id)})

    @server.tool(description=(
        "고객의 최근 3개월 평균 사용량으로 모든 요금제와 받을 수 있는 이벤트의 월 청구액, 12개월 총액, "
        "추천 결과와 절감액을 계산한다. 리포트의 금액은 반드시 이 결과의 값만 쓴다."
    ))
    def simulate_plans(user_id: str) -> str:
        if not _valid(user_id):
            return _json(INVALID_USER_ID)
        with closing(simulator.connect(db_path)) as conn:
            try:
                return _json(_without_name(simulator.simulate(conn, user_id)))
            except simulator.CustomerNotFound:
                return _json({"error": "customer_not_found", "user_id": user_id})

    @server.tool(description=(
        "요금제 설명, 이벤트 안내, 변경 정책, 고객 응대 가이드 문서에서 근거를 찾는다. "
        f"결과는 조각 ID, 문서 제목, 소제목, 본문이다. top_k는 1~{knowledge.MAX_TOP_K}. "
        "user_id를 주면 각 조각이 다루는 이벤트에 대해 그 고객이 대상인지 표시한다. "
        "관련 문서가 없으면 빈 목록을 돌려준다. 리포트에 인용할 때는 [근거: 조각ID] 형식을 쓴다."
    ))
    def search_knowledge(query: str, top_k: int = 3, user_id: str = "") -> str:
        if not 1 <= top_k <= knowledge.MAX_TOP_K:
            return _json({"error": "invalid_top_k", "message": f"top_k는 1~{knowledge.MAX_TOP_K}이어야 합니다."})
        status = None
        if user_id:
            if not _valid(user_id):
                return _json(INVALID_USER_ID)
            with closing(simulator.connect(db_path)) as conn:
                try:
                    status = promotion_status(simulator.simulate(conn, user_id))
                except simulator.CustomerNotFound:
                    return _json({"error": "customer_not_found", "user_id": user_id})
        # 점수는 돌려주지 않는다. 결과는 Claude로 가고, 숫자는 전송 전 검사(SEC-08)의 허용 목록 밖이다.
        results = [{
            "chunk_id": chunk.chunk_id,
            "title": chunk.document.title,
            "section": chunk.section,
            "text": chunk.text,
            **chunk_eligibility(chunk, status),
        } for chunk, _score in kb.search(query, top_k)]
        return _json({"results": results})

    return server


if __name__ == "__main__":
    build_server().run("stdio")

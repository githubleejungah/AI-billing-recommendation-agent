"""두 화면이 함께 쓰는 Streamlit 조각. 값은 모두 다른 모듈의 결과를 그대로 보여준다."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st  # noqa: E402

import documents  # noqa: E402
import policy as policy_module  # noqa: E402
import ui_logic  # noqa: E402


def config() -> ui_logic.Config:
    """화면 설정. 테스트는 session_state["cfg"]에 임시 경로를 넣는다."""
    if "cfg" not in st.session_state:
        st.session_state["cfg"] = ui_logic.Config()
    return st.session_state["cfg"]


def client():
    """Claude 클라이언트는 버튼을 눌렀을 때만 만든다(UI-07). 테스트는 session_state["client"]에 가짜를 넣는다."""
    if "client" not in st.session_state:
        st.session_state["client"] = ui_logic.make_client()
    return st.session_state["client"]


@st.cache_resource
def policy() -> policy_module.Policy:
    return policy_module.load()


def model(pol: policy_module.Policy) -> str:
    return os.environ.get("CLAUDE_MODEL", "").strip() or pol.allowed_models[0]


def money_table(rows: list[dict]) -> None:
    """공급가액·세액·합계를 나눠 보여준다(부가세 구분)."""
    st.table([{"구분": r["label"], "공급가액": documents.won(r["supply"]), "세액": documents.won(r["tax"]),
               "합계": documents.won(r["total"])} for r in rows])


@st.dialog("근거 문서")
def evidence_dialog(chunk_id: str, retrieved: dict, screen: str) -> None:
    """UI-10. 실행 때 받은 조각 원문만 보여준다(다시 검색하지 않음)."""
    try:
        item = ui_logic.evidence(chunk_id, retrieved, screen)
    except ui_logic.EvidenceError as error:
        st.error(str(error))
        return
    st.markdown(f"**{item['title']}** · {item['section']}")
    st.caption(f"{item['chunk_id']} · 대상: {item['audience']}")
    st.write(item["text"])
    for promo in item["not_eligible"]:
        st.warning(f"{promo['promo_id']}: 대상 아님 ({promo.get('reason', '')})")


def cited_text(text: str, retrieved: dict, screen: str, key: str) -> None:
    """인용이 있는 본문. 인용마다 근거 버튼을 단다."""
    st.markdown(text)
    ids = ui_logic.cited_ids(text)
    if ids:
        cols = st.columns(len(ids))
        for col, chunk_id in zip(cols, ids):
            if col.button(f"근거 {chunk_id}", key=f"{key}:{chunk_id}"):
                evidence_dialog(chunk_id, retrieved, screen)


def downloads(issued: dict, key: str) -> None:
    doc = issued["document"]
    mime = {"pdf": "application/pdf", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
    st.caption(f"문서 번호 {doc['document_no']} · 발행일 {doc['issued_at']}")
    cols = st.columns(len(issued["files"]))
    for col, (ext, data) in zip(cols, issued["files"].items()):
        name = documents.file_name(doc, ext) if "." not in ext else f"{doc['document_no']}_{ext}"
        col.download_button(ext.upper(), data, file_name=name, mime=mime.get(ext, "image/png"), key=f"{key}:{ext}")

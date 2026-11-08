"""고객 화면(UI-09): 알림·추천, 요금제 변경, AI 상담. 실행: streamlit run src/app_customer.py

시연에서는 로그인 대신 왼쪽에서 고객과 역할을 고른다. 금액은 모두 시뮬레이터 결과이고, AI는 변경을 실행하지 않는다.
"""

from __future__ import annotations

import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st  # noqa: E402

import change  # noqa: E402
import charts  # noqa: E402
import chat  # noqa: E402
import data_access  # noqa: E402
import documents  # noqa: E402
import ui_logic  # noqa: E402
import ui_widgets as w  # noqa: E402

CHARTS = (("cumulative", "12개월 누적 비용"), ("composition", "월 비용 구성"), ("usage", "사용량과 기본 제공량"))


@st.cache_data(show_spinner=False)
def review_data(db: str, user_id: str) -> tuple[dict, dict]:
    with closing(data_access.connect(Path(db))) as conn:
        data = documents.build_data(conn, user_id)
    return data, charts.render_all(data)


def notices_section(conn, cfg, code: str) -> None:
    notices = ui_logic.customer_notices(cfg.app_db, code)
    if not notices:
        st.caption("새 알림이 없습니다.")
    names = ui_logic.product_names(conn)
    for n in notices[:3]:
        st.info(ui_logic.notice_line(n, names))


def recommendation_tab(conn, cfg, user_id: str, actions: set[str]) -> None:
    code = data_access.load_identity(conn, user_id)["code"]
    notices_section(conn, cfg, code)
    try:
        data, pngs = review_data(str(cfg.billing_db), user_id)
    except documents.DocumentError as error:
        st.warning(str(error))
        return
    st.subheader(data["comparison"][1]["label"])
    st.caption(f"현재 {data['current_plan']} · 절감률 {data['saving_pct']}% · 추천 안정성 {data['stability']}")
    w.money_table(data["comparison"])
    for key, title in CHARTS:
        st.image(pngs[key], caption=title)
        with st.expander("그래프 해설"):
            for line in data["captions"][key]:
                st.write(line)
    with st.expander("계산 기준"):
        for line in data["basis"]:
            st.write(line)
    if "documents" in actions and st.button("비용 절감 검토서 만들기", key="issue_review"):
        st.session_state[f"review:{user_id}"] = documents.issue(
            conn, user_id, user_id, "review", issued=cfg.today(), app_db=cfg.app_db, audit_path=cfg.audit_path)
    if f"review:{user_id}" in st.session_state:
        w.downloads(st.session_state[f"review:{user_id}"], f"review:{user_id}")


def quote_view(quote: dict) -> None:
    check = quote["notification_check"]
    if check.get("message"):
        st.warning(check["message"])
    promo = f" + {quote['promotion']['name']}" if quote["promotion"] else ""
    st.markdown(f"**{quote['from_plan']['name']} → {quote['to_plan']['name']}{promo}** ({quote['action_label']})")
    st.write(f"적용일 {quote['effective_date']} · 취소 기한 {quote['cancel_deadline']}까지")
    w.money_table([{"label": "12개월 절감", **quote["saving_12m"]}])
    for notice in quote["notices"]:
        st.info(notice)


def change_tab(conn, cfg, user_id: str, role: str, actions: set[str]) -> None:
    if "change" not in actions:
        st.info("요금제 변경은 결제 권한자만 할 수 있습니다. 결제 권한자에게 변경을 요청해 주세요.")
        return
    code = data_access.load_identity(conn, user_id)["code"]
    done = st.session_state.pop("change_message", None)
    if done:
        st.success(done)
    request = ui_logic.open_change(cfg.app_db, code)
    if request:
        names = ui_logic.product_names(conn)
        st.write(f"접수 번호 **{request['request_id']}** · {names[request['from_plan']]} → {names[request['to_plan']]} · "
                 f"적용일 {request['effective_date']} · 취소 기한 {request['cancel_deadline']}")
        if st.button("변경 확인서 만들기", key="issue_confirmation"):
            st.session_state[f"confirmation:{user_id}"] = documents.issue(
                conn, user_id, user_id, "confirmation", change=change.confirmation_change(request),
                issued=cfg.today(), app_db=cfg.app_db, audit_path=cfg.audit_path)
        if f"confirmation:{user_id}" in st.session_state:
            w.downloads(st.session_state[f"confirmation:{user_id}"], f"confirmation:{user_id}")
        if "cancel" in actions and st.button("접수 취소", key="cancel_change"):
            try:
                change.cancel(conn, user_id, request["request_id"], role, cfg.today(), app_db=cfg.app_db,
                              audit_path=cfg.audit_path)
            except (change.ChangeError, PermissionError) as error:
                st.error(str(error))
            else:
                st.session_state.pop(f"confirmation:{user_id}", None)
                st.session_state["change_message"] = f"{request['request_id']} 접수를 취소했습니다."
                st.rerun()
        return

    if st.button("변경 확인 화면 열기", key="prepare_change"):
        notices = ui_logic.customer_notices(cfg.app_db, code)
        try:
            st.session_state[f"quote:{user_id}"] = change.prepare(conn, user_id, cfg.today(),
                                                                  notices[0] if notices else None)
        except change.ChangeError as error:
            st.error(str(error))
    quote = st.session_state.get(f"quote:{user_id}")
    if not quote:
        return
    quote_view(quote)
    with st.expander("세금계산서 발행 정보"):
        for line in change.INVOICE_CHECKS:
            st.write(line)
    confirmed = st.checkbox("세금계산서 발행 정보를 확인했습니다.", key="invoice_ok")
    if st.button("변경 접수", key="submit_change", type="primary", disabled=not quote["changeable"]):
        try:
            result = change.submit(conn, user_id, role, quote["quote_sha256"], confirmed, cfg.today(),
                                   app_db=cfg.app_db, audit_path=cfg.audit_path)
        except change.QuoteChanged as changed:
            st.session_state[f"quote:{user_id}"] = changed.fresh
            st.warning(str(changed))
        except (change.ChangeError, PermissionError) as error:
            st.error(str(error))
        else:
            st.session_state.pop(f"quote:{user_id}", None)
            st.session_state["change_message"] = f"접수했습니다. 접수 번호 {result['request']['request_id']}"
            st.rerun()


def chat_tab(conn, cfg, user_id: str) -> None:
    st.caption(chat.DISCLOSURE)  # AI 상담 고지(ASK-01)
    log = st.session_state.setdefault(f"chat:{user_id}", [])
    question = st.text_input("질문", key=f"question:{user_id}")
    ask_col, human_col = st.columns(2)
    if ask_col.button("질문하기", key="ask", disabled=not question.strip()):
        pol = w.policy()
        reply = chat.ask(conn, user_id, question, client=w.client(), model=w.model(pol), policy=pol,
                         app_db=cfg.app_db, audit_path=cfg.audit_path, ticket_dir=cfg.ticket_dir, now=cfg.now())
        log.append({"question": question, "reply": reply})
    if human_col.button("담당자 연결", key="human"):  # 늘 보인다(ASK-04)
        reply = chat.request_human(conn, user_id, question or "담당자 연결 요청", policy=w.policy(),
                                   app_db=cfg.app_db, audit_path=cfg.audit_path, ticket_dir=cfg.ticket_dir,
                                   now=cfg.now())
        log.append({"question": question or "담당자 연결 요청", "reply": reply})
    for i, item in enumerate(reversed(log)):
        reply = item["reply"]
        st.markdown(f"**질문** {item['question']}")
        if reply.answer:
            w.cited_text(reply.answer, reply.retrieved, ui_logic.SCREEN_CUSTOMER, f"chat{len(log) - i}")
        if reply.ticket:
            st.warning(f"담당자에게 연결했습니다. 접수 번호 {reply.ticket['ticket_id']}, "
                       f"답변 예정 {reply.ticket['eta']}.")
    tickets = ui_logic.tickets_for(cfg.app_db, data_access.load_identity(conn, user_id)["code"])
    if tickets:
        with st.expander(f"내 상담 접수 {len(tickets)}건"):
            for t in tickets:
                st.write(f"{t['ticket_id']} · {t['status']} · {t['question_masked']}")
                if t["answer"]:
                    st.success(f"담당자 답변: {t['answer']}")


def main() -> None:
    st.set_page_config(page_title="요금제 알림", page_icon="💡")
    cfg = w.config()
    with closing(data_access.connect(cfg.billing_db)) as conn:
        ids = data_access.customer_ids(conn)
        with st.sidebar:
            st.caption("시연용: 로그인 대신 고객과 역할을 고릅니다.")
            user_id = st.selectbox("고객", ids, index=ids.index(ui_logic.DEMO_CUSTOMERS[0]), key="user_id")
            role = st.radio("역할", (ui_logic.ROLE_ADMIN, ui_logic.ROLE_MEMBER), key="role")
        identity = data_access.load_identity(conn, user_id)
        st.title(f"{identity['name']} 요금제 알림")
        actions = ui_logic.actions_for(role)
        rec_tab, change_t, chat_t = st.tabs(["알림·추천", "요금제 변경", "AI 상담"])
        with rec_tab:
            recommendation_tab(conn, cfg, user_id, actions)
        with change_t:
            change_tab(conn, cfg, user_id, role, actions)
        with chat_t:
            chat_tab(conn, cfg, user_id)


main()

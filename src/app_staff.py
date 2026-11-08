"""담당자 화면(UI-09): 월간 선별, 상담 접수·답변, 실명 조회·반응 기록, 고객 리포트, 운영 지표.

실행: streamlit run src/app_staff.py. 고객은 가칭 코드로 보이고, 실명은 조회할 때만 보이며 감사 기록에 남는다(SEC-16).
Claude는 "분석" 버튼을 눌렀을 때만 부르고, 결과는 화면 세션에 보관한다(UI-07).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st  # noqa: E402

import agent  # noqa: E402
import audit  # noqa: E402
import batch  # noqa: E402
import data_access  # noqa: E402
import escalation  # noqa: E402
import guard  # noqa: E402
import history  # noqa: E402
import metrics  # noqa: E402
import report  # noqa: E402
import simulator  # noqa: E402
import ui_logic  # noqa: E402
import ui_widgets as w  # noqa: E402


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def selection_tab(conn, cfg) -> None:
    if st.button("월간 선별 실행", key="run_selection"):
        try:
            st.session_state["selection"] = batch.run(conn, w.policy(), app_db=cfg.app_db, output_dir=cfg.output_dir,
                                                      audit_path=cfg.audit_path, now=cfg.now())
        except batch.SelectionStopped as stopped:
            st.error(f"데이터 품질 점검에서 멈췄습니다. {stopped}")
    selection = st.session_state.get("selection")
    if selection:
        summary = selection["summary"]
        st.success(summary["text"])
        st.text(summary["blocks"][1]["text"]["text"])  # 사내 채널 요약(가칭 코드만, SEC-12)
        with st.expander(f"고객 알림 미리보기 {len(selection['previews'])}건"):
            for p in selection["previews"]:
                st.text(f"[{p['code']}] {p['text']}")


def tickets_tab(cfg) -> None:
    st.button("새로 고침", key="refresh_tickets")  # 탭을 바꿔도 화면이 다시 읽히지 않아, 새 접수를 보려면 누른다
    queue = ui_logic.tickets_for(cfg.app_db)
    if not queue:
        st.caption("접수가 없습니다.")
    for t in queue:
        faq = " · FAQ 후보" if t["faq_candidate"] else ""
        with st.expander(f"{t['ticket_id']} · {t['code']} · {t['reason']} · {t['status']}{faq}",
                         expanded=t["status"] != escalation.ANSWERED):
            st.write(f"질문(개인정보 가림): {t['question_masked']}")
            if t["answer"]:
                st.success(f"답변: {t['answer']}")
            else:
                answer = st.text_area("답변", key=f"answer:{t['ticket_id']}")
                faq_box = st.checkbox("FAQ 후보로 표시", key=f"faq:{t['ticket_id']}")
                if st.button("답변 보내기", key=f"send:{t['ticket_id']}"):
                    try:
                        with closing(history.connect(cfg.app_db)) as app:
                            escalation.answer_ticket(app, t["ticket_id"], answer, cfg.now(), faq_candidate=faq_box,
                                                     audit_path=cfg.audit_path)
                    except escalation.TicketError as error:
                        st.error(str(error))
                    else:
                        st.rerun()
            if t["answer"] and not t["faq_candidate"] and st.button("FAQ 후보로 표시", key=f"mark:{t['ticket_id']}"):
                with closing(history.connect(cfg.app_db)) as app:
                    escalation.mark_faq(app, t["ticket_id"], audit_path=cfg.audit_path)
                st.rerun()


def customer_tab(conn, cfg) -> None:
    code = st.text_input("가칭 코드", key="lookup_code").strip().upper()
    revealed = st.session_state.setdefault("revealed", {})
    name_col, react_col = st.columns(2)
    if name_col.button("실명 조회(기록 남음)", key="reveal", disabled=not code):
        try:
            revealed[code] = ui_logic.reveal_name(conn, code, cfg.audit_path)
        except LookupError as error:
            st.error(str(error))
    if code in revealed:
        name_col.info(f"{code} = {revealed[code]}")
    outcome = react_col.selectbox("고객 반응", ui_logic.REACTIONS, key="reaction")
    if react_col.button("반응 기록", key="record_reaction", disabled=not code):
        try:
            ui_logic.record_reaction(cfg.app_db, code, outcome, cfg.today(), cfg.audit_path)
        except LookupError as error:
            react_col.error(str(error))
        else:
            react_col.success(f"{code}: {outcome} 기록")


EXAMPLE_NUMBER = "이 고객은 1년에 700,000원을 아낍니다.\n"  # UI-06 시연용. 검증이 거부해야 하는 문장


def insert_number(key: str) -> None:
    st.session_state[key] = EXAMPLE_NUMBER + st.session_state.get(key, "")


def report_view(conn, cfg, code: str, user_id: str, saved: dict) -> None:
    result: agent.RunResult = saved["result"]
    log = json.loads(result.run_log_path.read_text(encoding="utf-8"))
    st.caption(f"실행 {result.run_id} · {result.status}")
    with st.expander("실행 단계"):  # UI-04
        st.table([{"단계": s["step"], "입력": s["input"], "출력": s["output"], "초": s["seconds"]} for s in log["steps"]])
    if result.status != "완료":  # UI-03: 본문 대신 거부 이유
        st.error("검증을 통과하지 못해 리포트를 보여주지 않습니다.")
        for problem in result.problems:
            st.write(f"- {problem}")
        return
    w.cited_text(result.report_path.read_text(encoding="utf-8"), result.retrieved, ui_logic.SCREEN_STAFF, f"rep:{code}",
                 ui_logic.product_names(conn))
    message = json.loads(result.slack_path.read_text(encoding="utf-8"))
    with st.expander("Slack 메시지 미리보기"):
        st.text(message.get("text", ""))
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if st.button("Slack 전송", key=f"slack:{code}", disabled=not webhook, help="Webhook 설정과 검증 통과 때만(UI-05)"):
        digest = hashlib.sha256(json.dumps(message, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        st.info(agent.send_slack(message, digest, run_id=result.run_id, code=code, webhook_url=webhook,
                                 audit_path=cfg.audit_path))
    with st.expander("숫자 직접 써 넣기(시연, 저장·전송 안 함)"):  # UI-06
        key = f"draft:{code}"
        st.session_state.setdefault(key, result.draft or "")
        st.caption("AI 초안에는 숫자 대신 {saving_12m} 같은 빈칸만 있습니다. 아래 칸에 숫자를 직접 써 넣거나 "
                   "'예시 숫자 넣기'를 누른 뒤 '다시 검증'을 누르세요.")
        st.button("예시 숫자 넣기", key=f"insert:{code}", on_click=insert_number, args=(key,))
        edited = st.text_area("초안(자리표시자)", key=key, height=240)
        if st.button("다시 검증", key=f"recheck:{code}"):
            problems = ui_logic.recheck_draft(edited, saved["sim"], result.retrieved)
            if problems:
                st.error("거부: " + " / ".join(problems))
            else:
                st.success("통과(이 초안은 저장하거나 보내지 않습니다)")
    with st.expander("Claude로 나간 내용"):  # 시나리오 10: 상황 카드(범주 정보)와 전송 기록
        st.code(saved["card"], language=None)
        sent = ui_logic.sent_records(audit.read(cfg.audit_path), result.run_id)
        st.table([{"요청 해시": e["request_sha256"][:12], "크기": e["request_bytes"], "종류": ", ".join(e["kinds"]),
                   "입력 토큰": e.get("input_tokens"), "출력 토큰": e.get("output_tokens")} for e in sent])


def report_tab(conn, cfg) -> None:
    by_code = {c: u for u, c in data_access.code_map(conn).items()}
    codes = sorted(by_code)
    default = data_access.load_identity(conn, ui_logic.DEMO_CUSTOMERS[0])["code"]
    code = st.selectbox("고객(가칭 코드)", codes, index=codes.index(default), key="report_code")
    user_id = by_code[code]
    if st.button("분석", key="analyze", type="primary"):  # Claude를 부르는 유일한 버튼(UI-07)
        client = w.client()
        if client is None:
            st.error("ANTHROPIC_API_KEY가 없어 분석할 수 없습니다.")
        else:
            pol = w.policy()
            with st.spinner("분석 중"):
                result = agent.run(user_id, client=client, model=w.model(pol), conn=conn, policy=pol,
                                   output_dir=cfg.output_dir, app_db=cfg.app_db, audit_path=cfg.audit_path,
                                   now=cfg.now())
            sim = simulator.simulate(conn, user_id)
            st.session_state[f"report:{code}"] = {
                "result": result, "sim": sim,
                "card": guard.situation_card(sim, guard.Aliases().alias(user_id), pol, report.load_registry())}
    saved = st.session_state.get(f"report:{code}")
    if saved:
        report_view(conn, cfg, code, user_id, saved)


def metrics_tab(cfg) -> None:
    month = st.text_input("월(YYYY-MM)", cfg.today().strftime("%Y-%m"), key="metrics_month")
    with closing(history.connect(cfg.app_db)) as app:
        m = metrics.compute(app, audit.read(cfg.audit_path), metrics.load_run_logs(cfg.runs_dir), month)
    cols = st.columns(4)
    cols[0].metric("알림 수", m["notices"])
    cols[1].metric("추천 수락률", pct(m["acceptance_rate"]))
    cols[2].metric("셀프 변경 취소율", pct(m["self_change_cancel_rate"]))
    cols[3].metric("상담 연결 비율", pct(m["escalation_rate"]))
    cols = st.columns(4)
    cols[0].metric("리포트 첫 시도 실패율", pct(m["first_try_fail_rate"]))
    cols[1].metric("데이터 품질 통과율", pct(m["quality_pass_rate"]))
    cols[2].metric("리포트 평균 시간", "—" if m["report_seconds_avg"] is None else f"{m['report_seconds_avg']:.1f}초")
    hours = m["ticket_answer_hours_avg"]
    cols[3].metric("상담 답변 평균 시간", "—" if hours is None else f"{hours:.1f}시간")
    cols = st.columns(4)
    cols[0].metric("API 호출", m["api_calls"])
    cols[1].metric("입력 토큰", f"{m['input_tokens']:,}")
    cols[2].metric("출력 토큰", f"{m['output_tokens']:,}")
    cols[3].metric("예상 비용(가정 단가)", "—" if m["cost_usd"] is None else f"${m['cost_usd']:.2f}")
    st.caption("상담 연결 이유별: " + (", ".join(f"{k} {pct(v)}" for k, v in m["escalation_by_reason"].items()) or "—"))


def main() -> None:
    st.set_page_config(page_title="담당자 화면", page_icon="🗂️", layout="wide")
    cfg = w.config()
    st.title("요금제 추천 · 담당자")
    with closing(data_access.connect(cfg.billing_db)) as conn:
        tabs = st.tabs(["월간 선별", "상담 접수", "실명 조회·반응", "고객 리포트", "운영 지표"])
        with tabs[0]:
            selection_tab(conn, cfg)
        with tabs[1]:
            tickets_tab(cfg)
        with tabs[2]:
            customer_tab(conn, cfg)
        with tabs[3]:
            report_tab(conn, cfg)
        with tabs[4]:
            metrics_tab(cfg)


main()

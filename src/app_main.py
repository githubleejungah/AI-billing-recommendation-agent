"""배포용 진입점(DEP-01): 고객 화면과 담당자 화면을 한 주소의 두 페이지로 연다.

로컬 시연은 지금처럼 두 화면을 따로 띄워도 된다(docs/demo_guide.md).
"""

import streamlit as st

st.navigation([
    st.Page("app_customer.py", title="고객 화면", url_path="customer", default=True),
    st.Page("app_staff.py", title="담당자 화면", url_path="staff"),
]).run()

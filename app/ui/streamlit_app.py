import streamlit as st

from app.config import get_settings
from app.ui.layout import inject_css, render_dashboard, render_selected_page, render_sidebar
from app.ui.services import bootstrap


st.set_page_config(page_title="长篇记忆小说", page_icon="SM", layout="wide")

try:
    bootstrap()
    inject_css()
    audit_only = get_settings().audit_only_mode
    project, section, page = render_sidebar(audit_only)

    if audit_only:
        render_selected_page(project, section, page, audit_only=True)
    elif st.sidebar.button("返回首页", use_container_width=True) or not st.session_state.get("_dashboard_seen"):
        st.session_state["_dashboard_seen"] = True
        render_dashboard(project)
    else:
        render_selected_page(project, section, page)
except Exception as exc:
    st.error("页面启动失败。请查看 exe 同级目录的 start_log.txt，或检查 data/.env 配置。")
    st.exception(exc)

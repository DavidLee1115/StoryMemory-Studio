import streamlit as st

from app.export.html_report_exporter import generate_errata_html


def render_html_export_button(project_id: int) -> None:
    st.download_button(
        "导出单文件 HTML 勘误报告",
        data=generate_errata_html(project_id).encode("utf-8"),
        file_name=f"项目{project_id}_勘误报告.html",
        mime="text/html",
        type="primary",
    )

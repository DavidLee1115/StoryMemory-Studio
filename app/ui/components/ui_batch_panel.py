from __future__ import annotations

import sqlite3

import streamlit as st

from app.db.database import db_session
from app.memory.batch_processor import DEFAULT_BATCH_MODEL, export_batch_jsonl, import_batch_results


def render_batch_panel(project_id: int) -> None:
    with db_session() as conn:
        chapters = {
            row["id"]: f"第 {row['chapter_number']} 章 · {row['title']}"
            for row in conn.execute(
                "SELECT id, chapter_number, title FROM chapters WHERE project_id = ? ORDER BY chapter_number",
                (project_id,),
            )
        }

    key = f"openai_batch_{project_id}"
    with st.expander("OpenAI Batch 离线记忆提取（5 折）"):
        st.caption("下载请求 JSONL，在 OpenAI 平台提交 Batch，完成后下载结果 JSONL 并在这里回填。")
        if not chapters:
            st.info("请先在上方导入至少一章正文。")
            return

        chapter_ids = list(chapters)
        start_col, end_col = st.columns(2)
        first = start_col.selectbox(
            "起始章节", chapter_ids, index=None, format_func=chapters.__getitem__, key=f"{key}_first",
        )
        last = end_col.selectbox(
            "结束章节", chapter_ids, index=None, format_func=chapters.__getitem__, key=f"{key}_last",
        )
        model_name = st.text_input("目标模型名", value=DEFAULT_BATCH_MODEL, key=f"{key}_model")

        data = b""
        if first is not None and last is not None and model_name.strip():
            start = chapter_ids.index(first)
            end = chapter_ids.index(last)
            if start > end:
                st.warning("结束章节应在起始章节之后，或与起始章节相同。")
            else:
                try:
                    data = export_batch_jsonl(project_id, chapter_ids[start : end + 1], model_name)
                except (ValueError, sqlite3.Error) as exc:
                    st.error(f"导出失败：{exc}")

        st.download_button(
            "下载请求 JSONL",
            data,
            file_name=f"storymemory_batch_project_{project_id}.jsonl",
            mime="application/x-ndjson",
            disabled=not data,
            key=f"{key}_download",
        )

        st.caption("成功回填会覆盖对应章节的旧记忆，包括关联到该章的时间线记录。")
        result_file = st.file_uploader("上传 Batch 结果 JSONL", type=["jsonl"], key=f"{key}_result")
        if st.button("回填数据库", disabled=result_file is None, key=f"{key}_import"):
            try:
                with st.spinner("正在回填章节记忆…"):
                    summary = import_batch_results(project_id, result_file.getvalue())
            except sqlite3.Error as exc:
                st.error(f"无法读取项目数据库：{exc}")
                return
            if summary["success_count"]:
                st.success(f"成功回填 {summary['success_count']} 章。")
            if summary["failures"]:
                st.warning(f"{len(summary['failures'])} 条结果未回填。")
                st.dataframe(
                    [
                        {
                            "文件行": failure["line_number"] or "—",
                            "章节": chapters.get(failure["chapter_id"], failure["custom_id"] or "—"),
                            "原因": failure["reason"],
                        }
                        for failure in summary["failures"]
                    ],
                    hide_index=True,
                )

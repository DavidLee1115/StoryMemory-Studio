from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from typing import Any, TypedDict

from app.db.database import db_session
from app.db.models import MemoryExtraction
from app.llm.client import DEFAULT_SYSTEM_PROMPT, extract_json
from app.memory.extractor import build_extraction_prompt
from app.memory.writer import replace_chapter_extraction


BATCH_ENDPOINT = "/v1/chat/completions"
DEFAULT_BATCH_MODEL = "gpt-6.2-sol"
_CUSTOM_ID = re.compile(r"proj_([0-9]+)_chap_([0-9]+)")


class BatchFailure(TypedDict):
    line_number: int
    custom_id: str | None
    chapter_id: int | None
    reason: str


class BatchImportSummary(TypedDict):
    success_count: int
    failures: list[BatchFailure]


def export_batch_jsonl(project_id: int, chapter_ids: Iterable[int], model_name: str) -> bytes:
    """Return one OpenAI Batch Chat Completions request per selected chapter."""
    model_name = model_name.strip()
    if not model_name:
        raise ValueError("请输入模型名")
    selected = list(dict.fromkeys(chapter_ids))
    if not selected:
        raise ValueError("请选择至少一章")
    if len(selected) > 50_000:
        raise ValueError("每个 Batch 文件最多包含 50,000 条请求")

    output = bytearray()
    with db_session() as conn:
        for chapter_id in selected:
            chapter = conn.execute(
                "SELECT title, content FROM chapters WHERE project_id = ? AND id = ?",
                (project_id, chapter_id),
            ).fetchone()
            if chapter is None:
                raise ValueError(f"章节 ID {chapter_id} 不属于当前项目或已不存在")
            if not chapter["content"].strip():
                raise ValueError(f"章节 ID {chapter_id} 的正文为空")

            request = {
                "custom_id": f"proj_{project_id}_chap_{chapter_id}",
                "method": "POST",
                "url": BATCH_ENDPOINT,
                "body": {
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
                        {"role": "user", "content": build_extraction_prompt(chapter["title"], chapter["content"])},
                    ],
                },
            }
            output.extend(json.dumps(request, ensure_ascii=False).encode("utf-8"))
            output.extend(b"\n")
            if len(output) > 200_000_000:
                raise ValueError("Batch 文件超过 200 MB，请缩小章节范围")
    return bytes(output)


def _error_detail(error: Any) -> str:
    if isinstance(error, dict):
        return " ".join(str(error[key]) for key in ("code", "message") if error.get(key)) or str(error)
    return str(error)


def _parse_extraction(row: dict[str, Any]) -> MemoryExtraction:
    if row.get("error"):
        raise ValueError(f"OpenAI 请求失败：{_error_detail(row['error'])}")
    response = row.get("response")
    if not isinstance(response, dict):
        raise ValueError("缺少 Batch response")
    body = response.get("body")
    if response.get("status_code") != 200:
        error = body.get("error") if isinstance(body, dict) else body
        detail = f"：{_error_detail(error)}" if error else ""
        raise ValueError(f"OpenAI 请求失败（HTTP {response.get('status_code')}）{detail}")

    try:
        choice = body["choices"][0]
        message = choice["message"]
        finish_reason = choice.get("finish_reason")
        refusal = message.get("refusal")
        content = message.get("content")
    except (AttributeError, KeyError, IndexError, TypeError) as exc:
        raise ValueError("缺少 Chat Completions 的 choices[0].message") from exc

    if refusal:
        raise ValueError(f"模型拒绝生成：{refusal}")
    if finish_reason and finish_reason != "stop":
        raise ValueError(f"模型未正常完成输出（finish_reason={finish_reason}）")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("模型返回的 JSON 内容为空")

    try:
        data = extract_json(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"模型 JSON 解析失败（第 {exc.lineno} 行，第 {exc.colno} 列）：{exc.msg}") from exc
    extraction = MemoryExtraction.model_validate(data)
    if not (extraction.model_fields_set - {"style_features"}):
        raise ValueError("模型 JSON 缺少可写入的记忆字段")
    return extraction


def import_batch_results(project_id: int, result_jsonl_bytes: bytes) -> BatchImportSummary:
    """Validate each result and commit one chapter at a time in chapter order."""
    with db_session() as conn:
        chapter_numbers = {
            row["id"]: row["chapter_number"]
            for row in conn.execute(
                "SELECT id, chapter_number FROM chapters WHERE project_id = ?", (project_id,)
            )
        }

    failures: list[BatchFailure] = []
    pending: dict[int, tuple[BatchFailure, MemoryExtraction]] = {}
    for line_number, line in enumerate(result_jsonl_bytes.removeprefix(b"\xef\xbb\xbf").splitlines(), start=1):
        if not line.strip():
            continue
        entry: BatchFailure = {
            "line_number": line_number, "custom_id": None, "chapter_id": None, "reason": "",
        }
        try:
            row = json.loads(line.decode("utf-8"))
            if not isinstance(row, dict):
                raise ValueError("Batch 输出行必须是 JSON 对象")
            custom_id = row.get("custom_id")
            entry["custom_id"] = custom_id if isinstance(custom_id, str) else None
            match = _CUSTOM_ID.fullmatch(entry["custom_id"] or "")
            if match is None:
                raise ValueError("custom_id 格式应为 proj_<项目 ID>_chap_<章节 ID>")
            row_project_id, chapter_id = map(int, match.groups())
            entry["chapter_id"] = chapter_id
            if row_project_id != project_id:
                raise ValueError(f"结果属于项目 ID {row_project_id}，当前项目 ID 为 {project_id}")
            if chapter_id not in chapter_numbers:
                raise ValueError("该章节不属于当前项目或已不存在")
            if chapter_id in pending:
                raise ValueError("结果文件包含重复章节")
            pending[chapter_id] = (entry, _parse_extraction(row))
        except (ValueError, RecursionError) as exc:
            failures.append({**entry, "reason": str(exc)})

    success_count = 0
    for chapter_id in sorted(pending, key=chapter_numbers.__getitem__):
        entry, extraction = pending[chapter_id]
        try:
            with db_session() as conn:
                conn.execute("BEGIN IMMEDIATE")
                replace_chapter_extraction(conn, project_id, chapter_id, extraction)
        except (sqlite3.Error, TypeError, ValueError, OverflowError) as exc:
            failures.append({**entry, "reason": f"SQLite 回填失败：{exc}"})
        else:
            success_count += 1

    if not pending and not failures:
        failures.append({"line_number": 0, "custom_id": None, "chapter_id": None, "reason": "结果文件为空"})
    return {"success_count": success_count, "failures": sorted(failures, key=lambda entry: entry["line_number"])}

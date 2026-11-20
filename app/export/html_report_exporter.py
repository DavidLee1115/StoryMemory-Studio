"""Read a project's manuscript and saved checks into an offline HTML report."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from app.db.database import db_session, loads, rows_to_dicts
from app.memory.retriever import MemoryRetriever


_TEMPLATE = Path(__file__).with_name("errata_report.html")
_TEMPLATE_TOKEN = re.compile(r"@@(?:REPORT_TITLE|ERRATA_DATA)@@")
_SCRIPT_ESCAPES = str.maketrans({c: f"\\u{ord(c):04x}" for c in "<>&\u2028\u2029"})
_SEVERITY = {"low": "low", "medium": "medium", "high": "high", "critical": "high"}


def _report_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        if len(value) == 10:
            return datetime.fromisoformat(value).date().isoformat()
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)  # SQLite CURRENT_TIMESTAMP is UTC.
    return timestamp.astimezone(timezone.utc).isoformat()


def _read_issues(
    conn: sqlite3.Connection, project_id: int, chapter_numbers: dict[int, int]
) -> tuple[list[dict], str | None]:
    # Chapter generation via CLI stores its check in a successful generate_chapter
    # log. Other log operations can mirror quality_reports.
    rows = conn.execute(
        """
        SELECT 'quality' AS kind, id, chapter_id, risk_level, report_json AS payload, created_at
        FROM quality_reports WHERE project_id = ? AND report_type = 'consistency'
        UNION ALL
        SELECT 'generation', id, chapter_id, NULL, structured_json, created_at
        FROM generation_logs WHERE project_id = ? AND operation = 'generate_chapter'
          AND status = 'success'
        ORDER BY created_at DESC, kind, id DESC
        """,
        (project_id, project_id),
    )
    issues = []
    checked_at = None
    for row in rows:
        report = loads(row["payload"], {})
        if not isinstance(report, dict):
            continue
        if row["kind"] == "generation":
            report = report.get("consistency")
        if not isinstance(report, dict) or not isinstance(report.get("issues"), list):
            continue

        timestamp = _report_time(row["created_at"])
        if timestamp and (checked_at is None or timestamp > checked_at):
            checked_at = timestamp

        chapter_id = row["chapter_id"]
        chapter_number = chapter_numbers.get(chapter_id)
        if chapter_number is None:
            chapter_id = None
            saved_number = report.get("chapter_number")
            chapter_number = saved_number if type(saved_number) is int and saved_number > 0 else None

        for index, issue in enumerate(report["issues"]):
            if not isinstance(issue, dict):
                continue
            severity = issue.get("severity") or row["risk_level"]
            initial_status = issue.get("initial_status")
            issues.append(
                {
                    "id": f"{row['kind']}-{row['id']}-{index}",
                    "chapter_id": chapter_id,
                    "chapter_number": chapter_number,
                    "issue_type": str(issue.get("issue_type") or "一致性问题"),
                    "severity": _SEVERITY.get(str(severity), "medium"),
                    "source_text": str(issue.get("source_text") or ""),
                    "historical_excerpt": str(issue.get("historical_excerpt") or ""),
                    "evidence": str(issue.get("evidence") or ""),
                    "suggestion": str(issue.get("suggestion") or ""),
                    "initial_status": initial_status if initial_status in ("fixed", "ignored") else "pending",
                    "checked_at": timestamp,
                }
            )
    return issues, checked_at


def _read_settings(
    conn: sqlite3.Connection, project_id: int, chapter_numbers: dict[int, int]
) -> list[dict]:
    memory = MemoryRetriever(conn, project_id)
    world = []
    for rule in memory.world_rules():
        number = chapter_numbers.get(rule["chapter_id"])
        source_parts = [f"第 {number} 章" if number is not None else "", rule["source"]]
        world.append(
            {
                "title": rule["category"] or "世界规则",
                "text": rule["rule_text"],
                "source": " · ".join(part for part in source_parts if part),
            }
        )
    forbidden = [
        {"title": rule["category"] or "禁止事项", "text": rule["rule_text"], "source": rule["source"] or ""}
        for rule in memory.forbidden_rules()
    ]
    characters = []
    for character in conn.execute(
        """
        SELECT name, role, hard_constraints, first_chapter_id FROM characters
        WHERE project_id = ? AND (TRIM(role) <> '' OR TRIM(hard_constraints) <> '')
        ORDER BY importance DESC, id
        """,
        (project_id,),
    ):
        number = chapter_numbers.get(character["first_chapter_id"])
        characters.append(
            {
                "title": character["name"],
                "text": "\n".join(part for part in (character["role"], character["hard_constraints"]) if part),
                "source": f"首次登场 · 第 {number} 章" if number is not None else "",
            }
        )
    return [
        {"label": label, "entries": entries}
        for label, entries in (("世界规则", world), ("禁止事项", forbidden), ("人物设定", characters))
        if entries
    ]


def generate_errata_html(project_id: int) -> str:
    """Return a self-contained UTF-8 HTML document for the selected SQLite project."""
    with db_session() as conn:
        conn.execute("BEGIN")
        project = conn.execute(
            "SELECT id, name, title, description FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project is None:
            raise ValueError(f"找不到项目（ID {project_id}）")
        chapters = rows_to_dicts(
            conn.execute(
                "SELECT id, chapter_number, title, content FROM chapters WHERE project_id = ? ORDER BY chapter_number",
                (project_id,),
            ).fetchall()
        )
        chapter_numbers = {chapter["id"]: chapter["chapter_number"] for chapter in chapters}
        issues, checked_at = _read_issues(conn, project_id, chapter_numbers)
        data = {
            "project": dict(project),
            "checked_at": checked_at,
            "chapters": chapters,
            "issues": issues,
            "settings": _read_settings(conn, project_id, chapter_numbers),
        }

    # Script data uses raw HTML text mode, so HTML entity escaping would corrupt
    # the JSON. Unicode escapes prevent a literal script end tag from being parsed.
    embedded_json = json.dumps(data, ensure_ascii=False, separators=(",", ":")).translate(_SCRIPT_ESCAPES)
    replacements = {
        "@@REPORT_TITLE@@": escape(project["title"] or project["name"]),
        "@@ERRATA_DATA@@": embedded_json,
    }
    return _TEMPLATE_TOKEN.sub(lambda match: replacements[match[0]], _TEMPLATE.read_text(encoding="utf-8"))

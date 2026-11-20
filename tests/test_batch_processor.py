import json
from unittest.mock import Mock

import pytest

from app.config import get_settings
from app.db.database import db_session, init_db
from app.llm.client import DEFAULT_SYSTEM_PROMPT, LLMClient
from app.memory.batch_processor import DEFAULT_BATCH_MODEL, export_batch_jsonl, import_batch_results
from app.memory.extractor import MemoryExtractor


@pytest.fixture
def seeded_project(monkeypatch, tmp_path):
    monkeypatch.setenv("STORYMEMORY_DB_PATH", str(tmp_path / "test.sqlite3"))
    get_settings.cache_clear()
    init_db()
    with db_session() as conn:
        project_id = conn.execute(
            "INSERT INTO projects (name, title) VALUES ('test', '批处理测试')"
        ).lastrowid
        # Row IDs intentionally run in a different order from chapter numbers.
        chapters = {}
        for number in (3, 1, 2):
            chapters[number] = conn.execute(
                "INSERT INTO chapters (project_id, chapter_number, title, content) VALUES (?, ?, ?, ?)",
                (project_id, number, f"第 {number} 章", f"正文 {number}\n保留 {{chapter_title}} 和 {{原始花括号}}"),
            ).lastrowid
    yield project_id, chapters
    get_settings.cache_clear()


def _result(project_id, chapter_id, content, *, finish_reason="stop"):
    return {
        "custom_id": f"proj_{project_id}_chap_{chapter_id}",
        "response": {
            "status_code": 200,
            "body": {"choices": [{
                "finish_reason": finish_reason,
                "message": {"content": content},
            }]},
        },
        "error": None,
    }


def _jsonl(*rows):
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")


def test_export_reuses_online_prompt_and_matches_openai_format(seeded_project):
    project_id, chapters = seeded_project
    payload = export_batch_jsonl(project_id, [*chapters.values(), chapters[1]], DEFAULT_BATCH_MODEL)
    requests = [json.loads(line) for line in payload.splitlines()]
    assert payload.endswith(b"\n")
    assert len(requests) == 3
    assert len({request["custom_id"] for request in requests}) == 3
    for (number, chapter_id), request in zip(chapters.items(), requests, strict=True):
        assert request["custom_id"] == f"proj_{project_id}_chap_{chapter_id}"
        assert request["method"] == "POST"
        assert request["url"] == "/v1/chat/completions"
        assert request["body"]["model"] == "gpt-6.2-sol"
        assert "temperature" not in request["body"]
        messages = request["body"]["messages"]
        assert messages[0] == {"role": "system", "content": DEFAULT_SYSTEM_PROMPT}
        assert messages[1]["role"] == "user"
        user_prompt = messages[1]["content"]
        assert '"summary": {' in user_prompt
        assert f"【章节标题】\n第 {number} 章" in user_prompt
        assert f"【章节正文】\n正文 {number}\n保留 {{chapter_title}} 和 {{原始花括号}}" in user_prompt

        online_client = Mock(spec=LLMClient)
        online_client.complete.return_value = '{"summary": {"short_summary": "逐章在线摘要"}}'
        result = MemoryExtractor(online_client).extract(
            f"第 {number} 章", f"正文 {number}\n保留 {{chapter_title}} 和 {{原始花括号}}"
        )
        online_client.complete.assert_called_once_with(user_prompt, temperature=0.1)
        assert result.summary["short_summary"] == "逐章在线摘要"


def test_import_overwrites_memory_and_reports_one_bad_chapter(seeded_project):
    project_id, chapters = seeded_project

    def memory(number):
        return {
            "summary": {"short_summary": f"摘要 {number}", "detailed_summary": f"详情 {number}"},
            "characters": [{"name": "叶含章", "status": f"状态 {number}"}],
            "facts": [{"fact_text": f"事实 {number}"}],
            "timeline_events": [{"event_text": f"事件 {number}"}],
            "world_rules": [{"rule_text": f"规则 {number}"}],
            "relationship_changes": [{"character_a": "叶含章", "character_b": "范允初"}],
            "unresolved_questions": [{"question": f"问题 {number}"}],
        }

    old_result = _jsonl(
        _result(project_id, chapters[1], json.dumps(memory("旧一"))),
        _result(project_id, chapters[2], json.dumps(memory("旧二"))),
    )
    assert import_batch_results(project_id, old_result) == {"success_count": 2, "failures": []}

    first = _result(project_id, chapters[1], "```json\n" + json.dumps(memory(1)) + "\n```")
    second = _result(project_id, chapters[2], '{"summary": ')
    third = _result(project_id, chapters[3], json.dumps(memory(3)))
    payload = _jsonl(third, second, first)
    for _ in range(2):
        summary = import_batch_results(project_id, payload)
        assert summary["success_count"] == 2
        failure, = summary["failures"]
        assert failure["line_number"] == 2
        assert failure["custom_id"] == second["custom_id"]
        assert failure["chapter_id"] == chapters[2]
        assert "JSON 解析失败" in failure["reason"]

    # A separate late import of chapter 1 must retain the shared character state from chapter 3.
    assert import_batch_results(project_id, _jsonl(first)) == {"success_count": 1, "failures": []}
    with db_session() as conn:
        for table, field, prefix in (
            ("chapter_summaries", "short_summary", "摘要"),
            ("chapter_facts", "fact_text", "事实"),
            ("world_rules", "rule_text", "规则"),
            ("timeline_events", "event_text", "事件"),
            ("unresolved_questions", "question", "问题"),
        ):
            rows = conn.execute(f"SELECT {field} FROM {table} WHERE project_id = ?", (project_id,))
            assert sorted(row[field] for row in rows) == sorted(f"{prefix} {n}" for n in (1, "旧二", 3))
        relationships = conn.execute(
            "SELECT chapter_id FROM character_relationships WHERE project_id = ?", (project_id,)
        )
        assert sorted(row["chapter_id"] for row in relationships) == sorted(chapters.values())
        character, = conn.execute("SELECT status, last_seen_chapter_id FROM characters WHERE project_id = ?", (project_id,))
        assert tuple(character) == ("状态 3", chapters[3])


def test_invalid_lines_and_failed_writes_leave_old_memory_intact(seeded_project):
    project_id, chapters = seeded_project
    with db_session() as conn:
        other_project = conn.execute("INSERT INTO projects (name, title) VALUES ('other', '其他项目')").lastrowid
        foreign_chapter = conn.execute(
            "INSERT INTO chapters (project_id, chapter_number, title, content) VALUES (?, 1, '标题', '正文')",
            (other_project,),
        ).lastrowid

    with pytest.raises(ValueError, match="不属于当前项目"):
        export_batch_jsonl(project_id, [chapters[1], foreign_chapter], DEFAULT_BATCH_MODEL)

    original = '{"summary":{"short_summary":"旧摘要"},"timeline_events":[{"event_text":"旧事件"}]}'
    assert import_batch_results(project_id, _jsonl(_result(project_id, chapters[1], original)))["success_count"] == 1
    invalid_write = {
        "summary": {"short_summary": "错误摘要"},
        "timeline_events": [{"event_text": "错误事件", "confidence": "无法转换"}],
    }
    batch = b"\xef\xbb\xbf{broken json}\n" + _jsonl(
        _result(other_project, chapters[1], original),
        _result(project_id, foreign_chapter, original),
        _result(project_id, chapters[1], json.dumps(invalid_write)),
        _result(project_id, chapters[2], '{"unexpected_field": []}'),
        _result(project_id, chapters[2], original, finish_reason="length"),
        {
            "custom_id": f"proj_{project_id}_chap_{chapters[3]}",
            "response": None,
            "error": {"code": "batch_expired", "message": "The batch expired"},
        },
        _result(project_id, chapters[3], original),
    )
    summary = import_batch_results(project_id, batch)
    assert summary["success_count"] == 1
    assert [failure["line_number"] for failure in summary["failures"]] == list(range(1, 8))
    for failure, reason in zip(
        summary["failures"],
        ["property name", "结果属于项目", "不属于当前项目", "SQLite 回填失败", "缺少可写入", "finish_reason=length", "batch_expired"],
        strict=True,
    ):
        assert reason in failure["reason"]
    with db_session() as conn:
        assert conn.execute(
            "SELECT short_summary FROM chapter_summaries WHERE chapter_id = ?", (chapters[1],)
        ).fetchone()["short_summary"] == "旧摘要"
        assert conn.execute(
            "SELECT event_text FROM timeline_events WHERE chapter_id = ?", (chapters[1],)
        ).fetchone()["event_text"] == "旧事件"

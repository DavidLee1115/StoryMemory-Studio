from __future__ import annotations

import json
import re
from html.parser import HTMLParser

import pytest

from app.config import get_settings
from app.db.database import db_session, dumps, init_db
from app.export.html_report_exporter import generate_errata_html


BOOK_TITLE = "雾港旧信"
CONFLICT_QUOTE = "昨夜退潮时，陆沉提灯走上石堤。"
CODE_QUOTE = '纸角印着 "<银印>&旧码"。'
BODY = "\n\n".join(
    [
        "　　钟楼响过十下，码头的灯从东往西依次暗下来。陆沉把空信封压在杯底，等茶摊老板收完最后一桌碗。",
        "　　茶面上浮着半片橘皮。他挪开杯子，露出信封左上角的邮戳，墨色从“十三”渗到牛皮纸背面。",
        "　　“你往北找，看见蓝窗就停。”老板把抹布搭上肩，对着窗外抬了抬下巴。",
        "　　陆沉走出茶摊，沿着骑楼往北拐。风把一张演出海报掀到脚边，背面记着仓库轮班的名单。",
        "　　擦鞋匠在桥头数硬币。陆沉借了半截铅笔，把海报上的四个名字抄进笔记本，旁边留了一行空白。",
        "　　桥下的木船把桨靠在岸上。船工用湿布擦船头的字，听见脚步就起身让路。",
        "　　仓库的墙根积着薄薄一层盐霜。他顺着墙往前摸，指头碰到了蓝窗下第三块松动的砖。",
        "　　砖后塞着一张叠了两次的凭单。" + CODE_QUOTE + "凭单下方的领用栏里，写着他自己的名字。",
        "　　他把砖按回去，等街口的人影走远才把凭单放进内袋。回程得经过石堤，他记得那里没有灯。",
        "　　" + CONFLICT_QUOTE + "一夜过去，堤边的脚印只剩下浅浅两个坑。",
        "　　潮水快漫过阶沿时，他在灯罩里找到第二封信。上面还留着一小截红色的线。",
        "　　信纸上只有一句话。隔着海雾，钟楼敲响十一点，他把信重新叠好。",
    ]
)


@pytest.fixture
def report_db(tmp_path, monkeypatch):
    path = tmp_path / "errata.sqlite3"
    monkeypatch.setenv("STORYMEMORY_DB_PATH", str(path))
    get_settings.cache_clear()
    init_db(path)
    try:
        with db_session(path) as conn:
            project_id = conn.execute(
                "INSERT INTO projects (name, title, description) VALUES (?, ?, ?)",
                ("雾港", BOOK_TITLE, "一封送错地址的信牵出码头旧案。"),
            ).lastrowid
            other_id = conn.execute("INSERT INTO projects (name, title) VALUES ('隔壁', '隔壁的作品')").lastrowid
            second_id = conn.execute(
                "INSERT INTO chapters (project_id, chapter_number, title, content) VALUES (?, 2, '潮水过桥', ?)",
                (project_id, BODY),
            ).lastrowid
            first_id = conn.execute(
                "INSERT INTO chapters (project_id, chapter_number, title, content) VALUES (?, 1, '茶摊夜雨', ?)",
                (project_id, "　　雨落在窗檐上。昨夜起封港，石堤被潮水淹没。\n\n　　陆沉把银印交给了江翎。"),
            ).lastrowid
            conn.execute(
                "INSERT INTO chapters (project_id, chapter_number, title, content) VALUES (?, 3, '空桌', '　　茶摊只剩一张空桌。')",
                (project_id,),
            )
            other_chapter = conn.execute(
                "INSERT INTO chapters (project_id, chapter_number, title, content) VALUES (?, 1, '隔壁正文', '外书不应泄露')",
                (other_id,),
            ).lastrowid

            conn.executemany(
                "INSERT INTO world_rules (project_id, category, rule_text, source, chapter_id, is_active) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (project_id, "港规", "封港期间石堤不可通行。", "潮汐告示", first_id, 1),
                    (project_id, "废止规则", "报告应排除此条。", "", None, 0),
                    (other_id, "外书规则", "报告应排此外书。", "", other_chapter, 1),
                ],
            )
            conn.execute(
                "INSERT INTO characters (project_id, name, role, hard_constraints, first_chapter_id) VALUES (?, '陆沉', '抄信人', '入夜后不带银印', ?)",
                (project_id, first_id),
            )
            conn.execute(
                "INSERT INTO forbidden_rules (project_id, category, rule_text, source) VALUES (?, '身份', '江翎不能进入钟楼。', '作者设定')",
                (project_id,),
            )

            issues = [
                {
                    "issue_type": "时间线与地点冲突",
                    "severity": "critical",
                    "source_text": CONFLICT_QUOTE,
                    "evidence": "第 1 章的告示写明，昨夜起封港，石堤在退潮时仍不可通行。",
                    "suggestion": "把取信改到桥头，或补交代解封时刻。",
                },
                {
                    "issue_type": "道具归属",
                    "severity": "medium",
                    "source_text": CODE_QUOTE,
                    "evidence": "第 1 章末银印交给江翎，陆沉手边只有收据。",
                    "suggestion": '交代印章从哪来，或把 "<银印>" 改成收据编号。',
                },
                {
                    "issue_type": "摘录待核对",
                    "severity": "low",
                    "source_text": "风刮得太急，他把钥匙掉在地上。",
                    "evidence": "体检时的稿件曾提到门钥匙。",
                    "suggestion": "确认当前稿的门钥匙是否仍在第 2 章。",
                },
            ]
            linked_report = {"passed": False, "issues": issues}
            draft_report = {
                "passed": False,
                "chapter_number": 4,
                "issues": [{"issue_type": "草稿人物动机", "source_text": "她为什么折返？"}],
            }
            unrelated_report = {"passed": False, "issues": [{"issue_type": "外书检查"}]}
            conn.executemany(
                """
                INSERT INTO quality_reports (project_id, chapter_id, report_type, risk_level, report_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (project_id, second_id, "consistency", "high", dumps(linked_report), "2026-11-20 13:14:00"),
                    (project_id, None, "consistency", "medium", dumps(draft_report), "2026-11-20 12:00:00"),
                    (project_id, first_id, "consistency", "low", '{"passed":true,"issues":[]}', "2026-11-20 14:00:00"),
                    (project_id, second_id, "consistency", "high", "{broken JSON", "2026-11-20 15:00:00"),
                    (project_id, second_id, "ai_tone_detector", "high", dumps(unrelated_report), "2026-11-20 16:00:00"),
                    (other_id, other_chapter, "consistency", "high", dumps(unrelated_report), "2026-11-20 17:00:00"),
                ],
            )
            cli_report = {"passed": False, "issues": [{"issue_type": "历史生成检查", "severity": "low"}]}
            conn.executemany(
                """
                INSERT INTO generation_logs (project_id, operation, structured_json, prompt_preview, created_at)
                VALUES (?, ?, ?, '不导出内部提示词', '2026-11-20 10:00:00')
                """,
                [
                    (project_id, "generate_chapter", dumps({"consistency": cli_report})),
                    (project_id, "consistency", dumps(linked_report)),
                    (project_id, "e2e_ollama_generate_chapter", dumps({"consistency": linked_report})),
                    (other_id, "generate_chapter", dumps({"consistency": unrelated_report})),
                ],
            )
        yield project_id, second_id
    finally:
        get_settings.cache_clear()


class ReportParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.json_blocks = []
        self.inside_report_data = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag == "script" and attrs.get("id") == "report-data" and attrs.get("type") == "application/json":
            self.inside_report_data = True

    def handle_data(self, data):
        if self.inside_report_data:
            self.json_blocks.append(data)

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside_report_data = False


def test_generate_errata_html_from_project_sqlite(report_db, tmp_path):
    project_id, second_id = report_db
    html = generate_errata_html(project_id)
    sample_file = tmp_path / "sample_report.html"
    sample_file.write_text(html, encoding="utf-8")

    rendered = ReportParser()
    rendered.feed(sample_file.read_text(encoding="utf-8"))
    tags = {tag for tag, _ in rendered.tags}
    assert {"header", "nav", "main", "aside", "style", "script"} <= tags
    for label in ("筛选严重度", "仅看未处理", "标记已修", "误报忽略"):
        assert label in html
    assert "localStorage" in html

    for tag, attrs in rendered.tags:
        assert tag not in {"img", "iframe", "object", "embed"}
        assert all(not name.startswith("on") for name in attrs)
        for name in {"src", "srcset", "href", "poster", "action", "data"} & attrs.keys():
            assert (attrs[name] or "").startswith("#")
    assert not re.search(r"@import\b|url\(\s*['\"]?(?:https?:)?//", html, flags=re.I)
    assert len(rendered.json_blocks) == 1
    raw_json, = rendered.json_blocks

    data = json.loads(raw_json)
    assert data["project"]["title"] == BOOK_TITLE
    assert data["checked_at"] == "2026-11-20T14:00:00+00:00"
    assert [chapter["chapter_number"] for chapter in data["chapters"]] == [1, 2, 3]
    assert data["chapters"][1]["content"] == BODY
    combined_settings = [entry for group in data["settings"] for entry in group["entries"]]
    assert {"title": "港规", "text": "封港期间石堤不可通行。", "source": "第 1 章 · 潮汐告示"} in combined_settings
    assert {entry["title"] for entry in combined_settings} == {"港规", "陆沉", "身份"}
    assert [issue["severity"] for issue in data["issues"]] == ["high", "medium", "low", "medium", "low"]
    assert data["issues"][0]["chapter_id"] == second_id
    assert data["issues"][1]["source_text"] == CODE_QUOTE
    assert data["issues"][3]["chapter_id"] is None
    assert data["issues"][3]["chapter_number"] == 4
    assert data["issues"][4]["chapter_number"] is None
    assert data["issues"][4]["issue_type"] == "历史生成检查"
    assert len({issue["id"] for issue in data["issues"]}) == 5
    assert "不导出内部提示词" not in html
    assert "外书" not in html


def test_html_escapes_report_payload(report_db):
    project_id, chapter_id = report_db
    injection = '</ScRiPt><img src="https://cdn.invalid/pixel" onerror="alert(1)">'
    title = '雾港旧信 <校订稿> "A&B" @@ERRATA_DATA@@'
    content = f"{injection}。\u2028\u2029编者校记 @@REPORT_TITLE@@。"
    with db_session() as conn:
        conn.execute("UPDATE projects SET title = ? WHERE id = ?", (title, project_id))
        conn.execute("UPDATE chapters SET content = ? WHERE id = ?", (content, chapter_id))

    html = generate_errata_html(project_id)
    rendered = ReportParser()
    rendered.feed(html)
    assert "雾港旧信 &lt;校订稿&gt; &quot;A&amp;B&quot; @@ERRATA_DATA@@" in html
    assert injection not in html
    assert "img" not in {tag for tag, _ in rendered.tags}
    assert len(rendered.json_blocks) == 1
    raw_json, = rendered.json_blocks
    assert not any(char in raw_json for char in "<>&\u2028\u2029")
    data = json.loads(raw_json)
    assert data["project"]["title"] == title
    assert data["chapters"][1]["content"] == content


def test_unknown_errata_project(report_db):
    with pytest.raises(ValueError, match="找不到项目"):
        generate_errata_html(-1)


def test_export_preserves_archived_issue_and_date_only(report_db):
    project_id, chapter_id = report_db
    issue = {
        "issue_type": "历史校准",
        "source_text": CONFLICT_QUOTE,
        "historical_excerpt": "出现在旧稿里的原句。",
        "initial_status": "fixed",
    }
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO quality_reports (project_id, chapter_id, report_type, report_json, created_at)
            VALUES (?, ?, 'consistency', ?, '2026-11-21')
            """,
            (project_id, chapter_id, dumps({"issues": [issue]})),
        )

    rendered = ReportParser()
    rendered.feed(generate_errata_html(project_id))
    raw_json, = rendered.json_blocks
    data = json.loads(raw_json)
    archived, *remaining = data["issues"]
    assert data["checked_at"] == archived["checked_at"] == "2026-11-21"
    assert archived["source_text"] == CONFLICT_QUOTE
    assert archived["historical_excerpt"] == "出现在旧稿里的原句。"
    assert archived["initial_status"] == "fixed"
    assert all(issue["initial_status"] == "pending" for issue in remaining)

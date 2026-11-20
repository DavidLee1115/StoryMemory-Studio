from app.memory.extractor import heuristic_extract
from app.ui.services import split_chapters_from_text


def test_heuristic_extract_summary():
    result = heuristic_extract("第一章", "林舟来到青岚城。他发现玉佩发烫。")
    assert result.summary["short_summary"]
    assert result.facts


def test_split_chapters_accepts_spaced_and_unspaced_headings():
    manuscript = "第 1 章 石堤旧案\n风吹过石堤。\n\n第二章 信封\n他打开信封。"
    assert split_chapters_from_text(manuscript) == [
        {"title": "第 1 章 石堤旧案", "content": "风吹过石堤。"},
        {"title": "第二章 信封", "content": "他打开信封。"},
    ]


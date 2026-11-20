from unittest.mock import Mock

from app.checkers.consistency import ConsistencyChecker
from app.memory.extractor import MemoryExtractor, heuristic_extract


def test_heuristic_extract_keeps_context_worthy_facts_only():
    text = """改松岩看着柜台，没有说话。
空气安静下来。
叶含章取出半枚印章，放在残玉旁边。
改松岩发现账本最后一页缺页，纸边还残留淡淡银粉。
"""
    result = heuristic_extract("测试章", text)
    facts = [item["fact_text"] for item in result.facts]
    assert "叶含章取出半枚印章，放在残玉旁边" in facts
    assert "改松岩发现账本最后一页缺页，纸边还残留淡淡银粉" in facts
    assert all("空气安静" not in fact for fact in facts)


def test_memory_extraction_renders_prompt_with_literal_json():
    model = Mock()
    model.complete.return_value = '{"summary": {"short_summary": "钥匙已销毁"}}'

    result = MemoryExtractor(model).extract("{chapter_content}", "钥匙已销毁")

    prompt = model.complete.call_args.args[0]
    assert '"summary": {' in prompt
    assert "【章节标题】\n{chapter_content}" in prompt
    assert "【章节正文】\n钥匙已销毁" in prompt
    assert result.summary["short_summary"] == "钥匙已销毁"


def test_consistency_check_renders_prompt_with_literal_json():
    model = Mock()
    model.complete.return_value = '{"passed": false, "issues": [], "suggestions": []}'

    report = ConsistencyChecker(model).check("钥匙已销毁", "钥匙又出现了", "检查道具状态")

    prompt = model.complete.call_args.args[0]
    assert '"passed": true' in prompt
    assert "【结构化上下文】\n钥匙已销毁" in prompt
    assert "【待检查章节】\n钥匙又出现了" in prompt
    assert "【章节目标】\n检查道具状态" in prompt
    assert report.passed is False

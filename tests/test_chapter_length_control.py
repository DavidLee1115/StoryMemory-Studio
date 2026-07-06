from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.generation.chapter import generate_chapter
from app.llm.client import LLMClient


class ShortThenContinueClient(LLMClient):
    provider = "test"
    model = "dummy"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, system: str = "", temperature: float = 0.3) -> str:
        self.calls += 1
        if self.calls == 1:
            return "短句。" * 80
        return "补足正文。" * 400


def test_generate_chapter_continues_when_too_short() -> None:
    client = ShortThenContinueClient()
    text = generate_chapter(
        client,
        "上下文",
        extra_instruction="请生成正文，约 1200 个中文字符。",
        target_chars=1200,
    )
    assert client.calls >= 2
    assert len(text) >= 1080

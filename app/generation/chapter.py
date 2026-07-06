from __future__ import annotations

import re
from pathlib import Path

from app.generation.length_controller import chapter_length_target, length_status
from app.llm.client import LLMClient
from app.llm.output_cleaner import clean_model_output
from app.quality.humanizer_zh import humanize_zh_text
from app.style.similarity_guard import check_similarity


PROMPT_PATH = Path(__file__).parents[1] / "prompts" / "generate_chapter.md"


def _postprocess_generated_prose(text: str) -> str:
    cleaned = clean_model_output(text, mode="prose")
    humanized, _report = humanize_zh_text(cleaned)
    return humanized.strip()


def _infer_target_chars(*texts: str) -> int | None:
    joined = "\n".join(text for text in texts if text)
    patterns = [
        r"(?:目标|约|不少于|至少)\s*(\d{3,5})\s*(?:个)?(?:中文)?(?:字|字符)",
        r"(\d{3,5})\s*(?:个)?(?:中文)?(?:字|字符)",
    ]
    for pattern in patterns:
        match = re.search(pattern, joined)
        if not match:
            continue
        value = int(match.group(1))
        if 500 <= value <= 20000:
            return value
    return None


def _dedupe_continuation(existing: str, addition: str) -> str:
    existing = existing.rstrip()
    addition = addition.lstrip()
    if not existing or not addition:
        return addition
    max_overlap = min(len(existing), len(addition), 240)
    for size in range(max_overlap, 20, -1):
        if existing[-size:] == addition[:size]:
            return addition[size:].lstrip()
    return addition


def _continue_until_min_length(
    client: LLMClient,
    context_prompt: str,
    current_text: str,
    target_chars: int,
    temperature: float,
    max_attempts: int = 3,
) -> str:
    target = chapter_length_target(target_chars)
    text = current_text.strip()
    for attempt in range(max_attempts):
        if length_status(text, target) != "too_short":
            return text
        remaining = target.min_chars - len(text)
        tail = text[-900:]
        prompt = (
            f"{context_prompt}\n\n"
            "【续写补足任务】\n"
            f"当前章节目标长度约 {target.target_chars} 个中文字符，最低需要 {target.min_chars} 个中文字符。\n"
            f"当前正文只有 {len(text)} 个中文字符，还至少缺少 {remaining} 个中文字符。\n"
            "请只从下面正文末尾继续写，不要重写前文，不要输出标题、解释、提纲、英文草稿或 Markdown。\n"
            "续写必须承接最后一个具体动作、物件或对话，让本章形成完整场景推进，并保留章节结尾的具体事件钩子。\n"
            "禁止用空泛总结句强行收尾，禁止输出 <think>。\n\n"
            f"【已有正文末尾】\n{tail}\n\n"
            "【请继续正文】"
        )
        raw = client.complete(
            prompt,
            system="你是长篇小说续写助手。只输出中文小说正文续写，不要解释，不要英文草稿，不要思考过程。",
            temperature=max(0.5, temperature - 0.08 * attempt),
        )
        addition = _postprocess_generated_prose(raw)
        addition = _dedupe_continuation(text, addition)
        if not addition:
            break
        text = f"{text}\n\n{addition}".strip()
    return text


def generate_chapter(
    client: LLMClient,
    context_prompt: str,
    mode: str = "generate_chapter",
    extra_instruction: str = "",
    temperature: float = 0.7,
    target_chars: int | None = None,
    min_chars: int | None = None,
) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    inferred_target = target_chars or _infer_target_chars(extra_instruction, context_prompt)
    length_target = chapter_length_target(inferred_target) if inferred_target else None
    required_min = min_chars or (length_target.min_chars if length_target else None)
    retry_note = ""
    last_text = ""
    for attempt in range(2):
        prompt = template.format(context=context_prompt, mode=mode, extra_instruction=extra_instruction + retry_note)
        raw = client.complete(
            prompt,
            system=(
                "你是严谨的长篇小说创作中控台。优先遵守结构化记忆、上章承接、人物关系、伏笔和时间线。"
                "只输出中文小说正文，不输出提纲、检查项、英文笔记、创作说明或思考过程。"
            ),
            temperature=temperature if attempt == 0 else max(0.45, temperature - 0.15),
        )
        last_text = _postprocess_generated_prose(raw)
        if len(last_text) >= 200 and (required_min is None or len(last_text) >= required_min):
            return last_text
        if required_min and len(last_text) >= 200:
            break
        retry_note = (
            "\n\n【重试要求】上一次输出为空或过短。"
            "请直接输出中文小说正文，不要输出英文、提纲、检查清单、创作说明或 Markdown。"
        )
    if inferred_target and mode in {"generate_chapter", "continue"}:
        return _continue_until_min_length(client, context_prompt, last_text, inferred_target, temperature)
    return last_text


def rewrite_if_too_similar(
    client: LLMClient,
    generated_text: str,
    sample_text: str,
    context_prompt: str,
    style_profile: str,
    user_goal: str = "",
) -> tuple[str, dict]:
    report = check_similarity(sample_text, generated_text)
    if not report.rewrite_required:
        return generated_text, report.model_dump()
    prompt_path = Path(__file__).parents[1] / "prompts" / "style_transfer_rewrite.md"
    prompt = prompt_path.read_text(encoding="utf-8").format(
        context=context_prompt,
        memory="见当前章节上下文",
        style_profile=style_profile,
        user_goal=user_goal,
        text=generated_text,
    )
    rewritten = client.complete(prompt, system="你是原创改写助手，必须避免具体表达相似。", temperature=0.5)
    rewritten = _postprocess_generated_prose(rewritten)
    second = check_similarity(sample_text, rewritten)
    payload = report.model_dump()
    payload["rewritten_similarity"] = second.model_dump()
    payload["humanizer_zh_applied"] = True
    return rewritten, payload

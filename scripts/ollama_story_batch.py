from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from app.config import get_settings
from app.context.builder import ContextBuilder
from app.creation.commit_memory import project_name_from_title
from app.creation.wizard import commit_preview, create_preview_from_kwargs
from app.db.database import db_session, dumps, get_project, init_db, log_generation, rows_to_dicts
from app.db.models import ContextRequest
from app.export.docx_export import export_project_docx
from app.generation.chapter import generate_chapter
from app.llm.client import OllamaClient
from app.llm.ollama_utils import select_best_ollama_model
from app.memory.extractor import MemoryExtractor, heuristic_extract
from app.memory.writer import upsert_extraction


def _safe_print(text: str = "") -> None:
    print(text, flush=True)


def _prompt(label: str, default: str = "", required: bool = False) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        value = input(f"{label}{suffix}: ").strip()
        value = value or default
        if value or not required:
            return value
        _safe_print("这一项不能为空，请再填一次。")


def _prompt_int(label: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    while True:
        raw = _prompt(label, str(default))
        try:
            value = int(re.sub(r"[^\d]", "", raw) or default)
        except ValueError:
            _safe_print("请输入数字。")
            continue
        if value < minimum:
            _safe_print(f"不能小于 {minimum}。")
            continue
        if maximum is not None and value > maximum:
            _safe_print(f"不能大于 {maximum}。")
            continue
        return value


def _slug(text: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", text).strip("_")
    return cleaned[:80] or "novel"


def _load_environment() -> None:
    os.environ.setdefault("PYTHONUTF8", "1")
    load_dotenv(ROOT / ".env", override=False)
    os.environ.setdefault("DEFAULT_MODEL_PROVIDER", "ollama")


def _select_ollama_model() -> str:
    settings = get_settings()
    selection = select_best_ollama_model(settings.ollama_base_url)
    if not selection.available:
        _safe_print("\n未检测到可用 Ollama 模型。")
        _safe_print(f"原因：{selection.reason}")
        if selection.install_hint:
            _safe_print(f"建议：{selection.install_hint}")
        raise SystemExit(2)
    os.environ["DEFAULT_MODEL_PROVIDER"] = "ollama"
    os.environ["DEFAULT_OLLAMA_MODEL"] = selection.model
    try:
        get_settings.cache_clear()
    except AttributeError:
        pass
    _safe_print("\n已检测到本地 Ollama。")
    _safe_print(f"推荐模型：{selection.model}")
    _safe_print(f"推荐理由：{selection.reason}")
    if selection.models:
        _safe_print("本地模型列表：" + "、".join(selection.models[:12]))
    return selection.model


def _list_projects() -> list[dict[str, Any]]:
    init_db()
    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT
              p.id,
              p.name,
              p.title,
              p.genre,
              p.target_platform,
              p.updated_at,
              COUNT(c.id) AS chapter_count,
              COALESCE(MAX(c.chapter_number), 0) AS last_chapter
            FROM projects p
            LEFT JOIN chapters c ON c.project_id = p.id
            GROUP BY p.id
            ORDER BY p.updated_at DESC, p.id DESC
            """
        ).fetchall()
        return rows_to_dicts(rows)


def _show_projects(projects: list[dict[str, Any]]) -> None:
    if not projects:
        _safe_print("\n当前还没有小说项目。")
        return
    _safe_print("\n已有小说项目：")
    for idx, project in enumerate(projects, 1):
        _safe_print(
            f"{idx}. {project.get('title') or project['name']} "
            f"(ID {project['id']}，已有 {project['chapter_count']} 章，最新第 {project['last_chapter']} 章)"
        )


def _choose_project(projects: list[dict[str, Any]]) -> dict[str, Any]:
    _show_projects(projects)
    while True:
        choice = _prompt_int("请选择要续写的项目序号", 1, 1, len(projects))
        return projects[choice - 1]


def _outline_for_chapter(project: dict[str, Any], chapter_number: int) -> str:
    try:
        metadata = json.loads(project.get("metadata_json") or "{}")
    except json.JSONDecodeError:
        metadata = {}
    outline = metadata.get("chapter_outlines") or metadata.get("volume_outline") or []
    if isinstance(outline, list):
        for item in outline:
            if isinstance(item, dict) and int(item.get("chapter_number") or 0) == chapter_number:
                return json.dumps(item, ensure_ascii=False)
        if 0 < chapter_number <= len(outline):
            return json.dumps(outline[chapter_number - 1], ensure_ascii=False)
    return ""


def _chapter_title_from_outline(outline: str, chapter_number: int) -> str:
    if outline:
        try:
            data = json.loads(outline)
            title = data.get("title") or data.get("chapter_title")
            if title:
                return str(title)
        except Exception:
            pass
    return f"第{chapter_number}章"


def _create_new_project(model_name: str) -> dict[str, Any]:
    _safe_print("\n=== 新建长篇小说 ===")
    title = _prompt("作品名称", required=True)
    genre = _prompt("题材 / 类型", "长篇小说")
    platform = _prompt("目标平台", "番茄 / 起点 / 小红书连载可读风格")
    premise = _prompt("一句话核心设定", required=True)
    protagonist = _prompt("主角基础信息", "请根据题材自动生成主角")
    goal = _prompt("主角目标 / 全书目标", "围绕核心设定推进主线并完成成长")
    selling_points = _prompt("核心看点 / 爽点", "人物关系、冲突、反转、悬念、情绪拉扯")
    avoid = _prompt("不想要的内容", "不要 AI 腔，不要设定卡插入，不要模板化结尾")
    total_chapters = _prompt_int("计划总章节数", 100, 1)
    style = _prompt("参考文风 / 参考作者气质", "自然、场景驱动、轻小说可读性、少说明多潜台词")
    words = _prompt_int("单章目标中文字数", 3000, 800)

    project_name = project_name_from_title(title)
    _safe_print("\n正在调用 Ollama 生成小说 Bible 和第一章预览，请耐心等待...")
    try:
        preview = create_preview_from_kwargs(
            provider="ollama",
            mode="minimal",
            title=title,
            genre=genre,
            platform=platform,
            premise=premise,
            protagonist=protagonist,
            goal=goal,
            selling_points=selling_points,
            avoid=avoid,
            length=f"{total_chapters}章",
            style=style,
            target_reader="喜欢长篇连载、人物关系和稳定更新的读者",
            word_count=words,
        )
    except Exception as exc:
        _safe_print(f"\nOllama 生成 Bible 失败：{exc}")
        _safe_print("将使用本地规则生成一个可运行的初始 Story Memory，后续章节仍由 Ollama 生成。")
        preview = create_preview_from_kwargs(
            provider="none",
            mode="minimal",
            title=title,
            genre=genre,
            platform=platform,
            premise=premise,
            protagonist=protagonist,
            goal=goal,
            selling_points=selling_points,
            avoid=avoid,
            length=f"{total_chapters}章",
            style=style,
            target_reader="喜欢长篇连载、人物关系和稳定更新的读者",
            word_count=words,
        )

    _safe_print(f"预览已保存：{preview['preview_path']}")
    confirm = _prompt("是否写入 Story Memory 数据库？输入 y 确认", "y").lower()
    if confirm not in {"y", "yes", "是", "确认"}:
        raise SystemExit("用户取消写入。")
    committed = commit_preview(preview["preview_path"], output_project=project_name, provider="ollama")
    with db_session() as conn:
        project = get_project(conn, committed["project"])
        log_generation(
            conn,
            project["id"],
            "ollama_bat_create_project",
            provider="ollama",
            model=model_name,
            response=json.dumps(committed, ensure_ascii=False),
            structured=committed,
            module_name="ollama_story_batch",
            user_action="create_new_project_from_bat",
        )
        return project


def _insert_generated_chapter(
    conn,
    project_id: int,
    chapter_number: int,
    title: str,
    content: str,
    outline: str,
) -> int:
    conn.execute(
        """
        INSERT INTO chapters (project_id, chapter_number, volume, title, content, outline, status, word_count)
        VALUES (?, ?, ?, ?, ?, ?, 'draft', ?)
        ON CONFLICT(project_id, chapter_number) DO UPDATE SET
          title=excluded.title,
          content=excluded.content,
          outline=excluded.outline,
          word_count=excluded.word_count,
          updated_at=CURRENT_TIMESTAMP
        """,
        (project_id, chapter_number, "第一卷", title, content, outline, len(content)),
    )
    return conn.execute(
        "SELECT id FROM chapters WHERE project_id = ? AND chapter_number = ?",
        (project_id, chapter_number),
    ).fetchone()["id"]


def _generate_next_chapters(
    project: dict[str, Any],
    model_name: str,
    batch_count: int,
    total_chapters: int,
    direction: str,
    words_per_chapter: int,
) -> dict[str, Any]:
    settings = get_settings()
    client = OllamaClient(settings.ollama_base_url, model_name)
    extractor = MemoryExtractor(client)
    generated: list[dict[str, Any]] = []

    with db_session() as conn:
        refreshed = conn.execute("SELECT * FROM projects WHERE id = ?", (project["id"],)).fetchone()
        if refreshed is None:
            raise RuntimeError("项目不存在，无法生成。")
        project = dict(refreshed)
        last = conn.execute(
            "SELECT COALESCE(MAX(chapter_number), 0) AS n FROM chapters WHERE project_id = ?",
            (project["id"],),
        ).fetchone()["n"]
        start = int(last) + 1
        end = min(total_chapters, int(last) + batch_count)
        if start > end:
            _safe_print("当前项目已达到或超过目标章节数，无需继续生成。")
            return {"generated": [], "export_docx": ""}

        for chapter_number in range(start, end + 1):
            outline = _outline_for_chapter(project, chapter_number)
            if not outline:
                outline = (
                    f"第{chapter_number}章：承接上一章结尾，推进主线；保持人物关系连贯，"
                    f"通过具体场景、动作、物件和潜台词释放信息。"
                )
            chapter_goal = (
                f"续写《{project.get('title') or project['name']}》第{chapter_number}章。"
                f"目标字数约 {words_per_chapter} 个中文字符；承接上一章开头，结尾留下具体事件钩子。"
            )
            if direction:
                chapter_goal += f" 本批次用户方向：{direction}"

            _safe_print(f"\n正在构建第 {chapter_number} 章上下文...")
            context = ContextBuilder(conn, project).build(
                ContextRequest(
                    project=project["name"],
                    chapter_number=chapter_number,
                    chapter_goal=chapter_goal,
                    chapter_outline=outline,
                    mode="standard",
                )
            )
            extra = (
                f"请生成第{chapter_number}章正文，约 {words_per_chapter} 个中文字符。"
                "必须承接上一章最后一个具体事件，不要重开新局；"
                "禁止输出 <think>、英文草稿、写作计划、Markdown 标题或解释。"
            )
            _safe_print(f"正在调用 Ollama 生成第 {chapter_number} 章...")
            content = generate_chapter(
                client,
                context,
                mode="generate_chapter",
                extra_instruction=extra,
                temperature=0.62,
                target_chars=words_per_chapter,
            )
            title = _chapter_title_from_outline(outline, chapter_number)
            chapter_id = _insert_generated_chapter(conn, project["id"], chapter_number, title, content, outline)

            _safe_print("正在抽取并写入 Story Memory...")
            try:
                extraction = extractor.extract(title, content)
            except Exception as exc:
                _safe_print(f"Ollama 记忆抽取失败，改用本地启发式抽取：{exc}")
                extraction = heuristic_extract(title, content)
            upsert_extraction(conn, project["id"], chapter_id, extraction)
            log_generation(
                conn,
                project["id"],
                "ollama_bat_generate_chapter",
                provider="ollama",
                model=model_name,
                prompt=context,
                response=content,
                structured={
                    "chapter_number": chapter_number,
                    "chapter_title": title,
                    "word_count": len(content),
                    "direction": direction,
                },
                chapter_id=chapter_id,
                module_name="ollama_story_batch",
                user_action="batch_generate_from_bat",
                applied_to_chapter=True,
            )
            generated.append(
                {
                    "chapter_id": chapter_id,
                    "chapter_number": chapter_number,
                    "title": title,
                    "chars": len(content),
                }
            )
            _safe_print(f"第 {chapter_number} 章完成：{len(content)} 字符。")

    return {"generated": generated}


def _export_outputs(project_id: int, title: str, model_name: str, report: dict[str, Any]) -> dict[str, str]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    export_dir = ROOT / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    base = f"{_slug(title)}_Ollama批量创作_{timestamp}"
    docx_path = export_dir / f"{base}.docx"
    report_path = export_dir / f"{base}_report.json"
    md_path = export_dir / f"{base}.md"

    with db_session() as conn:
        export_project_docx(conn, project_id, docx_path, model_name=model_name)
        chapters = rows_to_dicts(
            conn.execute(
                "SELECT chapter_number, title, content FROM chapters WHERE project_id = ? ORDER BY chapter_number",
                (project_id,),
            ).fetchall()
        )
    md_parts = [f"# {title}", "", f"> 使用模型：{model_name}", ""]
    for chapter in chapters:
        md_parts.extend([f"## 第{chapter['chapter_number']}章 {chapter['title']}", "", chapter["content"], ""])
    md_path.write_text("\n".join(md_parts), encoding="utf-8")
    report["exports"] = {"docx": str(docx_path), "markdown": str(md_path), "report": str(report_path)}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report["exports"]


def run_interactive(batch_count: int, dry_run: bool = False) -> None:
    _load_environment()
    model_name = _select_ollama_model()
    projects = _list_projects()
    _show_projects(projects)
    if dry_run:
        _safe_print("\nDry run 完成：Ollama、数据库和项目列表可读取。")
        return

    _safe_print("\n=== StoryMemory Ollama 长篇自动创作脚本 ===")
    if projects:
        action = _prompt("请选择：1 新建小说 / 2 续写已有小说", "2")
    else:
        action = "1"

    if action.strip() == "1":
        project = _create_new_project(model_name)
    else:
        project = _choose_project(projects)
        with db_session() as conn:
            project = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (project["id"],)).fetchone())

    title = project.get("title") or project["name"]
    last_chapter = 0
    with db_session() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(chapter_number), 0) AS n FROM chapters WHERE project_id = ?",
            (project["id"],),
        ).fetchone()
        last_chapter = int(row["n"] or 0)

    total_default = max(10, last_chapter + batch_count)
    total_chapters = _prompt_int("目标总章节数", total_default, last_chapter + 1 if last_chapter else 1)
    batch_count = _prompt_int("本次自动生成几章", batch_count, 1, max(1, total_chapters - last_chapter))
    words_per_chapter = _prompt_int("每章目标中文字数", 3000, 800)
    direction = _prompt("本批次创作方向 / 今天想推进什么", "承接上一章，推进主线，保持连贯和具体事件钩子")

    started = time.time()
    result = _generate_next_chapters(project, model_name, batch_count, total_chapters, direction, words_per_chapter)
    report = {
        "project_id": project["id"],
        "project_name": project["name"],
        "title": title,
        "model": model_name,
        "generated": result["generated"],
        "elapsed_seconds": round(time.time() - started, 2),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    paths = _export_outputs(project["id"], title, model_name, report)
    _safe_print("\n=== 本次创作完成 ===")
    _safe_print(f"项目：{title} (ID {project['id']})")
    _safe_print(f"使用模型：{model_name}")
    _safe_print(f"生成章节：{len(result['generated'])} 章")
    _safe_print(f"DOCX：{paths['docx']}")
    _safe_print(f"Markdown：{paths['markdown']}")
    _safe_print(f"报告：{paths['report']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="StoryMemory Studio + Ollama 长篇小说批量创作脚本")
    parser.add_argument("--batch-count", type=int, default=10, help="默认本次生成章节数")
    parser.add_argument("--dry-run", action="store_true", help="只检测 Ollama、数据库和项目列表，不生成内容")
    args = parser.parse_args()
    run_interactive(batch_count=args.batch_count, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

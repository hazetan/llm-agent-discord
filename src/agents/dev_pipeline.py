import asyncio
import re
from collections.abc import Callable, Coroutine
from typing import Any

from src.core.config import get_pipeline_idle_sec
from src.sandbox.tools import read_workspace_doc

from .runner import run_role_phase

StatusCallback = Callable[[str], Coroutine[Any, Any, None]]


def detect_next_pipeline_role() -> str | None:
    """~/TASK.md のコメントを除いた箇条書きステータスを見て、次に稼働すべき部署を決定する"""
    task_text = read_workspace_doc("~/TASK.md")
    clean_text = re.sub(r"<!--.*?-->", "", task_text, flags=re.DOTALL)

    if re.search(r"^\s*(?:[-*+]|\d+\.)\s*\[QA\]", clean_text, flags=re.MULTILINE):
        return "QA"
    if re.search(r"^\s*(?:[-*+]|\d+\.)\s*\[(IMPL|FIX)\]", clean_text, flags=re.MULTILINE):
        return "PG"
    if re.search(r"^\s*(?:[-*+]|\d+\.)\s*\[TODO\]", clean_text, flags=re.MULTILINE):
        return "SE"
    return None


async def continuous_dev_pipeline_loop(on_phase_complete: StatusCallback | None = None) -> None:
    """常時起動し、~/TASK.md に未完了タスクがあれば SE -> PG -> QA を自動で回し続ける"""
    while True:
        try:
            role = detect_next_pipeline_role()
            if not role:
                await asyncio.sleep(get_pipeline_idle_sec())
                continue

            instruction = (
                f"現在 `~/TASK.md` に {role} 部門が担当すべき未処理タスクが存在します。\n"
                "社内標準ドキュメント（`~/README.md`, `~/TASK.md`, `~/DESIGN.md`, `~/REPORT.md`）に従い、"
                f"{role} としての職務を遂行し、`~/TASK.md` のステータスを更新してから `complete_phase` を呼んでください。"
            )
            summary, _ = await run_role_phase(role, instruction)

            if on_phase_complete:
                await on_phase_complete(f"**[{role} 部門 完了報告]**\n{summary}")

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[DevPipeline Error] {exc}")
            await asyncio.sleep(5)
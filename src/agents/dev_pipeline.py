import asyncio
import re
from collections.abc import Callable, Coroutine
from typing import Any

from core.config import get_pipeline_idle_sec
from sandbox.tools import read_workspace_doc
from .runner import run_role_phase

StatusCallback = Callable[[str], Coroutine[Any, Any, None]]


def detect_pending_roles() -> list[str]:
    """~/TASK.md 内に未処理タスクが存在する部署を優先順 (QA -> PG -> SE) でリスト返却する"""
    task_text = read_workspace_doc("~/TASK.md")
    clean_text = re.sub(r"<!--.*?-->", "", task_text, flags=re.DOTALL)

    roles: list[str] = []
    if re.search(r"^\s*(?:[-*+]|\d+\.)\s*\[QA\]", clean_text, flags=re.MULTILINE):
        roles.append("QA")
    if re.search(r"^\s*(?:[-*+]|\d+\.)\s*\[(IMPL|FIX)\]", clean_text, flags=re.MULTILINE):
        roles.append("PG")
    if re.search(r"^\s*(?:[-*+]|\d+\.)\s*\[TODO\]", clean_text, flags=re.MULTILINE):
        roles.append("SE")
    return roles


async def _execute_single_role(role: str, on_phase_complete: StatusCallback | None) -> None:
    instruction = (
        f"現在 `~/TASK.md` に {role} 部門が担当すべき未処理タスクが存在します。\n"
        "社内標準ドキュメント（`~/README.md`, `~/TASK.md`, `~/DESIGN.md`, `~/REPORT.md`）に従い、"
        f"{role} としての職務を遂行し、`~/TASK.md` のステータスを更新してから `complete_phase` を呼んでください。"
    )
    summary, _ = await run_role_phase(role, instruction)
    if on_phase_complete and summary.strip():
        await on_phase_complete(f"**[{role} 部門 完了報告]**\n{summary}")


async def continuous_dev_pipeline_loop(on_phase_complete: StatusCallback | None = None) -> None:
    """未処理タスクが存在する最優先の1部署を順番に直列実行するシングルワーカーループ"""
    while True:
        try:
            pending_roles = detect_pending_roles()
            if not pending_roles:
                await asyncio.sleep(get_pipeline_idle_sec())
                continue

            await _execute_single_role(pending_roles[0], on_phase_complete)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[DevPipeline Error] {exc}")
            await asyncio.sleep(5)
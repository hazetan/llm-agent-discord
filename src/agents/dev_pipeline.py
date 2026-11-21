import asyncio
import re
import time
from collections.abc import Callable, Coroutine
from typing import Any

from core.config import (
    get_autonomous_enable,
    get_autonomous_idle_sec,
    get_autonomous_max_steps,
    get_pipeline_idle_sec,
)
from core.logger import system_logger
from core.telemetry import telemetry
from sandbox.tools import read_workspace_doc

from .roles import AUTONOMOUS_INITIATIVE_PROMPT
from .runner import run_role_phase

StatusCallback = Callable[[str], Coroutine[Any, Any, None]]


TASK_TAG_PATTERN = re.compile(
    r"^\s*(?:[-*+]|#+|\d+[.)])?\s*(?:\[[ xX]\]\s*)?(?:\*\*|__)?\s*\[(TODO|IMPL|FIX|QA|DONE)\]\s*(?:\*\*|__)?\s*:?\s*(.+)",
    re.MULTILINE,
)


def detect_pending_roles() -> list[str]:
    """~/TASK.md 内に未処理タスクが存在する部署を優先順 (QA -> PG -> SE) でリスト返却する"""
    task_text = read_workspace_doc("~/TASK.md")
    clean_text = re.sub(r"<!--.*?-->", "", task_text, flags=re.DOTALL)

    found_tags = {m.group(1) for m in TASK_TAG_PATTERN.finditer(clean_text)}

    roles: list[str] = []
    if "QA" in found_tags:
        roles.append("QA")
    if "IMPL" in found_tags or "FIX" in found_tags:
        roles.append("PG")
    if "TODO" in found_tags:
        roles.append("SE")
    return roles


async def _execute_single_role(role: str, on_phase_complete: StatusCallback | None) -> None:
    system_logger.log("DEV_PIPELINE", f"Worker started for role={role}")
    instruction = (
        f"現在 `~/TASK.md` に {role} 部門が担当すべき未処理タスクが存在します。\n"
        "社内標準ドキュメント（`~/README.md`, `~/TASK.md`, `~/DESIGN.md`, `~/REPORT.md`）に従い、"
        f"{role} としての職務を遂行し、`~/TASK.md` のステータスを更新してから `complete_phase` を呼んでください。"
    )
    try:
        summary, _ = await run_role_phase(role, instruction)
    except Exception as exc:
        err_detail = f"{type(exc).__name__}: {exc}"
        print(f"[DevPipeline Error ({role})] {err_detail}")
        system_logger.error("DEV_PIPELINE", f"Worker error in role={role}: {err_detail}", exc)
        await asyncio.sleep(30)
        return

    if summary.strip():
        system_logger.log("DEV_PIPELINE", f"Worker role={role} completed phase successfully.")
        if on_phase_complete:
            await on_phase_complete(f"**[{role} 部門 完了報告]**\n{summary}")
    else:
        print(f"[DevPipeline Warning ({role})] Reached max steps without complete_phase. Cooling down...")
        system_logger.log("DEV_PIPELINE", f"Worker role={role} finished without complete_phase. Cooling down 30s.", level="WARN")
        await asyncio.sleep(30)


async def _execute_autonomous_initiative(on_phase_complete: StatusCallback | None) -> None:
    """未処理タスクがないアイドル時に、軽量モデルでファイル整理整頓と新規[TODO]タスクの自律起票を行う"""
    system_logger.log("DEV_PIPELINE", "Autonomous initiative worker started (role=SE).")
    try:
        summary, _ = await run_role_phase(
            "SE",
            AUTONOMOUS_INITIATIVE_PROMPT,
            max_steps=get_autonomous_max_steps(),
        )
    except Exception as exc:
        err_detail = f"{type(exc).__name__}: {exc}"
        print(f"[DevPipeline Error (AUTONOMOUS)] {err_detail}")
        system_logger.error("DEV_PIPELINE", f"Autonomous worker error: {err_detail}", exc)
        await asyncio.sleep(30)
        return

    if summary.strip():
        system_logger.log("DEV_PIPELINE", "Autonomous initiative completed successfully.")
        if on_phase_complete:
            await on_phase_complete(f"**[自律行動・整理整頓＆自己タスク起票 完了報告]**\n{summary}")
    else:
        system_logger.log("DEV_PIPELINE", "Autonomous worker finished without complete_phase.", level="WARN")


async def continuous_dev_pipeline_loop(on_phase_complete: StatusCallback | None = None) -> None:
    """
    未処理タスクが存在する役職 (QA, PG, SE) を各役職最大1人ずつ同時に並列実行し、
    全タスク完了後のアイドル状態が一定時間続いた場合は自律行動（整理整頓・自己起票）を発動するループ
    """
    running_tasks: dict[str, asyncio.Task[None]] = {}
    raw_task_md = read_workspace_doc("~/TASK.md")
    system_logger.log(
        "DEV_PIPELINE",
        f"continuous_dev_pipeline_loop started. Current ~/TASK.md ({len(raw_task_md)} chars):\n{raw_task_md.strip()}",
    )
    last_pending: list[str] | None = None
    idle_since: float = time.monotonic()

    try:
        while True:
            try:
                for role, task in list(running_tasks.items()):
                    if task.done():
                        running_tasks.pop(role, None)
                        idle_since = time.monotonic()
                        try:
                            task.result()
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            err_detail = f"{type(exc).__name__}: {exc}"
                            print(f"[DevPipeline Error ({role})] {err_detail}")
                            system_logger.error("DEV_PIPELINE", f"Task result error ({role}): {err_detail}", exc)

                pending_roles = detect_pending_roles()
                if pending_roles != last_pending:
                    system_logger.log("DEV_PIPELINE", f"Detected pending roles in ~/TASK.md: {pending_roles}")
                    last_pending = list(pending_roles)

                for role in pending_roles:
                    if role not in running_tasks:
                        system_logger.log("DEV_PIPELINE", f"Spawning async worker task for role={role}")
                        running_tasks[role] = asyncio.create_task(
                            _execute_single_role(role, on_phase_complete),
                            name=f"worker_{role}",
                        )

                if not running_tasks:
                    if (
                        get_autonomous_enable()
                        and "PM" not in telemetry.active_roles
                        and (time.monotonic() - idle_since) >= get_autonomous_idle_sec()
                    ):
                        system_logger.log("DEV_PIPELINE", "Idle threshold reached. Spawning autonomous initiative task...")
                        idle_since = time.monotonic()
                        running_tasks["SE"] = asyncio.create_task(
                            _execute_autonomous_initiative(on_phase_complete),
                            name="worker_AUTONOMOUS_SE",
                        )
                    else:
                        await asyncio.sleep(get_pipeline_idle_sec())
                        continue

                await asyncio.wait(
                    running_tasks.values(),
                    timeout=get_pipeline_idle_sec(),
                    return_when=asyncio.FIRST_COMPLETED,
                )

            except asyncio.CancelledError:
                system_logger.log("DEV_PIPELINE", "continuous_dev_pipeline_loop cancelled.")
                raise
            except Exception as exc:
                err_detail = f"{type(exc).__name__}: {exc}"
                print(f"[DevPipeline Loop Error] {err_detail}")
                system_logger.error("DEV_PIPELINE", f"Loop error: {err_detail}", exc)
                await asyncio.sleep(5)
    finally:
        for task in running_tasks.values():
            if not task.done():
                task.cancel()
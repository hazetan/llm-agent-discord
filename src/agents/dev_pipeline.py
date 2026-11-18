import asyncio
import re
from collections.abc import Callable, Coroutine
from typing import Any

from core.config import get_pipeline_idle_sec, get_worker_count
from sandbox.tools import read_workspace_doc

from .runner import run_role_phase

StatusCallback = Callable[[str], Coroutine[Any, Any, None]]

ROLE_TARGET_TAGS: dict[str, tuple[str, ...]] = {
    "SE": ("TODO",),
    "PG": ("IMPL", "FIX"),
    "QA": ("QA",),
}


class TaskClaimManager:
    """複数のワーカーが同じタスクを重複して処理しないように排他ロックする管理クラス"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._claimed_titles: set[str] = set()

    async def claim_task_for_role(self, role: str) -> str | None:
        target_tags = ROLE_TARGET_TAGS.get(role.upper(), ())
        if not target_tags:
            return None

        async with self._lock:
            raw = read_workspace_doc("~/TASK.md")
            clean = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)
            pattern = re.compile(r"^\s*(?:[-*+]|\d+\.)\s*\[(TODO|IMPL|FIX|QA)\]\s*(.+)")

            for line in clean.splitlines():
                m = pattern.match(line)
                if not m:
                    continue
                tag, title = m.group(1), m.group(2).strip()
                if tag in target_tags and title not in self._claimed_titles:
                    self._claimed_titles.add(title)
                    return title
            return None

    async def release_task(self, title: str) -> None:
        async with self._lock:
            self._claimed_titles.discard(title)


claim_manager = TaskClaimManager()


async def _single_role_worker_loop(
    role: str,
    worker_id: str,
    on_phase_complete: StatusCallback | None = None,
) -> None:
    """1人の独立したエージェントワーカー（例: PG-1, PG-2, QA-1）の常駐ループ"""
    idle_sec = get_pipeline_idle_sec()
    while True:
        claimed_title: str | None = None
        try:
            claimed_title = await claim_manager.claim_task_for_role(role)
            if not claimed_title:
                await asyncio.sleep(idle_sec)
                continue

            instruction = (
                f"あなたは独立ワーカー `{worker_id}` ({role} 部門) です。\n"
                f"現在あなたに割り当てられた担当タスクは以下の1件です（他のタスクには手を付けないでください）：\n"
                f"▶ 担当タスク: `{claimed_title}`\n\n"
                "社内標準ドキュメント（`~/README.md`, `~/TASK.md`, `~/DESIGN.md`, `~/REPORT.md`）に従い、"
                f"この担当タスクに対して {role} としての職務を遂行し、`~/TASK.md` 上のこのタスクのステータスタグを更新してから "
                "`complete_phase` を呼んでください。"
            )
            summary, _ = await run_role_phase(role, instruction, worker_id=worker_id)

            if on_phase_complete and summary.strip():
                await on_phase_complete(f"**[{worker_id} 完了報告]**\n{summary}")

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[Worker {worker_id} Error] {exc}")
            await asyncio.sleep(5)
        finally:
            if claimed_title:
                await claim_manager.release_task(claimed_title)


def start_parallel_dev_workers(on_phase_complete: StatusCallback | None = None) -> list[asyncio.Task]:
    """設定された人数分の SE / PG / QA ワーカーを独立した並行タスクとして一斉起動する"""
    tasks: list[asyncio.Task] = []
    for role in ("SE", "PG", "QA"):
        count = get_worker_count(role)
        for idx in range(1, count + 1):
            worker_id = f"{role}-{idx}"
            t = asyncio.create_task(
                _single_role_worker_loop(role, worker_id, on_phase_complete),
                name=f"worker_{worker_id}",
            )
            tasks.append(t)
    return tasks
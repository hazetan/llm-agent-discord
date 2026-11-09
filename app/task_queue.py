import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from .secrets import get_max_concurrent_tasks


@dataclass
class QueuedAgentJob:
    session_id: int
    objective: str
    coro_factory: Callable[[], Coroutine[Any, Any, None]]
    on_queued: Callable[[int], Coroutine[Any, Any, None]] | None = None
    cancelled: bool = False


class AgentTaskQueue:
    """
    エージェントセッションの同時実行数を制御し，負荷分散を行う非同期タスクキュー．
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[QueuedAgentJob] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._running_tasks: dict[int, asyncio.Task] = {}
        self._pending_jobs: dict[int, QueuedAgentJob] = {}

    def start_workers(self) -> None:
        """設定された最大同時実行数分のバックグラウンドワーカーを起動する．"""
        if self._workers:
            return
        num_workers = get_max_concurrent_tasks()
        for idx in range(num_workers):
            worker_task = asyncio.create_task(
                self._worker_loop(idx), name=f"agent_worker_{idx}"
            )
            self._workers.append(worker_task)

    async def submit(
        self,
        session_id: int,
        objective: str,
        coro_factory: Callable[[], Coroutine[Any, Any, None]],
        on_queued: Callable[[int], Coroutine[Any, Any, None]] | None = None,
    ) -> int:
        """
        ジョブをキューへ投入し，現在の待機順位（0 なら即時実行可能）を返す．
        """
        self.start_workers()

        job = QueuedAgentJob(
            session_id=session_id,
            objective=objective,
            coro_factory=coro_factory,
            on_queued=on_queued,
        )
        self._pending_jobs[session_id] = job
        queue_position = len(self._pending_jobs)

        # 全ワーカーが稼働中の場合は待機通知コールバックを呼ぶ
        if len(self._running_tasks) >= get_max_concurrent_tasks() and on_queued:
            await on_queued(queue_position)

        await self._queue.put(job)
        return queue_position

    def cancel_session(self, session_id: int) -> bool:
        """待機中または実行中のセッションをキャンセルする．"""
        # 1. キュー待機中の場合
        if session_id in self._pending_jobs:
            job = self._pending_jobs.pop(session_id)
            job.cancelled = True
            return True

        # 2. 現在実行中の場合
        running_task = self._running_tasks.get(session_id)
        if running_task and not running_task.done():
            running_task.cancel()
            return True

        return False

    def get_active_session_ids(self) -> list[int]:
        """現在実行中およびキュー待機中の全セッション ID リストを返す．"""
        return list(self._running_tasks.keys()) + list(self._pending_jobs.keys())

    def get_status_summary(self) -> dict[str, Any]:
        """現在のワーカー稼働状況と待機キューの概要を返す．"""
        return {
            "max_workers": get_max_concurrent_tasks(),
            "running_sessions": list(self._running_tasks.keys()),
            "queued_sessions": [
                {"session_id": j.session_id, "objective": j.objective[:40]}
                for j in self._pending_jobs.values()
                if not j.cancelled
            ],
        }

    async def _worker_loop(self, worker_id: int) -> None:
        while True:
            job = await self._queue.get()
            self._pending_jobs.pop(job.session_id, None)

            if job.cancelled:
                self._queue.task_done()
                continue

            task = asyncio.create_task(job.coro_factory())
            self._running_tasks[job.session_id] = task
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                print(f"[Worker #{worker_id}] Session #{job.session_id} error: {exc}")
            finally:
                self._running_tasks.pop(job.session_id, None)
                self._queue.task_done()


# グローバルタスクキューインスタンス
agent_task_queue = AgentTaskQueue()
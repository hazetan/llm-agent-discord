import asyncio
import time

import discord

from core.config import (
    get_raw_log_channel_id,
    get_status_channel_id,
    get_status_update_interval_sec,
)
from core.logger import system_logger
from core.telemetry import render_raw_log_box, render_status_box, telemetry

RAW_LOG_MIN_INTERVAL_SEC = 5.0


def _strip_timestamp(box_text: str) -> str:
    """1行目の時刻表示を除いた実質的な表示内容を返す（差分判定用）"""
    lines = box_text.splitlines()
    if len(lines) > 1:
        return "\n".join(lines[1:])
    return box_text


class LiveStatusMonitor:
    """稼働状況Boxおよびリアルタイム生ログBoxを管理するクラス"""

    def __init__(self, client: discord.Client) -> None:
        self.client = client
        self.status_message: discord.Message | None = None
        self.raw_log_message: discord.Message | None = None
        self._last_raw_edit_t: float = 0.0
        self._pending_raw_task: asyncio.Task[None] | None = None
        self._status_lock = asyncio.Lock()
        self._raw_lock = asyncio.Lock()
        self._last_status_body: str = ""
        self._last_raw_body: str = ""
        telemetry.on_raw_update = self.refresh_raw_log_box

    async def _resolve_text_channel(self, ch_id: int | None) -> discord.TextChannel | None:
        if not ch_id:
            return None
        ch = self.client.get_channel(ch_id)
        if not ch:
            try:
                ch = await self.client.fetch_channel(ch_id)
            except Exception:
                return None
        return ch if isinstance(ch, discord.TextChannel) else None

    async def _purge_and_send(self, ch_id: int | None, content: str) -> discord.Message | None:
        channel = await self._resolve_text_channel(ch_id)
        if not channel:
            return None
        try:
            async for msg in channel.history(limit=15):
                if self.client.user and msg.author.id == self.client.user.id:
                    await msg.delete()
        except Exception:
            pass

        try:
            return await channel.send(content)
        except Exception as exc:
            print(f"[StatusMonitor] Send error in channel {ch_id}: {exc}")
            return None

    async def initialize_box(self) -> None:
        """起動時にステータスBoxと生ログBoxの両方を初期化する"""
        system_logger.log("STATUS_MON", f"Initializing boxes (status_ch={get_status_channel_id()}, raw_ch={get_raw_log_channel_id()})...")
        async with self._status_lock:
            status_content = await render_status_box()
            self.status_message = await self._purge_and_send(get_status_channel_id(), status_content)
            self._last_status_body = _strip_timestamp(status_content)

        async with self._raw_lock:
            raw_content = render_raw_log_box()
            self.raw_log_message = await self._purge_and_send(get_raw_log_channel_id(), raw_content)
            self._last_raw_body = _strip_timestamp(raw_content)
            self._last_raw_edit_t = time.monotonic()

        system_logger.log(
            "STATUS_MON",
            f"Boxes initialized (status_msg={bool(self.status_message)}, raw_msg={bool(self.raw_log_message)}).",
        )

    async def refresh_box(self) -> None:
        """稼働状況Boxを上書き更新する（多重実行防止＆差分なし時はスキップ）"""
        if not get_status_channel_id() or self._status_lock.locked():
            return
        async with self._status_lock:
            content = await render_status_box()
            body = _strip_timestamp(content)
            if not self.status_message:
                self.status_message = await self._purge_and_send(get_status_channel_id(), content)
                self._last_status_body = body
                return
            if body == self._last_status_body:
                return
            try:
                await self.status_message.edit(content=content)
                self._last_status_body = body
                system_logger.log("STATUS_MON", "Status box refreshed successfully.")
            except discord.NotFound:
                self.status_message = None
                self._last_status_body = ""
            except Exception as exc:
                print(f"[StatusMonitor] Status refresh error: {exc}")
                system_logger.error("STATUS_MON", f"Status refresh error: {exc}", exc)

    async def _delayed_raw_refresh(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            self._pending_raw_task = None
            await self.refresh_raw_log_box()
        except asyncio.CancelledError:
            raise
    
    async def refresh_raw_log_box(self) -> None:
        """log-raw チャンネルの生ログBoxを排他ロック＆完了後5秒間隔で安全に更新する"""
        if not get_raw_log_channel_id():
            return

        if self._raw_lock.locked():
            if not self._pending_raw_task or self._pending_raw_task.done():
                self._pending_raw_task = asyncio.create_task(
                    self._delayed_raw_refresh(RAW_LOG_MIN_INTERVAL_SEC)
                )
            return

        now = time.monotonic()
        elapsed = now - self._last_raw_edit_t
        if elapsed < RAW_LOG_MIN_INTERVAL_SEC:
            if not self._pending_raw_task or self._pending_raw_task.done():
                wait_sec = RAW_LOG_MIN_INTERVAL_SEC - elapsed + 0.1
                self._pending_raw_task = asyncio.create_task(self._delayed_raw_refresh(wait_sec))
            return

        async with self._raw_lock:
            content = render_raw_log_box()
            body = _strip_timestamp(content)
            if not self.raw_log_message:
                self.raw_log_message = await self._purge_and_send(get_raw_log_channel_id(), content)
                self._last_raw_body = body
                self._last_raw_edit_t = time.monotonic()
                return

            if body == self._last_raw_body:
                return

            try:
                await self.raw_log_message.edit(content=content)
                self._last_raw_body = body
            except discord.NotFound:
                self.raw_log_message = None
                self._last_raw_body = ""
            except Exception as exc:
                print(f"[StatusMonitor] Raw log refresh error: {exc}")
            finally:
                self._last_raw_edit_t = time.monotonic()

    async def remove_box(self) -> None:
        """Bot停止時に保留タスクをキャンセルし両方のBoxを削除する"""
        if self._pending_raw_task and not self._pending_raw_task.done():
            self._pending_raw_task.cancel()
        self._pending_raw_task = None

        for msg in (self.status_message, self.raw_log_message):
            if msg:
                try:
                    await msg.delete()
                except Exception:
                    pass
        self.status_message = None
        self.raw_log_message = None

    async def periodic_update_loop(self) -> None:
        await self.initialize_box()
        while True:
            await asyncio.sleep(get_status_update_interval_sec())
            await self.refresh_box()
    
    
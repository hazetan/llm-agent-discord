import asyncio
import time

import discord

from core.config import (
    get_raw_log_channel_id,
    get_status_channel_id,
    get_status_update_interval_sec,
)
from core.telemetry import render_raw_log_box, render_status_box, telemetry

RAW_LOG_MIN_INTERVAL_SEC = 2.5  # Discord APIレート制限回避用の最小更新間隔


class LiveStatusMonitor:
    """稼働状況Boxおよびリアルタイム生ログBoxを管理するクラス"""

    def __init__(self, client: discord.Client) -> None:
        self.client = client
        self.status_message: discord.Message | None = None
        self.raw_log_message: discord.Message | None = None
        self._last_raw_edit_t: float = 0.0
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
        status_content = await render_status_box()
        self.status_message = await self._purge_and_send(get_status_channel_id(), status_content)

        raw_content = render_raw_log_box()
        self.raw_log_message = await self._purge_and_send(get_raw_log_channel_id(), raw_content)

    async def refresh_box(self) -> None:
        """稼働状況Boxを上書き更新する"""
        if not get_status_channel_id():
            return
        if not self.status_message:
            status_content = await render_status_box()
            self.status_message = await self._purge_and_send(get_status_channel_id(), status_content)
            return
        try:
            content = await render_status_box()
            await self.status_message.edit(content=content)
        except discord.NotFound:
            self.status_message = None
        except Exception as exc:
            print(f"[StatusMonitor] Status refresh error: {exc}")

    async def refresh_raw_log_box(self) -> None:
        """ステップ実行ごとに log-raw チャンネルの生ログBoxをリアルタイム更新する"""
        if not get_raw_log_channel_id():
            return
        now = time.monotonic()
        if now - self._last_raw_edit_t < RAW_LOG_MIN_INTERVAL_SEC:
            return

        content = render_raw_log_box()
        if not self.raw_log_message:
            self.raw_log_message = await self._purge_and_send(get_raw_log_channel_id(), content)
            self._last_raw_edit_t = time.monotonic()
            return

        try:
            self._last_raw_edit_t = now
            await self.raw_log_message.edit(content=content)
        except discord.NotFound:
            self.raw_log_message = None
        except Exception as exc:
            print(f"[StatusMonitor] Raw log refresh error: {exc}")

    async def remove_box(self) -> None:
        """Bot停止時に両方のBoxを削除する"""
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
import asyncio
import re

import discord

from agents.auditor_agent import continuous_auditor_loop
from agents.dev_pipeline import continuous_dev_pipeline_loop
from agents.pm_agent import handle_client_message
from core.config import get_monitored_channel_ids
from core.nas_server import nas_server

from .status_monitor import LiveStatusMonitor

MAX_MSG_LEN = 1900

class SIerBotClient(discord.Client):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.status_monitor = LiveStatusMonitor(self)

    async def close(self) -> None:
        print("[Shutdown] Stopping NAS and removing live status boxes from Discord...")
        await nas_server.stop()
        await self.status_monitor.remove_box()
        await super().close()

intents = discord.Intents.default()
intents.message_content = True
bot = SIerBotClient(intents=intents)

_background_tasks: list[asyncio.Task] = []


async def send_chunked(channel: discord.abc.Messageable, text: str, reply_to: discord.Message | None = None) -> None:
    chunks = [text[i : i + MAX_MSG_LEN] for i in range(0, len(text), MAX_MSG_LEN)] or ["(Empty response)"]
    for idx, chunk in enumerate(chunks):
        if idx == 0 and reply_to is not None:
            await reply_to.reply(chunk)
        else:
            await channel.send(chunk)


def is_monitored_channel(channel: discord.abc.Messageable) -> bool:
    """テキストチャンネルID、またはフォーラム／スレッドの親チャンネルIDが .env に含まれるか判定"""
    monitored_ids = get_monitored_channel_ids()
    ch_id = getattr(channel, "id", None)
    parent_id = getattr(channel, "parent_id", None)
    return (ch_id in monitored_ids) or (parent_id in monitored_ids)


async def broadcast_to_primary_channel(message_text: str) -> None:
    """開発ライン（SE/PG/QA）の完了報告を監視対象の先頭テキストチャンネルへ通知する"""
    for ch_id in get_monitored_channel_ids():
        ch = bot.get_channel(ch_id)
        if isinstance(ch, discord.TextChannel):
            await send_chunked(ch, message_text)
            break

async def on_pipeline_phase_done(message_text: str) -> None:
    """開発ラインの1フェーズ完了時にレポート送信＆ステータスBoxを更新する"""
    await broadcast_to_primary_channel(message_text)
    await bot.status_monitor.refresh_box()


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} | Monitored Channels: {get_monitored_channel_ids()}")
    if not _background_tasks:
        await nas_server.start()
        _background_tasks.append(
            asyncio.create_task(bot.status_monitor.periodic_update_loop())
        )
        _background_tasks.append(
            asyncio.create_task(continuous_dev_pipeline_loop(on_phase_complete=on_pipeline_phase_done))
        )
        _background_tasks.append(
            asyncio.create_task(continuous_auditor_loop())
        )


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    if not is_monitored_channel(message.channel):
        return

    # 他のユーザーへのメンションが含まれている場合は無視（ボット自身へのメンションは許可）
    other_mentions = [u for u in message.mentions if bot.user and u.id != bot.user.id]
    if other_mentions or message.mention_everyone:
        return

    clean_text = re.sub(r"<@!?\d+>", "", message.content).strip()
    if not clean_text:
        return

    async with message.channel.typing():
        reply_text, zip_files = await handle_client_message(clean_text)
        await send_chunked(message.channel, reply_text, reply_to=message)

        for zp in zip_files:
            if zp.stat().st_size < 25 * 1024 * 1024:
                await message.channel.send(file=discord.File(str(zp)))
import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import discord

from .agent_runner import run_agent_loop
from .db_models import GoalSession
from .db_system import get_db_session
from .sandbox import IGNORE_PATTERNS, package_project_to_zip
from .secrets import get_projects_dir
from .task_queue import agent_task_queue

pending_human_questions: dict[int, asyncio.Future] = {}

MAX_DISCORD_MSG_LEN = 1900
MAX_ZIP_BYTES = 25 * 1024 * 1024
DASHBOARD_EDIT_INTERVAL = 1.2
HUMAN_ASK_TIMEOUT_SEC = 600


async def send_chunked_message(
    channel: discord.abc.Messageable,
    text: str,
    reply_target: discord.Message | None = None,
) -> None:
    for i in range(0, len(text), MAX_DISCORD_MSG_LEN):
        chunk = text[i : i + MAX_DISCORD_MSG_LEN]
        if i == 0 and reply_target is not None:
            await reply_target.reply(chunk)
        else:
            await channel.send(chunk)


class AgentDashboardView(discord.ui.View):
    def __init__(self, session_id: int, project_dir: Path):
        super().__init__(timeout=None)
        self.session_id = session_id
        self.project_dir = project_dir

    @discord.ui.button(
        label="🛑 中断", style=discord.ButtonStyle.danger, custom_id="btn_stop"
    )
    async def stop_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if agent_task_queue.cancel_session(self.session_id):
            await interaction.response.send_message(
                f"🛑 セッション #{self.session_id} に中断シグナルを送信しました．",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "既に完了しているか実行中ではありません．", ephemeral=True
            )

    @discord.ui.button(
        label="📄 ファイル一覧",
        style=discord.ButtonStyle.secondary,
        custom_id="btn_files",
    )
    async def list_files_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self.project_dir.exists():
            await interaction.response.send_message(
                "プロジェクトディレクトリが存在しません．", ephemeral=True
            )
            return

        lines = []
        for root, dirs, files in os.walk(self.project_dir):
            dirs[:] = [d for d in dirs if d not in IGNORE_PATTERNS]
            rel = Path(root).relative_to(self.project_dir)
            prefix = "" if str(rel) == "." else f"{rel}/"
            for f in files:
                lines.append(f"• {prefix}{f}")

        msg = (
            "**現在のワークスペースファイル:**\n" + "\n".join(lines[:30])
            if lines
            else "(空のワークスペース)"
        )
        await interaction.response.send_message(
            msg[:MAX_DISCORD_MSG_LEN], ephemeral=True
        )

    @discord.ui.button(
        label="📦 今すぐZIP出力",
        style=discord.ButtonStyle.primary,
        custom_id="btn_zip",
    )
    async def zip_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self.project_dir.exists():
            await interaction.response.send_message(
                "プロジェクトディレクトリが存在しません．", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        zip_path = package_project_to_zip(self.project_dir, self.session_id)
        if zip_path.stat().st_size < MAX_ZIP_BYTES:
            await interaction.followup.send(
                content=f"📦 セッション #{self.session_id} 現時点のスナップショット:",
                file=discord.File(str(zip_path)),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                content="ZIPが25MBを超えているため送信できませんでした．",
                ephemeral=True,
            )


async def enqueue_agent_with_dashboard(
    channel: discord.abc.Messageable,
    session_id: int,
    objective: str,
    reply_msg_id: int | None = None,
) -> None:
    """ダッシュボードを作成し，非同期タスクキューへエージェントジョブを登録する．"""
    project_dir = get_projects_dir() / f"session_{session_id}"
    view = AgentDashboardView(session_id, project_dir)

    embed = discord.Embed(
        title=f"🚀 Project Runner - Session #{session_id}",
        description=f"**目標**: {objective}",
        color=discord.Color.blue(),
        timestamp=datetime.now(UTC),
    )
    embed.add_field(name="進捗", value="キュー投入中...", inline=False)
    embed.add_field(name="最新アクション", value="ワーカー割り当て待機中", inline=False)

    dashboard_msg = await channel.send(embed=embed, view=view)
    last_edit_time = 0.0

    async def notify_queued(position: int) -> None:
        embed.color = discord.Color.light_grey()
        embed.set_field_at(
            0,
            name="進捗",
            value=f"⏳ **キュー待機中 (Waiting in Queue: #{position})**",
            inline=False,
        )
        try:
            await dashboard_msg.edit(embed=embed, view=view)
        except Exception:
            pass

    async def update_dashboard(
        step: int = 1,
        max_steps: int = 40,
        status: str = "実行中",
        thought: str | None = None,
        tool_name: str | None = None,
        tool_args: dict[str, Any] | None = None,
        tool_output: str | None = None,
    ) -> None:
        nonlocal last_edit_time
        elapsed = time.time() - last_edit_time
        if elapsed < DASHBOARD_EDIT_INTERVAL:
            await asyncio.sleep(DASHBOARD_EDIT_INTERVAL - elapsed)

        embed.color = (
            discord.Color.gold() if "LLM" in status else discord.Color.blue()
        )
        embed.set_field_at(
            0,
            name="進捗",
            value=f"**Step [{step}/{max_steps}]** - `{status}`",
            inline=False,
        )

        action_parts = []
        if tool_name:
            arg_str = json.dumps(tool_args, ensure_ascii=False) if tool_args else ""
            if len(arg_str) > 100:
                arg_str = arg_str[:100] + "..."
            action_parts.append(
                f"⚙️ **ツール**: `{tool_name}`\n```json\n{arg_str}\n```"
            )
        if tool_output:
            out_preview = (
                tool_output[:300] + "..." if len(tool_output) > 300 else tool_output
            )
            action_parts.append(f"📥 **出力**:\n```text\n{out_preview}\n```")

        if action_parts:
            embed.set_field_at(
                1, name="最新アクション", value="\n".join(action_parts), inline=False
            )

        try:
            await dashboard_msg.edit(embed=embed, view=view)
            last_edit_time = time.time()
        except Exception:
            pass

    async def human_ask_listener(question: str) -> str:
        prompt_text = (
            f"🙋‍♂️ **[エージェントからの質問] (Session #{session_id})**\n"
            f"> {question}\n\n"
            f"返答するにはこのチャンネルで `./tell {question[:15]}...` または直接返信してください．"
        )
        await channel.send(prompt_text)

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        pending_human_questions[session_id] = fut
        try:
            return await asyncio.wait_for(fut, timeout=HUMAN_ASK_TIMEOUT_SEC)
        except TimeoutError:
            return "No human response received within timeout. Proceed with best guess."
        finally:
            pending_human_questions.pop(session_id, None)

    async def _job_coroutine() -> None:
        try:
            result = await run_agent_loop(
                session_id,
                dashboard_callback=update_dashboard,
                human_ask_callback=human_ask_listener,
            )

            embed.color = discord.Color.green()
            embed.set_field_at(
                0, name="進捗", value="✅ **完了 (Completed)**", inline=False
            )
            await dashboard_msg.edit(embed=embed, view=view)

            final_report = f"**[Session #{session_id} 成果報告]**\n{result}"
            await send_chunked_message(channel, final_report)

            with get_db_session() as db:
                sess = db.query(GoalSession).filter(GoalSession.id == session_id).first()
                if sess and sess.project_dir and Path(sess.project_dir).exists():
                    zip_path = package_project_to_zip(Path(sess.project_dir), session_id)
                    sess.artifact_zip = str(zip_path)
                    db.commit()

                    if zip_path.stat().st_size < MAX_ZIP_BYTES:
                        await channel.send(
                            content=f"📦 **[成果物ZIP]** セッション #{session_id} の完成パッケージです．",
                            file=discord.File(str(zip_path)),
                        )

        except asyncio.CancelledError:
            embed.color = discord.Color.red()
            embed.set_field_at(
                0, name="進捗", value="🛑 **中断されました (Cancelled)**", inline=False
            )
            try:
                await dashboard_msg.edit(embed=embed, view=view)
            except Exception:
                pass
            await channel.send(
                f"🛑 **[Session #{session_id}]** ユーザーの指示によりタスクを中断しました．"
            )
            raise

    await agent_task_queue.submit(
        session_id=session_id,
        objective=objective,
        coro_factory=_job_coroutine,
        on_queued=notify_queued,
    )
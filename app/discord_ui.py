import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import discord

from .agent_runner import add_session_intervention, run_agent_loop
from .db_models import GoalSession
from .db_system import get_db_session
from .sandbox import IGNORE_PATTERNS, package_project_to_zip
from .secrets import get_projects_dir
from .task_queue import agent_task_queue

pending_human_questions: dict[int, asyncio.Future] = {}

MAX_DISCORD_MSG_LEN = 1900
MAX_ZIP_BYTES = 25 * 1024 * 1024
DASHBOARD_EDIT_INTERVAL = 2.5  # Rate Limit (5回/5秒) を確実に回避する安全間隔
HUMAN_ASK_TIMEOUT_SEC = 600


def _split_markdown_safe(text: str, max_len: int = MAX_DISCORD_MSG_LEN) -> list[str]:
    """コードブロックや改行構造を維持したまま安全にメッセージを分割する"""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0
    in_code_block = False
    code_fence_header = "```"

    for line in text.splitlines(keepends=True):
        # 1行自体が max_len を超える場合の強制分割
        while len(line) > max_len - 50:
            part = line[: max_len - 50]
            line = line[max_len - 50 :]
            if current_len + len(part) > max_len - 20:
                chunk_str = "".join(current_lines)
                if in_code_block:
                    chunk_str += "\n```"
                chunks.append(chunk_str)
                current_lines = [f"{code_fence_header}\n"] if in_code_block else []
                current_len = len(current_lines[0]) if current_lines else 0
            current_lines.append(part)
            current_len += len(part)

        if current_len + len(line) > max_len - 20:
            chunk_str = "".join(current_lines)
            if in_code_block:
                chunk_str += "\n```"
            chunks.append(chunk_str)
            current_lines = [f"{code_fence_header}\n"] if in_code_block else []
            current_len = len(current_lines[0]) if current_lines else 0

        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_code_block:
                in_code_block = True
                code_fence_header = stripped
            else:
                in_code_block = False
                code_fence_header = "```"

        current_lines.append(line)
        current_len += len(line)

    if current_lines:
        chunk_str = "".join(current_lines)
        if in_code_block:
            chunk_str += "\n```"
        chunks.append(chunk_str)

    return chunks


async def send_chunked_message(
    channel: discord.abc.Messageable,
    text: str,
    reply_target: discord.Message | None = None,
) -> None:
    chunks = _split_markdown_safe(text, MAX_DISCORD_MSG_LEN)
    for i, chunk in enumerate(chunks):
        if i == 0 and reply_target is not None:
            await reply_target.reply(chunk)
        else:
            await channel.send(chunk)


def _sync_get_session_info(session_id: int) -> tuple[str | None, str | None, str | None]:
    with get_db_session() as db:
        session = db.query(GoalSession).filter(GoalSession.id == session_id).first()
        if not session:
            return None, None, None
        return session.status, session.objective, session.project_dir


def _sync_set_session_pending(session_id: int) -> tuple[bool, str, str]:
    with get_db_session() as db:
        session = db.query(GoalSession).filter(GoalSession.id == session_id).first()
        if not session:
            return False, "セッションが見つかりません．", ""
        if session.status in ["running", "pending"]:
            return False, "このセッションは現在実行中または待機中です．", ""
        session.status = "pending"
        objective = session.objective
        db.commit()
        return True, "", objective


class ContinuePromptModal(discord.ui.Modal, title="プロンプトを追加して続行"):
    prompt_input = discord.ui.TextInput(
        label="追加の指示や修正要望",
        style=discord.TextStyle.paragraph,
        placeholder="例: 「〇〇の機能も追加して」「文字色を赤にして」",
        required=True,
    )

    def __init__(self, session_id: int):
        super().__init__()
        self.session_id = session_id

    async def on_submit(self, interaction: discord.Interaction):
        prompt_text = self.prompt_input.value
        ok, err_msg, objective = await asyncio.to_thread(_sync_set_session_pending, self.session_id)
        if not ok:
            await interaction.response.send_message(err_msg, ephemeral=True)
            return

        add_session_intervention(
            self.session_id,
            f"ユーザーからの追加指示・修正要望: {prompt_text}\nこの指示に従って作業を続行し、完全に動作することを確認してから完了してください。",
        )

        await interaction.response.send_message(
            f"セッション #{self.session_id} に追加指示を送り，作業を再開しました．",
            ephemeral=True,
        )
        asyncio.create_task(
            enqueue_agent_with_dashboard(interaction.channel, self.session_id, objective)
        )


class InjectPromptModal(discord.ui.Modal, title="実行中のエージェントに割り込み指示"):
    prompt_input = discord.ui.TextInput(
        label="指示や修正要望",
        style=discord.TextStyle.paragraph,
        placeholder="例: 「〇〇のファイルを確認して」「その方針はやめて〇〇にして」",
        required=True,
    )

    def __init__(self, session_id: int):
        super().__init__()
        self.session_id = session_id

    async def on_submit(self, interaction: discord.Interaction):
        prompt_text = self.prompt_input.value
        status, _, _ = await asyncio.to_thread(_sync_get_session_info, self.session_id)
        if not status:
            await interaction.response.send_message("セッションが見つかりません．", ephemeral=True)
            return
        if status not in ["running", "pending"]:
            await interaction.response.send_message(
                "このセッションは実行中ではないため割り込めません。「指示を追加して再開」を使用してください。",
                ephemeral=True,
            )
            return

        add_session_intervention(self.session_id, prompt_text)
        await interaction.response.send_message(
            f"セッション #{self.session_id} の次ステップへ指示を割り込ませました: `{prompt_text}`",
            ephemeral=True,
        )


class AgentDashboardView(discord.ui.View):
    def __init__(self, session_id: int, project_dir: Path):
        super().__init__(timeout=None)
        self.session_id = session_id
        self.project_dir = project_dir

    @discord.ui.button(
        label="🛑 中断", style=discord.ButtonStyle.danger, custom_id="btn_stop", row=0
    )
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if agent_task_queue.cancel_session(self.session_id):
            await interaction.response.send_message(
                f"セッション #{self.session_id} に中断シグナルを送信しました．", ephemeral=True
            )
        else:
            await interaction.response.send_message("既に完了しているか実行中ではありません．", ephemeral=True)

    @discord.ui.button(
        label="⚡ 割り込み指示", style=discord.ButtonStyle.primary, custom_id="btn_inject", row=0
    )
    async def inject_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        status, _, _ = await asyncio.to_thread(_sync_get_session_info, self.session_id)
        if not status:
            await interaction.response.send_message("セッションが見つかりません．", ephemeral=True)
            return
        if status not in ["running", "pending"]:
            await interaction.response.send_message(
                "実行中ではないため割り込めません。「指示を追加して再開」を使用してください。",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(InjectPromptModal(self.session_id))

    @discord.ui.button(
        label="📂 成果物プレビュー", style=discord.ButtonStyle.secondary, custom_id="btn_files", row=0
    )
    async def list_files_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.project_dir.exists():
            await interaction.response.send_message("プロジェクトディレクトリが存在しません．", ephemeral=True)
            return

        def _scan_files() -> list[str]:
            lines = []
            for root, dirs, files in os.walk(self.project_dir):
                dirs[:] = [d for d in dirs if d not in IGNORE_PATTERNS]
                rel = Path(root).relative_to(self.project_dir)
                prefix = "" if str(rel) == "." else f"{rel}/"
                for f in files:
                    lines.append(f"• {prefix}{f}")
            return lines

        lines = await asyncio.to_thread(_scan_files)
        msg = "**現在のワークスペースファイル:**\n" + "\n".join(lines[:30]) if lines else "(空のワークスペース)"
        await interaction.response.send_message(msg[:MAX_DISCORD_MSG_LEN], ephemeral=True)

    @discord.ui.button(
        label="📦 ZIP出力", style=discord.ButtonStyle.secondary, custom_id="btn_zip", row=0
    )
    async def zip_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.project_dir.exists():
            await interaction.response.send_message("プロジェクトディレクトリが存在しません．", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        zip_path = await asyncio.to_thread(package_project_to_zip, self.project_dir, self.session_id)

        def _save_zip_record() -> None:
            with get_db_session() as db:
                session = db.query(GoalSession).filter(GoalSession.id == self.session_id).first()
                if session:
                    session.artifact_zip = str(zip_path)
                    db.commit()

        await asyncio.to_thread(_save_zip_record)

        if zip_path.stat().st_size < MAX_ZIP_BYTES:
            await interaction.followup.send(
                content=f"セッション #{self.session_id} 現時点のスナップショット:",
                file=discord.File(str(zip_path)),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(content="ZIPが25MBを超えているため送信できませんでした．", ephemeral=True)

    @discord.ui.button(
        label="▶️ そのまま作業を再開", style=discord.ButtonStyle.success, custom_id="btn_continue", row=1
    )
    async def continue_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        ok, err_msg, objective = await asyncio.to_thread(_sync_set_session_pending, self.session_id)
        if not ok:
            await interaction.response.send_message(err_msg, ephemeral=True)
            return

        add_session_intervention(
            self.session_id,
            "ユーザーからの指示: 先ほどの報告ではプロジェクトが完全に完成していませんでした。不足している機能の実装、エラーの修正、および完全な動作確認を続行してください。",
        )
        await interaction.response.send_message(
            f"セッション #{self.session_id} の作業を再開し，タスクキューへ再投入しました．", ephemeral=True
        )
        asyncio.create_task(enqueue_agent_with_dashboard(interaction.channel, self.session_id, objective))

    @discord.ui.button(
        label="📝 指示を追加して再開", style=discord.ButtonStyle.primary, custom_id="btn_continue_with_prompt", row=1
    )
    async def continue_with_prompt_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        status, _, _ = await asyncio.to_thread(_sync_get_session_info, self.session_id)
        if not status:
            await interaction.response.send_message("セッションが見つかりません．", ephemeral=True)
            return
        if status in ["running", "pending"]:
            await interaction.response.send_message("このセッションは現在実行中または待機中です．", ephemeral=True)
            return
        await interaction.response.send_modal(ContinuePromptModal(self.session_id))


async def enqueue_agent_with_dashboard(
    channel: discord.abc.Messageable,
    session_id: int,
    objective: str,
    reply_msg_id: int | None = None,
    user_id: int | None = None,
) -> None:
    _, _, project_dir_str = await asyncio.to_thread(_sync_get_session_info, session_id)
    if not project_dir_str:
        project_dir_str = str(get_projects_dir() / f"session_{session_id}")

    project_dir = Path(project_dir_str)
    view = AgentDashboardView(session_id, project_dir)
    WIDTH_SPACER = "ㅤ" * 45

    embed = discord.Embed(
        title=f"Project Runner - Session #{session_id}",
        description=f"**目標**: {objective}\n{WIDTH_SPACER}",
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
            value=f"**キュー待機中 (Waiting in Queue: #{position})**",
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
        now = time.monotonic()
        # 完了以外の高頻度な更新はスリープせずに間引く（エージェントの足を引っ張らない＋429回避）
        if now - last_edit_time < DASHBOARD_EDIT_INTERVAL and "Completed" not in status:
            return

        embed.color = discord.Color.gold() if "Thinking" in status else discord.Color.blue()
        embed.set_field_at(
            0,
            name="進捗",
            value=f"**Step [{step}/{max_steps}]** - `{status}`",
            inline=False,
        )

        action_parts = []
        if tool_name:
            arg_str = json.dumps(tool_args, ensure_ascii=False) if tool_args else ""
            if len(arg_str) > 150:
                arg_str = arg_str[:150] + "..."
            action_parts.append(f"**ツール**: `{tool_name}`\n```json\n{arg_str}\n```")

        if tool_output:
            out_lines = tool_output.splitlines()
            if len(out_lines) > 6:
                out_preview = "\n".join(out_lines[:5]) + "\n...(省略)..."
            elif len(tool_output) > 300:
                out_preview = tool_output[:300] + "..."
            else:
                padding_lines = "\n" * max(0, 3 - len(out_lines))
                out_preview = tool_output + padding_lines
            action_parts.append(f"**出力**:\n```text\n{out_preview}\n```")

        if not action_parts:
            action_parts.append("**思考中...**\n```text\n(Waiting for tool dispatch...)\n\n\n```")

        embed.set_field_at(
            1, name="最新アクション", value="\n".join(action_parts), inline=False
        )

        try:
            last_edit_time = time.monotonic()
            await dashboard_msg.edit(embed=embed, view=view)
        except Exception:
            pass

    async def human_ask_listener(question: str) -> str:
        mention = f"<@{user_id}> " if user_id else ""
        prompt_text = (
            f"**[エージェントからの質問] (Session #{session_id})**\n"
            f"> {question}\n\n"
            f"{mention}返答するにはこのチャンネルで `!tell <回答>` または直接返信してください．"
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
            embed.set_field_at(0, name="進捗", value="**完了 (Completed)**", inline=False)
            await dashboard_msg.edit(embed=embed, view=view)

            final_report = f"**[Session #{session_id} 成果報告]**\n{result}"
            await send_chunked_message(channel, final_report)

        except asyncio.CancelledError:
            embed.color = discord.Color.red()
            embed.set_field_at(0, name="進捗", value="**中断されました (Cancelled)**", inline=False)
            try:
                await dashboard_msg.edit(embed=embed, view=view)
            except Exception:
                pass
            await channel.send(f"**[Session #{session_id}]** ユーザーの指示によりタスクを中断しました．")
            raise
        finally:
            pending_human_questions.pop(session_id, None)

    await agent_task_queue.submit(
        session_id=session_id,
        objective=objective,
        coro_factory=_job_coroutine,
        on_queued=notify_queued,
    )
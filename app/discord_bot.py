import asyncio
import json
import os
import re
import time
import zipfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import discord
from discord.ext import commands

from .agent_runner import add_session_intervention, run_agent_loop
from .db_models import Activity, GoalSession
from .db_system import SessionLocal, init_db
from .llama_acc import ask_llm
from .secret import get_projects_dir, load_token
from .tools import IGNORE_PATTERNS

JST = timezone(timedelta(hours=9))

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="./", intents=intents)

active_tasks: dict[int, asyncio.Task] = {}
# ask_human で回答待ちをしているセッション (session_id -> asyncio.Future)
pending_human_questions: dict[int, asyncio.Future] = {}


def package_project_to_zip(project_dir: Path, session_id: int) -> Path:
    zip_path = project_dir.parent / f"session_{session_id}_archive.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(project_dir):
            dirs[:] = [d for d in dirs if d not in IGNORE_PATTERNS]
            for file in files:
                if any(file.endswith(ext) for ext in [".pyc", ".tmp", ".log"]):
                    continue
                file_path = Path(root) / file
                arcname = file_path.relative_to(project_dir)
                zipf.write(file_path, arcname)
    return zip_path


# --- Discord インタラクティブ UI (ボタン付き View) ---
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
    ):
        task = active_tasks.get(self.session_id)
        if task and not task.done():
            task.cancel()
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
    ):
        if not self.project_dir.exists():
            await interaction.response.send_message(
                "プロジェクトディレクトリが存在しません．", ephemeral=True
            )
            return

        lines = []
        for root, dirs, files in os.walk(self.project_dir):
            dirs[:] = [d for d in dirs if d not in IGNORE_PATTERNS]
            rel = Path(root).relative_to(self.project_dir)
            pfx = "" if str(rel) == "." else f"{rel}/"
            for f in files:
                lines.append(f"• {pfx}{f}")

        msg = (
            "**現在のワークスペースファイル:**\n" + "\n".join(lines[:30])
            if lines
            else "(空のワークスペース)"
        )
        await interaction.response.send_message(msg[:1900], ephemeral=True)

    @discord.ui.button(
        label="📦 今すぐZIP出力",
        style=discord.ButtonStyle.primary,
        custom_id="btn_zip",
    )
    async def zip_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not self.project_dir.exists():
            await interaction.response.send_message(
                "プロジェクトディレクトリが存在しません．", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        zip_path = package_project_to_zip(self.project_dir, self.session_id)
        if zip_path.stat().st_size < 25 * 1024 * 1024:
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


# --- ダッシュボード連携エージェント実行器 ---
async def run_agent_with_dashboard(
    channel: discord.abc.Messageable,
    session_id: int,
    objective: str,
    reply_msg_id: int | None = None,
):
    project_dir = get_projects_dir() / f"session_{session_id}"
    view = AgentDashboardView(session_id, project_dir)

    embed = discord.Embed(
        title=f"🚀 Project Runner - Session #{session_id}",
        description=f"**目標**: {objective}",
        color=discord.Color.blue(),
        timestamp=datetime.now(UTC),
    )
    embed.add_field(name="進捗", value="初期化中...", inline=False)
    embed.add_field(name="最新アクション", value="準備中", inline=False)

    dashboard_msg = await channel.send(embed=embed, view=view)
    last_edit_time = 0.0

    async def update_dashboard(
        step: int = 1,
        max_steps: int = 40,
        status: str = "実行中",
        thought: str | None = None,
        tool_name: str | None = None,
        tool_args: dict | None = None,
        tool_output: str | None = None,
    ):
        nonlocal last_edit_time
        # Discord API レートリミット対策 (最低 1.2 秒間隔)
        now = time.time()
        if now - last_edit_time < 1.2:
            await asyncio.sleep(1.2 - (now - last_edit_time))

        embed.color = discord.Color.gold() if "LLM" in status else discord.Color.blue()
        embed.set_field_at(
            0,
            name="進捗",
            value=f"**Step [{step}/{max_steps}]** - `{status}`",
            inline=False,
        )

        action_text = ""
        if tool_name:
            arg_str = json.dumps(tool_args, ensure_ascii=False) if tool_args else ""
            if len(arg_str) > 100:
                arg_str = arg_str[:100] + "..."
            action_text += f"⚙️ **ツール**: `{tool_name}`\n```json\n{arg_str}\n```\n"
        if tool_output:
            out_preview = tool_output[:300] + "..." if len(tool_output) > 300 else tool_output
            action_text += f"📥 **出力**:\n```text\n{out_preview}\n```"

        if action_text:
            embed.set_field_at(1, name="最新アクション", value=action_text, inline=False)

        try:
            await dashboard_msg.edit(embed=embed, view=view)
            last_edit_time = time.time()
        except Exception:
            pass

    async def human_ask_listener(question: str) -> str:
        # ask_human が呼ばれた際に Discord スレッドへ質問を投稿し返答を待つ
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
            # 回答を最大 10 分間待機
            answer = await asyncio.wait_for(fut, timeout=600)
            return answer
        except TimeoutError:
            return "No human response received within timeout. Proceed with best guess."
        finally:
            pending_human_questions.pop(session_id, None)

    try:
        result = await run_agent_loop(
            session_id,
            dashboard_callback=update_dashboard,
            human_ask_callback=human_ask_listener,
        )

        embed.color = discord.Color.green()
        embed.set_field_at(0, name="進捗", value="✅ **完了 (Completed)**", inline=False)
        await dashboard_msg.edit(embed=embed, view=view)

        # 完了レポート送信
        final_report = f"**[Session #{session_id} 成果報告]**\n{result}"
        for i in range(0, len(final_report), 1900):
            await channel.send(final_report[i : i + 1900])

        # 成果物 ZIP アーカイブ送信
        db = SessionLocal()
        try:
            sess = db.query(GoalSession).filter(GoalSession.id == session_id).first()
            if sess and sess.project_dir and Path(sess.project_dir).exists():
                zip_path = package_project_to_zip(Path(sess.project_dir), session_id)
                sess.artifact_zip = str(zip_path)
                db.commit()

                if zip_path.stat().st_size < 25 * 1024 * 1024:
                    await channel.send(
                        content=f"📦 **[成果物ZIP]** セッション #{session_id} の完成パッケージです．",
                        file=discord.File(str(zip_path)),
                    )
        finally:
            db.close()

    except asyncio.CancelledError:
        embed.color = discord.Color.red()
        embed.set_field_at(0, name="進捗", value="🛑 **中断されました (Cancelled)**", inline=False)
        try:
            await dashboard_msg.edit(embed=embed, view=view)
        except Exception:
            pass
        await channel.send(f"🛑 **[Session #{session_id}]** ユーザーの指示によりタスクを中断しました．")

    finally:
        active_tasks.pop(session_id, None)


@bot.event
async def on_ready():
    init_db()
    print(f"Logged in as {bot.user.name} ({bot.user.id})")

    db = SessionLocal()
    try:
        unfinished = (
            db.query(GoalSession)
            .filter(GoalSession.status.in_(["pending", "running"]))
            .all()
        )
        for sess in unfinished:
            if not sess.channel_id:
                continue
            channel = bot.get_channel(sess.channel_id)
            if not channel:
                try:
                    channel = await bot.fetch_channel(sess.channel_id)
                except Exception:
                    continue

            if isinstance(channel, discord.Thread) and channel.archived:
                try:
                    await channel.edit(archived=False)
                except Exception:
                    pass

            await channel.send(f"🔄 **[システム再起動]** セッション #{sess.id} を再開します．")
            task = asyncio.create_task(
                run_agent_with_dashboard(
                    channel, sess.id, sess.objective, reply_msg_id=sess.message_id
                )
            )
            active_tasks[sess.id] = task
    finally:
        db.close()


@bot.command(name="start")
async def start_task(ctx, *, objective: str):
    db = SessionLocal()
    try:
        session = GoalSession(
            title=objective[:30],
            objective=objective,
            status="pending",
            channel_id=ctx.channel.id,
            message_id=ctx.message.id,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        session_id = session.id

        proj_dir = get_projects_dir() / f"session_{session_id}"
        proj_dir.mkdir(parents=True, exist_ok=True)
        session.project_dir = str(proj_dir)
        db.commit()
    finally:
        db.close()

    task = asyncio.create_task(
        run_agent_with_dashboard(
            ctx.channel, session_id, objective, reply_msg_id=ctx.message.id
        )
    )
    active_tasks[session_id] = task


@bot.command(name="stop")
async def stop_task(ctx, session_id: int | None = None):
    target_id = session_id or (max(active_tasks.keys()) if active_tasks else None)
    if not target_id:
        await ctx.reply("現在実行中のタスクはありません．")
        return

    task = active_tasks.get(target_id)
    if task and not task.done():
        task.cancel()
        await ctx.reply(f"セッション #{target_id} に中断シグナルを送信しました．")
    else:
        await ctx.reply(f"セッション #{target_id} は実行中ではありません．")


@bot.command(name="tell")
async def tell_agent(ctx, *, message: str):
    """ask_human に対する回答コマンド"""
    if pending_human_questions:
        target_id = max(pending_human_questions.keys())
        fut = pending_human_questions[target_id]
        if not fut.done():
            fut.set_result(message)
            await ctx.reply(f"セッション #{target_id} のエージェントへ回答を送信しました．")
            return
    await ctx.reply("現在質問を待機しているエージェントはいません．")


@bot.command(name="inject")
async def inject_task(ctx, *, message: str):
    """実行中エージェントの次ステップへ割り込み指示を送信"""
    if not active_tasks:
        await ctx.reply("実行中のタスクがありません．")
        return
    target_id = max(active_tasks.keys())
    add_session_intervention(target_id, message)
    await ctx.reply(
        f"セッション #{target_id} の次ステップへ指示を割り込ませました: `{message}`"
    )


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    # エージェントが ask_human で待機中のチャンネル発言を自動で回答として拾う
    if pending_human_questions:
        target_id = max(pending_human_questions.keys())
        fut = pending_human_questions[target_id]
        if not fut.done() and not message.content.startswith("./"):
            fut.set_result(message.content)
            await message.reply(f"セッション #{target_id} へ回答を反映しました．")
            return

    if bot.user.mentioned_in(message) and not message.mention_everyone:
        clean_prompt = re.sub(r"<@!?\d+>", "", message.content).strip()
        if not clean_prompt:
            await message.reply("Prompt not found.")
            return

        async with message.channel.typing():
            response_text = await ask_llm(clean_prompt)

        for i in range(0, len(response_text), 1900):
            chunk = response_text[i : i + 1900]
            if i == 0:
                await message.reply(chunk)
            else:
                await message.channel.send(chunk)
        return

    await bot.process_commands(message)


@bot.command()
async def project_status(ctx):
    db = SessionLocal()
    try:
        latest = db.query(GoalSession).order_by(GoalSession.id.desc()).first()
        if not latest:
            await ctx.reply("登録されているセッション情報はありません．")
            return

        acts = (
            db.query(Activity)
            .filter(Activity.session_id == latest.id)
            .order_by(Activity.id.desc())
            .limit(3)
            .all()
        )

        lines = [
            f"**[Session #{latest.id}] {latest.title}**",
            f"• **Status**: `{latest.status}`",
            f"• **Objective**: {latest.objective}",
        ]
        if latest.project_dir:
            lines.append(f"• **Workspace**: `{latest.project_dir}`")
        if latest.updated_at:
            dt = latest.updated_at.replace(tzinfo=UTC).astimezone(JST)
            lines.append(f"• **Updated**: {dt.strftime('%Y-%m-%d %H:%M:%S')}")

        if acts:
            lines.append("\n**Recent Activities:**")
            for act in reversed(acts):
                t_str = (
                    act.datetime.replace(tzinfo=UTC)
                    .astimezone(JST)
                    .strftime("%H:%M:%S")
                    if act.datetime
                    else "--:--:--"
                )
                lines.append(f"- `[{t_str}]` **{act.work}**: {act.result}")

        await ctx.reply("\n".join(lines))
    finally:
        db.close()


if __name__ == "__main__":
    bot.run(load_token())

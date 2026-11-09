import re
from datetime import UTC, timedelta, timezone

import discord
from discord.ext import commands

from .agent_runner import add_session_intervention
from .db_models import Activity, GoalSession
from .db_system import get_db_session, init_db
from .discord_ui import (
    enqueue_agent_with_dashboard,
    pending_human_questions,
    send_chunked_message,
)
from .llama_acc import ask_llm
from .secrets import get_projects_dir, load_token
from .task_queue import agent_task_queue

JST = timezone(timedelta(hours=9))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="./", intents=intents)


async def _resolve_channel(channel_id: int) -> discord.abc.Messageable | None:
    channel = bot.get_channel(channel_id)
    if not channel:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            return None

    if isinstance(channel, discord.Thread) and channel.archived:
        try:
            await channel.edit(archived=False)
        except Exception:
            pass
    return channel


@bot.event
async def on_ready() -> None:
    init_db()
    agent_task_queue.start_workers()
    print(f"Logged in as {bot.user.name} ({bot.user.id})")

    with get_db_session() as db:
        unfinished_sessions = (
            db.query(GoalSession)
            .filter(GoalSession.status.in_(["pending", "running"]))
            .all()
        )
        for sess in unfinished_sessions:
            if not sess.channel_id:
                continue
            channel = await _resolve_channel(sess.channel_id)
            if not channel:
                continue

            await channel.send(
                f"🔄 **[システム再起動]** セッション #{sess.id} をタスクキューへ再投入します．"
            )
            await enqueue_agent_with_dashboard(
                channel, sess.id, sess.objective, reply_msg_id=sess.message_id
            )


@bot.command(name="start")
async def start_task(ctx: commands.Context, *, objective: str) -> None:
    """新規自律エージェントセッションを作成し，非同期タスクキューへ投入する．"""
    with get_db_session() as db:
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

    await enqueue_agent_with_dashboard(
        ctx.channel, session_id, objective, reply_msg_id=ctx.message.id
    )


@bot.command(name="stop")
async def stop_task(ctx: commands.Context, session_id: int | None = None) -> None:
    """実行中またはキュー待機中のセッションを中断する．"""
    active_ids = agent_task_queue.get_active_session_ids()
    target_id = session_id or (max(active_ids) if active_ids else None)
    if not target_id:
        await ctx.reply("現在実行中または待機中のタスクはありません．")
        return

    if agent_task_queue.cancel_session(target_id):
        await ctx.reply(f"セッション #{target_id} に中断シグナルを送信しました．")
    else:
        await ctx.reply(f"セッション #{target_id} は実行中ではありません．")


@bot.command(name="queue")
async def queue_status(ctx: commands.Context) -> None:
    """タスクキューの稼働状況と待機リストを表示する．"""
    summary = agent_task_queue.get_status_summary()
    running = summary["running_sessions"]
    queued = summary["queued_sessions"]

    lines = [
        "**📊 タスクキュー稼働状況**",
        f"• **最大同時実行ワーカー数**: `{summary['max_workers']}`",
        f"• **現在実行中のセッション**: {running if running else 'なし'}",
    ]
    if queued:
        lines.append("\n**⏳ キュー待機中のセッション:**")
        for idx, item in enumerate(queued, 1):
            lines.append(
                f"{idx}. `Session #{item['session_id']}` - {item['objective']}..."
            )
    else:
        lines.append("• **待機中のセッション**: なし")

    await ctx.reply("\n".join(lines))


@bot.command(name="tell")
async def tell_agent(ctx: commands.Context, *, message: str) -> None:
    """ask_human で回答待機中のエージェントへ返答を送る．"""
    if pending_human_questions:
        target_id = max(pending_human_questions.keys())
        fut = pending_human_questions[target_id]
        if not fut.done():
            fut.set_result(message)
            await ctx.reply(
                f"セッション #{target_id} のエージェントへ回答を送信しました．"
            )
            return
    await ctx.reply("現在質問を待機しているエージェントはいません．")


@bot.command(name="inject")
async def inject_task(ctx: commands.Context, *, message: str) -> None:
    """実行中エージェントの次ステップへ割り込み指示を送信する．"""
    active_ids = agent_task_queue.get_active_session_ids()
    if not active_ids:
        await ctx.reply("実行中のタスクがありません．")
        return

    target_id = max(active_ids)
    add_session_intervention(target_id, message)
    await ctx.reply(
        f"セッション #{target_id} の次ステップへ指示を割り込ませました: `{message}`"
    )


@bot.command(name="project_status")
async def project_status(ctx: commands.Context) -> None:
    """最新セッションのステータスと直近のアクティビティを表示する．"""
    with get_db_session() as db:
        latest = db.query(GoalSession).order_by(GoalSession.id.desc()).first()
        if not latest:
            await ctx.reply("登録されているセッション情報はありません．")
            return

        activities = (
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

        if activities:
            lines.append("\n**Recent Activities:**")
            for act in reversed(activities):
                t_str = (
                    act.datetime.replace(tzinfo=UTC)
                    .astimezone(JST)
                    .strftime("%H:%M:%S")
                    if act.datetime
                    else "--:--:--"
                )
                lines.append(f"- `[{t_str}]` **{act.work}**: {act.result}")

        await ctx.reply("\n".join(lines))


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author == bot.user:
        return

    if pending_human_questions and not message.content.startswith("./"):
        target_id = max(pending_human_questions.keys())
        fut = pending_human_questions[target_id]
        if not fut.done():
            fut.set_result(message.content)
            await message.reply(f"セッション #{target_id} へ回答を反映しました．")
            return

    if bot.user and bot.user.mentioned_in(message) and not message.mention_everyone:
        clean_prompt = re.sub(r"<@!?\d+>", "", message.content).strip()
        if not clean_prompt:
            await message.reply("Prompt not found.")
            return

        async with message.channel.typing():
            response_text = await ask_llm(clean_prompt)

        await send_chunked_message(
            message.channel, response_text, reply_target=message
        )
        return

    await bot.process_commands(message)


if __name__ == "__main__":
    bot.run(load_token())
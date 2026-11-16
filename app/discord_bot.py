import asyncio
import re
import shutil
from datetime import UTC, timedelta, timezone

import discord
from discord.ext import commands

from .agent_runner import add_session_intervention
from .db_models import Activity, GoalSession
from .db_system import SessionLocal, get_db_session, init_db
from .discord_ui import (
    enqueue_agent_with_dashboard,
    pending_human_questions,
    send_chunked_message,
)
from .llama_acc import ask_llm
from .secrets import get_projects_dir, get_workspace_dir, load_token
from .task_queue import agent_task_queue

JST = timezone(timedelta(hours=9))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


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
                f"**[システム再起動]** セッション #{sess.id} をタスクキューへ再投入します．"
            )
            await enqueue_agent_with_dashboard(
                channel, sess.id, sess.objective, reply_msg_id=sess.message_id
            )


@bot.command(name="run")
async def start_task(ctx, *, objective: str):
    slug_prompt = (
        "次のタスク目標から、プロジェクトを保存するための適切なディレクトリ名を1つだけ出力してください。\n"
        "条件: 英小文字、数字、アンダースコア(_)のみを使用し、空白や他の記号は含めないこと。説明文は一切不要です。\n"
        "※ただし、タスクが「全体を検索する」「既存の全プロジェクトを調べる」といった内容で、特定のプロジェクトに隔離しない方が良い場合は、特殊キーワード 'workspace_root' と出力してください。\n"
        f"例: shogi_gui, web_server, react_todo_app, workspace_root\n"
        f"タスク目標: {objective}"
    )
    
    async with ctx.typing():
        slug_result = await ask_llm(slug_prompt)
        clean_slug = re.sub(r'[^a-z0-9_]', '', slug_result.strip().lower())
        
        if not clean_slug:
            clean_slug = "default_project"

    db = SessionLocal()
    try:
        session = GoalSession(
            title=objective[:30],
            objective=objective,
            status="pending",
            channel_id=ctx.channel.id,
            message_id=ctx.message.id,
            project_slug=clean_slug,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        session_id = session.id

        if clean_slug == "workspaceroot" or clean_slug == "workspace_root":
            proj_dir = get_projects_dir()
            display_name = "全体のワークスペース"
        else:
            proj_dir = get_projects_dir() / clean_slug
            proj_dir.mkdir(parents=True, exist_ok=True)
            display_name = clean_slug

        session.project_dir = str(proj_dir)
        db.commit()
    finally:
        db.close()

    await ctx.reply(f"プロジェクトディレクトリ `{display_name}` を作成/選択し、セッション #{session_id} を開始します！")

    await enqueue_agent_with_dashboard(
        ctx.channel, session_id, objective, reply_msg_id=ctx.message.id, user_id=ctx.author.id
    )


@bot.command(name="stop")
async def stop_task(ctx: commands.Context, session_id: int | None = None) -> None:
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
    summary = agent_task_queue.get_status_summary()
    running = summary["running_sessions"]
    queued = summary["queued_sessions"]

    lines = [
        "**タスクキュー稼働状況**",
        f"• **最大同時実行ワーカー数**: `{summary['max_workers']}`",
        f"• **現在実行中のセッション**: {running if running else 'なし'}",
    ]
    if queued:
        lines.append("\n**キュー待機中のセッション:**")
        for idx, item in enumerate(queued, 1):
            lines.append(
                f"{idx}. `Session #{item['session_id']}` - {item['objective']}..."
            )
    else:
        lines.append("• **待機中のセッション**: なし")

    await ctx.reply("\n".join(lines))


@bot.command(name="tell")
async def tell_agent(ctx: commands.Context, *, message: str) -> None:
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
    active_ids = agent_task_queue.get_active_session_ids()
    if not active_ids:
        await ctx.reply("実行中のタスクがありません．")
        return

    target_id = max(active_ids)
    add_session_intervention(target_id, message)
    await ctx.reply(
        f"セッション #{target_id} の次ステップへ指示を割り込ませました: `{message}`"
    )

@bot.command(name="start_from")
async def start_from_task(ctx: commands.Context, source_dir_name: str, *, objective: str) -> None:
    workspace_dir = get_workspace_dir()
    source_path = workspace_dir / source_dir_name
    
    if not source_path.exists() or not source_path.is_dir():
        await ctx.reply(f"指定されたフォルダ `{source_dir_name}` がワークスペース内に見つかりません。")
        return

    slug_prompt = (
        "次のタスク目標から、プロジェクトを保存するための適切なディレクトリ名を1つだけ出力してください。\n"
        "条件: 英小文字、数字、アンダースコア(_)のみを使用し、空白や他の記号は含めないこと。説明文は一切不要です。\n"
        "※ただし、タスクが「全体を検索する」「既存の全プロジェクトを調べる」といった内容で、特定のプロジェクトに隔離しない方が良い場合は、特殊キーワード 'workspace_root' と出力してください。\n"
        f"例: shogi_gui, web_server, react_todo_app, workspace_root\n"
        f"タスク目標: {objective}"
    )

    async with ctx.typing():
        slug_result = await ask_llm(slug_prompt)
        clean_slug = re.sub(r'[^a-z0-9_]', '', slug_result.strip().lower())
        if not clean_slug:
            clean_slug = "default_project"

    db = SessionLocal()
    try:
        enhanced_objective = (
            f"提供された既存のコード資産をベースに開発を行ってください。\n"
            f"まずはファイル一覧や中身を確認して現状を把握し、次の目標を達成してください: {objective}"
        )

        session = GoalSession(
            title=objective[:30],
            objective=enhanced_objective,
            status="pending",
            channel_id=ctx.channel.id,
            message_id=ctx.message.id,
            project_slug=clean_slug,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        session_id = session.id

        if clean_slug == "workspaceroot" or clean_slug == "workspace_root":
            proj_dir = get_projects_dir()
            display_name = "全体のワークスペース"
        else:
            proj_dir = get_projects_dir() / clean_slug
            if not proj_dir.exists():
                shutil.copytree(source_path, proj_dir)
            else:
                shutil.copytree(source_path, proj_dir, dirs_exist_ok=True)
            display_name = clean_slug
            
        session.project_dir = str(proj_dir)
        db.commit()
    finally:
        db.close()

    await ctx.reply(f"`{source_dir_name}` のコード資産をコピーして、プロジェクトディレクトリ `{display_name}` にてセッション #{session_id} を開始します！")
    
    await enqueue_agent_with_dashboard(
        ctx.channel, session_id, enhanced_objective, reply_msg_id=ctx.message.id, user_id=ctx.author.id
    )


@bot.command(name="project_status")
async def project_status(ctx: commands.Context) -> None:
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
            # ▼ 修正：意図判定と受領メッセージを同時に作らせる JSON プロンプト
            intent_prompt = (
                "以下のユーザーの発言が、「単なる雑談や簡単な質問」か、"
                "それとも「コードの作成、ファイルの編集、コマンドの実行など、"
                "ワークスペース上での何らかの作業依頼」かを判定してください。\n\n"
                "必ず以下の JSON 形式で出力してください。Markdown のコードブロック(```json ... ```)は不要です。\n"
                "{\n"
                '  "intent": "TASK または CHAT",\n'
                '  "reply": "TASKの場合: 作業を引き受ける際の、頼もしいアシスタントとしての自然な日本語の返事（例: 了解しました！〇〇の機能ですね。さっそくプロジェクトを立ち上げて開発を開始します！）。CHATの場合: 空文字"\n'
                "}\n\n"
                f"ユーザーの発言: {clean_prompt}"
            )
            
            intent_result = await ask_llm(intent_prompt)
            
            # JSON パースの試行
            intent = "CHAT"
            reply_text = "作業を開始します。"
            try:
                import json
                # Markdown ブロックがあれば除去
                clean_json_str = re.sub(r"```(?:json)?\s*(.*?)\s*```", r"\1", intent_result, flags=re.DOTALL).strip()
                data = json.loads(clean_json_str)
                intent = data.get("intent", "CHAT").strip().upper()
                reply_text = data.get("reply", "了解しました。作業用のエージェント環境を準備しますね！")
            except Exception:
                # パース失敗時のフォールバック
                if "TASK" in intent_result.upper():
                    intent = "TASK"

            if "TASK" in intent:
                # ▼ 修正：機械的な定型文ではなく、LLMが考えた自然な返事を使う
                await message.reply(f"🚀 {reply_text}")
                
                slug_prompt = (
                    "次のタスク目標から、プロジェクトを保存するための適切なディレクトリ名を1つだけ出力してください。\n"
                    "条件: 英小文字、数字、アンダースコア(_)のみを使用し、空白や他の記号は含めないこと。説明文は一切不要です。\n"
                    "※ただし、タスクが「全体を検索する」「既存の全プロジェクトを調べる」といった内容で、特定のプロジェクトに隔離しない方が良い場合は、特殊キーワード 'workspace_root' と出力してください。\n"
                    f"例: shogi_gui, web_server, react_todo_app, workspace_root\n"
                    f"タスク目標: {clean_prompt}"
                )
                
                slug_result = await ask_llm(slug_prompt)
                clean_slug = re.sub(r'[^a-z0-9_]', '', slug_result.strip().lower())
                
                if not clean_slug:
                    clean_slug = "default_project"

                db = SessionLocal()
                try:
                    session = GoalSession(
                        title=clean_prompt[:30],
                        objective=clean_prompt,
                        status="pending",
                        channel_id=message.channel.id,
                        message_id=message.id,
                        project_slug=clean_slug,
                    )
                    db.add(session)
                    db.commit()
                    db.refresh(session)
                    session_id = session.id

                    if clean_slug == "workspaceroot" or clean_slug == "workspace_root":
                        proj_dir = get_projects_dir()
                        display_name = "全体のワークスペース"
                    else:
                        proj_dir = get_projects_dir() / clean_slug
                        proj_dir.mkdir(parents=True, exist_ok=True)
                        display_name = clean_slug
                        
                    session.project_dir = str(proj_dir)
                    db.commit()
                finally:
                    db.close()

                await message.channel.send(f"プロジェクトディレクトリ `{display_name}` を作成/選択し、セッション #{session_id} を開始します！")

                await enqueue_agent_with_dashboard(
                    message.channel, session_id, clean_prompt, reply_msg_id=message.id, user_id=message.author.id
                )
                
            else:
                response_text = await ask_llm(clean_prompt)
                await send_chunked_message(
                    message.channel, response_text, reply_target=message
                )
        return

    await bot.process_commands(message)


if __name__ == "__main__":
    bot.run(load_token())
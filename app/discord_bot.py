import asyncio
import json
import re
import shutil
import traceback
from datetime import UTC, timedelta, timezone
from pathlib import Path

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
    async with ctx.typing():
        target_session_id = None
        
        # ▼ 1. フォーラム(Thread)かテキストチャンネルかで「既存セッション再利用」を判定する
        with get_db_session() as db:
            if isinstance(ctx.channel, discord.Thread):
                latest_session = db.query(GoalSession).filter(GoalSession.channel_id == ctx.channel.id).order_by(GoalSession.id.desc()).first()
                if latest_session:
                    target_session_id = latest_session.id
            else:
                recent_sessions = db.query(GoalSession).filter(GoalSession.channel_id == ctx.channel.id).order_by(GoalSession.id.desc()).limit(5).all()
                if recent_sessions:
                    session_list_str = "\n".join([f"ID: {s.id}, 目標: {s.objective}" for s in recent_sessions])
                    match_prompt = (
                        "以下は同じチャンネルで過去に行われたAIエージェントの作業セッションのリストです。\n"
                        f"{session_list_str}\n\n"
                        "今回のユーザーの新しいタスク目標は以下です。\n"
                        f"「{objective}」\n\n"
                        "今回の目標が、過去のいずれかのセッションの「続き」や「修正」「機能追加」である場合は、該当するセッションのIDを出力してください。\n"
                        "全く新しいプロジェクトや無関係な話題である場合は、IDとして -1 を出力してください。\n"
                        "必ず以下のJSON形式で出力し、説明は一切不要です。Markdownブロックは含めないでください。\n"
                        "{\n"
                        '  "session_id": 該当IDまたは-1\n'
                        "}"
                    )
                    match_result = await ask_llm(match_prompt)
                    try:
                        clean_match_json = re.sub(r"```(?:json)?\s*(.*?)\s*```", r"\1", match_result, flags=re.DOTALL).strip()
                        match_data = json.loads(clean_match_json)
                        matched_id = int(match_data.get("session_id", -1))
                        if matched_id != -1 and any(s.id == matched_id for s in recent_sessions):
                            target_session_id = matched_id
                    except Exception:
                        pass

        # ▼ 2. 関連セッションが見つかった場合（続きを実行）
        if target_session_id:
            with get_db_session() as db:
                session = db.query(GoalSession).filter(GoalSession.id == target_session_id).first()
                display_name = Path(session.project_dir).name if session.project_dir else f"session_{target_session_id}"
                
                if session.status in ["running", "pending"]:
                    add_session_intervention(target_session_id, f"ユーザーからの追加指示: {objective}")
                    await ctx.reply(f"関連プロジェクト `{display_name}` (Session #{target_session_id}) は現在実行中のため、次ステップへ指示を割り込ませました！")
                else:
                    session.status = "pending"
                    objective_for_run = session.objective
                    db.commit()
                    add_session_intervention(target_session_id, f"ユーザーからの追加指示: {objective}\nこの指示に従って作業を続行し、完全に動作することを確認してから完了してください。")
                    
                    await ctx.reply(f"関連プロジェクト `{display_name}` を検知しました。既存のセッション #{target_session_id} を再開します！")
                    asyncio.create_task(
                        enqueue_agent_with_dashboard(
                            ctx.channel, target_session_id, objective_for_run, reply_msg_id=ctx.message.id, user_id=ctx.author.id
                        )
                    )
            return

        # ▼ 3. 関連セッションが無い場合（完全新規で作成）
        slug_prompt = (
            "次のタスク目標から、プロジェクトを保存するための適切なディレクトリ名を1つだけ出力してください。\n"
            "条件: 英小文字、数字、アンダースコア(_)のみを使用し、空白や他の記号は含めないこと。説明文は一切不要です。\n"
            "※ただし、タスクが「全体を検索する」「既存の全プロジェクトを調べる」といった内容で、特定のプロジェクトに隔離しない方が良い場合は、特殊キーワード 'workspace_root' と出力してください。\n"
            f"例: shogi_gui, web_server, react_todo_app, workspace_root\n"
            f"タスク目標: {objective}"
        )
        
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
                is_forum=isinstance(ctx.channel, discord.Thread),
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
            try:
                # ▼ 1. チャット履歴の取得（直近10件）
                history_text = ""
                try:
                    hist_msgs = [m async for m in message.channel.history(limit=10, before=message)]
                    hist_msgs.reverse()  # 古い順に並び替え
                    lines = []
                    for m in hist_msgs:
                        author_name = "Bot" if m.author == bot.user else m.author.display_name
                        text = m.clean_content
                        # Embed（ダッシュボード等）の内容も拾う
                        if not text and m.embeds:
                            text = (m.embeds[0].title or "") + " " + (m.embeds[0].description or "")
                        
                        text = text.strip().replace("\n", " ")
                        if not text:
                            continue
                            
                        # 長すぎるログは切り詰め
                        if len(text) > 400:
                            text = text[:400] + "..."
                        lines.append(f"[{author_name}]: {text}")
                    history_text = "\n".join(lines)
                except Exception:
                    pass
                
                context_str = f"【直近のチャット履歴】\n{history_text}\n\n" if history_text else ""

                # ▼ 2. 履歴を踏まえた意図判定プロンプト
                intent_prompt = (
                    "以下のユーザーの発言が、「単なる雑談や簡単な質問」か、"
                    "それとも「コードの作成、ファイルの編集、コマンドの実行など、"
                    "ワークスペース上での何らかの作業依頼」かを判定してください。\n"
                    "直近のチャット履歴も参考にし、もし「続きをやって」「あれを修正して」のような指示があれば、"
                    "チャット履歴からその作業内容を推測して作業依頼（TASK）とみなしてください。\n\n"
                    "必ず以下の JSON 形式で出力してください。Markdown のコードブロック(```json ... ```)は不要です。\n"
                    "{\n"
                    '  "intent": "TASK または CHAT",\n'
                    '  "reply": "TASKの場合: 作業を引き受ける際の、頼もしいアシスタントとしての自然な日本語の返事（例: 了解しました！〇〇の機能ですね。さっそく開発を開始します！）。CHATの場合: 空文字"\n'
                    "}\n\n"
                    f"{context_str}"
                    f"【今回のユーザーの発言】: {clean_prompt}"
                )
                
                intent_result = await ask_llm(intent_prompt)
                
                # JSON パースの試行
                intent = "CHAT"
                reply_text = "作業を開始します。"
                try:
                    clean_json_str = re.sub(r"```(?:json)?\s*(.*?)\s*```", r"\1", intent_result, flags=re.DOTALL).strip()
                    data = json.loads(clean_json_str)
                    intent = data.get("intent", "CHAT").strip().upper()
                    reply_text = data.get("reply", "了解しました。作業用のエージェント環境を準備しますね！")
                except Exception:
                    if "TASK" in intent_result.upper():
                        intent = "TASK"

                if "TASK" in intent:
                    # ▼ [修正] 空文字の場合は reply を呼ばないことで 400 Bad Request 回避
                    if reply_text and reply_text.strip():
                        try:
                            await message.reply(f"{reply_text}")
                        except Exception:
                            pass
                    
                    # ▼ 3. 抽象的な指示を具体的なタスク目標(Objective)に翻訳する
                    objective_prompt = (
                        "以下の直近のチャット履歴とユーザーの今回の発言から、AIエージェントに渡すための『実行すべき具体的なタスク目標』を1文で生成してください。\n"
                        "「続きをやって」「さっきの件」といった代名詞や抽象的な表現は、履歴を元に具体的なプロジェクト名・機能名・要件に置き換えてください。\n"
                        "※重要1: どんな場合でも【必ず自然な日本語で】出力してください。\n"
                        "※重要2: 履歴内の「default_project」やエラーメッセージなどの、システム側の都合の文言は目標に含めず、ユーザーが本当にやりたい目的を抽出してください。\n"
                        "※今回の発言がすでに十分に具体的であれば、それをそのまま整えて出力してください。\n"
                        "※出力は目標の1文のみとし、説明は一切不要です。\n\n"
                        f"{context_str}"
                        f"【今回のユーザーの発言】: {clean_prompt}"
                    )
                    refined_objective = await ask_llm(objective_prompt)
                    refined_objective = refined_objective.strip()
                    if not refined_objective:
                        refined_objective = clean_prompt

                    # ▼ 4. フォーラム(Thread)かテキストチャンネルかで「既存セッション再利用」を判定する
                    target_session_id = None
                    with get_db_session() as db:
                        if isinstance(message.channel, discord.Thread):
                            # フォーラム投稿（スレッド）の場合: 無条件でそのスレッド内の最新セッションを引き継ぐ
                            latest_session = db.query(GoalSession).filter(GoalSession.channel_id == message.channel.id).order_by(GoalSession.id.desc()).first()
                            if latest_session:
                                target_session_id = latest_session.id
                        else:
                            # 通常のテキストチャンネルの場合: 過去5回のセッションからLLMに関連性を判定させる
                            recent_sessions = db.query(GoalSession).filter(GoalSession.channel_id == message.channel.id).order_by(GoalSession.id.desc()).limit(5).all()
                            if recent_sessions:
                                session_list_str = "\n".join([f"ID: {s.id}, 目標: {s.objective}" for s in recent_sessions])
                                match_prompt = (
                                    "以下は同じチャンネルで過去に行われたAIエージェントの作業セッションのリストです。\n"
                                    f"{session_list_str}\n\n"
                                    "今回のユーザーの新しいタスク目標は以下です。\n"
                                    f"「{refined_objective}」\n\n"
                                    "今回の目標が、過去のいずれかのセッションの「続き」や「修正」「機能追加」である場合は、該当するセッションのIDを出力してください。\n"
                                    "全く新しいプロジェクトや無関係な話題である場合は、IDとして -1 を出力してください。\n"
                                    "必ず以下のJSON形式で出力し、説明は一切不要です。Markdownブロックは含めないでください。\n"
                                    "{\n"
                                    '  "session_id": 該当IDまたは-1\n'
                                    "}"
                                )
                                match_result = await ask_llm(match_prompt)
                                try:
                                    clean_match_json = re.sub(r"```(?:json)?\s*(.*?)\s*```", r"\1", match_result, flags=re.DOTALL).strip()
                                    match_data = json.loads(clean_match_json)
                                    matched_id = int(match_data.get("session_id", -1))
                                    if matched_id != -1 and any(s.id == matched_id for s in recent_sessions):
                                        target_session_id = matched_id
                                except Exception:
                                    pass

                    # ▼ 5. セッションの再開(割り込み) または 新規作成
                    if target_session_id:
                        # 関連セッションが見つかった場合（続きを実行）
                        with get_db_session() as db:
                            session = db.query(GoalSession).filter(GoalSession.id == target_session_id).first()
                            display_name = Path(session.project_dir).name if session.project_dir else f"session_{target_session_id}"
                            
                            if session.status in ["running", "pending"]:
                                # 実行中の場合は割り込み指示を送信
                                add_session_intervention(target_session_id, f"ユーザーからの追加指示: {refined_objective}")
                                await message.channel.send(f"関連プロジェクト `{display_name}` (Session #{target_session_id}) は現在実行中のため、次ステップへ指示を割り込ませました！")
                            else:
                                # 停止中の場合は再起動（Resume）
                                session.status = "pending"
                                objective_for_run = session.objective  # 元の大きな目標は保持する
                                db.commit()
                                add_session_intervention(target_session_id, f"ユーザーからの追加指示: {refined_objective}\nこの指示に従って作業を続行し、完全に動作することを確認してから完了してください。")
                                
                                await message.channel.send(f"関連プロジェクト `{display_name}` を検知しました。既存のセッション #{target_session_id} を再開します！")
                                asyncio.create_task(
                                    enqueue_agent_with_dashboard(
                                        message.channel, target_session_id, objective_for_run, reply_msg_id=message.id, user_id=message.author.id
                                    )
                                )
                    else:
                        # 関連セッションが無い場合（完全新規で作成）
                        slug_prompt = (
                            "次のタスク目標から、プロジェクトを保存するための適切なディレクトリ名を1つだけ出力してください。\n"
                            "条件: 英小文字、数字、アンダースコア(_)のみを使用し、空白や他の記号は含めないこと。説明文は一切不要です。\n"
                            "※重要: もしタスクが「全体を検索する」「既存のプロジェクトを調べる」「workspaceを調査する」など、特定のプロジェクトへの隔離が不要（または不適切）な内容であれば、必ず特殊キーワード 'workspace_root' と出力してください。\n"
                            f"例: shogi_gui, web_server, react_todo_app, workspace_root\n"
                            f"タスク目標: {refined_objective}"
                        )
                        
                        slug_result = await ask_llm(slug_prompt)
                        clean_slug = re.sub(r'[^a-z0-9_]', '', slug_result.strip().lower())
                        
                        if not clean_slug:
                            clean_slug = "default_project"

                        db = SessionLocal()
                        try:
                            session = GoalSession(
                                title=refined_objective[:30],
                                objective=refined_objective,
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
                                proj_dir = get_workspace_dir()
                                display_name = "全体のワークスペース"
                            else:
                                proj_dir = get_projects_dir() / clean_slug
                                proj_dir.mkdir(parents=True, exist_ok=True)
                                display_name = clean_slug
                                
                            session.project_dir = str(proj_dir)
                            db.commit()
                        finally:
                            db.close()

                        await message.channel.send(f"新規プロジェクト `{display_name}` を作成/選択し、セッション #{session_id} を開始します！")
                        await enqueue_agent_with_dashboard(
                            message.channel, session_id, refined_objective, reply_msg_id=message.id, user_id=message.author.id
                        )
                    
                else:
                    # ▼ CHAT（雑談）の場合も文脈を踏まえて返答させる
                    chat_prompt = (
                        "あなたは優秀なプログラミングアシスタントです。以下のチャット履歴を踏まえて、ユーザーの発言に自然に返答してください。\n\n"
                        f"{context_str}"
                        f"【ユーザーの発言】: {clean_prompt}"
                    )
                    response_text = await ask_llm(chat_prompt)
                    await send_chunked_message(
                        message.channel, response_text, reply_target=message
                    )
            
            except Exception as e:
                # ▼ [修正] エラーによるハングアップ防止
                traceback.print_exc()
                try:
                    await message.reply(f"内部エラーが発生しました: {e}")
                except Exception:
                    pass
        return

    await bot.process_commands(message)


if __name__ == "__main__":
    bot.run(load_token())
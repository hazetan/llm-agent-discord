import asyncio
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
from .llama_acc import (
    ROLE_DESCRIPTIONS,
    ask_llm,
    decide_with_d1,
    fetch_available_ollama_models,
    get_all_role_models,
    get_locked_model,
    set_model_for_role,
)
from .secrets import get_ollama_base_url, get_projects_dir, get_workspace_dir, load_token
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


async def _find_related_session_id(channel: discord.abc.Messageable, objective: str) -> int | None:
    """
    現在のチャンネルまたはスレッドから、引き継ぐべき既存のセッションIDを判定する．
    テキストチャンネルの場合は判断モデル (d1-2B:Q4_K_M) を用いて高速判定する．
    """
    with get_db_session() as db:
        if isinstance(channel, discord.Thread):
            latest_session = (
                db.query(GoalSession)
                .filter(GoalSession.channel_id == channel.id)
                .order_by(GoalSession.id.desc())
                .first()
            )
            return latest_session.id if latest_session else None

        recent_sessions = (
            db.query(GoalSession)
            .filter(GoalSession.channel_id == channel.id)
            .order_by(GoalSession.id.desc())
            .limit(5)
            .all()
        )
        if not recent_sessions:
            return None

        choices: dict[str, str] = {}
        id_map: dict[str, int] = {}
        for s in recent_sessions:
            key = f"SESSION_{s.id}"
            choices[key] = f"Continue or modify existing project (ID {s.id}): {s.objective[:120]}"
            id_map[key] = s.id
        choices["NEW_PROJECT"] = "Start a completely unrelated, brand-new project."

        chosen_key = await decide_with_d1(
            state_text=f"New User Request: {objective}",
            question="Does this request continue/modify one of the existing sessions, or is it a brand-new unrelated project?",
            choices=choices,
            default_choice=f"SESSION_{recent_sessions[0].id}",
        )
        return id_map.get(chosen_key)


@bot.event
async def on_ready() -> None:
    init_db()
    agent_task_queue.start_workers()
    print(f"Logged in as {bot.user.name} ({bot.user.id}) | Ollama URL: {get_ollama_base_url()}")

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


@bot.command(name="models")
async def list_models_cmd(ctx: commands.Context) -> None:
    """現在割り当てられているロール別モデルと、Ollamaサーバー上の利用可能モデル一覧を表示する．"""
    async with ctx.typing():
        role_models = get_all_role_models()
        locked = get_locked_model()
        ollama_models = await fetch_available_ollama_models()

        lines = [
            f"**🤖 Ollama マルチモデル設定状況** (`{get_ollama_base_url()}`)",
            f"• **動作モード**: `{'固定ロック (' + locked + ')' if locked else '自律切り替えモード (Auto Routing)'}`",
            "\n**【役割（ロール）別モデル割り当て】**",
        ]
        for role, desc in ROLE_DESCRIPTIONS.items():
            lines.append(f"• **`{role}`** ({desc}): `{role_models.get(role)}`")

        if ollama_models:
            lines.append("\n**【Ollama サーバー上で検出されたモデル一覧】**")
            for m in ollama_models:
                name = m.get("name", "unknown")
                size_gb = m.get("size", 0) / (1024 ** 3)
                lines.append(f"- `{name}` ({size_gb:.1f} GB)")
        else:
            lines.append("\n⚠️ *Ollama サーバーからモデル一覧を取得できませんでした。URLや起動状態を確認してください。*")

        lines.append(
            "\n💡 **変更方法**: `!set_model <role> <model_name>` "
            "(例: `!set_model coding Qwen2.5-Coder-14B-Instruct:Q4_K_M`, `!set_model lock <model>`, `!set_model auto`)"
        )
        await ctx.reply("\n".join(lines))


@bot.command(name="set_model")
async def set_model_cmd(ctx: commands.Context, role: str, model_name: str = "") -> None:
    """ロール別の使用モデル、またはロック/自動モードを切り替える．"""
    msg = set_model_for_role(role, model_name)
    await ctx.reply(msg)


@bot.command(name="run")
async def start_task(ctx: commands.Context, *, objective: str) -> None:
    async with ctx.typing():
        # ▼ 1. 既存セッションの引き継ぎ判定 (d1-2B:Q4_K_M を使用)
        target_session_id = await _find_related_session_id(ctx.channel, objective)

        if target_session_id:
            with get_db_session() as db:
                session = db.query(GoalSession).filter(GoalSession.id == target_session_id).first()
                display_name = Path(session.project_dir).name if session.project_dir else f"session_{target_session_id}"

                if session.status in ["running", "pending"]:
                    add_session_intervention(target_session_id, f"ユーザーからの追加指示: {objective}")
                    await ctx.reply(
                        f"関連プロジェクト `{display_name}` (Session #{target_session_id}) は現在実行中のため、次ステップへ指示を割り込ませました！"
                    )
                else:
                    session.status = "pending"
                    objective_for_run = session.objective
                    db.commit()
                    add_session_intervention(
                        target_session_id,
                        f"ユーザーからの追加指示: {objective}\nこの指示に従って作業を続行し、完全に動作することを確認してから完了してください。",
                    )
                    await ctx.reply(
                        f"関連プロジェクト `{display_name}` を検知しました。既存のセッション #{target_session_id} を再開します！"
                    )
                    asyncio.create_task(
                        enqueue_agent_with_dashboard(
                            ctx.channel, target_session_id, objective_for_run, reply_msg_id=ctx.message.id, user_id=ctx.author.id
                        )
                    )
            return

        # ▼ 2. 新規プロジェクト作成時はちょっとした作業用モデル (LFM3-1.7B:Q4_K_M) でディレクトリ名を生成
        slug_prompt = (
            "次のタスク目標から、プロジェクトを保存するための適切なディレクトリ名を1つだけ出力してください。\n"
            "条件: 英小文字、数字、アンダースコア(_)のみを使用し、空白や他の記号は含めないこと。説明文は一切不要です。\n"
            "※ただし、タスクが「全体を検索する」「既存の全プロジェクトを調べる」といった内容で、特定のプロジェクトに隔離しない方が良い場合は、特殊キーワード 'workspace_root' と出力してください。\n"
            f"例: shogi_gui, web_server, react_todo_app, workspace_root\n"
            f"タスク目標: {objective}"
        )
        slug_result = await ask_llm(slug_prompt, role="light_work")
        clean_slug = re.sub(r"[^a-z0-9_]", "", slug_result.strip().lower())
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

            if clean_slug in ("workspaceroot", "workspace_root"):
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
        slug_result = await ask_llm(slug_prompt, role="light_work")
        clean_slug = re.sub(r"[^a-z0-9_]", "", slug_result.strip().lower())
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
            is_forum=isinstance(ctx.channel, discord.Thread),
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        session_id = session.id

        if clean_slug in ("workspaceroot", "workspace_root"):
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
                    hist_msgs.reverse()
                    lines = []
                    for m in hist_msgs:
                        author_name = "Bot" if m.author == bot.user else m.author.display_name
                        text = m.clean_content
                        if not text and m.embeds:
                            text = (m.embeds[0].title or "") + " " + (m.embeds[0].description or "")

                        text = text.strip().replace("\n", " ")
                        if not text:
                            continue

                        if len(text) > 400:
                            text = text[:400] + "..."
                        lines.append(f"[{author_name}]: {text}")
                    history_text = "\n".join(lines)
                except Exception:
                    pass

                context_str = f"【直近のチャット履歴】\n{history_text}\n\n" if history_text else ""

                # ▼ 2. 判断用モデル (d1-2B:Q4_K_M) でユーザー発言の意図を高速分類
                intent_choices = {
                    "TASK": (
                        "コードの作成、3Dモデル生成スクリプト作成、ファイルの編集、バグ修正、"
                        "コマンド実行、シミュレーション作成、「続きをやって」「もう一回実行して」などの作業依頼"
                    ),
                    "TECH_CHAT": (
                        "ファイル作成や編集は伴わないが、成果物のプレゼン作成、技術的な解説、"
                        "アーキテクチャの相談などを行う対話"
                    ),
                    "LIGHT_CHAT": "単なる挨拶、雑談、非常に簡単な質問",
                }
                intent = await decide_with_d1(
                    state_text=f"{context_str}【今回のユーザーの発言】: {clean_prompt}",
                    question="ユーザーの発言の意図を TASK, TECH_CHAT, LIGHT_CHAT のいずれかに分類してください。",
                    choices=intent_choices,
                    default_choice="TASK",
                )

                if intent == "TASK":
                    # ▼ 3. ちょっとした作業用モデル (LFM3-1.7B:Q4_K_M) で相槌と具体的な目標を生成
                    reply_prompt = (
                        "以下のユーザーの作業依頼を引き受ける際の、頼もしいアシスタントとしての自然な日本語の返事（1〜2文）のみを出力してください。\n"
                        f"【ユーザーの発言】: {clean_prompt}"
                    )
                    reply_text = await ask_llm(reply_prompt, role="light_work")
                    if not reply_text or not reply_text.strip() or reply_text.startswith("Error"):
                        reply_text = "了解しました！さっそくエージェント環境で作業を開始します！"

                    await message.reply(reply_text.strip())

                    objective_prompt = (
                        "以下の直近のチャット履歴とユーザーの今回の発言から、AIエージェントに渡すための『実行すべき具体的なタスク目標』を日本語の1〜2文で生成してください。\n"
                        "「続きをやって」「失敗したのでもう一回」といった抽象的な表現は、履歴を元に具体的な要件に置き換えてください。\n"
                        "※今回の発言がすでに具体的であれば、その要件（数値条件や制約を含む）を漏らさずそのまま整えて出力してください。\n"
                        "※出力は目標の文のみとし、説明は一切不要です。\n\n"
                        f"{context_str}"
                        f"【今回のユーザーの発言】: {clean_prompt}"
                    )
                    refined_objective = await ask_llm(objective_prompt, role="light_work")
                    refined_objective = refined_objective.strip()
                    if not refined_objective or refined_objective.startswith("Error"):
                        refined_objective = clean_prompt

                    # ▼ 4. 判断モデル (d1-2B:Q4_K_M) で既存セッションの引き継ぎ判定
                    target_session_id = await _find_related_session_id(message.channel, refined_objective)

                    # ▼ 5. セッションの再開（割り込み）または新規作成
                    if target_session_id:
                        with get_db_session() as db:
                            session = db.query(GoalSession).filter(GoalSession.id == target_session_id).first()
                            display_name = Path(session.project_dir).name if session.project_dir else f"session_{target_session_id}"

                            if session.status in ["running", "pending"]:
                                add_session_intervention(target_session_id, f"ユーザーからの追加指示: {refined_objective}")
                                await message.channel.send(
                                    f"関連プロジェクト `{display_name}` (Session #{target_session_id}) は現在実行中のため、次ステップへ指示を割り込ませました！"
                                )
                            else:
                                session.status = "pending"
                                objective_for_run = session.objective
                                db.commit()
                                add_session_intervention(
                                    target_session_id,
                                    f"ユーザーからの追加指示: {refined_objective}\nこの指示に従って作業を続行し、完全に動作することを確認してから完了してください。",
                                )
                                await message.channel.send(
                                    f"関連プロジェクト `{display_name}` を検知しました。既存のセッション #{target_session_id} を再開します！"
                                )
                                asyncio.create_task(
                                    enqueue_agent_with_dashboard(
                                        message.channel,
                                        target_session_id,
                                        objective_for_run,
                                        reply_msg_id=message.id,
                                        user_id=message.author.id,
                                    )
                                )
                    else:
                        slug_prompt = (
                            "次のタスク目標から、プロジェクトを保存するための適切なディレクトリ名を1つだけ出力してください。\n"
                            "条件: 英小文字、数字、アンダースコア(_)のみを使用し、空白や他の記号は含めないこと。説明文は一切不要です。\n"
                            "※重要: もしタスクが「全体を検索する」「既存のプロジェクトを調べる」など特定のプロジェクトへの隔離が不適切な内容であれば 'workspace_root' と出力してください。\n"
                            f"例: shogi_gui, web_server, drone_3d_model, workspace_root\n"
                            f"タスク目標: {refined_objective}"
                        )
                        slug_result = await ask_llm(slug_prompt, role="light_work")
                        clean_slug = re.sub(r"[^a-z0-9_]", "", slug_result.strip().lower())
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
                                is_forum=isinstance(message.channel, discord.Thread),
                            )
                            db.add(session)
                            db.commit()
                            db.refresh(session)
                            session_id = session.id

                            if clean_slug in ("workspaceroot", "workspace_root"):
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

                        await message.channel.send(
                            f"新規プロジェクト `{display_name}` を作成/選択し、セッション #{session_id} を開始します！"
                        )
                        await enqueue_agent_with_dashboard(
                            message.channel,
                            session_id,
                            refined_objective,
                            reply_msg_id=message.id,
                            user_id=message.author.id,
                        )

                else:
                    # ▼ CHAT（プレゼン・質問・雑談）の場合は内容に応じてモデルを自動選択
                    chat_role = "omni" if intent == "TECH_CHAT" else "light_work"
                    chat_prompt = (
                        "あなたは優秀なプログラミングアシスタントです。以下のチャット履歴を踏まえて、ユーザーの発言に自然な日本語で返答してください。\n\n"
                        f"{context_str}"
                        f"【ユーザーの発言】: {clean_prompt}"
                    )
                    response_text = await ask_llm(chat_prompt, role=chat_role)
                    if not response_text or not response_text.strip():
                        response_text = "申し訳ありません、応答を生成できませんでした。もう一度お試しください。"
                    await send_chunked_message(
                        message.channel, response_text, reply_target=message
                    )

            except Exception as exc:
                traceback.print_exc()
                try:
                    await message.reply(f"エラーが発生しました: `{exc}`")
                except Exception:
                    pass
        return

    await bot.process_commands(message)


if __name__ == "__main__":
    bot.run(load_token())
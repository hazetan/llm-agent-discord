import asyncio
import re
import traceback
from datetime import UTC, timedelta, timezone
from pathlib import Path
from typing import Any

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
from .llama_acc import (
    ROLE_DESCRIPTIONS,
    ask_llm,
    decide_with_d1,
    fetch_available_ollama_models,
    get_all_role_models,
    get_locked_model,
    set_model_for_role,
)
from .secrets import (
    get_ollama_base_url,
    get_workspace_dir,
    load_token,
)
from .task_queue import agent_task_queue

JST = timezone(timedelta(hours=9))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


def _sync_get_unfinished_sessions() -> list[dict[str, Any]]:
    with get_db_session() as db:
        sessions = (
            db.query(GoalSession)
            .filter(GoalSession.status.in_(["pending", "running"]))
            .all()
        )
        return [
            {
                "id": s.id,
                "channel_id": s.channel_id,
                "objective": s.objective,
                "message_id": s.message_id,
            }
            for s in sessions
        ]


def _sync_get_recent_channel_sessions(channel_id: int, limit: int = 5) -> list[dict[str, Any]]:
    with get_db_session() as db:
        sessions = (
            db.query(GoalSession)
            .filter(GoalSession.channel_id == channel_id)
            .order_by(GoalSession.id.desc())
            .limit(limit)
            .all()
        )
        return [{"id": s.id, "objective": s.objective} for s in sessions]


def _sync_resume_or_intervene_session(session_id: int) -> dict[str, Any] | None:
    with get_db_session() as db:
        session = db.query(GoalSession).filter(GoalSession.id == session_id).first()
        if not session:
            return None
        was_active = session.status in ["running", "pending"]
        if not was_active:
            session.status = "pending"
            db.commit()
        return {
            "id": session.id,
            "was_active": was_active,
            "objective": session.objective,
        }


def _sync_create_new_session(
    objective: str,
    channel_id: int,
    message_id: int,
    is_forum: bool,
) -> tuple[int, str]:
    with get_db_session() as db:
        session = GoalSession(
            title=objective[:30],
            objective=objective,
            status="pending",
            channel_id=channel_id,
            message_id=message_id,
            project_slug="workspace",
            is_forum=is_forum,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        session_id = session.id

        proj_dir = get_workspace_dir()
        proj_dir.mkdir(parents=True, exist_ok=True)
        session.project_dir = str(proj_dir)
        db.commit()
        return session_id, "全体のワークスペース"


def _sync_get_latest_project_status(channel_id: int | None = None) -> dict[str, Any] | None:
    with get_db_session() as db:
        query = db.query(GoalSession)
        if channel_id is not None:
            query = query.filter(GoalSession.channel_id == channel_id)
        latest = query.order_by(GoalSession.id.desc()).first()
        if not latest:
            return None
        activities = (
            db.query(Activity)
            .filter(Activity.session_id == latest.id)
            .order_by(Activity.id.desc())
            .limit(3)
            .all()
        )
        return {
            "id": latest.id,
            "title": latest.title,
            "status": latest.status,
            "objective": latest.objective,
            "project_dir": latest.project_dir,
            "updated_at": latest.updated_at,
            "activities": [
                {"work": a.work, "result": a.result, "datetime": a.datetime}
                for a in reversed(activities)
            ],
        }


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
    recent_sessions = await asyncio.to_thread(_sync_get_recent_channel_sessions, channel.id, 5)
    if not recent_sessions:
        return None

    if isinstance(channel, discord.Thread):
        return recent_sessions[0]["id"]

    choices: dict[str, str] = {}
    id_map: dict[str, int] = {}
    for s in recent_sessions:
        key = f"SESSION_{s['id']}"
        choices[key] = f"Continue or modify existing project (ID {s['id']}): {s['objective'][:120]}"
        id_map[key] = s["id"]
    choices["NEW_PROJECT"] = "Start a completely unrelated, brand-new project."

    chosen_key = await decide_with_d1(
        state_text=f"New User Request: {objective}",
        question="Does this request continue/modify one of the existing sessions, or is it a brand-new unrelated project?",
        choices=choices,
        default_choice=f"SESSION_{recent_sessions[0]['id']}",
    )
    return id_map.get(chosen_key)


@bot.event
async def on_ready() -> None:
    await asyncio.to_thread(init_db)
    agent_task_queue.start_workers()
    print(f"Logged in as {bot.user.name} ({bot.user.id}) | Ollama URL: {get_ollama_base_url()}")

    unfinished_sessions = await asyncio.to_thread(_sync_get_unfinished_sessions)
    for sess in unfinished_sessions:
        if not sess["channel_id"]:
            continue
        channel = await _resolve_channel(sess["channel_id"])
        if not channel:
            continue

        await channel.send(
            f"**[システム再起動]** セッション #{sess['id']} をタスクキューへ再投入します．"
        )
        await enqueue_agent_with_dashboard(
            channel, sess["id"], sess["objective"], reply_msg_id=sess["message_id"]
        )


@bot.command(name="models")
async def list_models_cmd(ctx: commands.Context) -> None:
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
    msg = set_model_for_role(role, model_name)
    await ctx.reply(msg)


async def _handle_run_command(ctx: commands.Context, objective: str, infinity_mode: bool) -> None:
    async with ctx.typing():
        target_session_id = await _find_related_session_id(ctx.channel, objective)

        if target_session_id:
            info = await asyncio.to_thread(_sync_resume_or_intervene_session, target_session_id)
            if info:
                if info["was_active"]:
                    add_session_intervention(target_session_id, f"ユーザーからの追加指示: {objective}")
                    await ctx.reply(
                        f"共有ワークスペース (Session #{target_session_id}) は現在実行中のため、次ステップへ指示を割り込ませました！"
                    )
                else:
                    add_session_intervention(
                        target_session_id,
                        f"ユーザーからの追加指示: {objective}\nこの指示に従って作業を続行し、完全に動作することを確認してから完了してください。",
                    )
                    await ctx.reply(
                        f"共有ワークスペースの既存のセッション #{target_session_id} を再開します！"
                    )
                    asyncio.create_task(
                        enqueue_agent_with_dashboard(
                            ctx.channel, target_session_id, info["objective"], reply_msg_id=ctx.message.id, user_id=ctx.author.id, infinity_mode=infinity_mode
                        )
                    )
                return

        session_id, display_name = await asyncio.to_thread(
            _sync_create_new_session,
            objective,
            ctx.channel.id,
            ctx.message.id,
            isinstance(ctx.channel, discord.Thread),
        )

    mode_str = " (Infinity Mode)" if infinity_mode else ""
    await ctx.reply(f"{display_name} にてセッション #{session_id} を開始します！{mode_str}")
    await enqueue_agent_with_dashboard(
        ctx.channel, session_id, objective, reply_msg_id=ctx.message.id, user_id=ctx.author.id, infinity_mode=infinity_mode
    )


@bot.command(name="run")
async def start_task(ctx: commands.Context, *, objective: str) -> None:
    await _handle_run_command(ctx, objective, False)


@bot.command(name="run_infinity")
async def start_task_infinity(ctx: commands.Context, *, objective: str) -> None:
    await _handle_run_command(ctx, objective, True)


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
    workspace_dir = await asyncio.to_thread(get_workspace_dir)
    source_path = (workspace_dir / source_dir_name).resolve()

    if workspace_dir not in source_path.parents or not source_path.exists() or not source_path.is_dir():
        await ctx.reply(f"指定されたフォルダ `{source_dir_name}` がワークスペース内に見つかりません。")
        return

    async with ctx.typing():
        enhanced_objective = (
            f"指定されたディレクトリ `{source_dir_name}` のコード資産をベースに開発を行ってください。\n"
            f"まずはファイル一覧や中身を確認して現状を把握し、次の目標を達成してください: {objective}"
        )

        session_id, display_name = await asyncio.to_thread(
            _sync_create_new_session,
            enhanced_objective,
            ctx.channel.id,
            ctx.message.id,
            isinstance(ctx.channel, discord.Thread),
        )

    await ctx.reply(
        f"`{source_dir_name}` に着目して、全体のワークスペースにてセッション #{session_id} を開始します！"
    )

    await enqueue_agent_with_dashboard(
        ctx.channel, session_id, enhanced_objective, reply_msg_id=ctx.message.id, user_id=ctx.author.id
    )


@bot.command(name="project_status")
async def project_status(ctx: commands.Context) -> None:
    status_data = await asyncio.to_thread(_sync_get_latest_project_status)
    if not status_data:
        await ctx.reply("登録されているセッション情報はありません．")
        return

    lines = [
        f"**[Session #{status_data['id']}] {status_data['title']}**",
        f"• **Status**: `{status_data['status']}`",
        f"• **Objective**: {status_data['objective']}",
    ]
    if status_data["project_dir"]:
        lines.append(f"• **Workspace**: `{status_data['project_dir']}`")
    if status_data["updated_at"]:
        dt = status_data["updated_at"].replace(tzinfo=UTC).astimezone(JST)
        lines.append(f"• **Updated**: {dt.strftime('%Y-%m-%d %H:%M:%S')}")

    if status_data["activities"]:
        lines.append("\n**Recent Activities:**")
        for act in status_data["activities"]:
            t_str = (
                act["datetime"].replace(tzinfo=UTC).astimezone(JST).strftime("%H:%M:%S")
                if act["datetime"]
                else "--:--:--"
            )
            lines.append(f"- `[{t_str}]` **{act['work']}**: {act['result']}")

    await ctx.reply("\n".join(lines))


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author == bot.user:
        return

    if pending_human_questions and not message.content.startswith("!"):
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

                intent_choices = {
                    "TASK": (
                        "ファイル操作、コード生成、コマンド実行、ファイルの検索、「作業を行って」「続きをやって」「探して」など、"
                        "エージェントに具体的な作業や行動を要求する指示。"
                    ),
                    "CHAT": (
                        "進捗状況の確認、技術的な質問、純粋な雑談や挨拶など、"
                        "エージェントを動かして作業させる必要のない対話。"
                    ),
                }
                intent = await decide_with_d1(
                    state_text=f"{context_str}【今回のユーザーの発言】: {clean_prompt}",
                    question="ユーザーの意図が、エージェントを動かして作業させる依頼(TASK)か、それとも単なる進捗確認や対話(CHAT)かを判定してください。迷った場合はTASKを選択してください。",
                    choices=intent_choices,
                    default_choice="TASK",
                )

                if intent == "TASK":
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

                    target_session_id = await _find_related_session_id(message.channel, refined_objective)

                    if target_session_id:
                        info = await asyncio.to_thread(_sync_resume_or_intervene_session, target_session_id)
                        if info:
                            if info["was_active"]:
                                add_session_intervention(target_session_id, f"ユーザーからの追加指示: {refined_objective}")
                                await message.channel.send(
                                    f"共有ワークスペース (Session #{target_session_id}) は現在実行中のため、次ステップへ指示を割り込ませました！"
                                )
                            else:
                                add_session_intervention(
                                    target_session_id,
                                    f"ユーザーからの追加指示: {refined_objective}\nこの指示に従って作業を続行し、完全に動作することを確認してから完了してください。",
                                )
                                await message.channel.send(
                                    f"共有ワークスペースの既存のセッション #{target_session_id} を再開します！"
                                )
                                asyncio.create_task(
                                    enqueue_agent_with_dashboard(
                                        message.channel,
                                        target_session_id,
                                        info["objective"],
                                        reply_msg_id=message.id,
                                        user_id=message.author.id,
                                    )
                                )
                            return

                    session_id, display_name = await asyncio.to_thread(
                        _sync_create_new_session,
                        refined_objective,
                        message.channel.id,
                        message.id,
                        isinstance(message.channel, discord.Thread),
                    )

                    await message.channel.send(
                        f"{display_name} にてセッション #{session_id} を開始します！"
                    )
                    await enqueue_agent_with_dashboard(
                        message.channel,
                        session_id,
                        refined_objective,
                        reply_msg_id=message.id,
                        user_id=message.author.id,
                    )

                else:
                    status_data = await asyncio.to_thread(_sync_get_latest_project_status, message.channel.id)
                    project_info = ""
                    if status_data:
                        act_str = "\n".join(
                            [f"- {a['work']}: {a['result']}" for a in status_data["activities"]]
                        )
                        project_info = (
                            "【現在のプロジェクト状況 (参考情報)】\n"
                            f"セッションID: {status_data['id']}\n"
                            f"目標: {status_data['objective']}\n"
                            f"ステータス: {status_data['status']}\n"
                            f"最近のアクティビティ:\n{act_str}\n\n"
                        )

                    chat_prompt = (
                        "あなたは優秀なプログラミングアシスタントです。以下のチャット履歴と現在のプロジェクト状況を踏まえて、ユーザーの発言に自然な日本語で返答してください。\n"
                        "※もし進捗や現状を聞かれた場合は、参考情報の「最近のアクティビティ」などを要約して分かりやすく答えてください。\n\n"
                        f"{project_info}"
                        f"{context_str}"
                        f"【ユーザーの発言】: {clean_prompt}"
                    )
                    response_text = await ask_llm(chat_prompt, role="omni")
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
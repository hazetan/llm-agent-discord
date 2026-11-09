import asyncio
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from .db_models import Activity, GoalSession, Planner
from .db_system import SessionLocal
from .llama_acc import chat_with_tools
from .prompts_and_schemas import AGENT_SYSTEM_PROMPT, AVAILABLE_TOOLS
from .rag_store import rag_store
from .secrets import (
    get_keep_recent_messages,
    get_max_history_messages,
    get_max_output_chars,
    get_max_steps,
    get_projects_dir,
    get_prompt_token_limit,
)
from .session_logger import (
    SessionFileLogger,
    StepLogRecord,
    ToolExecutionRecord,
)
from .token_monitor import estimate_messages_tokens, log_token_usage
from .tools import execute_tool_async, init_git_repo, stop_docker_container

INTERVENTION_QUEUES: dict[int, list[str]] = {}

COMPLETION_KEYWORDS: tuple[str, ...] = ("完了", "finished", "complete")
ERROR_INDICATORS: tuple[str, ...] = (
    "error",
    "exception",
    "traceback",
    "failed",
    "permission denied",
    "address already in use",
    "not found",
    "timed out",
)


def add_session_intervention(session_id: int, message: str) -> None:
    """実行中のセッションに対して次ステップで注入するユーザー指示を追加する．"""
    INTERVENTION_QUEUES.setdefault(session_id, []).append(message)


def extract_tool_call_from_text(content: str) -> dict[str, Any] | None:
    """テキスト内の Markdown JSON コードブロックからツール呼び出しを救済抽出する．"""
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
        if isinstance(data, dict) and "name" in data and "arguments" in data:
            return data
    except Exception:
        pass
    return None


def parse_tool_arguments(raw_args: Any) -> dict[str, Any]:
    """ツールの arguments を安全に dict としてパースする．"""
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def build_handover_context(db: Session, session_id: int) -> str:
    """コンテキスト圧縮時に挿入する最新の Planner 引き継ぎ情報を構築する．"""
    latest_plan = (
        db.query(Planner)
        .filter(Planner.session_id == session_id)
        .order_by(Planner.id.desc())
        .first()
    )
    if not latest_plan:
        return "No persistent planner history available."

    return (
        "--- [Project State Handover] ---\n"
        f"Previous Issue : {latest_plan.current_issue}\n"
        f"Executed Task  : {latest_plan.task}\n"
        f"Task Result    : {latest_plan.result or 'In progress'}\n"
        f"Next Backlog   : {latest_plan.future_issue or 'None'}\n"
        "--------------------------------"
    )


def _prepare_session_workspace(session: GoalSession) -> Path:
    if not session.project_dir:
        project_path = get_projects_dir() / f"session_{session.id}"
        session.project_dir = str(project_path)
    else:
        project_path = Path(session.project_dir)

    project_path.mkdir(parents=True, exist_ok=True)
    init_git_repo(project_path)
    return project_path


def _pop_user_interventions(
    session_id: int, messages: list[dict[str, Any]]
) -> list[str]:
    """割り込みキューからメッセージを取り出してプロンプトへ注入し，注入リストを返す．"""
    if not INTERVENTION_QUEUES.get(session_id):
        return []
    interventions = INTERVENTION_QUEUES.pop(session_id)
    for inject_msg in interventions:
        messages.append(
            {
                "role": "user",
                "content": f"[User Mid-Course Correction / Note]: {inject_msg}",
            }
        )
    return interventions


def _compress_context_if_needed(
    db: Session,
    session: GoalSession,
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool, str]:
    """
    上限超過時にコンテキスト圧縮を行い，(新messages, 圧縮発動フラグ, 引き継ぎノート) を返す．
    """
    max_history = get_max_history_messages()
    prompt_limit = get_prompt_token_limit()
    current_tokens = estimate_messages_tokens(messages, AVAILABLE_TOOLS)

    if len(messages) <= max_history and current_tokens <= prompt_limit:
        return messages, False, ""

    keep_recent = get_keep_recent_messages()
    handover_note = build_handover_context(db, session.id)
    compressed_messages = [
        messages[0],
        {"role": "user", "content": f"Task Objective: {session.objective}"},
        {"role": "system", "content": handover_note},
        *messages[-keep_recent:],
    ]
    return compressed_messages, True, handover_note


async def _inject_rag_hint_on_error(
    tool_output: str,
    messages: list[dict[str, Any]],
    injected_memory_ids: set[int],
) -> list[str]:
    """エラー出力検知時に RAG から解決ヒントを注入し，ヒットした知見ラベル一覧を返す．"""
    lower_out = tool_output.lower()
    if not any(indicator in lower_out for indicator in ERROR_INDICATORS):
        return []

    matched_memories = await rag_store.search_relevant_memories(tool_output)
    new_memories = [m for m in matched_memories if m.id not in injected_memory_ids]
    if not new_memories:
        return []

    hit_labels: list[str] = []
    for mem in new_memories:
        injected_memory_ids.add(mem.id)
        hit_labels.append(f"[{mem.category}] (ID:{mem.id})")

    rag_hint = rag_store.format_as_prompt_context(
        new_memories, header="RAG Auto-Retrieved Troubleshooting Hint"
    )
    messages.append({"role": "system", "content": rag_hint})
    return hit_labels


async def _request_llm_with_metrics(
    messages: list[dict[str, Any]], max_retries: int = 2
) -> tuple[dict[str, Any], float, int]:
    """LLM 推論を実行し，(response_msg, 推論秒数, リトライ回数) を返す．"""
    start_t = time.perf_counter()
    for attempt in range(max_retries):
        try:
            response_msg = await chat_with_tools(messages, tools=AVAILABLE_TOOLS)
            latency = time.perf_counter() - start_t
            return response_msg, latency, attempt
        except Exception as exc:
            if attempt + 1 >= max_retries:
                raise
            await asyncio.sleep(2)
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Tool execution failed with server error ({exc}). "
                        "Retry with concise parameters or split into smaller steps."
                    ),
                }
            )
    raise RuntimeError("Unreachable retry state")


def _truncate_output(output: str) -> str:
    max_chars = get_max_output_chars()
    if len(output) > max_chars:
        return output[:max_chars] + "\n...(output truncated)..."
    return output


def _classify_tool_status(tool_output: str, is_repeat_blocked: bool) -> str:
    if is_repeat_blocked:
        return "BLOCKED_REPEAT"
    lower = tool_output.lower()
    if lower.startswith("error:") or any(k in lower for k in ERROR_INDICATORS):
        return "ERROR"
    return "SUCCESS"


def _record_activity(
    db: Session, session_id: int, tool_name: str, args: dict[str, Any], result: str
) -> None:
    args_summary = json.dumps(args, ensure_ascii=False)[:300]
    activity = Activity(
        session_id=session_id,
        work=f"[{tool_name}] {args_summary}",
        result=result[:500],
    )
    db.add(activity)
    db.commit()


async def run_agent_loop(
    session_id: int,
    max_steps: int | None = None,
    dashboard_callback: Callable | None = None,
    human_ask_callback: Callable | None = None,
) -> str:
    if max_steps is None:
        max_steps = get_max_steps()

    db = SessionLocal()
    session_logger: SessionFileLogger | None = None
    final_result = (
        f"Reached maximum allowed steps ({max_steps}) without calling finish_task."
    )

    try:
        session = db.query(GoalSession).filter(GoalSession.id == session_id).first()
        if not session:
            return "Session not found."

        project_path = _prepare_session_workspace(session)
        session.status = "running"
        db.commit()

        # .log/session{session_id} ファイルロガーの初期化
        session_logger = SessionFileLogger(
            session_id=session_id,
            title=session.title,
            objective=session.objective,
            workspace_dir=project_path,
        )

        initial_memories = await rag_store.search_relevant_memories(session.objective)
        injected_memory_ids: set[int] = {m.id for m in initial_memories}
        pending_rag_labels: list[str] = [
            f"[{m.category}] (ID:{m.id})" for m in initial_memories
        ]
        rag_initial_block = rag_store.format_as_prompt_context(initial_memories)

        system_content = (
            f"{AGENT_SYSTEM_PROMPT}\n\n{rag_initial_block}"
            if rag_initial_block
            else AGENT_SYSTEM_PROMPT
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": f"Task Objective: {session.objective}"},
        ]

        last_tool_signature: str | None = None
        repeat_count = 0

        for step in range(1, max_steps + 1):
            interventions = _pop_user_interventions(session_id, messages)
            messages, compressed, handover_text = _compress_context_if_needed(
                db, session, messages
            )

            if dashboard_callback:
                await dashboard_callback(
                    step=step,
                    max_steps=max_steps,
                    status="Thinking (LLM推論中)...",
                )

            response_msg, latency_sec, retries = await _request_llm_with_metrics(
                messages
            )
            actual_usage = response_msg.pop("_usage", None)

            # コンソールへのトークン消費割合出力
            log_token_usage(
                step=step,
                max_steps=max_steps,
                session_id=session_id,
                messages=messages,
                tools=AVAILABLE_TOOLS,
                actual_usage=actual_usage,
            )

            content = (response_msg.get("content") or "").strip()
            tool_calls = response_msg.get("tool_calls")
            dispatch_mode = "native_tool_calls" if tool_calls else "text_only"

            if not tool_calls:
                fallback_tool = extract_tool_call_from_text(content)
                if fallback_tool:
                    dispatch_mode = "fallback_json"
                    tool_calls = [
                        {
                            "id": f"fallback_call_{step}",
                            "function": {
                                "name": fallback_tool["name"],
                                "arguments": json.dumps(fallback_tool["arguments"]),
                            },
                        }
                    ]

            # このステップのテレメトリレコードを作成
            inference_telemetry = session_logger.build_inference_telemetry(
                messages=messages,
                tools=AVAILABLE_TOOLS,
                actual_usage=actual_usage,
                latency_sec=latency_sec,
                retries=retries,
                dispatch_mode=dispatch_mode,
                thought_content=content,
            )
            step_record = StepLogRecord(
                step=step,
                max_steps=max_steps,
                interventions=interventions,
                context_compressed=compressed,
                handover_snapshot=handover_text,
                rag_injected_titles=list(pending_rag_labels),
                inference=inference_telemetry,
            )
            pending_rag_labels.clear()

            messages.append(response_msg)

            if dashboard_callback:
                await dashboard_callback(
                    step=step,
                    max_steps=max_steps,
                    thought=content,
                    status="Tool Dispatching...",
                )

            # ツール呼び出しが存在しない場合
            if not tool_calls:
                session_logger.log_step(step_record)

                if content and any(k in content.lower() for k in COMPLETION_KEYWORDS):
                    final_result = content
                    session.status = "completed"
                    db.commit()
                    session_logger.log_session_summary("completed", final_result)
                    return final_result

                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Do not explain your steps in text. Directly invoke "
                            "the appropriate tool using the function calling schema."
                        ),
                    }
                )
                continue

            # 各ツールの実行と計測
            finish_called = False
            for call in tool_calls:
                tool_name = call["function"]["name"]
                args = parse_tool_arguments(call["function"].get("arguments"))

                current_signature = f"{tool_name}:{json.dumps(args, sort_keys=True)}"
                if current_signature == last_tool_signature:
                    repeat_count += 1
                else:
                    last_tool_signature = current_signature
                    repeat_count = 0

                if dashboard_callback:
                    await dashboard_callback(
                        step=step,
                        max_steps=max_steps,
                        thought=content,
                        tool_name=tool_name,
                        tool_args=args,
                        status=f"Executing {tool_name}...",
                    )

                tool_start_t = time.perf_counter()
                is_repeat_blocked = repeat_count >= 1

                if is_repeat_blocked:
                    tool_output = (
                        f"Error: Repetitive action detected for '{tool_name}'. "
                        "Proceed to next verification step or call 'finish_task'."
                    )
                else:
                    tool_output = await execute_tool_async(
                        tool_name,
                        args,
                        session_id=session_id,
                        project_dir=project_path,
                        human_ask_callback=human_ask_callback,
                    )

                tool_duration = time.perf_counter() - tool_start_t
                truncated_output = _truncate_output(tool_output)
                tool_status = _classify_tool_status(tool_output, is_repeat_blocked)

                # ステップログにツールの実行詳細と生出力を追加
                step_record.tool_executions.append(
                    ToolExecutionRecord(
                        tool_name=tool_name,
                        args=args,
                        duration_sec=tool_duration,
                        status=tool_status,
                        raw_output=tool_output,
                        sent_output_len=len(truncated_output),
                    )
                )

                if dashboard_callback:
                    await dashboard_callback(
                        step=step,
                        max_steps=max_steps,
                        thought=content,
                        tool_name=tool_name,
                        tool_args=args,
                        tool_output=truncated_output,
                        status="Step Completed",
                    )

                _record_activity(db, session_id, tool_name, args, truncated_output)

                if tool_name == "finish_task":
                    final_result = tool_output
                    finish_called = True

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": tool_name,
                        "content": truncated_output,
                    }
                )

                new_rag_hits = await _inject_rag_hint_on_error(
                    truncated_output, messages, injected_memory_ids
                )
                pending_rag_labels.extend(new_rag_hits)

            # 1ステップ分の全推論・ツール実行記録を .log/session{id} へフラッシュ
            session_logger.log_step(step_record)

            if finish_called:
                session.status = "completed"
                db.commit()
                session_logger.log_session_summary("completed", final_result)
                return final_result

        session.status = "failed"
        db.commit()
        session_logger.log_session_summary("failed", final_result)
        return final_result

    except asyncio.CancelledError:
        db.rollback()
        session = db.query(GoalSession).filter(GoalSession.id == session_id).first()
        if session:
            session.status = "cancelled"
            db.commit()
        if session_logger:
            session_logger.log_session_summary(
                "cancelled", "Session cancelled by user."
            )
        raise

    except Exception as exc:
        db.rollback()
        err_msg = f"Agent failed with error: {exc}"
        session = db.query(GoalSession).filter(GoalSession.id == session_id).first()
        if session:
            session.status = "failed"
            db.commit()
        if session_logger:
            session_logger.log_session_summary("failed", err_msg)
        return err_msg

    finally:
        stop_docker_container(session_id)
        INTERVENTION_QUEUES.pop(session_id, None)
        db.close()
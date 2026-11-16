import asyncio
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from .context_builder import ContextBuilder
from .db_models import Activity, GoalSession, Planner, SessionMessage
from .db_system import SessionLocal
from .llama_acc import chat_with_tools
from .prompts_and_schemas import AGENT_SYSTEM_PROMPT, AVAILABLE_TOOLS
from .rag_store import rag_store
from .secrets import (
    get_max_output_chars,
    get_max_steps,
    get_projects_dir,
    get_prompt_token_limit,
    get_workspace_dir,
)
from .session_logger import (
    SessionFileLogger,
    StepLogRecord,
    ToolExecutionRecord,
)
from .token_monitor import log_token_usage
from .tools import execute_tool_async, init_git_repo, stop_docker_container

INTERVENTION_QUEUES: dict[int, list[str]] = {}

COMPLETION_KEYWORDS: tuple[str, ...] = ("完了", "finished", "complete")
ERROR_INDICATORS: tuple[str, ...] = (
    "error", "exception", "traceback", "failed",
    "permission denied", "address already in use", "not found", "timed out",
)


def add_session_intervention(session_id: int, message: str) -> None:
    INTERVENTION_QUEUES.setdefault(session_id, []).append(message)


def _pop_user_interventions(session_id: int) -> list[str]:
    if not INTERVENTION_QUEUES.get(session_id):
        return []
    return INTERVENTION_QUEUES.pop(session_id)


def _append_history(db: Session, session_id: int, step: int, msg: dict[str, Any], raw_history: list[dict[str, Any]]):
    """メモリ上の履歴に追加しつつ、DBへも永続化する"""
    raw_history.append(msg)
    sm = SessionMessage(
        session_id=session_id,
        step=step,
        role=msg.get("role", "unknown"),
        message_json=json.dumps(msg, ensure_ascii=False)
    )
    db.add(sm)
    db.commit()


def extract_tool_call_from_text(content: str) -> dict[str, Any] | None:
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if not match: return None
    try:
        data = json.loads(match.group(1))
        if isinstance(data, dict) and "name" in data and "arguments" in data:
            return data
    except Exception:
        pass
    return None


def parse_tool_arguments(raw_args: Any) -> dict[str, Any]:
    if isinstance(raw_args, dict): return raw_args
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def build_handover_context(db: Session, session_id: int) -> str:
    latest_plan = db.query(Planner).filter(Planner.session_id == session_id).order_by(Planner.id.desc()).first()
    if not latest_plan: return "No persistent planner history available."

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


async def _inject_rag_hint_on_error(
    tool_output: str,
    raw_history: list[dict[str, Any]],
    injected_memory_ids: set[int],
    db: Session,
    session_id: int,
    step: int
) -> tuple[list[str], list[int]]:
    lower_out = tool_output.lower()
    if not any(indicator in lower_out for indicator in ERROR_INDICATORS):
        return [], []

    matched_memories = await rag_store.search_relevant_memories(tool_output)
    new_memories = [m for m in matched_memories if m.id not in injected_memory_ids]
    if not new_memories:
        return [], []

    hit_labels = []
    used_ids = []
    for mem in new_memories:
        injected_memory_ids.add(mem.id)
        used_ids.append(mem.id)
        hit_labels.append(f"[{mem.category}] (ID:{mem.id})")

    rag_hint = rag_store.format_as_prompt_context(new_memories, header="RAG Auto-Retrieved Troubleshooting Hint")
    _append_history(db, session_id, step, {"role": "system", "content": rag_hint}, raw_history)
    return hit_labels, used_ids


async def _request_llm_with_metrics(messages: list[dict[str, Any]], max_retries: int = 2) -> tuple[dict[str, Any], float, int]:
    start_t = time.perf_counter()
    for attempt in range(max_retries):
        try:
            response_msg = await chat_with_tools(messages, tools=AVAILABLE_TOOLS)
            latency = time.perf_counter() - start_t
            return response_msg, latency, attempt
        except Exception as exc:
            if attempt + 1 >= max_retries: raise
            await asyncio.sleep(2)
            messages.append({"role": "user", "content": f"Tool execution failed with server error ({exc}). Retry."})
    raise RuntimeError("Unreachable retry state")


def _truncate_output(output: str) -> str:
    max_chars = get_max_output_chars()
    if len(output) > max_chars:
        return output[:max_chars] + "\n...(output truncated)..."
    return output


def _classify_tool_status(tool_output: str, is_repeat_blocked: bool) -> str:
    if is_repeat_blocked: return "BLOCKED_REPEAT"
    lower = tool_output.lower()
    if lower.startswith("error:") or any(k in lower for k in ERROR_INDICATORS): return "ERROR"
    return "SUCCESS"


def _record_activity(db: Session, session_id: int, tool_name: str, args: dict[str, Any], result: str):
    args_summary = json.dumps(args, ensure_ascii=False)[:300]
    activity = Activity(session_id=session_id, work=f"[{tool_name}] {args_summary}", result=result[:500])
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
    final_result = f"Reached maximum allowed steps ({max_steps}) without calling finish_task."
    used_rag_ids = set()

    try:
        session = db.query(GoalSession).filter(GoalSession.id == session_id).first()
        if not session: return "Session not found."

        project_path = _prepare_session_workspace(session)
        session.status = "running"
        db.commit()

        session_logger = SessionFileLogger(session_id=session_id, title=session.title, objective=session.objective, workspace_dir=project_path)

        # ▼ 1. DBから過去の履歴を全てロードし、再起動時の完全復元を行う
        db_msgs = db.query(SessionMessage).filter(SessionMessage.session_id == session_id).order_by(SessionMessage.id).all()
        raw_history = [json.loads(m.message_json) for m in db_msgs]
        last_step = db_msgs[-1].step if db_msgs else 0

        # ▼ 追加: 再起動時のツール宙ぶらりん対策（自己修復メカニズム）
        if raw_history:
            last_msg = raw_history[-1]
            if last_msg.get("role") == "assistant" and "tool_calls" in last_msg:
                # ツール結果を待っている間に落ちた場合、ダミーのエラーを返してLLMに再実行・再質問を促す
                for call in last_msg["tool_calls"]:
                    recovery_msg = {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": call["function"]["name"],
                        "content": (
                            "System Interruption: The bot was restarted while waiting for this tool's response. "
                            "If this was 'ask_human', the user's response was lost. "
                            "Please execute the tool again or ask the human again."
                        )
                    }
                    _append_history(db, session_id, last_step, recovery_msg, raw_history)

        initial_memories = await rag_store.search_relevant_memories(session.objective)
        injected_memory_ids = {m.id for m in initial_memories}
        used_rag_ids.update(injected_memory_ids)
        pending_rag_labels = [f"[{m.category}] (ID:{m.id})" for m in initial_memories]
        
        system_content = AGENT_SYSTEM_PROMPT
        
        # ▼ 追加: フォーラムモード時の行動指示
        if session.is_forum:
            system_content += (
                "\n\n[Forum Mode Directive]: You are operating in a persistent discussion thread. "
                "Do NOT use 'finish_task' to end the session unless explicitly requested to close the project. "
                "Instead, when you finish a sub-task or need feedback, use 'ask_human' to report your progress and ask what to do next. "
                "This keeps the session active and interactive."
            )

        if project_path == get_workspace_dir():
            try:
                subdirs = [d for d in os.listdir(project_path) if os.path.isdir(project_path / d) and not d.startswith('.')]
                if subdirs:
                    system_content += f"\n\n[Workspace Root Hint]: Directories here: {', '.join(subdirs)}."
            except Exception:
                pass

        last_tool_signature: str | None = None
        repeat_count = 0

        for step in range(1, max_steps + 1):
            # ▼ 2. 割り込み指示を履歴に保存
            for inv in _pop_user_interventions(session_id):
                _append_history(db, session_id, step, {"role": "user", "content": f"[User Mid-Course Correction]: {inv}"}, raw_history)

            # ▼ 3. ContextBuilder でトークン上限に合わせて賢く組み立てる
            builder = ContextBuilder(get_prompt_token_limit())
            builder.set_system(system_content)
            builder.set_objective(session.objective)
            builder.add_rag_or_plan(rag_store.format_as_prompt_context(initial_memories))
            builder.add_rag_or_plan(build_handover_context(db, session.id))

            prompt_messages, compressed, handover_text = builder.build_messages(raw_history)

            if dashboard_callback:
                await dashboard_callback(step=step, max_steps=max_steps, status="Thinking (LLM推論中)...")

            response_msg, latency_sec, retries = await _request_llm_with_metrics(prompt_messages)
            actual_usage = response_msg.pop("_usage", None)

            log_token_usage(step, max_steps, session_id, prompt_messages, AVAILABLE_TOOLS, actual_usage)

            content = (response_msg.get("content") or "").strip()
            tool_calls = response_msg.get("tool_calls")
            dispatch_mode = "native_tool_calls" if tool_calls else "text_only"

            if not tool_calls:
                fallback_tool = extract_tool_call_from_text(content)
                if fallback_tool:
                    dispatch_mode = "fallback_json"
                    tool_calls = [{"id": f"fallback_call_{step}", "function": {"name": fallback_tool["name"], "arguments": json.dumps(fallback_tool["arguments"])}}]

            inference_telemetry = session_logger.build_inference_telemetry(prompt_messages, AVAILABLE_TOOLS, actual_usage, latency_sec, retries, dispatch_mode, content)
            step_record = StepLogRecord(step=step, max_steps=max_steps, context_compressed=compressed, handover_snapshot=handover_text, rag_injected_titles=list(pending_rag_labels), inference=inference_telemetry)
            pending_rag_labels.clear()

            # ▼ 4. アシスタントの返答を履歴とDBに保存
            _append_history(db, session_id, step, response_msg, raw_history)

            if dashboard_callback:
                await dashboard_callback(step=step, max_steps=max_steps, thought=content, status="Tool Dispatching...")

            if not tool_calls:
                session_logger.log_step(step_record)
                if content and any(k in content.lower() for k in COMPLETION_KEYWORDS):
                    final_result = content
                    session.status = "completed"
                    db.commit()
                    session_logger.log_session_summary("completed", final_result)
                    rag_store.mark_memory_success(list(used_rag_ids), True)
                    return final_result

                _append_history(db, session_id, step, {"role": "user", "content": "Directly invoke tool using function calling schema."}, raw_history)
                continue

            finish_called = False
            for call in tool_calls:
                tool_name = call["function"]["name"]
                args = parse_tool_arguments(call["function"].get("arguments"))
                current_sig = f"{tool_name}:{json.dumps(args, sort_keys=True)}"
                if current_sig == last_tool_signature:
                    repeat_count += 1
                else:
                    last_tool_signature = current_sig
                    repeat_count = 0

                if dashboard_callback:
                    await dashboard_callback(step=step, max_steps=max_steps, thought=content, tool_name=tool_name, tool_args=args, status=f"Executing {tool_name}...")

                tool_start_t = time.perf_counter()
                is_repeat_blocked = repeat_count >= 1

                if is_repeat_blocked:
                    tool_output = f"Error: Repetitive action detected for '{tool_name}'."
                else:
                    tool_output = await execute_tool_async(tool_name, args, session_id=session_id, project_dir=project_path, human_ask_callback=human_ask_callback)

                tool_duration = time.perf_counter() - tool_start_t
                truncated_output = _truncate_output(tool_output)
                tool_status = _classify_tool_status(tool_output, is_repeat_blocked)

                step_record.tool_executions.append(ToolExecutionRecord(tool_name=tool_name, args=args, duration_sec=tool_duration, status=tool_status, raw_output=tool_output, sent_output_len=len(truncated_output)))

                if dashboard_callback:
                    await dashboard_callback(step=step, max_steps=max_steps, thought=content, tool_name=tool_name, tool_args=args, tool_output=truncated_output, status="Step Completed")

                _record_activity(db, session_id, tool_name, args, truncated_output)
                if tool_name == "finish_task":
                    final_result = tool_output
                    finish_called = True

                # ▼ 5. ツールの結果を履歴とDBに保存
                _append_history(db, session_id, step, {"role": "tool", "tool_call_id": call["id"], "name": tool_name, "content": truncated_output}, raw_history)

                hits, mem_ids = await _inject_rag_hint_on_error(truncated_output, raw_history, injected_memory_ids, db, session_id, step)
                pending_rag_labels.extend(hits)
                used_rag_ids.update(mem_ids)

            session_logger.log_step(step_record)

            if finish_called:
                session.status = "completed"
                db.commit()
                session_logger.log_session_summary("completed", final_result)
                # ▼ 成功時: 使用したノウハウを評価＋１
                rag_store.mark_memory_success(list(used_rag_ids), True)
                return final_result

        session.status = "failed"
        db.commit()
        session_logger.log_session_summary("failed", final_result)
        # ▼ 失敗時: 役に立たなかったノウハウとして評価－１
        rag_store.mark_memory_success(list(used_rag_ids), False)
        return final_result

    except asyncio.CancelledError:
        db.rollback()
        session = db.query(GoalSession).filter(GoalSession.id == session_id).first()
        if session:
            session.status = "cancelled"
            db.commit()
        if session_logger: session_logger.log_session_summary("cancelled", "Session cancelled by user.")
        raise
    except Exception as exc:
        db.rollback()
        err_msg = f"Agent failed with error: {exc}"
        session = db.query(GoalSession).filter(GoalSession.id == session_id).first()
        if session:
            session.status = "failed"
            db.commit()
        if session_logger: session_logger.log_session_summary("failed", err_msg)
        return err_msg
    finally:
        stop_docker_container(session_id)
        INTERVENTION_QUEUES.pop(session_id, None)
        db.close()
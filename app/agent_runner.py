import asyncio
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .context_builder import ContextBuilder
from .db_models import Activity, GoalSession, Planner, SessionMessage
from .db_system import get_db_session
from .llama_acc import (
    ask_llm,
    chat_with_tools,
    get_model_for_role,
    parse_any_tool_calls,
    select_optimal_model_for_step,
)
from .prompts_and_schemas import AGENT_SYSTEM_PROMPT, AVAILABLE_TOOLS
from .rag_store import rag_store
from .secrets import (
    get_max_output_chars,
    get_max_steps,
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
MAX_QA_REJECTIONS = 2

ERROR_INDICATORS: tuple[str, ...] = (
    "error:", "exception:", "traceback (most recent call last)", "failed:",
    "permission denied", "address already in use", "command not found", "timed out",
)
CODING_CRITICAL_TOOLS: set[str] = {
    "write_file",
    "replace_in_file",
    "insert_into_file",
    "record_troubleshooting",
}


def add_session_intervention(session_id: int, message: str) -> None:
    INTERVENTION_QUEUES.setdefault(session_id, []).append(message)


def _pop_user_interventions(session_id: int) -> list[str]:
    return INTERVENTION_QUEUES.pop(session_id, [])


def _sync_append_history(session_id: int, step: int, clean_msg: dict[str, Any]) -> None:
    with get_db_session() as db:
        sm = SessionMessage(
            session_id=session_id,
            step=step,
            role=clean_msg.get("role", "unknown"),
            message_json=json.dumps(clean_msg, ensure_ascii=False),
        )
        db.add(sm)
        db.commit()


async def _append_history(
    session_id: int, step: int, msg: dict[str, Any], raw_history: list[dict[str, Any]]
) -> None:
    clean_msg = {k: v for k, v in msg.items() if not k.startswith("_")}
    raw_history.append(clean_msg)
    await asyncio.to_thread(_sync_append_history, session_id, step, clean_msg)


def _sync_record_activity(session_id: int, tool_name: str, args: dict[str, Any], result: str) -> None:
    args_summary = json.dumps(args, ensure_ascii=False)[:300]
    with get_db_session() as db:
        activity = Activity(
            session_id=session_id,
            work=f"[{tool_name}] {args_summary}",
            result=result[:500],
        )
        db.add(activity)
        db.commit()


async def _record_activity(session_id: int, tool_name: str, args: dict[str, Any], result: str) -> None:
    await asyncio.to_thread(_sync_record_activity, session_id, tool_name, args, result)


def _sync_build_handover_context(session_id: int) -> str:
    with get_db_session() as db:
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


def _sync_init_session_state(session_id: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]], int]:
    with get_db_session() as db:
        session = db.query(GoalSession).filter(GoalSession.id == session_id).first()
        if not session:
            return None, [], 0

        project_path = get_workspace_dir()
        session.project_dir = str(project_path)

        project_path.mkdir(parents=True, exist_ok=True)
        init_git_repo(project_path)

        session.status = "running"
        db.commit()

        sess_info = {
            "id": session.id,
            "title": session.title,
            "objective": session.objective,
            "project_path": project_path,
            "is_forum": bool(session.is_forum),
        }

        db_msgs = (
            db.query(SessionMessage)
            .filter(SessionMessage.session_id == session_id)
            .order_by(SessionMessage.id)
            .all()
        )
        raw_history = [json.loads(m.message_json) for m in db_msgs]
        last_step = db_msgs[-1].step if db_msgs else 0
        return sess_info, raw_history, last_step


def _sync_update_session_status(session_id: int, status: str) -> None:
    with get_db_session() as db:
        session = db.query(GoalSession).filter(GoalSession.id == session_id).first()
        if session:
            session.status = status
            db.commit()


def extract_tool_call_from_text(content: str) -> dict[str, Any] | None:
    calls = parse_any_tool_calls(content)
    if calls:
        fn = calls[0].get("function", {})
        return {
            "name": fn.get("name"),
            "arguments": parse_tool_arguments(fn.get("arguments")),
        }
    return None


def parse_tool_arguments(raw_args: Any) -> dict[str, Any]:
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


async def _inject_rag_hint_on_error(
    tool_output: str,
    raw_history: list[dict[str, Any]],
    injected_memory_ids: set[int],
    session_id: int,
    step: int,
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

    rag_hint = rag_store.format_as_prompt_context(
        new_memories, header="RAG Auto-Retrieved Troubleshooting Hint"
    )
    await _append_history(session_id, step, {"role": "system", "content": rag_hint}, raw_history)
    return hit_labels, used_ids


async def _request_llm_with_metrics(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    step: int,
    objective: str,
    raw_history: list[dict[str, Any]],
    dashboard_callback: Callable | None = None,
    max_steps: int = 40,
    max_retries: int = 2,
) -> tuple[dict[str, Any], float, int, str, str]:
    start_t = time.perf_counter()
    working_messages = list(messages)

    for attempt in range(max_retries):
        role, model_name = await select_optimal_model_for_step(
            step=step,
            objective=objective,
            raw_history=raw_history,
            retry_attempt=attempt,
        )
        if dashboard_callback:
            await dashboard_callback(
                step=step,
                max_steps=max_steps,
                status=f"Thinking [{role}: {model_name}]...",
            )

        try:
            response_msg = await chat_with_tools(
                working_messages, tools=tools, role=role, model=model_name
            )

            coding_model = get_model_for_role("coding")
            if model_name != coding_model:
                t_calls = response_msg.get("tool_calls") or []
                called_names = {tc.get("function", {}).get("name", "") for tc in t_calls}
                needs_coder_escalation = bool(called_names & CODING_CRITICAL_TOOLS) or not t_calls

                if needs_coder_escalation:
                    role = "coding"
                    model_name = coding_model
                    if dashboard_callback:
                        await dashboard_callback(
                            step=step,
                            max_steps=max_steps,
                            status=f"Escalating to Coder [{model_name}]...",
                        )
                    response_msg = await chat_with_tools(
                        working_messages, tools=tools, role="coding", model=coding_model
                    )

            latency = time.perf_counter() - start_t
            return response_msg, latency, attempt, role, model_name
        except Exception as exc:
            if attempt + 1 >= max_retries:
                raise
            await asyncio.sleep(2)
            working_messages = [
                *messages,
                {"role": "system", "content": f"Previous generation failed ({exc}). Output valid tool calls."},
            ]
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


def _sync_check_planner_future_issue(session_id: int) -> str | None:
    with get_db_session() as db:
        latest_plan = (
            db.query(Planner)
            .filter(Planner.session_id == session_id)
            .order_by(Planner.id.desc())
            .first()
        )
        if latest_plan and latest_plan.future_issue:
            fi = latest_plan.future_issue.strip()
            if fi.lower() not in ["none", "なし", "n/a", "", "null", "nothing"]:
                return fi
    return None


async def _run_auto_qa_review(
    session_id: int,
    objective: str,
    raw_history: list[dict[str, Any]],
) -> tuple[bool, str]:
    pending_issue = await asyncio.to_thread(_sync_check_planner_future_issue, session_id)
    if pending_issue:
        return (
            False,
            f"System Guard: Planner still has an unresolved future_issue: '{pending_issue}'. "
            "Resolve it and update_plan_result with future_issue='None' before finishing.",
        )

    history_snippets = []
    for m in raw_history[-12:]:
        role = m.get("role", "")
        if role == "tool":
            history_snippets.append(f"Tool {m.get('name')} returned: {str(m.get('content'))[:300]}")
        elif role == "assistant" and "tool_calls" in m:
            calls = [tc.get("function", {}).get("name") for tc in m.get("tool_calls", [])]
            history_snippets.append(f"Agent called: {calls}")
    history_text = "\n".join(history_snippets)

    qa_prompt = (
        "You are a pragmatic QA Reviewer.\n"
        f"Original Objective: {objective}\n\n"
        "Recent Agent Actions:\n"
        f"{history_text}\n\n"
        "Evaluate if the core objective is implemented and verified.\n"
        "Output ONLY a JSON object:\n"
        '{"approved": true or false, "reason": "Specific actionable fix needed, or Approved."}'
    )

    qa_result = await ask_llm(qa_prompt, role="coding")
    try:
        clean_json = re.sub(r"```(?:json)?\s*(.*?)\s*```", r"\1", qa_result, flags=re.DOTALL).strip()
        match = re.search(r"\{.*\}", clean_json, flags=re.DOTALL)
        data = json.loads(match.group(0) if match else clean_json)
        approved = bool(data.get("approved", False))
        reason = data.get("reason", "Missing requirements.")
        if not approved:
            return False, f"QA Review Rejected: {reason}"
        return True, "QA Approved."
    except Exception:
        return True, "QA Approved (auto-passed due to reviewer parse fallback)."


async def run_agent_loop(
    session_id: int,
    max_steps: int | None = None,
    dashboard_callback: Callable | None = None,
    human_ask_callback: Callable | None = None,
    infinity_mode: bool = False,
) -> str:
    if max_steps is None:
        max_steps = get_max_steps()

    session_logger: SessionFileLogger | None = None
    final_result = f"Reached maximum allowed steps ({max_steps}) without calling finish_task."
    used_rag_ids: set[int] = set()
    qa_rejection_count = 0

    active_tools = [t for t in AVAILABLE_TOOLS if t["function"]["name"] != "finish_task"] if infinity_mode else AVAILABLE_TOOLS

    try:
        sess_info, raw_history, last_step = await asyncio.to_thread(_sync_init_session_state, session_id)
        if not sess_info:
            return "Session not found."

        project_path: Path = sess_info["project_path"]
        objective: str = sess_info["objective"]

        session_logger = SessionFileLogger(
            session_id=session_id,
            title=sess_info["title"],
            objective=objective,
            workspace_dir=project_path,
        )

        if raw_history:
            last_msg = raw_history[-1]
            if last_msg.get("role") == "assistant" and "tool_calls" in last_msg:
                for call in last_msg["tool_calls"]:
                    recovery_msg = {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": call["function"]["name"],
                        "content": (
                            "System Interruption: The bot restarted while waiting for this tool. "
                            "Please execute the tool or ask the human again if still needed."
                        ),
                    }
                    await _append_history(session_id, last_step, recovery_msg, raw_history)

        initial_memories = await rag_store.search_relevant_memories(objective)
        injected_memory_ids = {m.id for m in initial_memories}
        used_rag_ids.update(injected_memory_ids)
        pending_rag_labels = [f"[{m.category}] (ID:{m.id})" for m in initial_memories]

        system_content = AGENT_SYSTEM_PROMPT
        
        if infinity_mode:
            system_content += (
                "\n\n[Infinity Mode Directive]: The 'finish_task' tool has been removed. You operate in an infinite loop. "
                "When you complete a task or need new directions, you MUST use 'ask_human' to report progress and ask the user what to do next. "
                "Never attempt to call 'finish_task'."
            )
        elif sess_info["is_forum"]:
            system_content += (
                "\n\n[Forum Mode Directive]: You are operating in a persistent discussion thread. "
                "Do NOT use 'finish_task' unless explicitly requested to close the project. "
                "Use 'ask_human' to report progress and ask for next instructions."
            )

        signature_repeat_counts: dict[str, int] = {}

        for step in range(1, max_steps + 1):
            interventions = _pop_user_interventions(session_id)
            for inv in interventions:
                await _append_history(
                    session_id,
                    step,
                    {"role": "user", "content": f"[User Mid-Course Correction]: {inv}"},
                    raw_history,
                )

            handover_ctx = await asyncio.to_thread(_sync_build_handover_context, session_id)
            builder = ContextBuilder(get_prompt_token_limit())
            builder.set_system(system_content)
            builder.set_objective(objective)
            builder.add_rag_or_plan(rag_store.format_as_prompt_context(initial_memories))
            builder.add_rag_or_plan(handover_ctx)

            prompt_messages, compressed, handover_text = builder.build_messages(raw_history)

            response_msg, latency_sec, retries, selected_role, selected_model = await _request_llm_with_metrics(
                messages=prompt_messages,
                tools=active_tools,
                step=step,
                objective=objective,
                raw_history=raw_history,
                dashboard_callback=dashboard_callback,
                max_steps=max_steps,
            )
            actual_usage = response_msg.pop("_usage", None)
            response_msg.pop("_model_used", None)

            log_token_usage(step, max_steps, session_id, prompt_messages, active_tools, actual_usage)

            content = (response_msg.get("content") or "").strip()
            tool_calls = response_msg.get("tool_calls")
            dispatch_mode = (
                f"native_tool_calls ({selected_role}:{selected_model})"
                if tool_calls
                else f"text_only ({selected_role}:{selected_model})"
            )

            if not tool_calls:
                fallback_tool = extract_tool_call_from_text(content)
                if fallback_tool and fallback_tool.get("name"):
                    dispatch_mode = f"fallback_parsed ({selected_role}:{selected_model})"
                    tool_calls = [
                        {
                            "id": f"fallback_call_{step}",
                            "type": "function",
                            "function": {
                                "name": fallback_tool["name"],
                                "arguments": json.dumps(fallback_tool["arguments"], ensure_ascii=False),
                            },
                        }
                    ]
                    response_msg["tool_calls"] = tool_calls

            inference_telemetry = session_logger.build_inference_telemetry(
                prompt_messages, active_tools, actual_usage, latency_sec, retries, dispatch_mode, content
            )
            step_record = StepLogRecord(
                step=step,
                max_steps=max_steps,
                interventions=interventions,
                context_compressed=compressed,
                handover_snapshot=handover_text,
                rag_injected_titles=[f"Model:{selected_model}({selected_role})", *pending_rag_labels],
                inference=inference_telemetry,
            )
            pending_rag_labels.clear()

            await _append_history(session_id, step, response_msg, raw_history)

            if dashboard_callback:
                await dashboard_callback(
                    step=step,
                    max_steps=max_steps,
                    thought=content,
                    status=f"Tool Dispatching [{selected_role}: {selected_model}]...",
                )

            if not tool_calls:
                await asyncio.to_thread(session_logger.log_step, step_record)
                await _append_history(
                    session_id,
                    step,
                    {
                        "role": "user",
                        "content": "Directly invoke a tool using the function calling schema. Do NOT end with text only.",
                    },
                    raw_history,
                )
                continue

            finish_called = False
            current_step_signatures: set[str] = set()

            for call in tool_calls:
                tool_name = call["function"]["name"]
                args = parse_tool_arguments(call["function"].get("arguments"))
                current_sig = f"{tool_name}:{json.dumps(args, sort_keys=True)}"

                signature_repeat_counts[current_sig] = signature_repeat_counts.get(current_sig, 0) + 1
                current_step_signatures.add(current_sig)
                
                is_repeat_blocked = signature_repeat_counts[current_sig] >= 2

                if dashboard_callback:
                    await dashboard_callback(
                        step=step,
                        max_steps=max_steps,
                        thought=content,
                        tool_name=f"{tool_name} ({selected_model})",
                        tool_args=args,
                        status=f"Executing {tool_name} [{selected_role}]...",
                    )

                tool_start_t = time.perf_counter()

                if tool_name == "finish_task" and infinity_mode:
                    tool_output = "Error: 'finish_task' is disabled in Infinity Mode. Use 'ask_human' instead."
                    tool_status = "ERROR"
                    is_repeat_blocked = False
                elif is_repeat_blocked:
                    tool_output = f"Error: Repetitive action detected for '{tool_name}'. DO NOT repeat the exact same action. Analyze the human's response or previous error, and change your approach."
                    tool_status = "BLOCKED_REPEAT"
                else:
                    tool_output = await execute_tool_async(
                        tool_name,
                        args,
                        session_id=session_id,
                        project_dir=project_path,
                        human_ask_callback=human_ask_callback,
                    )
                    tool_status = _classify_tool_status(tool_output, False)

                tool_duration = time.perf_counter() - tool_start_t
                truncated_output = _truncate_output(tool_output)

                if tool_name == "finish_task" and not infinity_mode:
                    if qa_rejection_count >= MAX_QA_REJECTIONS:
                        approved = True
                        qa_msg = f"QA Auto-Approved (reached max QA review limit of {MAX_QA_REJECTIONS})."
                    else:
                        if dashboard_callback:
                            await dashboard_callback(
                                step=step,
                                max_steps=max_steps,
                                thought=content,
                                tool_name="System QA Guard",
                                tool_args={},
                                status="Running QA Review...",
                            )
                        approved, qa_msg = await _run_auto_qa_review(session_id, objective, raw_history)

                    if not approved:
                        qa_rejection_count += 1
                        tool_output = (
                            f"Error (System Guard [{qa_rejection_count}/{MAX_QA_REJECTIONS}]): "
                            f"finish_task rejected.\n{qa_msg}\nPlease address this issue and try again."
                        )
                        truncated_output = _truncate_output(tool_output)
                        tool_status = "ERROR"
                        finish_called = False
                    else:
                        tool_output = f"{tool_output}\n\n[System QA]: {qa_msg}"
                        truncated_output = _truncate_output(tool_output)
                        final_result = tool_output
                        finish_called = True

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
                        tool_name=f"{tool_name} ({selected_model})",
                        tool_args=args,
                        tool_output=truncated_output,
                        status=f"Step Completed [{selected_role}]",
                    )

                await _record_activity(session_id, f"{tool_name}@{selected_model}", args, truncated_output)
                await _append_history(
                    session_id,
                    step,
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": tool_name,
                        "content": truncated_output,
                    },
                    raw_history,
                )

                hits, mem_ids = await _inject_rag_hint_on_error(
                    truncated_output, raw_history, injected_memory_ids, session_id, step
                )
                pending_rag_labels.extend(hits)
                used_rag_ids.update(mem_ids)
                
            signature_repeat_counts = {k: v for k, v in signature_repeat_counts.items() if k in current_step_signatures}

            await asyncio.to_thread(session_logger.log_step, step_record)

            if finish_called:
                await asyncio.to_thread(_sync_update_session_status, session_id, "completed")
                await asyncio.to_thread(session_logger.log_session_summary, "completed", final_result)
                await asyncio.to_thread(rag_store.mark_memory_success, list(used_rag_ids), True)
                return final_result

        await asyncio.to_thread(_sync_update_session_status, session_id, "failed")
        await asyncio.to_thread(session_logger.log_session_summary, "failed", final_result)
        await asyncio.to_thread(rag_store.mark_memory_success, list(used_rag_ids), False)
        return final_result

    except asyncio.CancelledError:
        await asyncio.to_thread(_sync_update_session_status, session_id, "cancelled")
        if session_logger:
            await asyncio.to_thread(session_logger.log_session_summary, "cancelled", "Session cancelled by user.")
        raise
    except Exception as exc:
        err_msg = f"Agent failed with error: {exc}"
        await asyncio.to_thread(_sync_update_session_status, session_id, "failed")
        if session_logger:
            await asyncio.to_thread(session_logger.log_session_summary, "failed", err_msg)
        return err_msg
    finally:
        await asyncio.to_thread(stop_docker_container, session_id)
        INTERVENTION_QUEUES.pop(session_id, None)
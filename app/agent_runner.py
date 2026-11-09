import asyncio
import json
import re
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
    get_max_steps,
    get_max_token,
    get_projects_dir,
    get_server_ctx_size,
)
from .tools import execute_tool_async, init_git_repo, stop_docker_container

INTERVENTION_QUEUES: dict[int, list[str]] = {}

MAX_OUTPUT_CHARS = 4000
COMPLETION_KEYWORDS = ("完了", "finished", "complete")
ERROR_INDICATORS = (
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
    INTERVENTION_QUEUES.setdefault(session_id, []).append(message)


def extract_tool_call_from_text(content: str) -> dict[str, Any] | None:
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


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    total_chars = sum(len(str(m.get("content", ""))) for m in messages)
    return int(total_chars / 2.5) + 600


def _prepare_session_workspace(session: GoalSession) -> Path:
    if not session.project_dir:
        project_path = get_projects_dir() / f"session_{session.id}"
        session.project_dir = str(project_path)
    else:
        project_path = Path(session.project_dir)

    project_path.mkdir(parents=True, exist_ok=True)
    init_git_repo(project_path)
    return project_path


def _inject_user_interventions(
    session_id: int, messages: list[dict[str, Any]]
) -> None:
    if not INTERVENTION_QUEUES.get(session_id):
        return
    interventions = INTERVENTION_QUEUES.pop(session_id)
    for inject_msg in interventions:
        messages.append(
            {
                "role": "user",
                "content": f"[User Mid-Course Correction / Note]: {inject_msg}",
            }
        )


def _compress_context_if_needed(
    db: Session,
    session: GoalSession,
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    prompt_token_limit = get_server_ctx_size() - get_max_token() - 1024
    if len(messages) <= 16 and estimate_tokens(messages) <= prompt_token_limit:
        return messages

    handover_note = build_handover_context(db, session.id)
    return [
        messages[0],
        {"role": "user", "content": f"Task: {session.objective}"},
        {"role": "system", "content": handover_note},
        *messages[-8:],
    ]


async def _inject_rag_hint_on_error(
    tool_output: str,
    messages: list[dict[str, Any]],
    injected_memory_ids: set[int],
) -> None:
    """ツール出力にエラー兆候がある場合，ベクターストアを動的検索して解決ヒントを注入する．"""
    lower_out = tool_output.lower()
    if not any(indicator in lower_out for indicator in ERROR_INDICATORS):
        return

    matched_memories = await rag_store.search_relevant_memories(tool_output)
    new_memories = [m for m in matched_memories if m.id not in injected_memory_ids]
    if not new_memories:
        return

    for m in new_memories:
        injected_memory_ids.add(m.id)

    rag_hint = rag_store.format_as_prompt_context(
        new_memories, header="RAG Auto-Retrieved Troubleshooting Hint"
    )
    messages.append({"role": "system", "content": rag_hint})


async def _request_llm_with_retry(
    messages: list[dict[str, Any]], max_retries: int = 2
) -> dict[str, Any]:
    for attempt in range(max_retries):
        try:
            return await chat_with_tools(messages, tools=AVAILABLE_TOOLS)
        except Exception as exc:
            if attempt + 1 >= max_retries:
                raise
            await asyncio.sleep(2)
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Tool execution failed with server error ({exc}). "
                        "Retry with concise parameters."
                    ),
                }
            )
    raise RuntimeError("Unreachable retry state")


def _truncate_output(output: str) -> str:
    if len(output) > MAX_OUTPUT_CHARS:
        return output[:MAX_OUTPUT_CHARS] + "\n...(output truncated)..."
    return output


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
    try:
        session = db.query(GoalSession).filter(GoalSession.id == session_id).first()
        if not session:
            return "Session not found."

        project_path = _prepare_session_workspace(session)
        session.status = "running"
        db.commit()

        # 初期 RAG 検索: タスク目標に関連する過去のトラブルシューティング知見を抽出
        initial_memories = await rag_store.search_relevant_memories(session.objective)
        injected_memory_ids: set[int] = {m.id for m in initial_memories}
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

        final_result = (
            f"Reached maximum allowed steps ({max_steps}) without calling finish_task."
        )
        last_tool_signature: str | None = None
        repeat_count = 0

        for step in range(1, max_steps + 1):
            _inject_user_interventions(session_id, messages)
            messages = _compress_context_if_needed(db, session, messages)

            if dashboard_callback:
                await dashboard_callback(
                    step=step,
                    max_steps=max_steps,
                    status="Thinking (LLM推論中)...",
                )

            response_msg = await _request_llm_with_retry(messages)
            messages.append(response_msg)

            content = response_msg.get("content", "").strip()
            tool_calls = response_msg.get("tool_calls")

            if dashboard_callback:
                await dashboard_callback(
                    step=step,
                    max_steps=max_steps,
                    thought=content,
                    status="Tool Dispatching...",
                )

            if not tool_calls:
                fallback_tool = extract_tool_call_from_text(content)
                if fallback_tool:
                    tool_calls = [
                        {
                            "id": f"fallback_call_{step}",
                            "function": {
                                "name": fallback_tool["name"],
                                "arguments": json.dumps(fallback_tool["arguments"]),
                            },
                        }
                    ]
                elif content and any(k in content.lower() for k in COMPLETION_KEYWORDS):
                    session.status = "completed"
                    db.commit()
                    return content
                else:
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

                if repeat_count >= 1:
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

                truncated_output = _truncate_output(tool_output)

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

                # エラー出力検知時に RAG ベクターストアから解決ヒントを動的注入
                await _inject_rag_hint_on_error(
                    truncated_output, messages, injected_memory_ids
                )

            if finish_called:
                session.status = "completed"
                db.commit()
                return final_result

        session.status = "failed"
        db.commit()
        return final_result

    except asyncio.CancelledError:
        db.rollback()
        session = db.query(GoalSession).filter(GoalSession.id == session_id).first()
        if session:
            session.status = "cancelled"
            db.commit()
        raise

    except Exception as exc:
        db.rollback()
        session = db.query(GoalSession).filter(GoalSession.id == session_id).first()
        if session:
            session.status = "failed"
            db.commit()
        return f"Agent failed with error: {exc}"

    finally:
        stop_docker_container(session_id)
        INTERVENTION_QUEUES.pop(session_id, None)
        db.close()
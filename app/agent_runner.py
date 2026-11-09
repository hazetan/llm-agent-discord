import asyncio
import json
import re
from pathlib import Path

from .db_models import Activity, GoalSession, Planner
from .db_system import SessionLocal
from .llama_acc import chat_with_tools
from .secret import (
    get_max_steps,
    get_max_token,
    get_projects_dir,
    get_server_ctx_size,
)
from .tools import (
    AVAILABLE_TOOLS,
    execute_tool_async,
    init_git_repo,
    stop_docker_container,
)

# セッションごとの割り込みキュー (session_id -> list[str])
INTERVENTION_QUEUES: dict[int, list[str]] = {}


def add_session_intervention(session_id: int, message: str):
    INTERVENTION_QUEUES.setdefault(session_id, []).append(message)


def extract_tool_call_from_text(content: str) -> dict | None:
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            if "name" in data and "arguments" in data:
                return data
        except Exception:
            pass
    return None


def parse_tool_arguments(raw_args) -> dict:
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def build_handover_context(db, session_id: int) -> str:
    latest_plan = (
        db.query(Planner)
        .filter(Planner.session_id == session_id)
        .order_by(Planner.id.desc())
        .first()
    )
    if not latest_plan:
        return "No persistent planner history available."

    return (
        f"--- [Project State Handover] ---\n"
        f"Previous Issue : {latest_plan.current_issue}\n"
        f"Executed Task  : {latest_plan.task}\n"
        f"Task Result    : {latest_plan.result or 'In progress'}\n"
        f"Next Backlog   : {latest_plan.future_issue or 'None'}\n"
        f"--------------------------------"
    )


def estimate_tokens(messages: list) -> int:
    total_chars = sum(len(str(m.get("content", ""))) for m in messages)
    return int(total_chars / 2.5) + 600


async def run_agent_loop(
    session_id: int,
    max_steps: int | None = None,
    dashboard_callback=None,
    human_ask_callback=None,
) -> str:
    if max_steps is None:
        max_steps = get_max_steps()

    db = SessionLocal()
    project_path = None
    try:
        session = db.query(GoalSession).filter(GoalSession.id == session_id).first()
        if not session:
            return "Session not found."

        if not session.project_dir:
            project_path = get_projects_dir() / f"session_{session_id}"
            project_path.mkdir(parents=True, exist_ok=True)
            session.project_dir = str(project_path)
        else:
            project_path = Path(session.project_dir)
            project_path.mkdir(parents=True, exist_ok=True)

        session.status = "running"
        db.commit()

        # Git リポジトリ初期化
        init_git_repo(project_path)

        messages = [
            {
                "role": "system",
                "content": (
                    "You are an elite autonomous engineering agent operating inside an isolated Docker sandbox.\n"
                    "Environment constraints:\n"
                    "- All command executions strictly run in Docker container (/workspace).\n"
                    "- Before starting complex operations, use 'record_plan' to document issue and tasks.\n"
                    "- Use 'list_files' to inspect directory and 'read_file' to view code.\n"
                    "- For modifying existing files, PREFER 'replace_in_file' for small edits or 'write_file' for whole files.\n"
                    "- Keep code modular. If implementing complex applications (like games or web apps), divide logic into separate files (e.g. state.js, pieces.js, ui.js) instead of dumping thousands of lines into a single call.\n"
                    "- Ensure that JSON payloads and string contents are completed and not abruptly cut off.\n"
                    "- When developing large single-page applications or complex HTML/JS:\n"
                    "  1. Create a minimal skeleton first via 'write_file' (e.g. basic HTML structure with placeholders or comments like '<!-- JS_LOGIC -->' or '</body>').\n"
                    "  2. Use 'insert_into_file' to incrementally inject functions, event listeners, and data models in small chunks instead of overwriting the whole file.\n"
                    "  3. This avoids output token truncation and keeps tool calls concise.\n"
                    "- Use 'git_diff' to inspect code changes and 'git_revert' if an action broke the project.\n"
                    "- When using 'web_search', keep queries concise and limited to 2-3 key terms\n"
                    "- If you lack domain info or need latest docs, invoke 'web_search' or 'fetch_url'.\n"
                    "- If requirements or credentials are truly missing, ask human directly via 'ask_human'.\n"
                    "- Test code syntax and behavior via 'run_command'.\n"
                    "- Conclude sub-steps using 'update_plan_result'.\n"
                    "- Once fully verified and satisfied, call 'finish_task'. The 'result' parameter MUST be written in polite, clear Japanese detailing accomplishments and key files.\n"
                    "- IMPORTANT: Invoke tools directly. Do NOT output markdown code blocks in text."
                ),
            },
            {
                "role": "user",
                "content": f"Task Objective: {session.objective}",
            },
        ]

        final_result = f"Reached maximum allowed steps ({max_steps}) without calling finish_task."
        last_tool_signature = None
        repeat_count = 0

        for step in range(1, max_steps + 1):
            # ユーザーからの途中割り込み指示があればプロンプトにインジェクト
            if INTERVENTION_QUEUES.get(session_id):
                interventions = INTERVENTION_QUEUES.pop(session_id)
                for inject_msg in interventions:
                    messages.append(
                        {
                            "role": "user",
                            "content": f"[User Mid-Course Correction / Note]: {inject_msg}",
                        }
                    )

            server_ctx_size = get_server_ctx_size()
            current_max_token = get_max_token()
            prompt_token_limit = server_ctx_size - current_max_token - 1024

            # コンテキスト自動圧縮
            if len(messages) > 16 or estimate_tokens(messages) > prompt_token_limit:
                handover_note = build_handover_context(db, session_id)
                messages = [
                    messages[0],
                    {"role": "user", "content": f"Task: {session.objective}"},
                    {"role": "system", "content": handover_note},
                ] + messages[-8:]

            if dashboard_callback:
                await dashboard_callback(
                    step=step,
                    max_steps=max_steps,
                    status="Thinking (LLM推論中)...",
                )

            retry_count = 0
            response_msg = None
            while retry_count < 2:
                try:
                    response_msg = await chat_with_tools(
                        messages, tools=AVAILABLE_TOOLS
                    )
                    break
                except Exception as e:
                    retry_count += 1
                    if retry_count >= 2:
                        raise e
                    await asyncio.sleep(2)
                    messages.append(
                        {
                            "role": "user",
                            "content": f"Tool execution failed with server error ({e}). Retry with concise parameters.",
                        }
                    )

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

            # ツール呼び出し無しの場合のフォールバック
            if not tool_calls:
                fallback_tool = extract_tool_call_from_text(content)
                if fallback_tool:
                    tool_calls = [
                        {
                            "id": f"fallback_call_{step}",
                            "function": {
                                "name": fallback_tool["name"],
                                "arguments": json.dumps(
                                    fallback_tool["arguments"]
                                ),
                            },
                        }
                    ]
                else:
                    if content and any(
                        w in content.lower()
                        for w in ["完了", "finished", "complete"]
                    ):
                        final_result = content
                        session.status = "completed"
                        db.commit()
                        return final_result

                    messages.append(
                        {
                            "role": "user",
                            "content": "Do not explain your steps in text. Directly invoke the appropriate tool using the function calling schema.",
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
                        f"Error: Repetitive action detected for '{tool_name}'. Proceed to next verification "
                        f"step or call 'finish_task'."
                    )
                else:
                    tool_output = await execute_tool_async(
                        tool_name,
                        args,
                        session_id=session_id,
                        project_dir=project_path,
                        human_ask_callback=human_ask_callback,
                    )

                truncated_output = (
                    tool_output[:4000] + "\n...(output truncated)..."
                    if len(tool_output) > 4000
                    else tool_output
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

                act = Activity(
                    session_id=session_id,
                    work=f"[{tool_name}] {json.dumps(args, ensure_ascii=False)[:300]}",
                    result=truncated_output[:500],
                )
                db.add(act)
                db.commit()

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

    except Exception as e:
        db.rollback()
        session = db.query(GoalSession).filter(GoalSession.id == session_id).first()
        if session:
            session.status = "failed"
            db.commit()
        return f"Agent failed with error: {e}"

    finally:
        # Docker コンテナの破棄
        stop_docker_container(session_id)
        INTERVENTION_QUEUES.pop(session_id, None)
        db.close()

from typing import Any

from core.config import get_max_steps_per_phase, get_sandbox_env_hint
from core.llm_client import chat_step
from core.logger import RoleLogger
from core.telemetry import telemetry
from sandbox.container import current_worker_ctx
from sandbox.tools import execute_tool, parse_tool_args, read_workspace_doc

from .roles import ROLE_ALLOWED_TOOLS, ROLE_PROMPTS, get_tools_for_role


def build_fresh_workspace_snapshot() -> str:
    """毎回最新の社内標準ドキュメント4点を読み込んでプロンプトに注入する"""
    readme = read_workspace_doc("~/README.md")
    task = read_workspace_doc("~/TASK.md")
    design = read_workspace_doc("~/DESIGN.md")
    report = read_workspace_doc("~/REPORT.md")

    return (
        "=== 現在の社内標準ドキュメント (Single Source of Truth) ===\n"
        f"[~/README.md]\n{readme}\n\n"
        f"[~/TASK.md]\n{task}\n\n"
        f"[~/DESIGN.md]\n{design}\n\n"
        f"[~/REPORT.md]\n{report}\n"
        "=========================================================="
    )


async def run_role_phase(
    role: str,
    instruction: str,
    max_steps: int | None = None,
    worker_id: str | None = None,
) -> tuple[str, list[str]]:
    role = role.upper()
    w_id = (worker_id or role).upper()
    token = current_worker_ctx.set(w_id)
    telemetry.set_worker_active(w_id, role, True)

    steps_limit = max_steps or get_max_steps_per_phase()
    logger = RoleLogger(w_id)
    tools = get_tools_for_role(ROLE_ALLOWED_TOOLS[role])

    system_prompt = ROLE_PROMPTS[role]
    if role in ("SE", "PG", "QA"):
        env_hint = get_sandbox_env_hint()
        if env_hint:
            system_prompt += f"\n\n{env_hint}"

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": build_fresh_workspace_snapshot()},
        {"role": "user", "content": instruction},
    ]

    created_zips: list[str] = []
    final_summary = ""
    header_logged = False

    try:
        for step in range(1, steps_limit + 1):
            try:
                response_msg, latency, model_name = await chat_step(role, messages, tools=tools)
                if not header_logged:
                    logger.log_phase_start(model_name, instruction[:200])
                    header_logged = True
            except Exception as exc:
                logger.log_error(str(exc))
                return f"推論エラーが発生しました ({role}): {exc}", created_zips

            thought = (response_msg.get("content") or "").strip()
            tool_calls = response_msg.get("tool_calls") or []
            messages.append(response_msg)

            if not tool_calls:
                logger.log_step(step, thought, latency_sec=latency)
                telemetry.record_raw_step(role, model_name, step, steps_limit, latency, thought)
                if telemetry.on_raw_update:
                    await telemetry.on_raw_update()

                messages.append({
                    "role": "user",
                    "content": "ツールを直接呼び出して作業を進めるか、`complete_phase` を呼び出して完了してください。",
                })
                continue

            phase_done = False
            for call in tool_calls:
                fn_name = call["function"]["name"]
                raw_args = call["function"].get("arguments", {})
                parsed_args = parse_tool_args(raw_args)

                output = await execute_tool(fn_name, parsed_args)
                logger.log_step(step, thought, fn_name, parsed_args, output, latency)
                telemetry.add_branch_event(w_id, fn_name, parsed_args, output)

                telemetry.record_raw_step(
                    w_id, model_name, step, steps_limit, latency, thought, fn_name, parsed_args, output
                )
                if telemetry.on_raw_update:
                    await telemetry.on_raw_update()

                if output.startswith("ZIP_CREATED:"):
                    created_zips.append(output.split("ZIP_CREATED:", 1)[1].strip())

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", f"call_{step}"),
                    "name": fn_name,
                    "content": output[:6000],
                })

                if fn_name == "complete_phase":
                    final_summary = output
                    phase_done = True
                    break

            if phase_done:
                break

        return final_summary, created_zips
    finally:
        telemetry.set_worker_active(w_id, role, False)
        current_worker_ctx.reset(token)
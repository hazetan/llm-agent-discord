from typing import Any

from core.config import (
    get_max_ctx_chars_budget,
    get_max_steps_per_phase,
    get_sandbox_env_hint,
)
from core.llm_client import chat_step
from core.logger import RoleLogger, system_logger
from core.telemetry import telemetry
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


def _enforce_context_budget(messages: list[dict[str, Any]], budget_chars: int) -> None:
    """
    累積メッセージ長が256kコンテキスト予算を超えないよう、
    古い tool 出力メッセージから順に中間を折りたたんでシステムプロンプトを保護する。
    """
    def _total_chars() -> int:
        return sum(len(str(m.get("content") or "")) for m in messages)

    if _total_chars() <= budget_chars:
        return

    # 直近2件のメッセージは保護し、古い tool メッセージから圧縮する
    for msg in messages[:-2]:
        if _total_chars() <= budget_chars:
            break
        if msg.get("role") == "tool":
            content = str(msg.get("content") or "")
            if len(content) > 4000:
                msg["content"] = (
                    content[:2000]
                    + f"\n...(older tool output compressed to preserve 256k context budget; {len(content)} chars)...\n"
                    + content[-1500:]
                )



async def run_role_phase(
    role: str,
    instruction: str,
    max_steps: int | None = None,
) -> tuple[str, list[str]]:
    role = role.upper()
    telemetry.start_role(role)

    steps_limit = max_steps or get_max_steps_per_phase()
    ctx_char_budget = get_max_ctx_chars_budget()
    max_tool_output_chars = int(ctx_char_budget * 0.7)
    logger = RoleLogger(role)
    tools = get_tools_for_role(ROLE_ALLOWED_TOOLS[role])

    system_logger.log("RUNNER", f"▶ Start role={role} | max_steps={steps_limit} | ctx_char_budget={ctx_char_budget:,}")

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
            _enforce_context_budget(messages, ctx_char_budget)
            system_logger.log("RUNNER", f"[{role}] Step {step}/{steps_limit}: Calling chat_step (msgs={len(messages)})...")
            try:
                response_msg, latency, model_name, tps = await chat_step(role, messages, tools=tools)
                if not header_logged:
                    logger.log_phase_start(model_name, instruction[:200])
                    header_logged = True
            except Exception as exc:
                logger.log_error(str(exc))
                system_logger.error("RUNNER", f"[{role}] Step {step} chat_step failed: {exc}", exc)
                raise

            thought = (response_msg.get("content") or "").strip()
            tool_calls = response_msg.get("tool_calls") or []
            messages.append(response_msg)

            system_logger.log(
                "RUNNER",
                f"[{role}] Step {step} response received in {latency:.2f}s ({tps:.1f} tok/s) | tool_calls={len(tool_calls)}",
            )

            if not tool_calls:
                logger.log_step(step, thought, latency_sec=latency, tps=tps)
                telemetry.record_raw_step(role, model_name, step, steps_limit, latency, thought, tps=tps)
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

                system_logger.log("RUNNER", f"[{role}] Step {step} executing tool: {fn_name}")
                output = await execute_tool(fn_name, parsed_args)
                system_logger.log("RUNNER", f"[{role}] Step {step} tool {fn_name} finished (output_len={len(output)})")

                logger.log_step(step, thought, fn_name, parsed_args, output, latency, tps=tps)
                telemetry.add_branch_event(role, fn_name, parsed_args, output)

                telemetry.record_raw_step(
                    role, model_name, step, steps_limit, latency, thought, fn_name, parsed_args, output, tps=tps
                )
                if telemetry.on_raw_update:
                    await telemetry.on_raw_update()

                if output.startswith("ZIP_CREATED:"):
                    created_zips.append(output.split("ZIP_CREATED:", 1)[1].strip())

                if len(output) > max_tool_output_chars:
                    trimmed_out = (
                        output[:max_tool_output_chars]
                        + f"\n...(truncated at {max_tool_output_chars:,} chars to fit within 256k context budget)..."
                    )
                else:
                    trimmed_out = output

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", f"call_{step}"),
                    "name": fn_name,
                    "content": trimmed_out,
                })

                if fn_name == "complete_phase":
                    final_summary = output
                    phase_done = True
                    break

            if phase_done:
                break

        system_logger.log("RUNNER", f"■ End role={role} | summary_len={len(final_summary)} | zips={len(created_zips)}")
        return final_summary, created_zips
    finally:
        telemetry.end_role(role)
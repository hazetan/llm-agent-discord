import json
from dataclasses import dataclass
from typing import Any

from .secrets import (
    get_chars_per_token,
    get_max_token,
    get_prompt_token_limit,
    get_server_ctx_size,
)


def estimate_item_tokens(text_or_obj: Any) -> int:
    """任意の文字列・辞書・リストから概算トークン数を算出する．"""
    if not text_or_obj:
        return 0
    if isinstance(text_or_obj, (dict, list)):
        raw_str = json.dumps(text_or_obj, ensure_ascii=False)
    else:
        raw_str = str(text_or_obj)

    chars_per_tok = get_chars_per_token()
    return max(1, int(len(raw_str) / chars_per_tok))


def estimate_messages_tokens(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> int:
    """メッセージ配列とツール定義の合計概算トークン数を返す．"""
    snapshot = TokenUsageSnapshot.from_context(messages, tools)
    return snapshot.prompt_total


def _format_pct(part: int, whole: int) -> str:
    if whole <= 0:
        return "  0.0 %"
    return f"{(part / whole) * 100:5.1f} %"


def _format_ctx_label(ctx_size: int) -> str:
    """65536 -> '64k', 16384 -> '16k' のようにコンテキストサイズ表記を動的生成する．"""
    if ctx_size >= 1024 and ctx_size % 1024 == 0:
        return f"{ctx_size // 1024}k"
    return f"{ctx_size:,}"


@dataclass
class TokenUsageSnapshot:
    """プロンプト内のカテゴリ別トークン消費内訳を保持するデータクラス．"""

    system_prompt: int = 0
    available_tools: int = 0
    task_objective: int = 0
    rag_and_handover: int = 0
    conversation_history: int = 0

    @property
    def prompt_total(self) -> int:
        return (
            self.system_prompt
            + self.available_tools
            + self.task_objective
            + self.rag_and_handover
            + self.conversation_history
        )

    @classmethod
    def from_context(
        cls,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> "TokenUsageSnapshot":
        snapshot = cls(available_tools=estimate_item_tokens(tools))

        for idx, msg in enumerate(messages):
            role = msg.get("role", "")
            content = str(msg.get("content") or "")
            tok_count = estimate_item_tokens(content)

            if msg.get("tool_calls"):
                tok_count += estimate_item_tokens(msg["tool_calls"])

            if idx == 0 and role == "system":
                snapshot.system_prompt += tok_count
            elif idx == 1 and role == "user":
                snapshot.task_objective += tok_count
            elif role == "system" and (
                "Handover" in content or "Troubleshooting" in content
            ):
                snapshot.rag_and_handover += tok_count
            else:
                snapshot.conversation_history += tok_count

        return snapshot


def log_token_usage(
    step: int,
    max_steps: int,
    session_id: int,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    actual_usage: dict[str, Any] | None = None,
) -> None:
    """
    各ステップのトークン消費内訳（プロンプト内の割合・サーバー上限に対する割合）と
    llama-server の実測値を整形してコンソールに出力する．
    """
    server_ctx = get_server_ctx_size()
    max_token = get_max_token()
    prompt_limit = get_prompt_token_limit()
    ctx_label = _format_ctx_label(server_ctx)

    snap = TokenUsageSnapshot.from_context(messages, tools)
    total = snap.prompt_total
    free_ctx = max(0, server_ctx - total)

    categories = [
        ("1. System Prompt", snap.system_prompt),
        ("2. Available Tools", snap.available_tools),
        ("3. Task Objective", snap.task_objective),
        ("4. RAG / Handover", snap.rag_and_handover),
        ("5. History & Outputs", snap.conversation_history),
    ]

    col_ctx_header = f"全体({ctx_label})占有率"
    border = "=" * 82
    divider = "-" * 82

    lines = [
        "",
        border,
        f"📊 [Token Usage Breakdown] Step {step}/{max_steps} (Session #{session_id})",
        divider,
        f"{'カテゴリ':<19} {'概算トークン':>8} {'プロンプト内割合':>8} {col_ctx_header}",
        divider,
    ]

    for label, count in categories:
        lines.append(
            f"{label:<24} {count:>10,} tok "
            f"{_format_pct(count, total):>16} "
            f"{_format_pct(count, server_ctx):>18}"
        )

    lines.extend(
        [
            divider,
            f"{'合計 (Prompt Total)':<22} {total:>10,} tok {'100.0 %':>16} {_format_pct(total, server_ctx):>18}",
            f"{'圧縮発動ライン (Limit)':<17} {prompt_limit:>10,} tok {'-':>16} {_format_pct(prompt_limit, server_ctx):>18}",
            f"{'サーバー上限 (Max CTX)':<18} {server_ctx:>10,} tok {'-':>16} ",
            f"{f'100.0 % (空き {_format_pct(free_ctx, server_ctx).strip()})':>18}",
        ]
    )

    if actual_usage:
        p_tok = actual_usage.get("prompt_tokens", 0)
        c_tok = actual_usage.get("completion_tokens", 0)
        t_tok = actual_usage.get("total_tokens", 0)
        lines.extend(
            [
                divider,
                "📈 [llama-server 実測値 (API Response)]",
                f"  • Input (Prompt)   : {p_tok:>8,} tok (全体 {ctx_label} の {_format_pct(p_tok, server_ctx).strip()})",
                f"  • Output (Gen)     : {c_tok:>8,} tok (MAX_TOKEN={max_token:,} の {_format_pct(c_tok, max_token).strip()} を消費)",
                f"  • Total Evaluated  : {t_tok:>8,} tok (全体 {ctx_label} の {_format_pct(t_tok, server_ctx).strip()})",
            ]
        )

    lines.append(border)
    print("\n".join(lines))
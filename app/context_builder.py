from typing import Any

from .token_monitor import estimate_item_tokens


class ContextBuilder:
    """LLMに送るコンテキストを、重要文脈を保護しつつトークン上限内に収めるクラス"""

    def __init__(self, max_prompt_tokens: int):
        self.max_prompt_tokens = max_prompt_tokens
        self.system_content = ""
        self.objective_content = ""
        self.rag_and_plan_blocks: list[str] = []

    def set_system(self, content: str) -> None:
        self.system_content = content

    def set_objective(self, content: str) -> None:
        self.objective_content = content

    def add_rag_or_plan(self, content: str) -> None:
        if content:
            self.rag_and_plan_blocks.append(content)

    def _summarize_pruned_messages(self, pruned: list[dict[str, Any]]) -> str:
        """切り捨てられた古いメッセージから、重要なユーザー指示と実行履歴の要約を作る"""
        user_interventions = []
        executed_tools = []

        for m in pruned:
            role = m.get("role", "")
            content = str(m.get("content") or "")
            if role == "user" and "Mid-Course Correction" in content:
                user_interventions.append(content)
            elif role == "tool":
                t_name = m.get("name", "tool")
                first_line = content.splitlines()[0][:80] if content else ""
                executed_tools.append(f"{t_name} -> {first_line}")

        summary_lines = [f"--- [Compressed Earlier History ({len(pruned)} messages pruned)] ---"]
        if user_interventions:
            summary_lines.append("Key User Corrections (MUST OBEY):")
            for ui in user_interventions[-5:]:
                summary_lines.append(f"  * {ui}")
        if executed_tools:
            summary_lines.append("Earlier Tool Executions (Digest):")
            for et in executed_tools[-10:]:
                summary_lines.append(f"  - {et}")
        summary_lines.append("---------------------------------------------------------------")
        return "\n".join(summary_lines)

    def build_messages(self, raw_history: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool, str]:
        messages = [
            {"role": "system", "content": self.system_content},
            {"role": "user", "content": f"Task Objective: {self.objective_content}"},
        ]

        handover_text = ""
        if self.rag_and_plan_blocks:
            handover_text = "\n\n".join(self.rag_and_plan_blocks)
            messages.append({"role": "system", "content": handover_text})

        base_tok = estimate_item_tokens(messages)
        # ダイジェスト挿入用の予備マージン 400 トークンを引いておく
        available_for_history = max(1200, self.max_prompt_tokens - base_tok - 400)

        selected_history: list[dict[str, Any]] = []
        current_hist_tok = 0
        cutoff_idx = 0

        for idx in range(len(raw_history) - 1, -1, -1):
            msg = raw_history[idx]
            msg_tok = estimate_item_tokens(msg)
            if current_hist_tok + msg_tok > available_for_history:
                cutoff_idx = idx + 1
                break
            selected_history.insert(0, msg)
            current_hist_tok += msg_tok

        # 先頭が孤立した role="tool" になった場合、APIエラーを防ぐため直前の assistant まで除外する
        while selected_history and selected_history[0].get("role") == "tool":
            selected_history.pop(0)
            cutoff_idx += 1

        compressed = cutoff_idx > 0
        if compressed:
            pruned_msgs = raw_history[:cutoff_idx]
            digest_text = self._summarize_pruned_messages(pruned_msgs)
            messages.append({"role": "system", "content": digest_text})

        messages.extend(selected_history)
        return messages, compressed, handover_text
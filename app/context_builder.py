from typing import Any

from .token_monitor import estimate_item_tokens


class ContextBuilder:
    """LLMに送るコンテキストを、トークン上限ギリギリまで最も賢く組み立てるクラス"""

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

    def build_messages(self, raw_history: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool, str]:
        """トークン制限を守りつつ、LLMに渡す最終的なリストを生成し (messages, compressed, handover) を返す"""
        messages = []
        messages.append({"role": "system", "content": self.system_content})
        messages.append({"role": "user", "content": f"Task Objective: {self.objective_content}"})

        handover_text = ""
        if self.rag_and_plan_blocks:
            handover_text = "\n\n".join(self.rag_and_plan_blocks)
            messages.append({"role": "system", "content": handover_text})

        # 固定ブロック（System, Objective, RAG/Plan）のトークンを計算
        base_tok = estimate_item_tokens(messages)
        
        # 履歴に使える残り枠（最低でも1000トークンは確保）
        available_for_history = max(1000, self.max_prompt_tokens - base_tok)

        selected_history = []
        current_hist_tok = 0
        compressed = False

        # 履歴を「最新のものから」順に遡って追加していく
        for msg in reversed(raw_history):
            msg_tok = estimate_item_tokens(msg)
            # 枠からはみ出る場合は、それより古い記憶を打ち切る（圧縮発動）
            if current_hist_tok + msg_tok > available_for_history:
                compressed = True
                break
            
            selected_history.insert(0, msg)
            current_hist_tok += msg_tok

        messages.extend(selected_history)
        return messages, compressed, handover_text
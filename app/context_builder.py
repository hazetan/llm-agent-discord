class ContextBuilder:
    """LLMに送るコンテキストを、トークン上限ギリギリまで最も賢く組み立てるクラス"""

    def __init__(self, max_prompt_tokens: int):
        self.max_prompt_tokens = max_prompt_tokens
        self.blocks = {
            "system": [],        # 絶対外せないシステムプロンプト (最優先)
            "rag": [],           # エラー解決ノウハウ (優先度高)
            "plan_handover": [], # 直近の作業プラン (優先度高)
            "history": []        # 過去の会話履歴 (優先度低・枠が余った分だけ入れる)
        }
        self.current_tokens = 0

    def add_history_from_db(self, session_id: int, keep_recent_n: int = 5):
        """DB(SessionMessage)から履歴をロードし、重要なものだけを詰め込む"""
        # ここでDBから履歴を引き出し、新しいもの順にトークンが許す限り self.blocks["history"] に追加する
        pass

    def build_messages(self) -> list[dict]:
        """トークン制限を守りつつ、LLMに渡す最終的なリストを生成"""
        final_messages = []
        
        # 1. System Prompt
        final_messages.extend(self.blocks["system"])
        
        # 2. RAG & Plan (RAGの知識と現在のプランをSystem Roleとして差し込む)
        if self.blocks["rag"] or self.blocks["plan_handover"]:
            sys_content = "\n".join(self.blocks["rag"] + self.blocks["plan_handover"])
            final_messages.append({"role": "system", "content": sys_content})
            
        # 3. History (古いものから順に並び替えて結合)
        final_messages.extend(reversed(self.blocks["history"]))
        
        return final_messages
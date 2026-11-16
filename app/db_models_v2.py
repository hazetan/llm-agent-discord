from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)

from .db_system import Base


def utc_now():
    return datetime.now(UTC)

# --- 1. 会話履歴の完全永続化テーブル ---
class SessionMessage(Base):
    """LLMとの対話・ツール実行の生の履歴を保存。再起動時の完全復元に使用。"""
    __tablename__ = "session_message"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("goalsession.id"), index=True)
    step = Column(Integer, nullable=False, index=True)
    
    role = Column(String(32), nullable=False) # "user", "assistant", "system", "tool"
    content = Column(Text, nullable=True)
    tool_calls_json = Column(Text, nullable=True) # ツール呼び出し（JSON）
    tool_call_id = Column(String(64), nullable=True) # ツール結果紐付け用
    
    token_count = Column(Integer, default=0) # コンテキスト圧縮計算用
    created_at = Column(DateTime(timezone=True), default=utc_now)

# --- 2. RAGノウハウテーブルの最適化 ---
class TroubleshootingMemory(Base):
    __tablename__ = "troubleshooting_memory"

    id = Column(Integer, primary_key=True, index=True)
    category = Column(String(64), nullable=False, index=True)
    error_pattern = Column(Text, nullable=False)
    root_cause = Column(Text, nullable=False)
    solution = Column(Text, nullable=False)
    embedding_json = Column(Text, nullable=True)
    
    # ▼ 追加：自己学習・評価用カラム
    success_count = Column(Integer, default=0)    # この解決策で実際に解決した回数
    fail_count = Column(Integer, default=0)       # 試したがダメだった回数
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)
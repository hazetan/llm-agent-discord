from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)

from .db_system import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class GoalSession(Base):
    __tablename__ = "goalsession"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(Text, nullable=False)
    objective = Column(Text, nullable=False)
    status = Column(String(32), default="pending", nullable=False, index=True)

    channel_id = Column(BigInteger, nullable=True, index=True)
    message_id = Column(BigInteger, nullable=True)

    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    project_slug = Column(String(64), nullable=True)
    project_dir = Column(String(256), nullable=True)
    artifact_zip = Column(String(256), nullable=True)
    docker_container = Column(String(64), nullable=True)


class Activity(Base):
    __tablename__ = "activity"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("goalsession.id"), nullable=True, index=True)
    work = Column(Text, nullable=False)
    result = Column(Text, nullable=False)
    datetime = Column(DateTime(timezone=True), default=utc_now, nullable=False)


class Planner(Base):
    __tablename__ = "planner"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("goalsession.id"), index=True)

    current_issue = Column(Text, nullable=False)
    task = Column(Text, nullable=False)
    result = Column(Text, nullable=True)
    future_issue = Column(Text, nullable=True)
    status = Column(String(32), default="pending", nullable=False, index=True)
    datetime = Column(DateTime(timezone=True), default=utc_now, nullable=False)


# ▼ 新規追加: 会話履歴の完全永続化テーブル
class SessionMessage(Base):
    __tablename__ = "session_message"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("goalsession.id"), index=True)
    step = Column(Integer, nullable=False, index=True)
    
    role = Column(String(32), nullable=False)
    message_json = Column(Text, nullable=False)  # メッセージ（辞書）をまるごと保存
    
    created_at = Column(DateTime(timezone=True), default=utc_now)


# ▼ 更新: RAGの自己学習・評価カラムを追加
class TroubleshootingMemory(Base):
    __tablename__ = "troubleshooting_memory"

    id = Column(Integer, primary_key=True, index=True)
    category = Column(String(64), nullable=False, index=True)
    error_pattern = Column(Text, nullable=False)
    root_cause = Column(Text, nullable=False)
    solution = Column(Text, nullable=False)
    embedding_json = Column(Text, nullable=True)
    
    success_count = Column(Integer, default=0)
    fail_count = Column(Integer, default=0)
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
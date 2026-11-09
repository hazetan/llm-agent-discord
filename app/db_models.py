from datetime import UTC, datetime

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Integer, String, Text

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


class TroubleshootingMemory(Base):
    __tablename__ = "troubleshooting_memory"

    id = Column(Integer, primary_key=True, index=True)
    category = Column(String(64), nullable=False, index=True)
    error_pattern = Column(Text, nullable=False)
    root_cause = Column(Text, nullable=False)
    solution = Column(Text, nullable=False)
    # 生成された Embedding ベクトル（JSON 配列文字列）
    embedding_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
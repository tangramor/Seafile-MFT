"""
数据库模型定义
"""
from sqlalchemy import Column, String, Integer, DateTime, Text, Enum
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from datetime import datetime
import enum


class ReviewStatus(str, enum.Enum):
    PENDING = "pending"       # 等待审批
    APPROVED = "approved"     # 已通过
    REJECTED = "rejected"     # 已拒绝
    TRANSFERRED = "transferred"  # 已传输到外网
    FAILED = "failed"         # 传输失败


class Base(DeclarativeBase):
    pass


class ReviewTask(Base):
    """审核任务表"""
    __tablename__ = "review_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    token = Column(String(128), unique=True, index=True, nullable=False)

    # 文件信息
    file_name = Column(String(512), nullable=False)
    file_path = Column(String(1024), nullable=False)   # 内网文件路径
    file_size = Column(Integer, default=0)
    repo_id = Column(String(64), nullable=False)       # 内网 repo
    commit_id = Column(String(64), default="")         # 对应 commit

    # 上传者信息
    uploader = Column(String(256), default="")
    uploader_email = Column(String(256), default="")

    # 审批状态
    status = Column(Enum(ReviewStatus), default=ReviewStatus.PENDING, index=True)
    reviewer_comment = Column(Text, default="")
    reviewed_by = Column(String(256), default="")
    reviewed_at = Column(DateTime, nullable=True)

    # 传输结果
    transfer_error = Column(Text, default="")
    transferred_at = Column(DateTime, nullable=True)
    extranet_file_path = Column(String(1024), default="")

    # 时间戳
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    expire_at = Column(DateTime, nullable=True)   # token 过期时间


# ==============================
# 数据库引擎和 Session 工厂
# ==============================
_engine = None
_async_session = None


def init_engine(database_url: str):
    global _engine, _async_session
    _engine = create_async_engine(database_url, echo=False)
    _async_session = sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def create_tables():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncSession:
    async with _async_session() as session:
        yield session

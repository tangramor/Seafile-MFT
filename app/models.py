"""
数据库模型定义 - 使用同步 SQLAlchemy 避免 aiosqlite 线程问题
"""
import enum
import logging
from datetime import datetime
from contextlib import contextmanager

from sqlalchemy import Column, String, Integer, DateTime, Text, Enum, Boolean, create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session
from sqlalchemy import inspect as sa_inspect

logger = logging.getLogger(__name__)


class ReviewStatus(str, enum.Enum):
    PENDING = "pending"           # 等待审批
    APPROVED = "approved"         # 已通过
    REJECTED = "rejected"         # 已拒绝
    TRANSFERRED = "transferred"   # 已传输到外网
    FAILED = "failed"             # 传输失败


class UserRole(str, enum.Enum):
    SUBMITTER = "submitter"   # 提交者：可上传文件、查看自己的申请
    REVIEWER  = "reviewer"    # 审核者：可审核所有申请
    ADMIN     = "admin"       # 管理员：同时拥有以上权限 + 用户管理


class Base(DeclarativeBase):
    pass


# ──────────────────────────────────────────
# 用户表（LDAP 同步 / 手动创建）
# ──────────────────────────────────────────
class User(Base):
    """系统用户表（对应 LDAP 账号或本地账号）"""
    __tablename__ = "users"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    username    = Column(String(128), unique=True, index=True, nullable=False)
    email       = Column(String(256), default="")
    display_name = Column(String(256), default="")
    role        = Column(Enum(UserRole), default=UserRole.SUBMITTER, nullable=False)
    is_active   = Column(Boolean, default=True)
    # LDAP 用户无本地密码；本地账号（如 admin）才有
    password_hash = Column(String(256), default="")
    last_login  = Column(DateTime, nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)


# ──────────────────────────────────────────
# Web Session 表（服务端 Session）
# ──────────────────────────────────────────
class UserSession(Base):
    """Web 登录 Session（存 DB，避免依赖 Redis）"""
    __tablename__ = "user_sessions"

    session_id  = Column(String(128), primary_key=True)
    user_id     = Column(Integer, nullable=False, index=True)
    username    = Column(String(128), nullable=False)
    role        = Column(String(32), nullable=False)
    created_at  = Column(DateTime, default=datetime.utcnow)
    expires_at  = Column(DateTime, nullable=False)


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
    repo_pair_id = Column(Integer, nullable=True, index=True)  # 所属配对仓库
    commit_id = Column(String(64), default="")         # 对应 commit

    # 上传者信息
    uploader = Column(String(256), default="")
    uploader_email = Column(String(256), default="")
    # 来源：poller（轮询检测）或 web（Web 界面上传）
    source = Column(String(32), default="poller")

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


class PollerState(Base):
    """
    轮询进度表 - 记录每个 repo 最后处理到的 commit_id
    用于跨重启持久化轮询位置，避免重复触发审核
    """
    __tablename__ = "poller_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    repo_id = Column(String(64), unique=True, index=True, nullable=False)
    last_commit_id = Column(String(64), nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RepoPair(Base):
    """配对仓库：内网仓库与对应外网仓库的配对关系

    一个配对代表「内网某个仓库的文件，审批通过后传送到对应的外网仓库」。
    内网/外网仓库以同名方式存在于各自的 Seafile 实例上（不存在则自动创建）。
    """
    __tablename__ = "repo_pairs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(256), unique=True, nullable=False, index=True)
    intranet_repo_id = Column(String(64), nullable=False)
    extranet_repo_id = Column(String(64), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class UserGroup(Base):
    """用户分组：用于隔离不同团队的仓库可见范围"""
    __tablename__ = "user_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(256), unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class UserGroupMember(Base):
    """用户-分组 多对多关联（一个用户可属多个分组）"""
    __tablename__ = "user_group_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    group_id = Column(Integer, nullable=False, index=True)


class GroupRepoPair(Base):
    """分组-配对仓库 多对多关联（一个分组可挂载多个配对仓库）"""
    __tablename__ = "group_repo_pairs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, nullable=False, index=True)
    repo_pair_id = Column(Integer, nullable=False, index=True)


class AuditLog(Base):
    """
    审计日志表 - 记录所有关键操作（上传、审批、用户管理等）
    """
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(128), nullable=False, index=True)
    action = Column(String(64), nullable=False, index=True)
    target_type = Column(String(32), nullable=False)
    target_id = Column(Integer, nullable=True)
    details = Column(Text, default="")
    ip_address = Column(String(64), default="")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


# ==============================
# 数据库引擎和 Session 工厂（同步模式）
# ==============================
_engine = None
_session_factory = None


def init_engine(database_url: str):
    """初始化数据库引擎（同步模式）"""
    global _engine, _session_factory
    # 移除 aiosqlite 前缀，使用普通 sqlite
    if database_url.startswith("sqlite+aiosqlite"):
        database_url = database_url.replace("sqlite+aiosqlite", "sqlite")
    _engine = create_engine(database_url, echo=False, connect_args={"check_same_thread": False})
    _session_factory = sessionmaker(_engine, expire_on_commit=False)


def create_tables():
    """创建所有表（同步），忽略已存在的表"""
    Base.metadata.create_all(bind=_engine, checkfirst=True)


def migrate_db():
    """
    对已有数据库的轻量迁移（新增列等）。
    create_all 只建新表、不改已有表，因此已有 review_tasks 表需要手动 ALTER 补列。
    """
    from sqlalchemy import inspect
    inspector = inspect(_engine)
    existing = set(inspector.get_table_names())
    # 确保新表存在（幂等）
    Base.metadata.create_all(bind=_engine, checkfirst=True)
    # 为已有的 review_tasks 补加 repo_pair_id 列
    if "review_tasks" in existing:
        cols = [c["name"] for c in inspector.get_columns("review_tasks")]
        if "repo_pair_id" not in cols:
            with _engine.begin() as conn:
                conn.execute(text("ALTER TABLE review_tasks ADD COLUMN repo_pair_id INTEGER"))
            logger.info("[Migrate] review_tasks 已补加 repo_pair_id 列")


def seed_default_repo_pair(db: Session, settings):
    """
    首次启动时，将 config 中的单仓库迁移为「默认配对」，
    并把尚未归属的审核任务回填到该配对，保证旧数据与旧行为不丢失。
    """
    if db.query(RepoPair).count() > 0:
        return
    if settings.intranet_repo_id and settings.extranet_repo_id:
        pair = RepoPair(
            name="默认配对",
            intranet_repo_id=settings.intranet_repo_id,
            extranet_repo_id=settings.extranet_repo_id,
            is_active=True,
        )
        db.add(pair)
        db.commit()
        db.refresh(pair)
        db.query(ReviewTask).filter(ReviewTask.repo_pair_id == None).update(
            {ReviewTask.repo_pair_id: pair.id}
        )
        db.commit()
        logger.info(f"[Migrate] 已将 config 单仓库迁移为默认配对 #{pair.id}")


def get_accessible_pair_ids(db: Session, user_id: int, is_admin: bool):
    """
    返回用户可见的 repo_pair_id 列表。
    - 管理员：返回 None，表示可见全部配对。
    - 其他用户：可属多个分组，可见范围为所有所属分组挂载配对的并集；
      若用户不属于任何分组，返回空列表（看不到任何配对内容）。
    """
    if is_admin:
        return None
    members = db.query(UserGroupMember).filter(UserGroupMember.user_id == user_id).all()
    group_ids = [m.group_id for m in members]
    if not group_ids:
        return []
    links = db.query(GroupRepoPair).filter(GroupRepoPair.group_id.in_(group_ids)).all()
    return list({l.repo_pair_id for l in links})


@contextmanager
def get_db() -> Session:
    """获取数据库会话（同步上下文管理器）"""
    db = _session_factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# 为 FastAPI Depends 提供的异步包装（实际使用同步数据库）
async def get_db_async():
    """FastAPI 依赖注入用的异步包装"""
    with get_db() as db:
        yield db

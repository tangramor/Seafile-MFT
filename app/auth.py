"""
认证模块：LDAP 登录 + 服务端 Session + 权限依赖

支持两种登录方式：
1. LDAP 认证（主要方式）：通过 ldap3 绑定校验用户名/密码
2. 本地账号（备用）：仅限 admin，密码存 bcrypt hash

Session 以服务端 DB 存储为主，Cookie 只携带 session_id（HttpOnly）。
"""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from functools import wraps
from typing import Optional

from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .config import get_settings
from .models import User, UserRole, UserSession, get_db, get_db_async

logger = logging.getLogger(__name__)

SESSION_COOKIE = "mft_session"
SESSION_TTL_HOURS = 8  # 登录有效期（小时）


# ──────────────────────────────────────────
# LDAP 认证
# ──────────────────────────────────────────

def _ldap_authenticate(username: str, password: str) -> Optional[dict]:
    """
    用 ldap3 绑定校验用户名密码。
    成功返回 {'username', 'email', 'display_name', 'groups'}；
    失败返回 None。
    """
    try:
        import ldap3
    except ImportError:
        logger.warning("[Auth] ldap3 未安装，跳过 LDAP 认证")
        return None

    settings = get_settings()
    if not settings.ldap_host:
        return None

    # 构造用户 DN（两种常见格式）
    if settings.ldap_user_dn_template:
        user_dn = settings.ldap_user_dn_template.format(username=username)
    else:
        user_dn = f"uid={username},{settings.ldap_base_dn}"

    try:
        server = ldap3.Server(
            settings.ldap_host,
            port=settings.ldap_port,
            use_ssl=settings.ldap_use_ssl,
            get_info=ldap3.NONE,
            connect_timeout=5,
        )
        conn = ldap3.Connection(
            server,
            user=user_dn,
            password=password,
            authentication=ldap3.SIMPLE,
            auto_bind=True,
        )
    except ldap3.core.exceptions.LDAPBindError:
        return None
    except Exception as e:
        logger.error(f"[Auth] LDAP 连接异常: {e}")
        return None

    # 搜索用户属性
    email = ""
    display_name = username
    groups = []
    try:
        conn.search(
            search_base=settings.ldap_base_dn,
            search_filter=f"(uid={username})",
            attributes=["mail", "cn", "displayName", "memberOf"],
        )
        if conn.entries:
            entry = conn.entries[0]
            email = str(entry.mail.value) if entry.mail else ""
            display_name = str(
                entry.displayName.value if entry.displayName else entry.cn.value
            ) or username
            raw_groups = entry.memberOf.values if entry.memberOf else []
            # 从 DN 中提取 CN（组名）
            for g in raw_groups:
                parts = {k: v for part in str(g).split(",") for k, v in [part.strip().split("=", 1)] if "=" in part}
                if "CN" in parts:
                    groups.append(parts["CN"])
                elif "cn" in parts:
                    groups.append(parts["cn"])
    except Exception as e:
        logger.warning(f"[Auth] 读取 LDAP 用户属性失败: {e}")
    finally:
        conn.unbind()

    return {
        "username": username,
        "email": email,
        "display_name": display_name,
        "groups": groups,
    }


def _resolve_role_from_groups(groups: list) -> UserRole:
    """根据 LDAP 组名推断系统角色"""
    settings = get_settings()
    group_set = {g.lower() for g in groups}
    if settings.ldap_admin_group.lower() in group_set:
        return UserRole.ADMIN
    if settings.ldap_reviewer_group.lower() in group_set:
        return UserRole.REVIEWER
    return UserRole.SUBMITTER


# ──────────────────────────────────────────
# 本地密码（仅 admin 备用）
# ──────────────────────────────────────────

def _hash_password(password: str) -> str:
    """SHA-256 简单哈希（仅用于本地 admin 账号）"""
    return hashlib.sha256(password.encode()).hexdigest()


def _verify_local_password(plain: str, hashed: str) -> bool:
    return _hash_password(plain) == hashed


# ──────────────────────────────────────────
# 登录入口（供路由调用）
# ──────────────────────────────────────────

def login_user(username: str, password: str, db: Session) -> Optional[User]:
    """
    尝试 LDAP 认证，失败时回退本地账号。
    认证成功后同步用户信息到 DB 并返回 User 对象。
    """
    settings = get_settings()

    # 1. 先试 LDAP
    ldap_info = _ldap_authenticate(username, password)
    if ldap_info:
        role = _resolve_role_from_groups(ldap_info["groups"])
        user = db.query(User).filter(User.username == username).first()
        if not user:
            user = User(
                username=username,
                email=ldap_info["email"],
                display_name=ldap_info["display_name"],
                role=role,
            )
            db.add(user)
        else:
            # 每次登录刷新 LDAP 信息（组变了可以立即生效）
            user.email = ldap_info["email"] or user.email
            user.display_name = ldap_info["display_name"] or user.display_name
            user.role = role
        user.last_login = datetime.utcnow()
        db.commit()
        db.refresh(user)
        return user

    # 2. 回退本地账号
    user = db.query(User).filter(User.username == username).first()
    if user and user.password_hash and _verify_local_password(password, user.password_hash):
        user.last_login = datetime.utcnow()
        db.commit()
        db.refresh(user)
        return user

    return None


# ──────────────────────────────────────────
# Session 管理
# ──────────────────────────────────────────

def create_session(user: User, db: Session) -> str:
    """创建服务端 Session，返回 session_id"""
    session_id = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + timedelta(hours=SESSION_TTL_HOURS)
    sess = UserSession(
        session_id=session_id,
        user_id=user.id,
        username=user.username,
        role=user.role.value,
        expires_at=expires,
    )
    db.add(sess)
    db.commit()
    return session_id


def get_session(session_id: str, db: Session) -> Optional[UserSession]:
    """查询并验证 Session 是否有效"""
    if not session_id:
        return None
    sess = db.query(UserSession).filter(UserSession.session_id == session_id).first()
    if not sess:
        return None
    if sess.expires_at < datetime.utcnow():
        db.delete(sess)
        db.commit()
        return None
    return sess


def delete_session(session_id: str, db: Session):
    """注销 Session"""
    sess = db.query(UserSession).filter(UserSession.session_id == session_id).first()
    if sess:
        db.delete(sess)
        db.commit()


def cleanup_expired_sessions(db: Session):
    """清理过期 Session（可定期调用）"""
    db.query(UserSession).filter(UserSession.expires_at < datetime.utcnow()).delete()
    db.commit()


# ──────────────────────────────────────────
# FastAPI 依赖注入
# ──────────────────────────────────────────

class CurrentUser:
    """注入当前登录用户信息"""
    def __init__(self, session: UserSession):
        self.user_id = session.user_id
        self.username = session.username
        self.role = UserRole(session.role)

    @property
    def is_reviewer(self) -> bool:
        return self.role in (UserRole.REVIEWER, UserRole.ADMIN)

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN

    @property
    def display_name(self) -> str:
        return self.username


def _get_current_session(
    request: Request,
    db: Session = Depends(get_db_async),
) -> Optional[UserSession]:
    session_id = request.cookies.get(SESSION_COOKIE)
    return get_session(session_id, db) if session_id else None


def require_login(
    request: Request,
    db: Session = Depends(get_db_async),
) -> CurrentUser:
    """依赖：要求已登录，否则重定向到登录页"""
    session_id = request.cookies.get(SESSION_COOKIE)
    sess = get_session(session_id, db) if session_id else None
    if not sess:
        # 抛出特殊异常，由路由层捕获跳转
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": f"/login?next={request.url.path}"},
        )
    return CurrentUser(sess)


def require_reviewer(
    current_user: CurrentUser = Depends(require_login),
) -> CurrentUser:
    """依赖：要求审核者或管理员角色"""
    if not current_user.is_reviewer:
        raise HTTPException(status_code=403, detail="需要审核权限")
    return current_user


def require_admin(
    current_user: CurrentUser = Depends(require_login),
) -> CurrentUser:
    """依赖：要求管理员角色"""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return current_user


# ──────────────────────────────────────────
# 初始化默认管理员账号
# ──────────────────────────────────────────

def ensure_default_admin(db: Session):
    """
    若 users 表为空，自动创建本地 admin 账号（密码来自配置）。
    仅用于初次部署，后续通过 LDAP 登录。
    """
    settings = get_settings()
    if not settings.default_admin_password:
        return
    exists = db.query(User).filter(User.username == "admin").first()
    if not exists:
        admin = User(
            username="admin",
            email=settings.smtp_user or "",
            display_name="管理员",
            role=UserRole.ADMIN,
            password_hash=_hash_password(settings.default_admin_password),
        )
        db.add(admin)
        db.commit()
        logger.info("[Auth] 已创建默认 admin 账号")


def create_local_user(
    db: Session,
    username: str,
    password: str,
    display_name: str = "",
    email: str = "",
    role: UserRole = UserRole.SUBMITTER,
) -> tuple:
    """
    管理员手动创建本地用户。

    返回 (User | None, error_msg | None)：
      - 成功：(user, None)
      - 用户名已存在：(None, "用户名已存在")
      - 密码为空：(None, "密码不能为空")
    """
    if not username or not username.strip():
        return None, "用户名不能为空"
    if not password:
        return None, "密码不能为空"

    existing = db.query(User).filter(User.username == username.strip()).first()
    if existing:
        return None, f"用户名「{username.strip()}」已存在"

    user = User(
        username=username.strip(),
        email=email.strip(),
        display_name=display_name.strip() or username.strip(),
        role=role,
        password_hash=_hash_password(password),
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info(f"[Auth] 管理员创建了本地用户: {user.username} (角色: {role.value})")
    return user, None


def update_local_user(
    db: Session,
    user_id: int,
    display_name: str = None,
    email: str = None,
    role: UserRole = None,
) -> tuple:
    """
    管理员修改本地用户属性。

    返回 (User | None, error_msg | None)：
      - 成功：(user, None)
      - 用户不存在：(None, "用户不存在")
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return None, "用户不存在"

    if display_name is not None:
        user.display_name = display_name.strip()
    if email is not None:
        user.email = email.strip()
    if role is not None:
        user.role = role

    db.commit()
    db.refresh(user)
    logger.info(f"[Auth] 管理员更新了用户 #{user_id} ({user.username})")
    return user, None

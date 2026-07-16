"""
认证模块：多种认证方式 + 服务端 Session + 权限依赖

支持三种认证方式（由 AUTH_METHOD 配置决定）：
1. local  — 仅本地账密登录
2. ldap   — admin 用本地账号，其他用户走 LDAP 认证
3. seafile— admin 用本地账号，其他用户走 Seafile API /api2/auth-token/ 认证

Session 以服务端 DB 存储为主，Cookie 只携带 session_id（HttpOnly）。
"""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .config import get_settings
from .i18n import _
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
# Seafile API 认证
# ──────────────────────────────────────────

def _seafile_authenticate(username: str, password: str) -> Optional[dict]:
    """
    通过 Seafile API /api2/auth-token/ 校验用户名密码。
    成功返回 {'username', 'email', 'display_name'}；
    失败返回 None。
    """
    settings = get_settings()

    # 确定使用内网还是外网 Seafile
    if settings.auth_seafile == "extranet":
        seafile_url = settings.extranet_seafile_url
    else:
        seafile_url = settings.intranet_seafile_url

    try:
        # 调用 /api2/auth-token/ 验证账密
        resp = httpx.post(
            f"{seafile_url}/api2/auth-token/",
            data={"username": username, "password": password},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.debug(f"[Auth] Seafile 认证失败 ({resp.status_code}): {resp.text[:200]}")
            return None

        token = resp.json().get("token", "")
        if not token:
            return None

        # 尝试获取用户信息（邮箱、显示名）
        email = ""
        display_name = username
        try:
            info_resp = httpx.get(
                f"{seafile_url}/api/v2.1/user/",
                headers={"Authorization": f"Token {token}"},
                timeout=10,
            )
            if info_resp.status_code == 200:
                info = info_resp.json()
                email = info.get("email", "")
                display_name = info.get("name", "") or username
        except Exception as e:
            logger.debug(f"[Auth] 获取 Seafile 用户信息失败: {e}")

        return {
            "username": username,
            "email": email,
            "display_name": display_name,
        }
    except Exception as e:
        logger.error(f"[Auth] Seafile API 连接异常: {e}")
        return None


# ──────────────────────────────────────────
# 本地密码
# ──────────────────────────────────────────

def _hash_password(password: str) -> str:
    """SHA-256 简单哈希（仅用于本地账号）"""
    return hashlib.sha256(password.encode()).hexdigest()


def _verify_local_password(plain: str, hashed: str) -> bool:
    return _hash_password(plain) == hashed


def _try_local_auth(username: str, password: str, db: Session) -> Optional[User]:
    """本地账密验证，成功返回 User 并更新 last_login"""
    user = db.query(User).filter(User.username == username).first()
    if user and user.password_hash and _verify_local_password(password, user.password_hash):
        user.last_login = datetime.utcnow()
        db.commit()
        db.refresh(user)
        return user
    return None


def _upsert_external_user(
    db: Session,
    username: str,
    email: str,
    display_name: str,
    role: UserRole = UserRole.SUBMITTER,
) -> User:
    """创建或更新外部认证用户（LDAP / Seafile），返回 User 对象"""
    user = db.query(User).filter(User.username == username).first()
    if not user:
        user = User(
            username=username,
            email=email,
            display_name=display_name or username,
            role=role,
        )
        db.add(user)
    else:
        # 每次登录刷新外部信息（不覆盖已有 role，由管理员手动管理）
        if email:
            user.email = email
        if display_name:
            user.display_name = display_name
        # 首次登录默认 submitter，已有用户保留原 role
    user.last_login = datetime.utcnow()
    db.commit()
    db.refresh(user)
    return user


# ──────────────────────────────────────────
# 登录入口（供路由调用）
# ──────────────────────────────────────────

def login_user(username: str, password: str, db: Session) -> Optional[User]:
    """
    根据 AUTH_METHOD 配置选择认证策略：
    - local:   仅本地账密
    - ldap:    admin 走本地，其他用户走 LDAP（失败回退本地）
    - seafile: admin 走本地，其他用户走 Seafile API（失败回退本地）
    """
    settings = get_settings()
    method = settings.auth_method.lower()

    # admin 始终使用本地认证（在 ldap / seafile 模式下）
    if username == "admin" and method in ("ldap", "seafile"):
        return _try_local_auth(username, password, db)

    # ── local 模式：仅本地认证 ──
    if method == "local":
        return _try_local_auth(username, password, db)

    # ── ldap 模式：先 LDAP，失败回退本地 ──
    if method == "ldap":
        ldap_info = _ldap_authenticate(username, password)
        if ldap_info:
            role = _resolve_role_from_groups(ldap_info["groups"])
            return _upsert_external_user(
                db,
                username=ldap_info["username"],
                email=ldap_info["email"],
                display_name=ldap_info["display_name"],
                role=role,
            )
        # 回退本地
        return _try_local_auth(username, password, db)

    # ── seafile 模式：先 Seafile API，失败回退本地 ──
    if method == "seafile":
        seafile_info = _seafile_authenticate(username, password)
        if seafile_info:
            return _upsert_external_user(
                db,
                username=seafile_info["username"],
                email=seafile_info["email"],
                display_name=seafile_info["display_name"],
            )
        # 回退本地
        return _try_local_auth(username, password, db)

    # 未知配置，回退本地
    logger.warning(f"[Auth] 未知 AUTH_METHOD: {method}，回退本地认证")
    return _try_local_auth(username, password, db)


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
        raise HTTPException(status_code=403, detail=_("需要审核权限"))
    return current_user


def require_admin(
    current_user: CurrentUser = Depends(require_login),
) -> CurrentUser:
    """依赖：要求管理员角色"""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail=_("需要管理员权限"))
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
            display_name=_("管理员"),
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
        return None, _("用户名不能为空")
    if not password:
        return None, _("密码不能为空")

    existing = db.query(User).filter(User.username == username.strip()).first()
    if existing:
        return None, _("用户名「{username}」已存在", username=username.strip())

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
        return None, _("用户不存在")

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


def change_password(
    db: Session,
    user_id: int,
    old_password: str,
    new_password: str,
) -> tuple:
    """
    用户修改自己的密码（需验证旧密码）。

    返回 (True, None) 或 (False, error_msg)：
      - 成功：(True, None)
      - 旧密码错误：(False, "旧密码错误")
      - LDAP 用户不允许本地修改：(False, "该账号通过 LDAP 认证，无法在此修改密码")
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return False, _("用户不存在")

    # 外部认证用户（password_hash 为空）不能通过本地系统修改密码
    if not user.password_hash:
        auth_method = get_settings().auth_method.lower()
        if auth_method == "seafile":
            return False, _("该账号通过 Seafile 认证，无法在此修改密码")
        return False, _("该账号通过 LDAP 认证，无法在此修改密码")

    if not _verify_local_password(old_password, user.password_hash):
        return False, _("旧密码错误")

    if not new_password or len(new_password) < 4:
        return False, _("新密码长度不能少于4位")

    user.password_hash = _hash_password(new_password)
    db.commit()
    logger.info(f"[Auth] 用户 #{user_id} ({user.username}) 修改了密码")
    return True, None


def reset_password(
    db: Session,
    user_id: int,
    new_password: str,
) -> tuple:
    """
    管理员重置指定用户的密码（无需旧密码）。

    返回 (True, None) 或 (False, error_msg)：
      - 成功：(True, None)
      - LDAP 用户不允许重置：(False, "该账号通过 LDAP 认证，无法重置密码")
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return False, _("用户不存在")

    if not user.password_hash:
        auth_method = get_settings().auth_method.lower()
        if auth_method == "seafile":
            return False, _("该账号通过 Seafile 认证，无法重置密码")
        return False, _("该账号通过 LDAP 认证，无法重置密码")

    if not new_password or len(new_password) < 4:
        return False, _("新密码长度不能少于4位")

    user.password_hash = _hash_password(new_password)
    db.commit()
    logger.info(f"[Auth] 管理员重置了用户 #{user_id} ({user.username}) 的密码")
    return True, None


def delete_local_user(db: Session, user_id: int) -> tuple:
    """
    管理员删除指定本地用户。

    同时删除该用户的所有 Session 记录。

    返回 (True, None) 或 (False, error_msg)：
      - 成功：(True, None)
      - 用户不存在：(False, "用户不存在")
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return False, _("用户不存在")

    username = user.username

    # 删除该用户的所有 Session
    db.query(UserSession).filter(UserSession.user_id == user_id).delete()

    db.delete(user)
    db.commit()
    logger.info(f"[Auth] 管理员删除了用户 #{user_id} ({username})")
    return True, None

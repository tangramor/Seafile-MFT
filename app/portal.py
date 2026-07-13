"""
Portal 路由模块

包含：
- /login  /logout             登录 / 注销
- /dashboard                  首页（按角色跳转）
- /my/submissions             提交者：我的申请列表
- /my/upload                  提交者：Web 上传文件（仅内网）
- /review-board               审核者：待审核列表
- /review-board/{id}/approve  审核者：通过
- /review-board/{id}/reject   审核者：拒绝
- /downloads                  外网：已通过文件下载列表
- /downloads/{id}             外网：下载单个文件
- /admin/users                管理员：用户列表
"""
import asyncio
import logging
import os
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .auth import (
    CurrentUser,
    SESSION_COOKIE,
    SESSION_TTL_HOURS,
    create_session,
    create_local_user,
    update_local_user,
    change_password,
    reset_password,
    delete_local_user,
    delete_session,
    ensure_default_admin,
    login_user,
    require_login,
    require_reviewer,
    require_admin,
)
from .config import get_settings
from .email_notify import send_review_notification, send_result_notification
from .i18n import _, get_locale
from .models import ReviewStatus, ReviewTask, User, UserRole, get_db
from .transfer import SeafileClient, transfer_file_to_extranet
from .audit import log_action

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["_"] = _
templates.env.globals["get_locale"] = get_locale

STATUS_LABELS = {
    "pending":     ("待审批",   "warning"),
    "approved":    ("已通过",   "success"),
    "rejected":    ("已拒绝",   "danger"),
    "transferred": ("已同步外网", "info"),
    "failed":      ("同步失败",  "danger"),
}


# ─────────────────────────────────────────────
# 登录 / 注销
# ─────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/dashboard"):
    # 已登录则直接跳转
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        with get_db() as db:
            from .auth import get_session
            if get_session(session_id, db):
                return RedirectResponse(next, status_code=302)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "next": next, "error": None},
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="/dashboard"),
):
    with get_db() as db:
        ensure_default_admin(db)
        user = login_user(username, password, db)
        if not user:
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "next": next, "error": _("用户名或密码错误")},
                status_code=401,
            )
        if not user.is_active:
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "next": next, "error": _("账号已被禁用")},
                status_code=403,
            )
        session_id = create_session(user, db)

    ip = request.client.host if request.client else ""
    log_action(username, "user_login", "user", user.id,
               {"username": username},
               ip_address=ip)

    response = RedirectResponse(next or "/dashboard", status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL_HOURS * 3600,
    )
    return response


@router.get("/logout")
async def logout(request: Request):
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        with get_db() as db:
            delete_session(session_id, db)
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ─────────────────────────────────────────────
# Dashboard（首页）
# ─────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    current_user: CurrentUser = Depends(require_login),
):
    with get_db() as db:
        if current_user.is_reviewer:
            pending_count = db.query(ReviewTask).filter(
                ReviewTask.status == ReviewStatus.PENDING
            ).count()
            my_count = None
        else:
            pending_count = None
            my_count = db.query(ReviewTask).filter(
                ReviewTask.uploader == current_user.username
            ).count()

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": current_user,
            "pending_count": pending_count,
            "my_count": my_count,
            "status_labels": STATUS_LABELS,
        },
    )


# ─────────────────────────────────────────────
# 提交者：我的申请
# ─────────────────────────────────────────────

@router.get("/my/submissions", response_class=HTMLResponse)
async def my_submissions(
    request: Request,
    page: int = 1,
    status: str = None,
    current_user: CurrentUser = Depends(require_login),
):
    page_size = 20
    offset = (page - 1) * page_size

    with get_db() as db:
        query = (
            db.query(ReviewTask)
            .filter(ReviewTask.uploader == current_user.username)
            .order_by(ReviewTask.created_at.desc())
        )
        if status:
            try:
                query = query.filter(ReviewTask.status == ReviewStatus(status))
            except ValueError:
                pass
        total = query.count()
        tasks = query.offset(offset).limit(page_size).all()

    return templates.TemplateResponse(
        "my_submissions.html",
        {
            "request": request,
            "user": current_user,
            "tasks": tasks,
            "page": page,
            "total": total,
            "page_size": page_size,
            "current_status": status,
            "status_labels": STATUS_LABELS,
        },
    )


# ─────────────────────────────────────────────
# 提交者：Web 上传（内网）
# ─────────────────────────────────────────────

@router.get("/my/upload", response_class=HTMLResponse)
async def upload_page(
    request: Request,
    current_user: CurrentUser = Depends(require_login),
):
    return templates.TemplateResponse(
        "upload.html",
        {"request": request, "user": current_user, "message": None},
    )


@router.post("/my/upload", response_class=HTMLResponse)
async def upload_submit(
    request: Request,
    file: UploadFile = File(...),
    target_path: str = Form(default="/"),
    comment: str = Form(default=""),
    current_user: CurrentUser = Depends(require_login),
):
    """
    接收前端上传的文件，直接写入内网 Seafile，然后创建审核任务。
    """
    settings = get_settings()
    client = SeafileClient(settings.intranet_seafile_url, settings.intranet_seafile_token)

    # 规范化目标路径
    target_dir = target_path.strip() or "/"
    if not target_dir.startswith("/"):
        target_dir = "/" + target_dir

    try:
        content = await file.read()
        file_name = file.filename or "unknown"

        # 确保目录存在
        await client.ensure_dir(settings.intranet_repo_id, target_dir)

        # 上传到内网 Seafile
        intranet_path = await client.upload_file(
            settings.intranet_repo_id,
            file_name,
            content,
            target_dir=target_dir,
        )
        logger.info(f"[Upload] {current_user.username} 上传文件: {intranet_path}")
    except Exception as e:
        logger.error(f"[Upload] 上传失败: {e}")
        return templates.TemplateResponse(
            "upload.html",
            {
                "request": request,
                "user": current_user,
                "message": _("上传失败：{error}", error=str(e)),
                "msg_type": "danger",
            },
        )

    # 查询用户邮箱
    with get_db() as db:
        user_obj = db.query(User).filter(User.username == current_user.username).first()
        uploader_email = user_obj.email if user_obj else ""

        expire_at = datetime.utcnow() + timedelta(hours=settings.review_token_expire_hours)
        token = secrets.token_urlsafe(32)
        task = ReviewTask(
            token=token,
            file_name=file_name,
            file_path=intranet_path,
            file_size=len(content),
            repo_id=settings.intranet_repo_id,
            commit_id="web-upload",
            uploader=current_user.username,
            uploader_email=uploader_email,
            source="web",
            status=ReviewStatus.PENDING,
            expire_at=expire_at,
        )
        db.add(task)
        db.flush()
        db.refresh(task)
        task_id = task.id

        log_action(current_user.username, "task_created", "review_task", task_id,
                   {"file_name": file_name, "source": "web"},
                   ip_address=request.client.host if request.client else "")

        asyncio.create_task(send_review_notification(task))

    logger.info(f"[Upload] 审核任务已创建 #{task_id}: {intranet_path}")
    return templates.TemplateResponse(
        "upload.html",
        {
            "request": request,
            "user": current_user,
            "message": _("✅ 文件已上传，审核申请 #{task_id} 已提交，审核完成后将通知您。", task_id=task_id),
            "msg_type": "success",
        },
    )


# ─────────────────────────────────────────────
# 审核者：待审核列表
# ─────────────────────────────────────────────

@router.get("/review-board", response_class=HTMLResponse)
async def review_board(
    request: Request,
    page: int = 1,
    status: str = "pending",
    current_user: CurrentUser = Depends(require_reviewer),
):
    page_size = 20
    offset = (page - 1) * page_size

    with get_db() as db:
        query = db.query(ReviewTask).order_by(ReviewTask.created_at.desc())
        if status:
            try:
                query = query.filter(ReviewTask.status == ReviewStatus(status))
            except ValueError:
                pass
        total = query.count()
        tasks = query.offset(offset).limit(page_size).all()

    return templates.TemplateResponse(
        "review_board.html",
        {
            "request": request,
            "user": current_user,
            "tasks": tasks,
            "page": page,
            "total": total,
            "page_size": page_size,
            "current_status": status,
            "status_labels": STATUS_LABELS,
        },
    )


@router.post("/review-board/{task_id}/approve", response_class=HTMLResponse)
async def board_approve(
    request: Request,
    task_id: int,
    comment: str = Form(default=""),
    current_user: CurrentUser = Depends(require_reviewer),
):
    with get_db() as db:
        task = db.query(ReviewTask).filter(ReviewTask.id == task_id).first()
        if not task:
            raise HTTPException(status_code=404, detail=_("任务不存在"))
        if task.status != ReviewStatus.PENDING:
            raise HTTPException(status_code=400, detail=_("任务已处理"))

        task.status = ReviewStatus.APPROVED
        task.reviewed_by = current_user.username
        task.reviewer_comment = comment
        task.reviewed_at = datetime.utcnow()
        db.commit()
        db.refresh(task)

    log_action(current_user.username, "task_approved", "review_task", task_id,
               {"file_name": task.file_name, "uploader": task.uploader, "comment": comment},
               ip_address=request.client.host if request.client else "")

    asyncio.create_task(_transfer_and_notify(task_id))

    return RedirectResponse(
        f"/review-board?status=pending&msg=approved&id={task_id}",
        status_code=302,
    )


@router.post("/review-board/{task_id}/reject", response_class=HTMLResponse)
async def board_reject(
    request: Request,
    task_id: int,
    comment: str = Form(default=""),
    current_user: CurrentUser = Depends(require_reviewer),
):
    with get_db() as db:
        task = db.query(ReviewTask).filter(ReviewTask.id == task_id).first()
        if not task:
            raise HTTPException(status_code=404, detail=_("任务不存在"))
        if task.status != ReviewStatus.PENDING:
            raise HTTPException(status_code=400, detail=_("任务已处理"))

        task.status = ReviewStatus.REJECTED
        task.reviewed_by = current_user.username
        task.reviewer_comment = comment
        task.reviewed_at = datetime.utcnow()
        db.commit()
        db.refresh(task)

    log_action(current_user.username, "task_rejected", "review_task", task_id,
               {"file_name": task.file_name, "uploader": task.uploader, "comment": comment},
               ip_address=request.client.host if request.client else "")

    asyncio.create_task(send_result_notification(task))

    return RedirectResponse(
        f"/review-board?status=pending&msg=rejected&id={task_id}",
        status_code=302,
    )


# ─────────────────────────────────────────────
# 外网下载：已通过文件列表
# ─────────────────────────────────────────────

@router.get("/downloads", response_class=HTMLResponse)
async def download_list(
    request: Request,
    page: int = 1,
    current_user: CurrentUser = Depends(require_login),
):
    page_size = 20
    offset = (page - 1) * page_size

    with get_db() as db:
        query = (
            db.query(ReviewTask)
            .filter(ReviewTask.status == ReviewStatus.TRANSFERRED)
            .order_by(ReviewTask.transferred_at.desc())
        )
        # 提交者只看自己的文件
        if not current_user.is_reviewer:
            query = query.filter(ReviewTask.uploader == current_user.username)

        total = query.count()
        tasks = query.offset(offset).limit(page_size).all()

    return templates.TemplateResponse(
        "downloads.html",
        {
            "request": request,
            "user": current_user,
            "tasks": tasks,
            "page": page,
            "total": total,
            "page_size": page_size,
            "status_labels": STATUS_LABELS,
        },
    )


@router.get("/downloads/{task_id}")
async def download_file(
    task_id: int,
    current_user: CurrentUser = Depends(require_login),
):
    """代理下载：从外网 Seafile 获取文件后流式返回给客户端"""
    with get_db() as db:
        task = db.query(ReviewTask).filter(ReviewTask.id == task_id).first()
        if not task or task.status != ReviewStatus.TRANSFERRED:
            raise HTTPException(status_code=404, detail=_("文件不存在或尚未通过审批"))
        # 提交者只能下载自己的文件
        if not current_user.is_reviewer and task.uploader != current_user.username:
            raise HTTPException(status_code=403, detail=_("无权下载此文件"))
        file_path = task.extranet_file_path
        file_name = task.file_name
        repo_id_to_use = task.repo_id  # 保存一份，会话关闭后仍可用

    settings = get_settings()
    client = SeafileClient(settings.extranet_seafile_url, settings.extranet_seafile_token)
    try:
        content = await client.download_file(settings.extranet_repo_id, file_path)
    except Exception as e:
        raise HTTPException(status_code=502, detail=_("从外网 Seafile 下载失败：{error}", error=str(e)))

    log_action(current_user.username, "task_downloaded", "review_task", task_id,
               {"file_name": file_name},
               ip_address="")

    import urllib.parse
    encoded_name = urllib.parse.quote(file_name)
    return StreamingResponse(
        iter([content]),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
            "Content-Length": str(len(content)),
        },
    )


# ─────────────────────────────────────────────
# 管理员：用户管理
# ─────────────────────────────────────────────

@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users(
    request: Request,
    current_user: CurrentUser = Depends(require_admin),
):
    msg = request.query_params.get("msg")
    msg_type = request.query_params.get("msg_type", "success")
    with get_db() as db:
        users = db.query(User).order_by(User.created_at.desc()).all()
    return templates.TemplateResponse(
        "admin_users.html",
        {
            "request": request,
            "user": current_user,
            "users": users,
            "roles": UserRole,
            "msg": msg,
            "msg_type": msg_type,
        },
    )


@router.post("/admin/users/create", response_class=HTMLResponse)
async def admin_create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    display_name: str = Form(default=""),
    email: str = Form(default=""),
    role: str = Form(default="submitter"),
    current_user: CurrentUser = Depends(require_admin),
):
    """管理员创建本地用户"""
    if password != confirm_password:
        return RedirectResponse(
            "/admin/users?msg=" + _("两次输入的密码不一致") + "&msg_type=danger",
            status_code=302,
        )

    try:
        role_enum = UserRole(role)
    except ValueError:
        return RedirectResponse(
            "/admin/users?msg=" + _("无效的角色选择") + "&msg_type=danger",
            status_code=302,
        )

    with get_db() as db:
        user, error = create_local_user(
            db,
            username=username,
            password=password,
            display_name=display_name,
            email=email,
            role=role_enum,
        )

    if error:
        return RedirectResponse(
            "/admin/users?msg=" + error + "&msg_type=danger",
            status_code=302,
        )

    log_action(current_user.username, "user_created", "user", user.id,
               {"username": username, "role": role_enum.value},
               ip_address=request.client.host if request.client else "")

    return RedirectResponse(
        "/admin/users?msg=" + _("用户「{username}」创建成功", username=username) + "&msg_type=success",
        status_code=302,
    )


@router.post("/admin/users/{user_id}/role")
async def admin_change_role(
    user_id: int,
    role: str = Form(...),
    current_user: CurrentUser = Depends(require_admin),
):
    with get_db() as db:
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            raise HTTPException(status_code=404, detail=_("用户不存在"))
        try:
            u.role = UserRole(role)
        except ValueError:
            raise HTTPException(status_code=400, detail=_("无效角色"))
        db.commit()
        log_action(current_user.username, "user_role_changed", "user", user_id,
                   {"username": u.username, "new_role": role},
                   ip_address=request.client.host if request.client else "")
    return RedirectResponse("/admin/users", status_code=302)


@router.post("/admin/users/{user_id}/toggle")
async def admin_toggle_user(
    user_id: int,
    request: Request,
    current_user: CurrentUser = Depends(require_admin),
):
    with get_db() as db:
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            raise HTTPException(status_code=404, detail=_("用户不存在"))
        u.is_active = not u.is_active
        db.commit()
        log_action(current_user.username,
                   "user_disabled" if not u.is_active else "user_enabled",
                   "user", user_id,
                   {"username": u.username},
                   ip_address=request.client.host if request.client else "")
    return RedirectResponse("/admin/users", status_code=302)


@router.post("/admin/users/{user_id}/edit")
async def admin_edit_user(
    user_id: int,
    request: Request,
    display_name: str = Form(default=""),
    email: str = Form(default=""),
    role: str = Form(default=""),
    current_user: CurrentUser = Depends(require_admin),
):
    """管理员编辑用户属性（显示名、邮箱、角色）"""
    try:
        role_enum = UserRole(role) if role else None
    except ValueError:
        return RedirectResponse(
            "/admin/users?msg=" + _("无效的角色选择") + "&msg_type=danger",
            status_code=302,
        )

    with get_db() as db:
        user, error = update_local_user(
            db, user_id,
            display_name=display_name if display_name else None,
            email=email if email else None,
            role=role_enum,
        )

    if error:
        return RedirectResponse(
            "/admin/users?msg=" + error + "&msg_type=danger",
            status_code=302,
        )

    log_action(current_user.username, "user_updated", "user", user_id,
               {"username": user.username, "display_name": display_name, "email": email, "role": role},
               ip_address=request.client.host if request.client else "")

    return RedirectResponse(
        "/admin/users?msg=" + _("用户「{username}」已更新", username=user.username) + "&msg_type=success",
        status_code=302,
    )


@router.post("/admin/users/{user_id}/reset-password")
async def admin_reset_password(
    user_id: int,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    current_user: CurrentUser = Depends(require_admin),
):
    """管理员重置用户密码"""
    if new_password != confirm_password:
        return RedirectResponse(
            "/admin/users?msg=" + _("两次输入的密码不一致") + "&msg_type=danger",
            status_code=302,
        )

    with get_db() as db:
        ok, error = reset_password(db, user_id, new_password)
        if not ok:
            return RedirectResponse(
                "/admin/users?msg=" + error + "&msg_type=danger",
                status_code=302,
            )
        target_user = db.query(User).filter(User.id == user_id).first()

    log_action(current_user.username, "user_password_reset", "user", user_id,
               {"username": target_user.username},
               ip_address=request.client.host if request.client else "")

    return RedirectResponse(
        "/admin/users?msg=" + _("用户「{username}」的密码已重置", username=target_user.username) + "&msg_type=success",
        status_code=302,
    )


@router.post("/admin/users/{user_id}/delete")
async def admin_delete_user(
    user_id: int,
    request: Request,
    current_user: CurrentUser = Depends(require_admin),
):
    """管理员删除用户"""
    # 不允许删除自己
    if current_user.user_id == user_id:
        return RedirectResponse(
            "/admin/users?msg=" + _("不能删除当前登录的账号") + "&msg_type=danger",
            status_code=302,
        )

    with get_db() as db:
        # 先查询用户名用于审计日志
        target = db.query(User).filter(User.id == user_id).first()
        target_username = target.username if target else str(user_id)
        ok, error = delete_local_user(db, user_id)
        if not ok:
            return RedirectResponse(
                "/admin/users?msg=" + error + "&msg_type=danger",
                status_code=302,
            )

    log_action(current_user.username, "user_deleted", "user", user_id,
               {"username": target_username},
               ip_address=request.client.host if request.client else "")

    return RedirectResponse(
        "/admin/users?msg=" + _("用户已删除") + "&msg_type=success",
        status_code=302,
    )


# ─────────────────────────────────────────────
# 修改密码（所有已登录用户）
# ─────────────────────────────────────────────

@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(
    request: Request,
    current_user: CurrentUser = Depends(require_login),
):
    """修改密码页面"""
    msg = request.query_params.get("msg")
    msg_type = request.query_params.get("msg_type", "success")

    # 检查是否为 LDAP 用户
    with get_db() as db:
        user = db.query(User).filter(User.id == current_user.user_id).first()
        is_ldap = user and not user.password_hash

    return templates.TemplateResponse(
        "change_password.html",
        {
            "request": request,
            "user": current_user,
            "msg": msg,
            "msg_type": msg_type,
            "is_ldap": is_ldap,
        },
    )


@router.post("/change-password")
async def change_password_submit(
    request: Request,
    old_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    current_user: CurrentUser = Depends(require_login),
):
    """提交修改密码"""
    if new_password != confirm_password:
        return RedirectResponse(
            "/change-password?msg=" + _("两次输入的密码不一致") + "&msg_type=danger",
            status_code=302,
        )

    with get_db() as db:
        ok, error = change_password(db, current_user.user_id, old_password, new_password)

    if not ok:
        return RedirectResponse(
            "/change-password?msg=" + error + "&msg_type=danger",
            status_code=302,
        )

    log_action(current_user.username, "user_password_changed", "user", current_user.user_id,
               {"username": current_user.username},
               ip_address=request.client.host if request.client else "")

    return RedirectResponse(
        "/change-password?msg=" + _("密码修改成功") + "&msg_type=success",
        status_code=302,
    )


# ─────────────────────────────────────────────
# 审计日志（管理员 + 审核者可见）
# ─────────────────────────────────────────────

@router.get("/admin/audit-log", response_class=HTMLResponse)
async def audit_log_page(
    request: Request,
    action: str = "",
    page: int = 1,
    current_user: CurrentUser = Depends(require_reviewer),
):
    """审计日志页面 - 可用于管理员和审批员查看"""
    from .models import AuditLog as AuditLogModel
    page_size = 30

    with get_db() as db:
        query = db.query(AuditLogModel)
        if action:
            query = query.filter(AuditLogModel.action == action)
        total = query.count()
        entries = query.order_by(AuditLogModel.id.desc()).offset(
            (page - 1) * page_size
        ).limit(page_size).all()

    # 预解析 details JSON 方便模板使用
    import json
    for e in entries:
        try:
            e.details_json = json.loads(e.details) if e.details else {}
        except (json.JSONDecodeError, TypeError):
            e.details_json = {}

    return templates.TemplateResponse(
        "audit_log.html",
        {
            "request": request,
            "user": current_user,
            "entries": entries,
            "current_action": action,
            "page": page,
            "page_size": page_size,
            "total": total,
        },
    )


# ─────────────────────────────────────────────
# 语言切换
# ─────────────────────────────────────────────

@router.get("/lang/{locale}")
async def set_language(locale: str, request: Request):
    """切换界面语言，设置 Cookie 后重定向回来源页。"""
    from .i18n import SUPPORTED_LOCALES
    if locale not in SUPPORTED_LOCALES:
        locale = "zh"
    referer = request.headers.get("referer", "/dashboard")
    response = RedirectResponse(url=referer, status_code=303)
    response.set_cookie("lang", locale, max_age=365 * 24 * 3600, httponly=True)
    return response


# ─────────────────────────────────────────────
# 内部：传输并通知（复用 review.py 逻辑）
# ─────────────────────────────────────────────

async def _transfer_and_notify(task_id: int):
    from .models import get_db as _get_db
    with _get_db() as db:
        task = db.query(ReviewTask).filter(ReviewTask.id == task_id).first()
        if not task:
            return
        success, error_msg, extranet_path = await transfer_file_to_extranet(task)
        if success:
            task.status = ReviewStatus.TRANSFERRED
            task.extranet_file_path = extranet_path
            task.transferred_at = datetime.utcnow()
            log_action("system", "task_transferred", "review_task", task_id,
                       {"file_name": task.file_name, "extranet_path": extranet_path})
        else:
            task.status = ReviewStatus.FAILED
            task.transfer_error = error_msg
            log_action("system", "task_failed", "review_task", task_id,
                       {"file_name": task.file_name, "error": error_msg})
        db.commit()
        db.refresh(task)
        await send_result_notification(task)

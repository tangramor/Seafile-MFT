"""
审批逻辑路由

提供以下端点：
- GET  /review/{token}           - 审批详情页
- POST /review/{token}/approve   - 通过审批
- POST /review/{token}/reject    - 拒绝审批
- GET  /admin/tasks              - 管理后台（任务列表）
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .models import ReviewTask, ReviewStatus, get_db
from .transfer import transfer_file_to_extranet
from .email_notify import send_result_notification

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


async def get_valid_task(token: str, db: AsyncSession) -> ReviewTask:
    """根据 token 获取有效任务"""
    result = await db.execute(select(ReviewTask).where(ReviewTask.token == token))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="审批任务不存在")
    if task.expire_at and task.expire_at < datetime.utcnow():
        raise HTTPException(status_code=410, detail="审批链接已过期")
    return task


@router.get("/review/{token}", response_class=HTMLResponse)
async def review_detail(
    request: Request,
    token: str,
    action: str = None,
    db: AsyncSession = Depends(get_db),
):
    """审批详情页 - 支持直接从 URL 参数快速审批"""
    task = await get_valid_task(token, db)

    # 支持邮件中的快速审批链接 ?action=approve|reject
    quick_action = None
    if action in ("approve", "reject") and task.status == ReviewStatus.PENDING:
        quick_action = action

    return templates.TemplateResponse(
        "review.html",
        {
            "request": request,
            "task": task,
            "quick_action": quick_action,
            "status_labels": {
                "pending": ("待审批", "warning"),
                "approved": ("已通过", "success"),
                "rejected": ("已拒绝", "danger"),
                "transferred": ("已同步外网", "info"),
                "failed": ("同步失败", "danger"),
            },
        },
    )


@router.post("/review/{token}/approve")
async def approve_task(
    request: Request,
    token: str,
    reviewer_name: str = Form(default="审批人"),
    comment: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    """通过审批 → 触发文件传输"""
    task = await get_valid_task(token, db)

    if task.status != ReviewStatus.PENDING:
        return templates.TemplateResponse(
            "review.html",
            {
                "request": request,
                "task": task,
                "message": f"任务当前状态为「{task.status.value}」，无法重复审批",
                "message_type": "warning",
                "status_labels": {
                    "pending": ("待审批", "warning"),
                    "approved": ("已通过", "success"),
                    "rejected": ("已拒绝", "danger"),
                    "transferred": ("已同步外网", "info"),
                    "failed": ("同步失败", "danger"),
                },
            },
        )

    # 更新状态
    task.status = ReviewStatus.APPROVED
    task.reviewed_by = reviewer_name
    task.reviewer_comment = comment
    task.reviewed_at = datetime.utcnow()
    await db.commit()
    await db.refresh(task)

    # 异步传输文件到外网
    import asyncio
    asyncio.create_task(_transfer_and_notify(task.id))

    return templates.TemplateResponse(
        "review.html",
        {
            "request": request,
            "task": task,
            "message": "✅ 已通过审批！文件正在同步至外网 Seafile，完成后将通知上传者。",
            "message_type": "success",
            "status_labels": {
                "pending": ("待审批", "warning"),
                "approved": ("已通过", "success"),
                "rejected": ("已拒绝", "danger"),
                "transferred": ("已同步外网", "info"),
                "failed": ("同步失败", "danger"),
            },
        },
    )


@router.post("/review/{token}/reject")
async def reject_task(
    request: Request,
    token: str,
    reviewer_name: str = Form(default="审批人"),
    comment: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    """拒绝审批"""
    task = await get_valid_task(token, db)

    if task.status != ReviewStatus.PENDING:
        raise HTTPException(status_code=400, detail="任务已处理，无法重复审批")

    task.status = ReviewStatus.REJECTED
    task.reviewed_by = reviewer_name
    task.reviewer_comment = comment
    task.reviewed_at = datetime.utcnow()
    await db.commit()
    await db.refresh(task)

    # 通知上传者
    await send_result_notification(task)

    return templates.TemplateResponse(
        "review.html",
        {
            "request": request,
            "task": task,
            "message": f"❌ 已拒绝审批。拒绝原因：{comment or '无'}",
            "message_type": "danger",
            "status_labels": {
                "pending": ("待审批", "warning"),
                "approved": ("已通过", "success"),
                "rejected": ("已拒绝", "danger"),
                "transferred": ("已同步外网", "info"),
                "failed": ("同步失败", "danger"),
            },
        },
    )


@router.get("/admin/tasks", response_class=HTMLResponse)
async def admin_task_list(
    request: Request,
    status: str = None,
    page: int = 1,
    db: AsyncSession = Depends(get_db),
):
    """管理员任务列表页"""
    page_size = 20
    offset = (page - 1) * page_size

    query = select(ReviewTask).order_by(ReviewTask.created_at.desc())
    if status:
        try:
            query = query.where(ReviewTask.status == ReviewStatus(status))
        except ValueError:
            pass

    result = await db.execute(query.offset(offset).limit(page_size))
    tasks = result.scalars().all()

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "tasks": tasks,
            "current_status": status,
            "page": page,
            "status_labels": {
                "pending": ("待审批", "warning"),
                "approved": ("已通过", "success"),
                "rejected": ("已拒绝", "danger"),
                "transferred": ("已同步外网", "info"),
                "failed": ("同步失败", "danger"),
            },
        },
    )


async def _transfer_and_notify(task_id: int):
    """后台任务：传输文件并发送通知"""
    from .models import _async_session
    async with _async_session() as db:
        result = await db.execute(select(ReviewTask).where(ReviewTask.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            return

        success, error_msg, extranet_path = await transfer_file_to_extranet(task)

        if success:
            task.status = ReviewStatus.TRANSFERRED
            task.extranet_file_path = extranet_path
            task.transferred_at = datetime.utcnow()
        else:
            task.status = ReviewStatus.FAILED
            task.transfer_error = error_msg

        await db.commit()
        await db.refresh(task)
        await send_result_notification(task)

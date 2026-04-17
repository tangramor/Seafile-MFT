"""
Seafile Webhook 接收处理器

Seafile 在文件上传时会发送 POST 请求到配置的 Webhook URL。
本模块负责：
1. 验证 Webhook 签名
2. 解析上传事件
3. 创建审核任务
4. 触发邮件通知
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .models import ReviewTask, ReviewStatus, get_db
from .email_notify import send_review_notification

router = APIRouter()


def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    """验证 Seafile Webhook HMAC-SHA256 签名"""
    if not secret:
        return True  # 未配置 secret 则跳过验证（仅限开发环境）
    expected = hmac.new(
        secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


@router.post("/webhook/seafile")
async def receive_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """
    接收 Seafile Webhook 事件

    Seafile Webhook payload 示例：
    {
        "event": "repo-update",
        "repo_id": "xxx",
        "repo_name": "文件库名",
        "oper": "web",
        "operator": "user@example.com",
        "commit_id": "xxx",
        "changed_files": {
            "added": ["/path/to/file.pdf"],
            "modified": [],
            "deleted": []
        }
    }
    """
    settings = get_settings()
    body = await request.body()

    # 验证签名
    signature = request.headers.get("X-Seafile-Signature", "")
    if not verify_webhook_signature(body, signature, settings.webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event = payload.get("event", "")
    repo_id = payload.get("repo_id", "")

    # 只处理目标 repo 的文件更新事件
    if event != "repo-update" or repo_id != settings.intranet_repo_id:
        return {"status": "ignored", "reason": "not target repo or event"}

    changed_files = payload.get("changed_files", {})
    added_files = changed_files.get("added", [])
    modified_files = changed_files.get("modified", [])

    # 合并新增和修改的文件
    target_files = added_files + modified_files
    if not target_files:
        return {"status": "ignored", "reason": "no added/modified files"}

    operator = payload.get("operator", "unknown")
    commit_id = payload.get("commit_id", "")
    expire_at = datetime.utcnow() + timedelta(hours=settings.review_token_expire_hours)

    created_tasks = []
    for file_path in target_files:
        file_name = file_path.split("/")[-1]

        # 生成唯一审核 token
        token = secrets.token_urlsafe(32)

        task = ReviewTask(
            token=token,
            file_name=file_name,
            file_path=file_path,
            repo_id=repo_id,
            commit_id=commit_id,
            uploader=operator,
            uploader_email=operator if "@" in operator else "",
            status=ReviewStatus.PENDING,
            expire_at=expire_at,
        )
        db.add(task)
        created_tasks.append(task)

    await db.commit()

    # 刷新以获取自增 ID
    for task in created_tasks:
        await db.refresh(task)

    # 发送审批邮件通知
    for task in created_tasks:
        await send_review_notification(task)

    return {
        "status": "ok",
        "created_tasks": len(created_tasks),
        "files": [t.file_name for t in created_tasks],
    }

"""
Seafile Webhook 接收处理器

Seafile >= 7.0 在文件上传/修改时会发送 POST 请求到配置的 Webhook URL。

本模块负责：
1. 验证 Webhook 签名（HMAC-SHA256）
2. 解析 repo-update 事件
3. 为新增/修改的文件创建审核任务
4. 触发邮件通知

Seafile 7.0+ Webhook payload 格式：
{
    "event": "repo-update",
    "repo_id": "xxx",
    "repo_name": "\u6587\u4ef6\u5e93\u540d",
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

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Request, HTTPException

from .config import get_settings
from .models import ReviewTask, ReviewStatus, RepoPair, get_db
from .audit import log_action

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhook"])


def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    """
    验证 Seafile Webhook HMAC-SHA256 签名。

    Seafile 签名格式：X-Seafile-Signature: sha256=<hex_digest>
    """
    if not secret:
        # 未配置 secret 则跳过验证（仅限内网/开发环境）
        logger.warning("[Webhook] 未配置 webhook_secret，跳过签名验证（仅开发环境推荐）")
        return True
    expected = hmac.new(
        secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    signature_value = signature
    if signature_value.startswith("sha256="):
        signature_value = signature_value[7:]
    return hmac.compare_digest(expected, signature_value)


@router.post("/webhook/seafile")
async def receive_webhook(request: Request):
    """
    接收 Seafile Webhook 事件。

    Seafile 需要配置 Webhook URL 为：
        http://<本机IP>:8081/webhook/seafile

    仅处理监听的 repo 中的 repo-update 事件。
    """
    settings = get_settings()
    body = await request.body()

    # 验证签名
    signature = request.headers.get("X-Seafile-Signature", "")
    if not verify_webhook_signature(body, signature, settings.webhook_secret):
        logger.warning("[Webhook] 签名验证失败")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event = payload.get("event", "")
    repo_id = payload.get("repo_id", "")

    logger.info(f"[Webhook] 收到事件: event={event}, repo_id={repo_id[:8]}...")

    # 只处理已配置配对仓库（内网侧）的 repo-update 事件
    with get_db() as db:
        pairs = db.query(RepoPair).filter(RepoPair.is_active == True).all()
    valid_ids = {p.intranet_repo_id: p.id for p in pairs}
    if event != "repo-update" or repo_id not in valid_ids:
        return {"status": "ignored", "reason": "not target repo or event"}

    repo_pair_id = valid_ids[repo_id]

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

    with get_db() as db:
        for file_path in target_files:
            file_name = file_path.split("/")[-1] if "/" in file_path else file_path

            # 去重1：同一 commit + 文件路径
            existing = db.query(ReviewTask).filter(
                ReviewTask.repo_id == repo_id,
                ReviewTask.file_path == file_path,
                ReviewTask.commit_id == commit_id,
            ).first()
            if existing:
                logger.info(f"[Webhook] 跳过重复任务: {file_path} (commit {commit_id[:8]})")
                continue

            # 去重2：同一文件路径已有审核记录（不限 commit_id）
            # 防止 Seafile 内部操作创建新 commit 导致重复审核
            existing_any = db.query(ReviewTask).filter(
                ReviewTask.repo_id == repo_id,
                ReviewTask.file_path == file_path,
            ).first()
            if existing_any:
                logger.info(f"[Webhook] 文件已存在审核记录，跳过: {file_path}")
                continue

            token = secrets.token_urlsafe(32)
            task = ReviewTask(
                token=token,
                file_name=file_name,
                file_path=file_path,
                file_size=0,  # Webhook payload 不含文件大小
                repo_id=repo_id,
                repo_pair_id=repo_pair_id,
                commit_id=commit_id,
                uploader=operator,
                uploader_email=operator if "@" in operator else "",
                source="webhook",     # 标识来源为 Webhook
                status=ReviewStatus.PENDING,
                expire_at=expire_at,
            )
            db.add(task)
            db.flush()
            db.refresh(task)
            created_tasks.append(task)
            logger.info(f"[Webhook] 创建审核任务 #{task.id}: {file_path} (commit {commit_id[:8]})")

            log_action(operator, "task_created", "review_task", task.id,
                       {"file_name": file_name, "source": "webhook"})

        # 事务由 get_db() 上下文管理器自动提交

    # 发送审批邮件通知（异步，不阻塞 Webhook 响应）
    import asyncio
    from .email_notify import send_review_notification
    for task in created_tasks:
        asyncio.create_task(send_review_notification(task))

    return {
        "status": "ok",
        "event": event,
        "created_tasks": len(created_tasks),
        "files": [t.file_name for t in created_tasks],
    }

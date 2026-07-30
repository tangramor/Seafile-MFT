"""
审计日志写入模块 — 在关键操作后异步/同步记录审计日志
"""
import json
import logging
from typing import Optional

from .models import AuditLog, get_db, Session

logger = logging.getLogger("audit")


def log_action(
    username: str,
    action: str,
    target_type: str,
    target_id: Optional[int] = None,
    details: Optional[dict] = None,
    ip_address: str = "",
    db: Optional["Session"] = None,
):
    """
    写入一条审计日志。

    参数：
      username     - 操作人用户名（系统操作如 poller 用 "system"）
      action       - 操作代码（task_created / task_approved / user_created 等）
      target_type  - 目标类型（review_task / user）
      target_id    - 目标 ID
      details      - 附加详情 dict，自动序列化为 JSON
      ip_address   - 操作来源 IP
      db           - 可选：复用调用方已开启的数据库会话。传入时审计行加入调用方事务
                     （由调用方提交），避免在同一未提交事务内再开写入连接导致
                     SQLite "database is locked"；不传则自行开启并独立提交会话。
    """
    try:
        details_str = json.dumps(details, ensure_ascii=False, default=str) if details else ""
        entry = AuditLog(
            username=username,
            action=action,
            target_type=target_type,
            target_id=target_id or 0,
            details=details_str,
            ip_address=ip_address,
        )
        if db is not None:
            # 复用调用方事务，避免同一未提交事务内再开写入连接导致 SQLite 锁冲突
            db.add(entry)
        else:
            with get_db() as s:
                s.add(entry)
    except Exception:
        logger.exception("[Audit] 写入审计日志失败")

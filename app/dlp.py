"""
DLP（出口数据防泄露）集成模块

职责：
- 把待传出的文件提交给 scan-worker 网关进行扫描（解包 → 分类 → YARA）。
- 接收 scan-worker 的结果回调（RESULT_WEBHOOK），根据 verdict 决策：
    - clean  → 自动放行，执行传输管线
    - alert  → 挂起任务，记录命中详情，通知审核者人工确认（误报可放行 / 确认可拦截）

设计要点：
- scan-worker 通过网关 /upload 接收文件，uploader 字段携带 "mft-task-{id}" 用于回关联。
- scan-worker 在每个文件扫描结束后，将含 verdict 的结果 POST 到 RESULT_WEBHOOK（即本模块暴露的回调）。
- 主生命周期仍由 ReviewStatus 枚举管理；DLP 闸门状态单独存于 ReviewTask.dlp_state。
"""
import json
import logging
from datetime import datetime

import httpx

from .config import get_settings
from .models import ReviewTask, get_db
from .transfer import run_transfer_pipeline
from .audit import log_action
from .email_notify import send_review_notification

log = logging.getLogger("dlp")


def parse_task_id(uploader: str):
    """从 scan-worker 回传的 uploader（"mft-task-{id}"）解析出任务 id"""
    if uploader and uploader.startswith("mft-task-"):
        try:
            return int(uploader.split("-", 2)[2])
        except (ValueError, IndexError):
            return None
    return None


def alert_severity_set():
    """需要挂起等审核的严重度集合（小写）"""
    s = get_settings().dlp_alert_severities or ""
    return {x.strip().lower() for x in s.split(",") if x.strip()}


async def submit_scan(task: ReviewTask, file_bytes: bytes, filename: str) -> bool:
    """
    将文件提交给 scan-worker 网关扫描。
    返回 True 表示已成功入队（等待异步回调）；False 表示未提交（应走 fail-open/closed 策略）。
    """
    settings = get_settings()
    if not settings.dlp_enabled:
        return False
    if len(file_bytes) > settings.dlp_max_file_size:
        log.info(f"[DLP] task#{task.id} 文件过大({len(file_bytes)}B)，跳过扫描直接放行")
        return False

    try:
        async with httpx.AsyncClient(timeout=settings.dlp_submit_timeout) as client:
            r = await client.post(
                settings.dlp_gateway_url.rstrip("/") + "/upload",
                files={"file": (filename, file_bytes, "application/octet-stream")},
                data={"uploader": f"mft-task-{task.id}", "source_ip": "mft"},
            )
            if r.status_code in (200, 202):
                log.info(f"[DLP] task#{task.id} 已提交扫描，等待回调")
                return True
            log.error(f"[DLP] 网关返回异常 HTTP {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        log.error(f"[DLP] 提交扫描失败: {e}")
        return False


async def handle_dlp_result(payload: dict):
    """
    处理 scan-worker 的结果回传（RESULT_WEBHOOK）。
    payload 结构见 scan-worker/scanner.py 的 send_result：
        {verdict, object_key, original_name, uploader, source_ip,
         sha256, severity, hit_count, hits, filename}
    """
    if not isinstance(payload, dict) or "verdict" not in payload:
        log.info("[DLP] 忽略非结果回调（缺少 verdict 字段）")
        return

    verdict = payload.get("verdict")
    task_id = parse_task_id(payload.get("uploader", ""))
    if task_id is None:
        log.warning(f"[DLP] 无法从 uploader={payload.get('uploader')!r} 解析 task_id，忽略回调")
        return

    need_transfer = False
    with get_db() as db:
        task = db.query(ReviewTask).filter(ReviewTask.id == task_id).first()
        if not task:
            log.warning(f"[DLP] task#{task_id} 不存在，忽略回调")
            return
        if task.dlp_state not in ("scanning", ""):
            log.info(f"[DLP] task#{task_id} 当前 dlp_state={task.dlp_state!r}，状态已变化，忽略回调")
            return

        if verdict == "alert" and payload.get("severity", "").lower() in alert_severity_set():
            task.dlp_state = "alert"
            task.dlp_severity = payload.get("severity", "")
            task.dlp_hits = json.dumps(payload.get("hits", []), ensure_ascii=False)
            task.dlp_checked_at = datetime.utcnow()
            db.commit()
            log_action("system", "dlp_alert", "review_task", task_id,
                       {"severity": task.dlp_severity,
                        "hit_count": payload.get("hit_count", 0),
                        "filename": task.file_name})
            # 通知审核者人工确认（误报可放行 / 确认可拦截）
            try:
                await send_review_notification(task)
            except Exception as e:
                log.warning(f"[DLP] 通知审核者失败: {e}")
            log.warning(f"[DLP] task#{task_id} 命中敏感内容，已挂起等待审核者确认")
        else:
            # clean 或未达告警阈值的命中：自动放行传输
            task.dlp_state = "cleared"
            task.dlp_severity = payload.get("severity", "clean")
            task.dlp_checked_at = datetime.utcnow()
            db.commit()
            log.info(f"[DLP] task#{task_id} 扫描通过（verdict={verdict}），自动放行传输")
            need_transfer = True

    # 在 with 块外执行传输管线，避免嵌套会话
    if need_transfer:
        await run_transfer_pipeline(task_id)

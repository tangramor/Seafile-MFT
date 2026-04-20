"""
邮件通知模块

支持双网络（内网/外网）SMTP 配置：
- 内网 SMTP 发送的邮件中，审批链接指向内网 App URL
- 外网 SMTP 发送的邮件中，审批链接指向外网 App URL
- 若只配置了内网 SMTP，则回退到单路发送
"""
import ssl
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

from .config import get_settings
from .models import ReviewTask

logger = logging.getLogger(__name__)


def build_review_email(task: ReviewTask, reviewer_email: str, app_url: str) -> MIMEMultipart:
    """
    构造审批通知邮件。

    :param task:           审核任务对象
    :param reviewer_email: 收件人邮箱
    :param app_url:        本次邮件使用的 App 访问地址（内网或外网）
    """
    settings = get_settings()
    base = app_url.rstrip("/")
    approve_url = f"{base}/review/{task.token}?action=approve"
    reject_url  = f"{base}/review/{task.token}?action=reject"
    detail_url  = f"{base}/review/{task.token}"

    subject = f"【文件审批】{task.file_name} 待审核"

    html_body = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f5f5f5; margin: 0; padding: 20px; }}
  .container {{ max-width: 600px; margin: 0 auto; background: white;
                border-radius: 12px; overflow: hidden;
                box-shadow: 0 2px 12px rgba(0,0,0,0.1); }}
  .header {{ background: linear-gradient(135deg, #1a73e8, #0d47a1);
             padding: 30px; text-align: center; color: white; }}
  .header h1 {{ margin: 0; font-size: 22px; font-weight: 600; }}
  .header p {{ margin: 8px 0 0; opacity: 0.85; font-size: 14px; }}
  .body {{ padding: 30px; }}
  .info-card {{ background: #f8f9fa; border-radius: 8px;
                padding: 20px; margin-bottom: 24px;
                border-left: 4px solid #1a73e8; }}
  .info-row {{ display: flex; margin-bottom: 10px; font-size: 14px; }}
  .info-label {{ color: #666; width: 90px; flex-shrink: 0; }}
  .info-value {{ color: #222; font-weight: 500; word-break: break-all; }}
  .actions {{ display: flex; gap: 12px; margin-top: 8px; }}
  .btn {{ display: inline-block; padding: 12px 28px; border-radius: 8px;
          text-decoration: none; font-size: 15px; font-weight: 600;
          text-align: center; flex: 1; }}
  .btn-approve {{ background: #34a853; color: white; }}
  .btn-reject  {{ background: #ea4335; color: white; }}
  .btn-detail  {{ background: #f1f3f4; color: #1a73e8; border: 1px solid #dadce0;
                  display: block; text-align: center; margin-top: 12px; }}
  .footer {{ background: #f8f9fa; padding: 18px 30px;
             font-size: 12px; color: #999; text-align: center; }}
  .expire-note {{ color: #e67e22; font-size: 13px; margin-top: 16px; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>📄 文件审批通知</h1>
    <p>内网文件上传，需要您审核后方可发布至外网</p>
  </div>
  <div class="body">
    <div class="info-card">
      <div class="info-row">
        <span class="info-label">文件名</span>
        <span class="info-value">📎 {task.file_name}</span>
      </div>
      <div class="info-row">
        <span class="info-label">文件路径</span>
        <span class="info-value">{task.file_path}</span>
      </div>
      <div class="info-row">
        <span class="info-label">上传者</span>
        <span class="info-value">{task.uploader}</span>
      </div>
      <div class="info-row">
        <span class="info-label">上传时间</span>
        <span class="info-value">{task.created_at.strftime('%Y-%m-%d %H:%M:%S')} UTC</span>
      </div>
      <div class="info-row">
        <span class="info-label">任务编号</span>
        <span class="info-value">#{task.id}</span>
      </div>
    </div>

    <p style="color:#444; font-size:14px; margin-bottom:16px;">
      请点击下方按钮进行审批，或进入详情页查看文件后再决定：
    </p>

    <div class="actions">
      <a href="{approve_url}" class="btn btn-approve">✅ 快速通过</a>
      <a href="{reject_url}"  class="btn btn-reject">❌ 快速拒绝</a>
    </div>
    <a href="{detail_url}" class="btn btn-detail">🔍 查看详情后审批</a>

    <p class="expire-note">
      ⏰ 此审批链接将于 {task.expire_at.strftime('%Y-%m-%d %H:%M') if task.expire_at else 'N/A'} UTC 过期
    </p>
  </div>
  <div class="footer">
    此邮件由 Seafile 文件审核系统自动发送，请勿回复。
  </div>
</div>
</body>
</html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_user if not settings.intranet_smtp_user else settings.intranet_smtp_user
    msg["To"] = reviewer_email
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg


def _send_via_smtp(smtp_cfg: dict, task: ReviewTask, reviewer_emails: list):
    """
    使用单个 SMTP 配置向所有审批人发送邮件。

    :param smtp_cfg:        来自 settings.active_smtp_configs 的单项配置字典
    :param task:            审核任务对象
    :param reviewer_emails: 收件人列表
    """
    host      = smtp_cfg["host"]
    port      = smtp_cfg["port"]
    user      = smtp_cfg["user"]
    password  = smtp_cfg["password"]
    use_ssl   = smtp_cfg["use_ssl"]
    app_url   = smtp_cfg["app_url"]
    label     = smtp_cfg["label"]

    try:
        if use_ssl:
            ctx = ssl.create_default_context()
            server_cls = smtplib.SMTP_SSL(host, port, context=ctx)
        else:
            server_cls = smtplib.SMTP(host, port)

        with server_cls as server:
            if not use_ssl:
                server.starttls()
            server.login(user, password)
            for reviewer in reviewer_emails:
                msg = build_review_email(task, reviewer, app_url)
                msg["From"] = user  # 覆盖 From 为实际发件账号
                server.sendmail(user, reviewer, msg.as_string())
                logger.info(f"[Email] [{label}] 已发送审批通知 -> {reviewer}（task #{task.id}，链接: {app_url}）")
    except Exception as e:
        logger.error(f"[Email] [{label}] 发送失败（task #{task.id}）: {e}")


async def send_review_notification(task: ReviewTask):
    """
    异步发送审批通知邮件。
    若配置了双 SMTP，则分别通过内网和外网 SMTP 各发一封，链接对应各自网络地址。
    若只配置了单 SMTP，则只发一封（向下兼容）。
    """
    settings = get_settings()
    if not settings.reviewer_email_list:
        logger.warning("[Email] 未配置审批人邮箱，跳过通知发送")
        return

    smtp_configs = settings.active_smtp_configs
    if not smtp_configs:
        logger.warning("[Email] 未找到任何有效的 SMTP 配置，跳过通知发送")
        return

    import asyncio
    loop = asyncio.get_event_loop()

    for cfg in smtp_configs:
        await loop.run_in_executor(
            None, _send_via_smtp, cfg, task, settings.reviewer_email_list
        )


# ─────────────────────────────────────────────
# 审批结果通知（发送给上传者）
# ─────────────────────────────────────────────

async def send_result_notification(task: ReviewTask):
    """发送审批结果通知给上传者（使用内网 SMTP 或回退到旧 SMTP）"""
    if not task.uploader_email:
        return
    settings = get_settings()

    status_text = (
        "已通过并同步至外网"
        if task.status.value == "transferred"
        else f"已被拒绝：{task.reviewer_comment}"
    )
    subject = f"【文件审批结果】{task.file_name} - {status_text}"

    html_body = f"""
<html><body style="font-family:sans-serif;padding:20px;">
<h3>您上传的文件审批结果通知</h3>
<p><b>文件名：</b>{task.file_name}</p>
<p><b>审批结果：</b>{status_text}</p>
<p><b>审批人：</b>{task.reviewed_by}</p>
<p><b>审批时间：</b>{task.reviewed_at}</p>
{"<p><b>外网路径：</b>" + task.extranet_file_path + "</p>" if task.extranet_file_path else ""}
</body></html>
"""
    # 结果通知只用内网 SMTP（或回退的单 SMTP）发送一次
    smtp_configs = settings.active_smtp_configs
    if not smtp_configs:
        logger.warning("[Email] 未找到任何有效的 SMTP 配置，跳过结果通知")
        return

    # 取第一个（内网优先）
    cfg = smtp_configs[0]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["user"]
    msg["To"] = task.uploader_email
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    import asyncio
    loop = asyncio.get_event_loop()

    def _send():
        try:
            if cfg["use_ssl"]:
                ctx = ssl.create_default_context()
                with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ctx) as s:
                    s.login(cfg["user"], cfg["password"])
                    s.sendmail(cfg["user"], task.uploader_email, msg.as_string())
            else:
                with smtplib.SMTP(cfg["host"], cfg["port"]) as s:
                    s.starttls()
                    s.login(cfg["user"], cfg["password"])
                    s.sendmail(cfg["user"], task.uploader_email, msg.as_string())
            logger.info(f"[Email] 审批结果通知已发送 -> {task.uploader_email}")
        except Exception as e:
            logger.error(f"[Email] 发送审批结果通知失败: {e}")

    await loop.run_in_executor(None, _send)

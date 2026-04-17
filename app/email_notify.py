"""
邮件通知模块

发送审批通知邮件给审批人员，包含：
- 文件名、上传者、上传时间
- 一键审批通过 / 拒绝链接
"""
import ssl
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

from .config import get_settings
from .models import ReviewTask


def build_review_email(task: ReviewTask, reviewer_email: str) -> MIMEMultipart:
    settings = get_settings()
    approve_url = f"{settings.app_base_url}/review/{task.token}?action=approve"
    reject_url = f"{settings.app_base_url}/review/{task.token}?action=reject"
    detail_url = f"{settings.app_base_url}/review/{task.token}"

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
    msg["From"] = settings.smtp_user
    msg["To"] = reviewer_email
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg


async def send_review_notification(task: ReviewTask):
    """异步发送审批通知邮件给所有审批人"""
    settings = get_settings()
    if not settings.reviewer_email_list:
        print("[Email] No reviewer emails configured, skipping notification.")
        return

    import asyncio
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _send_emails_sync, task, settings)


def _send_emails_sync(task: ReviewTask, settings):
    """同步发送邮件（在线程池中执行）"""
    try:
        if settings.smtp_use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context) as server:
                server.login(settings.smtp_user, settings.smtp_password)
                for reviewer in settings.reviewer_email_list:
                    msg = build_review_email(task, reviewer)
                    server.sendmail(settings.smtp_user, reviewer, msg.as_string())
                    print(f"[Email] Sent review notification to {reviewer} for task #{task.id}")
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
                server.starttls()
                server.login(settings.smtp_user, settings.smtp_password)
                for reviewer in settings.reviewer_email_list:
                    msg = build_review_email(task, reviewer)
                    server.sendmail(settings.smtp_user, reviewer, msg.as_string())
                    print(f"[Email] Sent review notification to {reviewer} for task #{task.id}")
    except Exception as e:
        print(f"[Email] Failed to send email for task #{task.id}: {e}")


async def send_result_notification(task: ReviewTask):
    """发送审批结果通知给上传者"""
    if not task.uploader_email:
        return
    settings = get_settings()

    status_text = "已通过并同步至外网" if task.status.value == "transferred" else f"已被拒绝：{task.reviewer_comment}"
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
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_user
    msg["To"] = task.uploader_email
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    import asyncio
    loop = asyncio.get_event_loop()

    def _send():
        try:
            if settings.smtp_use_ssl:
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context) as s:
                    s.login(settings.smtp_user, settings.smtp_password)
                    s.sendmail(settings.smtp_user, task.uploader_email, msg.as_string())
            else:
                with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as s:
                    s.starttls()
                    s.login(settings.smtp_user, settings.smtp_password)
                    s.sendmail(settings.smtp_user, task.uploader_email, msg.as_string())
        except Exception as e:
            print(f"[Email] Failed to send result notification: {e}")

    await loop.run_in_executor(None, _send)

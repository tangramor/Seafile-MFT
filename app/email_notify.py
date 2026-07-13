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

from jinja2 import Environment, FileSystemLoader

from .config import get_settings
from .i18n import get_translator, DEFAULT_LOCALE
from .models import ReviewTask, User, get_db

logger = logging.getLogger(__name__)

# 邮件模板引擎（独立于 HTTP 请求，使用默认中文 locale）
_email_env = Environment(loader=FileSystemLoader("app/templates/email"))


def _email_gettext(text: str, **kwargs) -> str:
    """邮件翻译函数：默认使用中文，后续可扩展用户语言偏好。"""
    translator = get_translator()
    # 邮件默认 zh，待用户语言偏好字段就绪后可改为用户偏好
    return translator.translate(text, "zh", **kwargs)


def _render(template_name: str, **context) -> str:
    """渲染邮件模板。"""
    template = _email_env.get_template(template_name)
    return template.render(_=_email_gettext, **context)


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

    subject = _email_gettext("【文件审批】{file_name} 待审核", file_name=task.file_name)

    expire_time = task.expire_at.strftime('%Y-%m-%d %H:%M') if task.expire_at else 'N/A'

    html_body = _render("review_notify.html",
        file_name=task.file_name,
        file_path=task.file_path,
        uploader=task.uploader,
        upload_time=task.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        task_id=task.id,
        approve_url=approve_url,
        reject_url=reject_url,
        detail_url=detail_url,
        expire_time=expire_time,
    )

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

    # 收集审批人邮箱：.env 配置 + 数据库中 reviewer/admin 角色的活跃用户
    reviewer_emails = set(settings.reviewer_email_list)

    with get_db() as db:
        db_reviewers = db.query(User).filter(
            User.role.in_(["reviewer", "admin"]),
            User.is_active == True,
            User.email != "",
            User.email.isnot(None),
        ).all()
        for u in db_reviewers:
            reviewer_emails.add(u.email)
            logger.debug(f"[Email] 已添加数据库审核者邮箱: {u.email} ({u.username})")

    if not reviewer_emails:
        logger.warning("[Email] 未配置审批人邮箱且无审核者用户，跳过通知发送")
        return

    smtp_configs = settings.active_smtp_configs
    if not smtp_configs:
        logger.warning("[Email] 未找到任何有效的 SMTP 配置，跳过通知发送")
        return

    reviewer_email_list = list(reviewer_emails)
    logger.info(f"[Email] 审批人邮箱汇总: {reviewer_email_list}")

    import asyncio
    loop = asyncio.get_event_loop()

    for cfg in smtp_configs:
        await loop.run_in_executor(
            None, _send_via_smtp, cfg, task, reviewer_email_list
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
        _email_gettext("已通过并同步至外网")
        if task.status.value == "transferred"
        else _email_gettext("已被拒绝：{comment}", comment=task.reviewer_comment or "")
    )
    subject = _email_gettext("【文件审批结果】{file_name} - {status}", file_name=task.file_name, status=status_text)

    html_body = _render("result_notify.html",
        file_name=task.file_name,
        status_text=status_text,
        reviewer_name=task.reviewed_by or "",
        review_time=str(task.reviewed_at) if task.reviewed_at else "",
        extranet_path=task.extranet_file_path or "",
    )
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

"""
FastAPI 主入口

文件检测模式（可通过 DETECTION_MODE 环境变量控制）：
  auto    → 启动时自动检测 Seafile 版本，>= 7.0 使用 Webhook，否则使用轮询（默认）
  webhook → 强制使用 Webhook（需 Seafile >= 7.0 并在后台配置 Webhook URL）
  poll    → 强制使用轮询（兼容 Seafile 6.x 及以上所有版本）
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, JSONResponse

from .config import get_settings
from .i18n import _
from .i18n.middleware import I18nMiddleware
from .models import create_tables, init_engine, migrate_db, seed_default_repo_pair, get_db
from .review import router as review_router
from .portal import router as portal_router

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

_polling_task = None
# 当前运行的检测模式，由 lifespan 设置，供 /health 和 /admin/poll-now 查询
_active_detection_mode: str = "unknown"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动 / 关闭生命周期"""
    global _polling_task, _active_detection_mode

    settings = get_settings()
    init_engine(settings.database_url)
    create_tables()  # 同步创建表（含 User / UserSession 等）
    migrate_db()     # 轻量迁移：为已有表补列（如 review_tasks.repo_pair_id）

    # 将 config 中的单仓库迁移为「默认配对」（首次启动时）
    with get_db() as db:
        seed_default_repo_pair(db, settings)

    # 初始化默认管理员账号
    from .auth import ensure_default_admin
    with get_db() as db:
        ensure_default_admin(db)

    logger.info(f"[App] Seafile MFT 审核服务启动中...")
    logger.info(f"[App] 内网 Seafile : {settings.intranet_seafile_url}")
    logger.info(f"[App] 外网 Seafile : {settings.extranet_seafile_url}")
    logger.info(f"[App] LDAP 服务    : {settings.ldap_host or '未配置（仅本地账号）'}")

    # ── 检测文件检测模式 ──────────────────────────────────────────────────
    from .seafile_version import detect_detection_mode
    mode, seafile_version, is_pro = await detect_detection_mode(
        settings.intranet_seafile_url,
        settings.intranet_seafile_token,
    )
    _active_detection_mode = mode

    if mode == "webhook":
        # ── Webhook 模式 ────────────────────────────────────────────────
        from .webhook import router as webhook_router
        app.include_router(webhook_router)

        webhook_url_hint = (
            f"{(settings.intranet_app_url or settings.app_base_url).rstrip('/')}/webhook/seafile"
        )
        edition = "专业版" if is_pro else "社区版"
        logger.info(f"[App] 检测模式     : Webhook（Seafile {seafile_version or '?'} {edition}）")
        logger.info(f"[App] Webhook URL  : {webhook_url_hint}")
        if not is_pro:
            logger.warning(
                "[App] ⚠️  当前为 Seafile 社区版，Webhook API 不可用！"
                "请将 DETECTION_MODE 改为 poll 后重启。"
            )
        else:
            logger.info(f"[App] ⚠️  请确认 Seafile 后台已配置上方 Webhook URL")
        if not settings.webhook_secret:
            logger.warning("[App] ⚠️  未配置 WEBHOOK_SECRET，签名验证已跳过（建议生产环境配置）")
        else:
            logger.info(f"[App] Webhook 签名: 已启用")

    else:
        # ── 轮询模式 ──────────────────────────────────────────────────────
        from .poller import start_polling_loop
        logger.info(f"[App] 检测模式     : 轮询（Seafile {seafile_version or '未知版本'}）")
        logger.info(f"[App] 轮询间隔     : {settings.poll_interval_seconds} 秒")

        if settings.poll_on_startup:
            logger.info("[App] 正在启动轮询任务...")
            _polling_task = asyncio.create_task(start_polling_loop())
            logger.info("[App] 轮询任务已启动")
        else:
            logger.info("[App] 轮询任务已禁用（poll_on_startup=False）")

    logger.info(f"[App] Seafile MFT 服务启动完成 ✓")

    yield

    # ── 优雅关闭 ──────────────────────────────────────────────────────────
    if _polling_task and not _polling_task.done():
        _polling_task.cancel()
        try:
            await _polling_task
        except asyncio.CancelledError:
            pass
    logger.info("[App] 服务已关闭")


app = FastAPI(
    title="Seafile 内外网文件审核系统",
    description=(
        "支持 Webhook（Seafile >= 7.0）和轮询（Seafile < 7.0）两种文件检测方式，"
        "通过 Web 上传/邮件审批将内网文件同步至外网 Seafile。"
    ),
    version="2.1.0",
    lifespan=lifespan,
)

# 注册 i18n 中间件（语言检测）
app.add_middleware(I18nMiddleware)

# 注册路由
app.include_router(portal_router)   # 登录、Dashboard、上传、审核看板、下载、用户管理
app.include_router(review_router)   # 邮件 token 审批链接
# Webhook 路由在 lifespan 中动态注册（仅 Webhook 模式下注册）


@app.get("/")
async def root():
    """根路径重定向到 Dashboard（未登录则跳转登录页）"""
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/health")
async def health():
    """健康检查：包含当前检测模式信息"""
    settings = get_settings()
    return {
        "status": "ok",
        "detection_mode": _active_detection_mode,
        "poll_interval": settings.poll_interval_seconds if _active_detection_mode == "poll" else None,
        "webhook_enabled": _active_detection_mode == "webhook",
    }


@app.post("/admin/poll-now")
async def trigger_poll_now():
    """手动触发一次立即轮询（仅轮询模式下有效，管理员用）"""
    if _active_detection_mode != "poll":
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "message": _("当前为 {mode} 模式，无需手动触发轮询", mode=_active_detection_mode),
            }
        )
    from .poller import poll_once
    asyncio.create_task(poll_once())
    return {"status": "triggered", "message": _("轮询任务已在后台启动")}


@app.get("/admin/detection-mode")
async def get_detection_mode():
    """查询当前文件检测模式（管理员用）"""
    settings = get_settings()
    info: dict = {
        "active_mode": _active_detection_mode,
        "config_mode": settings.detection_mode,
    }
    if _active_detection_mode == "webhook":
        webhook_url_hint = (
            f"{(settings.intranet_app_url or settings.app_base_url).rstrip('/')}/webhook/seafile"
        )
        info["webhook_url"] = webhook_url_hint
        info["webhook_secret_configured"] = bool(settings.webhook_secret)
    else:
        info["poll_interval_seconds"] = settings.poll_interval_seconds
    return info

"""
FastAPI 主入口
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import get_settings
from .models import create_tables, init_engine
from .poller import start_polling_loop
from .review import router as review_router

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

_polling_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动 / 关闭生命周期"""
    global _polling_task

    settings = get_settings()
    init_engine(settings.database_url)
    create_tables()  # 同步创建表

    logger.info(f"[App] Seafile MFT 审核服务已启动")
    logger.info(f"[App] 内网 Seafile : {settings.intranet_seafile_url}")
    logger.info(f"[App] 外网 Seafile : {settings.extranet_seafile_url}")
    logger.info(f"[App] 审批人邮箱  : {settings.reviewer_emails}")
    logger.info(f"[App] 轮询间隔    : {settings.poll_interval_seconds} 秒")
    logger.info(f"[App] 启动轮询    : {settings.poll_on_startup}")

    # 启动后台轮询任务
    if settings.poll_on_startup:
        logger.info("[App] 正在启动轮询任务...")
        _polling_task = asyncio.create_task(start_polling_loop())
        logger.info("[App] 轮询任务已启动")
    else:
        logger.info("[App] 轮询任务已禁用")

    yield

    # 优雅关闭轮询任务
    if _polling_task and not _polling_task.done():
        _polling_task.cancel()
        try:
            await _polling_task
        except asyncio.CancelledError:
            pass
    logger.info("[App] 服务关闭")


app = FastAPI(
    title="Seafile 内外网文件审核系统",
    description="定时轮询内网 Seafile → 邮件审批 → 同步至外网 Seafile",
    version="1.1.0",
    lifespan=lifespan,
)

# 注册路由（移除了 webhook_router，改为后台轮询）
app.include_router(review_router)


@app.get("/")
async def root():
    return {
        "service": "Seafile MFT 文件审核系统",
        "version": "1.1.0",
        "mode": "polling",
        "endpoints": {
            "review": "GET  /review/{token}",
            "admin":  "GET  /admin/tasks",
            "docs":   "GET  /docs",
        },
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/admin/poll-now")
async def trigger_poll_now():
    """
    手动触发一次立即轮询（管理员用，无需等待下次定时）
    POST /admin/poll-now
    """
    from .poller import poll_once
    asyncio.create_task(poll_once())
    return {"status": "triggered", "message": "轮询任务已在后台启动"}

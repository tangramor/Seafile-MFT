"""
FastAPI 主入口
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .models import create_tables, init_engine
from .webhook import router as webhook_router
from .review import router as review_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动 / 关闭生命周期"""
    settings = get_settings()
    init_engine(settings.database_url)
    await create_tables()
    print(f"[App] Seafile MFT 审核服务已启动")
    print(f"[App] 内网 Seafile: {settings.intranet_seafile_url}")
    print(f"[App] 外网 Seafile: {settings.extranet_seafile_url}")
    print(f"[App] 审批人邮箱: {settings.reviewer_emails}")
    yield
    print("[App] 服务关闭")


app = FastAPI(
    title="Seafile 内外网文件审核系统",
    description="内网文件上传 → 邮件审批 → 同步至外网 Seafile",
    version="1.0.0",
    lifespan=lifespan,
)

# 注册路由
app.include_router(webhook_router)
app.include_router(review_router)


@app.get("/")
async def root():
    return {
        "service": "Seafile MFT 文件审核系统",
        "version": "1.0.0",
        "endpoints": {
            "webhook": "POST /webhook/seafile",
            "review":  "GET  /review/{token}",
            "admin":   "GET  /admin/tasks",
            "docs":    "GET  /docs",
        },
    }


@app.get("/health")
async def health():
    return {"status": "ok"}

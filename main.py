from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from tortoise.contrib.fastapi import register_tortoise

from app.core.config import settings
from app.core.logging import setup_logging
from app.core.exceptions import register_exception_handlers
from app.infrastructure.meilisearch_client import meili_client
from app.infrastructure.redis_client import init_redis, close_redis
from app.api.v1 import artworks, auth, users, interactions, search, tags, creators, ws, notifications, reports, admin, \
    messages, announcements, payments, moderation, captcha, membership, translations, account_claims, pixiv_sync
from app.services.storage_service import storage, LocalStorageBackend

import logging
import os
import sys
import subprocess

setup_logging(log_level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)


# FastAPI 启动与关闭时的生命周期管理
async def _seed_initial_admin() -> None:
    """首次部署时自动创建管理员账号（仅当 DB 中不存在任何 admin 且 env 已配置时执行）。"""
    from app.models.user import User
    from app.core.security import get_password_hash

    u = settings.INITIAL_ADMIN_USERNAME.strip()
    e = settings.INITIAL_ADMIN_EMAIL.strip()
    p = settings.INITIAL_ADMIN_PASSWORD.strip()
    if not (u and e and p):
        return  # 未配置，跳过

    if await User.filter(role="admin").exists():
        return  # 已存在管理员，跳过

    await User.create(
        login_id=u,
        username=u,
        email=e,
        password_hash=get_password_hash(p),
        role="admin",
    )
    logger.info(f"✅ 初始管理员账号已创建: {u} <{e}>")


_worker_proc: subprocess.Popen | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _worker_proc
    logger.info("🚀 正在唤醒数据库引擎与 Redis...")

    meili_client.init_index()
    if settings.ENABLE_AI_FEATURES:
        from app.infrastructure.qdrant_client import qdrant_client as _qc
        _qc.init_collection()
    else:
        logger.info("⏭️ ENABLE_AI_FEATURES=false，跳过 Qdrant 初始化")

    try:
        await init_redis()
        logger.info("✅ Redis 已连接")
    except Exception as e:
        logger.warning(f"⚠️  Redis 连接失败（降级运行）: {e}")

    try:
        await _seed_initial_admin()
    except Exception as e:
        logger.warning(f"⚠️  初始管理员创建失败: {e}")

    # ── 启动 ARQ Worker 子进程 ──────────────────────────────────────
    try:
        _worker_proc = subprocess.Popen(
            [sys.executable, "-m", "arq", "app.worker.worker.WorkerSettings"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        logger.info(f"✅ ARQ Worker 已启动 (PID {_worker_proc.pid})")
    except Exception as e:
        logger.error(f"❌ ARQ Worker 启动失败: {e}")

    yield

    logger.info("🛑 正在关闭服务器...")
    if _worker_proc and _worker_proc.poll() is None:
        _worker_proc.terminate()
        try:
            _worker_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _worker_proc.kill()
        logger.info("✅ ARQ Worker 已停止")
    await close_redis()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    lifespan=lifespan,
)

# -------------------------------------------------------------------
# CORS 中间件（前后端分离必须）
# -------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=getattr(settings, "ALLOWED_ORIGINS", ["*"]),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册全局异常处理器
register_exception_handlers(app)

if isinstance(storage, LocalStorageBackend):
    os.makedirs("uploads", exist_ok=True)
    app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")


@app.get("/")
async def health_check():
    return {"status": "online", "project": settings.PROJECT_NAME}


# 挂载路由，指定前缀和 Swagger 标签
app.include_router(auth.router, prefix=f"{settings.API_V1_STR}/auth", tags=["安全认证"])
app.include_router(users.router, prefix=f"{settings.API_V1_STR}/users", tags=["用户管理"])
app.include_router(artworks.router, prefix=f"{settings.API_V1_STR}/artworks", tags=["作品与投稿"])
app.include_router(search.router, prefix=f"{settings.API_V1_STR}/search", tags=["检索引擎"])
app.include_router(tags.router, prefix=f"{settings.API_V1_STR}/tags", tags=["AI基准库"])
app.include_router(interactions.router, prefix=f"{settings.API_V1_STR}/interactions", tags=["社交互动"])
app.include_router(creators.router, prefix=f"{settings.API_V1_STR}/creators", tags=["创作者管理"])
app.include_router(notifications.router, prefix=f"{settings.API_V1_STR}/notifications", tags=["通知中心"])
app.include_router(reports.router, prefix=f"{settings.API_V1_STR}/reports", tags=["内容举报"])
app.include_router(admin.router, prefix=f"{settings.API_V1_STR}/admin", tags=["后台管理"])
app.include_router(messages.router, prefix=f"{settings.API_V1_STR}/messages", tags=["私信聊天"])
app.include_router(announcements.router, prefix=f"{settings.API_V1_STR}/announcements", tags=["公告中心"])
app.include_router(payments.router, prefix=f"{settings.API_V1_STR}/payments", tags=["支付网关"])
app.include_router(moderation.router, prefix=f"{settings.API_V1_STR}/admin/moderation", tags=["内容审核"])
app.include_router(captcha.router, prefix=f"{settings.API_V1_STR}/captcha", tags=["安全验证码"])
app.include_router(membership.router, prefix=f"{settings.API_V1_STR}/membership", tags=["会员体系"])
app.include_router(translations.router, prefix=f"{settings.API_V1_STR}/artworks", tags=["漫画翻译"])
app.include_router(account_claims.router, prefix=f"{settings.API_V1_STR}/account-claims", tags=["账号认领"])
app.include_router(pixiv_sync.router, prefix=settings.API_V1_STR, tags=["Pixiv同步"])
app.include_router(ws.router, prefix=settings.API_V1_STR, tags=["实时通信"])

# 挂载 PostgreSQL（Tortoise ORM）
register_tortoise(
    app,
    db_url=settings.DATABASE_URL,
    modules={"models": ["app.models"]},
    generate_schemas=True,
    add_exception_handlers=True,
)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, reload_excludes=["docker-data/*", "logs/*"])

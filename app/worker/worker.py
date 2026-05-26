"""
ARQ Worker 入口。

启动方式：
    .venv/Scripts/python.exe -m arq app.worker.worker.WorkerSettings

或在 docker-compose 中作为独立服务运行。
"""
from __future__ import annotations

import logging
from arq import cron
from arq.connections import RedisSettings
from app.core.config import settings
from app.core.logging import setup_logging

setup_logging(log_level=settings.LOG_LEVEL, log_dir="logs/worker")
logger = logging.getLogger(__name__)


async def startup(ctx: dict) -> None:
    """Worker 启动时初始化 ORM 和 Redis。"""
    from tortoise import Tortoise
    from app.models import (
        User, Artwork, ArtworkImage, ArtworkSeries, ArtworkSeriesItem,
        SeriesFollow, StyleReference, BookmarkFolder, Bookmark, Comment, CommentLike,
        Like, ViewHistory, ConceptAnchor, ArtworkTag, TagVote, Follow,
        UserBlock, FollowTag, Notification, Commission, ArtworkReport,
        UserReport, DirectMessage, Announcement, PasswordResetToken, SiteConfig,
        AccountClaimRequest,
    )
    await Tortoise.init(
        db_url=settings.DATABASE_URL,
        modules={"models": ["app.models"]},
    )
    logger.info("[ARQ Worker] ORM 初始化完成")
    from app.infrastructure.redis_client import init_redis
    await init_redis()
    logger.info("[ARQ Worker] Redis 初始化完成")


async def shutdown(ctx: dict) -> None:
    """Worker 关闭时释放 ORM 连接。"""
    from tortoise import Tortoise
    await Tortoise.close_connections()
    from app.infrastructure.redis_client import close_redis
    await close_redis()
    logger.info("[ARQ Worker] ORM 连接已关闭")


from app.worker.tasks import (
    task_ai_tag_artwork,
    task_send_email,
    task_fanout_new_artwork,
    task_sync_artwork_meili,
    task_delete_artwork_meili,
    task_flush_view_counts,
    task_refresh_trending_tags,
    task_flush_email_notifications,
    task_translate_artwork,
    task_publish_scheduled_artworks,
    task_pixiv_node_ping,
    task_pixiv_sync_assign,
    task_pixiv_sync_poll,
    task_pixiv_sync_import_cached,
)


def _parse_redis_settings(url: str) -> RedisSettings:
    """将 redis:// URL 转为 arq.RedisSettings。"""
    import urllib.parse
    parsed = urllib.parse.urlparse(url)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        password=parsed.password,
        database=int(parsed.path.lstrip("/") or 0),
    )


class WorkerSettings:
    functions = [
        task_ai_tag_artwork,
        task_send_email,
        task_fanout_new_artwork,
        task_sync_artwork_meili,
        task_delete_artwork_meili,
        task_flush_view_counts,
        task_refresh_trending_tags,
        task_flush_email_notifications,
        task_translate_artwork,
        task_publish_scheduled_artworks,
        task_pixiv_node_ping,
        task_pixiv_sync_assign,
        task_pixiv_sync_poll,
        task_pixiv_sync_import_cached,
    ]

    # 定时任务
    cron_jobs = [
        cron(task_flush_view_counts, second=0),                   # 每分钟整点触发
        cron(task_publish_scheduled_artworks, second=30),          # 每分钟30秒触发（错开 flush_view）
        cron(task_refresh_trending_tags, minute=0, second=0),      # 每小时整点触发
        cron(task_flush_email_notifications, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}, second=0),  # 每 5 分钟
        # Pixiv 同步定时任务
        cron(task_pixiv_node_ping, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}, second=30),  # 每 5 分钟
        cron(task_pixiv_sync_assign, minute=0, second=0),          # 每小时（与 trending_tags 错开 by second）
        cron(task_pixiv_sync_poll, minute={0, 10, 20, 30, 40, 50}, second=45),  # 每 10 分钟
    ]

    on_startup = startup
    on_shutdown = shutdown

    redis_settings = _parse_redis_settings(settings.REDIS_URL)

    max_jobs = 10
    job_timeout = 300       # 单个任务最长 5 min
    keep_result = 3600      # 保留任务结果 1 hr
    retry_jobs = True
    max_tries = 3

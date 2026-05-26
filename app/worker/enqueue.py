"""
任务入队助手 — 在 FastAPI 路由中调用，将任务推送到 ARQ 队列。

用法示例：
    from app.worker.enqueue import enqueue

    await enqueue("task_fanout_new_artwork",
                  artwork_id=artwork.id,
                  author_id=author.id,
                  title=artwork.title)
"""
from __future__ import annotations

import logging
from typing import Any

from arq.connections import ArqRedis, create_pool
from app.worker.worker import _parse_redis_settings
from app.core.config import settings

logger = logging.getLogger(__name__)

_pool: ArqRedis | None = None


async def get_arq_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(_parse_redis_settings(settings.REDIS_URL))
    return _pool


async def enqueue(task_name: str, _defer_by: int | None = None, **kwargs: Any) -> None:
    """
    将任务加入 ARQ 队列，失败时静默降级（不影响主请求）。

    Args:
        task_name: tasks.py 中的函数名称字符串。
        _defer_by: 可选延迟秒数。
        **kwargs:  传递给任务函数的关键字参数。
    """
    try:
        pool = await get_arq_pool()
        job = await pool.enqueue_job(task_name, **kwargs, _defer_by=_defer_by)
        if job:
            logger.debug(f"[ARQ] 任务入队: {task_name} job_id={job.job_id}")
        else:
            logger.warning(f"[ARQ] 任务重复，跳过: {task_name}")
    except Exception as e:
        logger.error(f"[ARQ] 入队失败 {task_name}: {e}")

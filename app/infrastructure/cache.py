"""
通用缓存助手 — 基于 Redis 的 JSON 键值缓存。

用法示例：
    data = await cache_get("artwork:123")
    if data is None:
        data = await fetch_from_db(123)
        await cache_set("artwork:123", data, ttl=300)
"""
from __future__ import annotations

import json
from typing import Any, Optional

from app.infrastructure.redis_client import get_redis

# ── TTL 常量（秒）──────────────────────────────────────────────────────────────
TTL_ARTWORK        = 300   # 5 min — 作品详情
TTL_USER_PROFILE   = 300   # 5 min — 用户资料
TTL_TAG_LIST       = 600   # 10 min — 热门标签
TTL_RANKING        = 60    # 1 min — 排行榜（随 view_count 刷写周期对齐）
TTL_FEED           = 30    # 30 s — 首页推荐 Feed
TTL_SITE_CONFIG    = 3600  # 1 hr — 全站配置（变动少）
TTL_SEARCH         = 30    # 30 s — 搜索结果
TTL_NOTIF_COUNT    = 60    # 1 min — 未读通知计数
TTL_BLOCK_FILTER   = 300   # 5 min — 用户拉黑关系过滤字符串（follow/block 时失效）
TTL_FOLLOWING_IDS  = 300   # 5 min — 用户关注 ID 列表
TTL_FOLLOWED_TAGS  = 300   # 5 min — 用户关注标签列表
TTL_RELATED        = 300   # 5 min — 相关作品推荐
TTL_REC_TAGS       = 300   # 5 min — 个性化推荐 top_tags
TTL_TAG_IDF        = 21600 # 6 hr — 标签 IDF 权重（全站统计，变化慢）
TTL_REC_UVEC       = 600   # 10 min — 用户偏好向量（HVCR-U）


async def cache_get(key: str) -> Optional[Any]:
    """从 Redis 读取 JSON 值，不存在或 Redis 不可用时返回 None。"""
    try:
        raw = await get_redis().get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception:
        return None


async def cache_set(key: str, value: Any, ttl: int = 300) -> None:
    """将值序列化为 JSON 存入 Redis，失败时静默忽略。"""
    try:
        await get_redis().setex(key, ttl, json.dumps(value, default=str))
    except Exception:
        pass


async def cache_delete(key: str) -> None:
    """删除单个缓存键。"""
    try:
        await get_redis().delete(key)
    except Exception:
        pass


async def cache_delete_pattern(pattern: str) -> None:
    """按 glob 模式批量删除缓存键（使用 SCAN 代替 KEYS，避免阻塞 Redis）。"""
    try:
        r = get_redis()
        keys = []
        async for key in r.scan_iter(match=pattern, count=100):
            keys.append(key)
        if keys:
            await r.delete(*keys)
    except Exception:
        pass


# ── 便捷的具名失效函数 ──────────────────────────────────────────────────────────

async def invalidate_artwork(artwork_id: int) -> None:
    await cache_delete(f"artwork:{artwork_id}")
    await cache_delete(f"artwork:{artwork_id}:tags")


async def invalidate_user(user_id: int) -> None:
    await cache_delete(f"user:{user_id}")
    await cache_delete(f"user:{user_id}:stats")


async def invalidate_site_config() -> None:
    await cache_delete("site_config")


async def invalidate_block_filter(user_id: int) -> None:
    """拉黑/取关时调用，清除该用户的 Meilisearch 屏蔽过滤字符串缓存。"""
    await cache_delete(f"block_filter:{user_id}")


async def invalidate_following(user_id: int) -> None:
    """关注/取关时调用，清除关注 ID 列表和标签列表缓存。"""
    await cache_delete(f"following_ids:{user_id}")
    await cache_delete(f"followed_tags:{user_id}")
    await cache_delete(f"rec_tags:{user_id}")


async def invalidate_related(artwork_id: int) -> None:
    """作品标签变动时调用，清除相关作品推荐缓存（含全部 limit 变种）。"""
    await cache_delete_pattern(f"related:{artwork_id}:*")

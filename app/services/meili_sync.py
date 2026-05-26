"""
MeiliSearch 实时同步工具：
- sync_artwork_to_meili(artwork, all_tags) — 作品创建/修改/AI 刷新后同步到 artworks 索引
- sync_user_to_meili(user)                — 用户注册/资料变更/角色/封禁状态变更时调用
- sync_tags_to_meili(tag_names)           — 作品标签新增/删除时更新 tags 索引计数
"""
import asyncio
import logging
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.user import User

logger = logging.getLogger(__name__)


async def sync_artwork_to_meili(artwork, all_tags: Optional[List[str]] = None) -> None:
    """
    将单个作品文档同步到 Meilisearch artworks 索引。
    all_tags 如果已在上层查询则直接传入，否则内部自行查询。
    """
    from app.infrastructure.meilisearch_client import meili_client
    from app.models.tag import ArtworkTag
    from app.models.user import User as UserModel

    try:
        if all_tags is None:
            all_tags = list(await ArtworkTag.filter(artwork_id=artwork.id).values_list("tag_name", flat=True))
        author = await UserModel.get_or_none(id=artwork.author_id)
        doc = {
            "id":             str(artwork.id),
            "title":          artwork.title,
            "description":    artwork.description or "",
            "tags":           list(all_tags),
            "is_ai":          artwork.is_ai,
            "rating":         artwork.rating,
            "visibility":     artwork.visibility,
            "artwork_type":   artwork.artwork_type or "illustration",
            "content_origin": artwork.content_origin or "original",
            "author_id":      str(artwork.author_id),
            "author_name":    author.username if author else "",
            "like_count":     artwork.like_count,
            "view_count":     artwork.view_count,
            "bookmark_count": artwork.bookmark_count,
            "created_at":     artwork.created_at.timestamp(),
        }
        await asyncio.to_thread(meili_client.add_documents, [doc])
        logger.debug(f"✅ Meili artworks 同步: {artwork.title} (id={artwork.id})")
    except Exception as e:
        logger.error(f"❌ Meili artworks 同步失败 (id={artwork.id}): {e}")


async def sync_user_to_meili(user) -> None:
    """
    将单个用户文档同步到 Meilisearch users 索引。
    在线程池中执行（Meili SDK 为同步调用）。
    """
    from app.infrastructure.meilisearch_client import meili_client
    from app.models.social import Follow

    try:
        followers_count = await Follow.filter(followed_id=user.id).count()
        doc = {
            "id": str(user.id),
            "username": user.username,
            "bio": user.bio or "",
            "avatar_url": user.avatar_url or "",
            "is_creator": user.is_creator,
            "is_banned": user.is_banned,
            "role": user.role,
            "followers_count": followers_count,
            "created_at": int(user.created_at.timestamp()) if user.created_at else 0,
        }
        await asyncio.to_thread(meili_client.add_users, [doc])
        logger.debug(f"✅ Meili users 同步: {user.username} (id={user.id})")
    except Exception as e:
        logger.error(f"❌ Meili users 同步失败 (id={user.id}): {e}")


async def sync_tags_to_meili(tag_names: List[str]) -> None:
    """
    对指定标签重新计算使用次数，并同步到 Meilisearch tags 索引。
    使用单次 GROUP BY 聚合替代 N 次 COUNT 查询（N+1 修复）。
    在线程池中执行 Meili 写入。
    """
    from app.infrastructure.meilisearch_client import meili_client
    from app.models.tag import ArtworkTag
    from tortoise.functions import Count
    from datetime import datetime, timedelta, timezone

    if not tag_names:
        return

    try:
        names = list(set(tag_names))
        since_7d = datetime.now(timezone.utc) - timedelta(days=7)

        # 单次聚合查询所有标签的总计数
        total_rows = (
            await ArtworkTag.filter(tag_name__in=names)
            .annotate(cnt=Count("id"))
            .group_by("tag_name")
            .values("tag_name", "cnt")
        )
        total_map = {r["tag_name"]: r["cnt"] for r in total_rows}

        # 单次聚合查询近 7 天计数
        week_rows = (
            await ArtworkTag.filter(tag_name__in=names, created_at__gte=since_7d)
            .annotate(cnt=Count("id"))
            .group_by("tag_name")
            .values("tag_name", "cnt")
        )
        week_map = {r["tag_name"]: r["cnt"] for r in week_rows}

        docs = [
            {
                "id": name,
                "tag_name": name,
                "count": total_map.get(name, 0),
                "count_7d": week_map.get(name, 0),
            }
            for name in names
        ]
        if docs:
            await asyncio.to_thread(meili_client.add_tags, docs)
            logger.debug(f"✅ Meili tags 同步: {[d['tag_name'] for d in docs]}")
    except Exception as e:
        logger.error(f"❌ Meili tags 同步失败: {e}")

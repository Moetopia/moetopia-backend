import os
import asyncio
import logging
from io import BytesIO
from typing import List, Optional, Tuple
from fastapi import UploadFile, HTTPException
from app.models.artwork import Artwork, ArtworkImage, ArtworkSeries, ArtworkSeriesItem
from app.models.user import User
from app.schemas.artwork_schema import ArtworkCreate
from app.services.storage_service import storage

logger = logging.getLogger(__name__)

MAX_DISPLAY_PX = 1200  # 展示用图片最大尺寸（长边）


def _compress_to_max(raw: bytes, max_px: int = MAX_DISPLAY_PX):
    """若图像任一边超过 max_px，则返回 (compressed_bytes, width, height)。
    否则返回 (None, None, None) 表示不需要压缩。跳过 GIF 动图。
    """
    try:
        from PIL import Image as PILImage
        with PILImage.open(BytesIO(raw)) as img:
            fmt = img.format or 'JPEG'
            if fmt == 'GIF' or (img.width <= max_px and img.height <= max_px):
                return None, None, None
            img_copy = img.copy()
        img_copy.thumbnail((max_px, max_px), PILImage.LANCZOS)
        out = BytesIO()
        save_fmt = fmt if fmt in ('JPEG', 'PNG', 'WEBP') else 'JPEG'
        kw: dict = {}
        if save_fmt == 'JPEG':
            kw = {'quality': 90, 'optimize': True}
        img_copy.save(out, format=save_fmt, **kw)
        return out.getvalue(), img_copy.width, img_copy.height
    except Exception:
        return None, None, None


class ArtworkService:

    @staticmethod
    async def create_artwork_flow(
        user_id: int,
        artwork_data: ArtworkCreate,
        files: List[UploadFile],
    ) -> Tuple[Artwork, List[ArtworkImage]]:
        """处理完整的作品上传与落库工作流"""
        # 1. Postgres 创建主作品记录
        artwork = await Artwork.create(
            author_id=user_id,
            title=artwork_data.title,
            description=artwork_data.description,
            artwork_type=getattr(artwork_data, 'artwork_type', 'illustration'),
            is_ai=artwork_data.is_ai,
            rating=artwork_data.rating,
            visibility=artwork_data.visibility,
            allow_ai_tagging=getattr(artwork_data, 'allow_ai_tagging', True),
            allow_community_tagging=getattr(artwork_data, 'allow_community_tagging', True),
            content_origin=getattr(artwork_data, 'content_origin', 'original'),
            pixiv_id=getattr(artwork_data, 'pixiv_id', None),
            source=getattr(artwork_data, 'source', None),
            original_author_name=getattr(artwork_data, 'original_author_name', None),
        )

        from app.models.tag import ArtworkTag
        if artwork_data.tags:
            tag_objects = [
                ArtworkTag(
                    artwork_id=artwork.id,
                    tag_name=t.strip().lower(),
                    type="author",
                    confidence=1.0,
                )
                for t in artwork_data.tags
                if t.strip()
            ]
            await ArtworkTag.bulk_create(tag_objects, ignore_conflicts=True)

        # 2. 存储文件 + 建立 DB 记录（撞车检测由 Qdrant 向量相似度在 sync_service 异步完成）
        allowed_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        saved_images = []

        def _measure_dimensions(raw: bytes):
            try:
                from PIL import Image as PILImage
                with PILImage.open(BytesIO(raw)) as img:
                    return img.width, img.height
            except Exception:
                return None, None

        for index, file in enumerate(files):
            data = await file.read()
            if len(data) > 20 * 1024 * 1024:
                await artwork.delete()
                raise HTTPException(status_code=400, detail=f"图片「{file.filename}」超过 20MB 限制")
            file_ext = os.path.splitext(file.filename or "image.jpg")[1].lower() or ".jpg"
            if file_ext not in allowed_exts:
                await artwork.delete()
                raise HTTPException(status_code=400, detail=f"不支持的文件类型: {file_ext}")
            secure_filename = f"{artwork.id}_{index}{file_ext}"

            # 尝试压缩到 1200px。如果图像超大，先存原图，再存压缩版。
            compressed, c_w, c_h = await asyncio.to_thread(_compress_to_max, data)
            if compressed is not None:
                original_url = await storage.save(data, f"artworks/{artwork.id}_{index}_orig{file_ext}")
                file_url = await storage.save(compressed, f"artworks/{secure_filename}")
                width, height = c_w, c_h
            else:
                file_url = await storage.save(data, f"artworks/{secure_filename}")
                original_url = None
                width, height = await asyncio.to_thread(_measure_dimensions, data)

            img_record = await ArtworkImage.create(
                artwork=artwork,
                file_url=file_url,
                original_url=original_url,
                width=width,
                height=height,
                sort_order=index,
            )
            saved_images.append(img_record)

        # 3. 触发 ARQ 后台任务（支持自动重试）
        from app.worker.enqueue import enqueue
        await enqueue("task_ai_tag_artwork", artwork_id=artwork.id)

        return artwork, saved_images

    @staticmethod
    async def get_artworks(
        limit: int = 20,
        offset: int = 0,
        user: Optional[User] = None,
        artwork_type: Optional[str] = None,
    ) -> List[Artwork]:
        """公开列表，强制安全过滤"""
        qs = Artwork.filter(visibility="public").prefetch_related("images", "tags", "author")
        if artwork_type:
            if artwork_type == 'illustration':
                qs = qs.filter(artwork_type__in=['illustration', 'animated'])
            else:
                qs = qs.filter(artwork_type=artwork_type)
        if user is None or not user.r18_enabled:
            qs = qs.filter(rating="safe")
        if user is not None:
            if user.hide_ai_generated:
                qs = qs.filter(is_ai=False)
            if user.muted_user_ids:
                qs = qs.exclude(author_id__in=user.muted_user_ids)
            if user.muted_tags:
                from app.models.tag import ArtworkTag
                muted_qs = ArtworkTag.filter(
                    tag_name__in=[t.lower() for t in user.muted_tags]
                ).values_list("artwork_id", flat=True)
                qs = qs.exclude(id__in=muted_qs)
        return await qs.order_by("-created_at").offset(offset).limit(limit)

    @staticmethod
    async def get_artwork(
        artwork_id: int,
        requesting_user: Optional[User] = None,
        ip_address: Optional[str] = None,
    ) -> Artwork:
        artwork = await Artwork.get_or_none(id=artwork_id).prefetch_related("images", "author", "tags")
        if not artwork:
            raise HTTPException(status_code=404, detail="Artwork not found")

        # 管理员/版主：跳过可见性与内容评级检查（审核需要）
        is_privileged = requesting_user is not None and requesting_user.role in ("admin", "moderator")

        if not is_privileged:
            # 可见性检查
            if artwork.visibility == "private":
                if requesting_user is None or artwork.author_id != requesting_user.id:
                    raise HTTPException(status_code=403, detail="This artwork is private")
            elif artwork.visibility == "followers":
                if requesting_user is None:
                    raise HTTPException(status_code=403, detail="Login required to view this artwork")
                if artwork.author_id != requesting_user.id:
                    from app.models.social import Follow
                    is_following = await Follow.exists(follower_id=requesting_user.id, followed_id=artwork.author_id)
                    if not is_following:
                        raise HTTPException(status_code=403, detail="Only followers can view this artwork")

            # R-18 内容检查
            if artwork.rating != "safe":
                if requesting_user is None or not requesting_user.r18_enabled:
                    raise HTTPException(status_code=403, detail="R-18 content requires account setting")

        # 记录浏览历史并增加浏览量（1 小时内同用户/IP 去重）
        from app.models.interaction import ViewHistory
        from datetime import datetime, timedelta, timezone
        one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        if requesting_user:
            already_viewed = await ViewHistory.exists(
                artwork_id=artwork.id,
                user_id=requesting_user.id,
                viewed_at__gte=one_hour_ago,
            )
        else:
            already_viewed = await ViewHistory.exists(
                artwork_id=artwork.id,
                user_id=None,
                ip_address=ip_address,
                viewed_at__gte=one_hour_ago,
            )
        if not already_viewed:
            await ViewHistory.create(
                user_id=requesting_user.id if requesting_user else None,
                artwork_id=artwork.id,
                ip_address=ip_address,
            )
            # 浏览量写入 Redis 缓冲区，由定时任务批量刷写到 DB，避免每次请求都写库
            try:
                from app.infrastructure.redis_client import get_redis
                r = get_redis()
                await r.hincrby("view_counts", str(artwork.id), 1)
            except Exception:
                # Redis 不可用时降级：直接写 DB
                artwork.view_count += 1
                await artwork.save(update_fields=["view_count"])

        return artwork

    @staticmethod
    async def modify_artwork(artwork_id: int, user_id: int, data: dict) -> Artwork:
        artwork = await Artwork.get_or_none(id=artwork_id).prefetch_related("images", "tags", "author")
        if not artwork:
            raise HTTPException(status_code=404, detail="Artwork not found")
        if artwork.author_id != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")

        if "title" in data:
            artwork.title = data["title"]
        if "description" in data:
            artwork.description = data["description"]
        if "rating" in data:
            artwork.rating = data["rating"]
        if "visibility" in data:
            _VISIBILITY_ORDER = {"private": 0, "followers": 1, "public": 2}
            new_vis = data["visibility"]
            if _VISIBILITY_ORDER.get(new_vis, 0) > _VISIBILITY_ORDER.get(artwork.visibility, 0):
                if artwork.moderation_status == "under_review":
                    raise HTTPException(status_code=400, detail="作品正在审核中，暂不能提升可见性")
                if artwork.moderation_status == "rejected":
                    raise HTTPException(status_code=400, detail="作品已被拒绝，不能提升可见性")
            artwork.visibility = new_vis
        if "is_ai" in data:
            artwork.is_ai = data["is_ai"]
        if "artwork_type" in data:
            artwork.artwork_type = data["artwork_type"]
        if "allow_ai_tagging" in data:
            artwork.allow_ai_tagging = data["allow_ai_tagging"]
        if "allow_community_tagging" in data:
            artwork.allow_community_tagging = data["allow_community_tagging"]
        if "tags" in data:
            from app.models.tag import ArtworkTag
            await ArtworkTag.filter(artwork_id=artwork_id, type="author").delete()
            new_tags = [
                ArtworkTag(
                    artwork_id=artwork.id,
                    tag_name=t.strip().lower(),
                    type="author",
                    confidence=1.0,
                )
                for t in data["tags"]
                if t.strip()
            ]
            if new_tags:
                await ArtworkTag.bulk_create(new_tags, ignore_conflicts=True)

        await artwork.save()

        # 同步更新 Meilisearch
        from app.worker.enqueue import enqueue
        await enqueue("task_sync_artwork_meili", artwork_id=artwork.id)
        if "tags" in data:
            from app.models.tag import ArtworkTag
            from app.services.meili_sync import sync_tags_to_meili
            new_tag_names = list(await ArtworkTag.filter(artwork_id=artwork.id).values_list("tag_name", flat=True))
            await sync_tags_to_meili(new_tag_names)

        return artwork

    @staticmethod
    async def delete_artwork(artwork_id: int, user_id: int, role: str = "user"):
        artwork = await Artwork.get_or_none(id=artwork_id)
        if not artwork:
            raise HTTPException(status_code=404, detail="Artwork not found")
        if artwork.author_id != user_id and role not in ("admin", "moderator"):
            raise HTTPException(status_code=403, detail="Forbidden")

        # 删除存储文件 + Qdrant 向量（必须在 DB 删除前完成）
        from app.core.config import settings
        images = await ArtworkImage.filter(artwork_id=artwork_id)
        for img in images:
            await storage.delete_by_url(img.file_url)
            if settings.ENABLE_AI_FEATURES:
                from app.infrastructure.qdrant_client import qdrant_client as qc
                await asyncio.to_thread(qc.delete_vector, str(img.id))

        # Meilisearch 删除通过 ARQ异步处理（已存 artwork_id 足够）
        from app.worker.enqueue import enqueue
        await enqueue("task_delete_artwork_meili", artwork_id=artwork_id)

        await artwork.delete()

    # ------------------------------------------------------------------
    # 系列管理
    # ------------------------------------------------------------------

    @staticmethod
    async def create_series(user_id: int, title: str, description: Optional[str] = None) -> ArtworkSeries:
        return await ArtworkSeries.create(author_id=user_id, title=title, description=description)

    @staticmethod
    async def get_series(series_id: int) -> ArtworkSeries:
        series = await ArtworkSeries.get_or_none(id=series_id).prefetch_related("items__artwork__images")
        if not series:
            raise HTTPException(status_code=404, detail="Series not found")
        return series

    @staticmethod
    async def add_artwork_to_series(series_id: int, artwork_id: int, user_id: int, order: int = 0):
        series = await ArtworkSeries.get_or_none(id=series_id)
        if not series:
            raise HTTPException(status_code=404, detail="Series not found")
        if series.author_id != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        item, _ = await ArtworkSeriesItem.get_or_create(
            series_id=series_id, artwork_id=artwork_id, defaults={"order": order}
        )
        return item

    @staticmethod
    async def remove_artwork_from_series(series_id: int, artwork_id: int, user_id: int):
        series = await ArtworkSeries.get_or_none(id=series_id)
        if not series:
            raise HTTPException(status_code=404, detail="Series not found")
        if series.author_id != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        await ArtworkSeriesItem.filter(series_id=series_id, artwork_id=artwork_id).delete()

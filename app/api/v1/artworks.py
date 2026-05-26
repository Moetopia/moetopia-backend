import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends, Request, BackgroundTasks, Body, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from app.schemas.artwork_schema import (
    ArtworkCreate, ArtworkResponse, ArtworkSeriesCreate, ArtworkSeriesResponse, serialize_artwork
)
from app.services.artwork_service import ArtworkService
from app.services.search_service import SearchService
from app.models.user import User
from app.api.dependencies import get_current_user, get_optional_user
from app.schemas.common import ResponseBase, PaginatedData

router = APIRouter()


MAX_IMAGES_PER_UPLOAD = 20


async def _batch_comment_info(artwork_ids: list) -> tuple:
    """Batch fetch top_comment and comment_count for a list of artwork_ids."""
    if not artwork_ids:
        return {}, {}
    from app.models.interaction import Comment
    from tortoise.functions import Count
    count_rows = await (
        Comment.filter(artwork_id__in=artwork_ids, is_deleted=False, parent_id=None)
        .group_by("artwork_id")
        .annotate(cnt=Count("id"))
        .values("artwork_id", "cnt")
    )
    comment_counts = {r["artwork_id"]: r["cnt"] for r in count_rows}
    all_root = await (
        Comment.filter(artwork_id__in=artwork_ids, is_deleted=False, parent_id=None)
        .order_by("artwork_id", "-like_count", "-created_at")
        .prefetch_related("user")
    )
    top_comments: dict = {}
    for c in all_root:
        if c.artwork_id not in top_comments:
            top_comments[c.artwork_id] = c
    return comment_counts, top_comments


@router.get("/by-pixiv/{pixiv_id}", response_model=ResponseBase[Optional[ArtworkResponse]])
async def get_artwork_by_pixiv_id(
    pixiv_id: int,
    current_user: Optional[User] = Depends(get_optional_user),
):
    """通过 Pixiv 作品 ID 查找本站已导入的作品（用于去重检查）"""
    from app.models.artwork import Artwork
    artwork = await Artwork.get_or_none(pixiv_id=pixiv_id).prefetch_related("images", "tags", "author")
    if not artwork:
        return ResponseBase(data=None)
    return ResponseBase(data=serialize_artwork(artwork))


@router.post("/upload", response_model=ResponseBase[ArtworkResponse])
async def upload_artwork(
    title: str = Form(...),
    description: str = Form(None),
    tags_str: str = Form("[]"),
    artwork_type: str = Form("illustration"),
    is_ai: bool = Form(False),
    rating: str = Form("safe"),
    visibility: str = Form("public"),
    allow_ai_tagging: bool = Form(True),
    allow_community_tagging: bool = Form(True),
    content_origin: str = Form('original'),
    pixiv_id: Optional[int] = Form(None),
    source: Optional[str] = Form(None),
    original_author_name: Optional[str] = Form(None),
    scheduled_at: Optional[str] = Form(None),
    author_id: Optional[int] = Form(None),
    files: List[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
):
    if len(files) > MAX_IMAGES_PER_UPLOAD:
        raise HTTPException(status_code=400, detail=f"每次最多上传 {MAX_IMAGES_PER_UPLOAD} 张图片")
    try:
        tags = json.loads(tags_str)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="tags_str 必须是有效的 JSON 数组字符串")

    if pixiv_id is not None:
        from app.models.artwork import Artwork as ArtworkModel
        existing = await ArtworkModel.get_or_none(pixiv_id=pixiv_id)
        if existing:
            raise HTTPException(status_code=409, detail=f"Pixiv #{pixiv_id} 已导入，本站作品 ID: {existing.id}")

    parsed_scheduled_at = None
    if scheduled_at:
        try:
            parsed_scheduled_at = datetime.fromisoformat(scheduled_at.replace('Z', '+00:00'))
            if parsed_scheduled_at <= datetime.now(timezone.utc):
                raise HTTPException(status_code=400, detail="定时时间必须在未来")
        except ValueError:
            raise HTTPException(status_code=400, detail="无效的时间格式")
        visibility = "scheduled"
    # Admin can upload on behalf of imported author accounts
    effective_author_id = current_user.id
    if author_id is not None and author_id != current_user.id:
        if current_user.role != "admin":
            raise HTTPException(status_code=403, detail="仅管理员可指定 author_id")
        from app.models.user import User as UserModel
        target_author = await UserModel.get_or_none(id=author_id)
        if not target_author or not target_author.is_imported:
            raise HTTPException(status_code=400, detail="目标 author_id 不是导入账号")
        effective_author_id = author_id

    artwork_data = ArtworkCreate(
        title=title,
        description=description,
        tags=tags,
        artwork_type=artwork_type,
        is_ai=is_ai,
        rating=rating,
        visibility=visibility,
        allow_ai_tagging=allow_ai_tagging,
        allow_community_tagging=allow_community_tagging,
        content_origin=content_origin,
        pixiv_id=pixiv_id,
        source=source,
        original_author_name=original_author_name,
    )
    artwork, saved_images = await ArtworkService.create_artwork_flow(effective_author_id, artwork_data, files)
    from app.models.artwork import Artwork
    if parsed_scheduled_at:
        await Artwork.filter(id=artwork.id).update(scheduled_at=parsed_scheduled_at)
    artwork = await Artwork.get(id=artwork.id).prefetch_related("images", "tags", "author")
    # 关注者通知通过 ARQ 后台任务处理，避免阻塞上传接口
    if artwork_data.visibility == "public":
        try:
            from app.worker.enqueue import enqueue
            await enqueue(
                "task_fanout_new_artwork",
                artwork_id=artwork.id,
                author_id=current_user.id,
                title=artwork.title,
            )
        except Exception:
            pass
    return ResponseBase(data=serialize_artwork(artwork))


@router.get("/me/scheduled", response_model=ResponseBase[list])
async def get_my_scheduled(
    current_user: User = Depends(get_current_user),
):
    """获取我的所有定时投稿列表"""
    from app.models.artwork import Artwork
    artworks = await (
        Artwork.filter(author_id=current_user.id, visibility="scheduled")
        .order_by("scheduled_at")
        .prefetch_related("images", "tags", "author")
    )
    return ResponseBase(data=[serialize_artwork(a).model_dump() for a in artworks])


@router.patch("/{artwork_id}/schedule", response_model=ResponseBase[dict])
async def set_schedule(
    artwork_id: int,
    scheduled_at: Optional[str] = Body(None, embed=True),
    current_user: User = Depends(get_current_user),
):
    """设置或取消定时发布时间。传 null 可取消定时并还原为私密。"""
    from app.models.artwork import Artwork
    artwork = await Artwork.get_or_none(id=artwork_id, author_id=current_user.id)
    if not artwork:
        raise HTTPException(status_code=404, detail="作品不存在")
    if scheduled_at is None:
        if artwork.visibility == "scheduled":
            await artwork.fetch_related("images", "tags", "author")
            artwork.visibility = "private"
            artwork.scheduled_at = None
            await artwork.save(update_fields=["visibility", "scheduled_at"])
        return ResponseBase(data={"scheduled_at": None, "visibility": artwork.visibility})
    try:
        dt = datetime.fromisoformat(scheduled_at.replace('Z', '+00:00'))
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的时间格式")
    if dt <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="定时时间必须在未来")
    artwork.scheduled_at = dt
    artwork.visibility = "scheduled"
    await artwork.save(update_fields=["scheduled_at", "visibility"])
    return ResponseBase(data={"scheduled_at": dt.isoformat(), "visibility": "scheduled"})


@router.get("/", response_model=ResponseBase[List[ArtworkResponse]])
async def get_artworks(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    type: Optional[str] = None,
    current_user: Optional[User] = Depends(get_optional_user),
):
    artworks = await ArtworkService.get_artworks(limit, offset, user=current_user, artwork_type=type)
    comment_counts, top_comments = await _batch_comment_info([a.id for a in artworks])
    return ResponseBase(data=[
        serialize_artwork(a, top_comment=top_comments.get(a.id), comment_count=comment_counts.get(a.id, 0))
        for a in artworks
    ])


@router.get("/{artwork_id}", response_model=ResponseBase[ArtworkResponse])
async def get_artwork(
    artwork_id: int,
    request: Request,
    current_user: Optional[User] = Depends(get_optional_user),
):
    from app.infrastructure.cache import cache_get, cache_set, TTL_ARTWORK
    ip = request.client.host if request.client else None

    # 匿名用户走缓存，已登录用户跳过缓存（需要个人化的 is_liked/is_bookmarked）
    if current_user is None:
        cached = await cache_get(f"artwork:{artwork_id}")
        if cached is not None:
            return ResponseBase(data=cached)

    artwork = await ArtworkService.get_artwork(artwork_id, requesting_user=current_user, ip_address=ip)
    serialized = serialize_artwork(artwork)

    if current_user:
        from app.models.interaction import Like, Bookmark
        is_liked = await Like.filter(user_id=current_user.id, artwork_id=artwork_id).exists()
        is_bookmarked = await Bookmark.filter(user_id=current_user.id, artwork_id=artwork_id).exists()
        serialized = serialized.model_copy(update={"is_liked": is_liked, "is_bookmarked": is_bookmarked})
    else:
        await cache_set(f"artwork:{artwork_id}", serialized.model_dump(), TTL_ARTWORK)

    return ResponseBase(data=serialized)


@router.get("/{artwork_id}/images/{image_id}/download")
async def download_artwork_image(
    artwork_id: int,
    image_id: str,
    current_user: Optional[User] = Depends(get_optional_user),
):
    """
    下载图片。
    - 会员或作品作者：下载原始全分辨率文件（如有）
    - 其余已登录用户：下载 1200px 压缩版
    - 未登录：同上（压缩版）
    """
    from app.models.artwork import ArtworkImage, Artwork
    from app.models.user_membership import UserMembership
    from app.services.storage_service import storage

    img = await ArtworkImage.get_or_none(id=image_id)
    if not img:
        raise HTTPException(status_code=404, detail="图片不存在")

    artwork = await Artwork.get_or_none(id=artwork_id)
    if not artwork or artwork.visibility == "private":
        raise HTTPException(status_code=404, detail="作品不存在")

    use_original = False
    if img.original_url and current_user:
        if current_user.id == artwork.author_id:
            use_original = True
        else:
            now = datetime.now(timezone.utc)
            sub = await UserMembership.filter(
                user_id=current_user.id, status="active", expires_at__gt=now
            ).first()
            if sub:
                use_original = True

    serve_url = img.original_url if use_original else img.file_url
    ext = serve_url.rsplit(".", 1)[-1].split("?")[0] if "." in serve_url else "jpg"
    filename = f"moetopia_{artwork_id}.{ext}"
    return await storage.make_download_response(serve_url, filename)


@router.get("/{artwork_id}/images/download-zip")
async def download_artwork_zip(
    artwork_id: int,
    use_original: bool = Query(default=False),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """下载作品全部图片，打包为 ZIP 文件输出。会员/作者可请求原图版本。"""
    import io
    import zipfile
    from urllib.parse import quote
    from fastapi.responses import StreamingResponse
    from app.models.artwork import ArtworkImage, Artwork
    from app.models.user_membership import UserMembership
    from app.services.storage_service import storage

    artwork = await Artwork.get_or_none(id=artwork_id)
    if not artwork:
        raise HTTPException(status_code=404, detail="作品不存在")
    if artwork.visibility == "private":
        if not current_user or (
            current_user.id != artwork.author_id
            and current_user.role not in ("admin", "moderator")
        ):
            raise HTTPException(status_code=403, detail="无权访问")

    is_author = current_user and current_user.id == artwork.author_id
    is_admin = current_user and current_user.role in ("admin", "moderator")
    can_original = bool(is_author or is_admin)
    if use_original and not can_original and current_user:
        now = datetime.now(timezone.utc)
        sub = await UserMembership.filter(
            user_id=current_user.id, status="active", expires_at__gt=now
        ).first()
        if sub:
            can_original = True

    images = await ArtworkImage.filter(artwork_id=artwork_id).order_by("sort_order").all()
    if not images:
        raise HTTPException(status_code=404, detail="作品无图片")

    title = artwork.title or f"artwork_{artwork_id}"
    safe_title = "".join(c for c in title if c.isalnum() or c in (" ", "-", "_")).strip() or f"artwork_{artwork_id}"

    class _StreamBuf:
        """捕获 zipfile 写入的字节，每次 get_chunk() 后重置游标。"""
        def __init__(self) -> None:
            self._buf = io.BytesIO()

        def write(self, data: bytes) -> int:
            return self._buf.write(data)

        def flush(self) -> None:
            pass

        def tell(self) -> int:
            return self._buf.tell()

        def seek(self, pos: int) -> int:  # zipfile 内部偏移计算用
            return self._buf.seek(pos)

        def read(self, n: int = -1) -> bytes:  # 中央目录写入时用
            return self._buf.read(n)

        def get_chunk(self) -> bytes:
            chunk = self._buf.getvalue()
            self._buf = io.BytesIO()
            return chunk

    async def _generate():
        sbuf = _StreamBuf()
        zf = zipfile.ZipFile(sbuf, "w", zipfile.ZIP_STORED)
        for i, img in enumerate(images):
            serve_url = (
                img.original_url
                if (use_original and can_original and img.original_url)
                else img.file_url
            )
            if not serve_url:
                continue
            ext = serve_url.rsplit(".", 1)[-1].split("?")[0] or "jpg"
            async with storage.open_for_processing(serve_url) as local_path:
                data = await asyncio.to_thread(open(local_path, "rb").read)
            zf.writestr(f"{safe_title}_{i + 1:02d}.{ext}", data)
            del data
            chunk = sbuf.get_chunk()
            if chunk:
                yield chunk
        zf.close()                      # 写入中央目录
        chunk = sbuf.get_chunk()
        if chunk:
            yield chunk

    filename = f"{safe_title}_images.zip"
    return StreamingResponse(
        _generate(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.delete("/{artwork_id}/images/{image_id}", response_model=ResponseBase[dict])
async def delete_artwork_image(
    artwork_id: int,
    image_id: str,
    current_user: User = Depends(get_current_user),
):
    """删除作品中的单张图片（作者本人）。作品至少保留 1 张图片。"""
    from app.models.artwork import ArtworkImage, Artwork
    from app.services.storage_service import storage

    artwork = await Artwork.get_or_none(id=artwork_id)
    if not artwork:
        raise HTTPException(status_code=404, detail="作品不存在")
    if artwork.author_id != current_user.id and current_user.role not in ("admin", "moderator"):
        raise HTTPException(status_code=403, detail="无权限")

    total = await ArtworkImage.filter(artwork_id=artwork_id).count()
    if total <= 1:
        raise HTTPException(status_code=400, detail="作品至少保留一张图片")

    img = await ArtworkImage.get_or_none(id=image_id, artwork_id=artwork_id)
    if not img:
        raise HTTPException(status_code=404, detail="图片不存在")

    if img.file_url:
        await storage.delete_by_url(img.file_url)
    if img.original_url:
        await storage.delete_by_url(img.original_url)
    await img.delete()

    from app.infrastructure.cache import invalidate_artwork
    await invalidate_artwork(artwork_id)
    return ResponseBase(data={"deleted": True})


@router.patch("/{artwork_id}/images/reorder", response_model=ResponseBase[dict])
async def reorder_artwork_images(
    artwork_id: int,
    image_ids: List[str],
    current_user: User = Depends(get_current_user),
):
    """重新排序作品图片（传入完整有序的 image_id 列表）。"""
    from app.models.artwork import ArtworkImage, Artwork

    artwork = await Artwork.get_or_none(id=artwork_id)
    if not artwork:
        raise HTTPException(status_code=404, detail="作品不存在")
    if artwork.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权限")

    for sort_order, img_id in enumerate(image_ids):
        await ArtworkImage.filter(id=img_id, artwork_id=artwork_id).update(sort_order=sort_order)

    from app.infrastructure.cache import invalidate_artwork
    await invalidate_artwork(artwork_id)
    return ResponseBase(data={"reordered": True})


@router.get("/{artwork_id}/related", response_model=ResponseBase[List[ArtworkResponse]])
async def get_related_artworks(
    artwork_id: int,
    limit: int = Query(default=12, ge=1, le=50),
    current_user: Optional[User] = Depends(get_optional_user),
):
    artworks = await SearchService.get_related_artworks(artwork_id, user=current_user, limit=limit)
    return ResponseBase(data=artworks)


@router.put("/{artwork_id}", response_model=ResponseBase[ArtworkResponse])
async def modify_artwork(
    artwork_id: int,
    title: str = Form(None),
    description: str = Form(None),
    tags_str: str = Form(None),
    rating: str = Form(None),
    visibility: str = Form(None),
    is_ai: Optional[bool] = Form(None),
    artwork_type: Optional[str] = Form(None),
    allow_ai_tagging: Optional[bool] = Form(None),
    allow_community_tagging: Optional[bool] = Form(None),
    current_user: User = Depends(get_current_user),
):
    data = {}
    if title is not None:
        data["title"] = title
    if description is not None:
        data["description"] = description
    if rating is not None:
        data["rating"] = rating
    if visibility is not None:
        data["visibility"] = visibility
    if is_ai is not None:
        data["is_ai"] = is_ai
    if artwork_type is not None:
        data["artwork_type"] = artwork_type
    if allow_ai_tagging is not None:
        data["allow_ai_tagging"] = allow_ai_tagging
    if allow_community_tagging is not None:
        data["allow_community_tagging"] = allow_community_tagging
    if tags_str is not None:
        try:
            data["tags"] = json.loads(tags_str)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON for tags")

    artwork = await ArtworkService.modify_artwork(artwork_id, current_user.id, data)
    from app.infrastructure.cache import invalidate_artwork, invalidate_related
    await invalidate_artwork(artwork_id)
    if data.get("tags") is not None:
        await invalidate_related(artwork_id)
    return ResponseBase(data=serialize_artwork(artwork))


@router.delete("/{artwork_id}", response_model=ResponseBase[dict])
async def delete_artwork(
    artwork_id: int,
    current_user: User = Depends(get_current_user),
):
    await ArtworkService.delete_artwork(artwork_id, current_user.id, role=current_user.role)
    from app.infrastructure.cache import invalidate_artwork, invalidate_related
    await invalidate_artwork(artwork_id)
    await invalidate_related(artwork_id)
    return ResponseBase(data={"message": "Artwork deleted successfully"})


# ------------------------------------------------------------------
# 系列（Series）端点
# ------------------------------------------------------------------

async def _build_series_list(series_list: list, include_updated_at: bool = False) -> list:
    """共享：批量构建系列列表（含封面 + 作品数），避免 N+1。"""
    from app.models.artwork import ArtworkSeriesItem
    from collections import defaultdict
    if not series_list:
        return []
    series_ids = [s.id for s in series_list]
    all_items = await (
        ArtworkSeriesItem.filter(series_id__in=series_ids)
        .order_by("series_id", "order")
        .prefetch_related("artwork__images")
    )
    cover_map: dict[int, str | None] = {}
    count_map: dict[int, int] = defaultdict(int)
    seen_series: set[int] = set()
    for item in all_items:
        count_map[item.series_id] += 1
        if item.series_id not in seen_series:
            seen_series.add(item.series_id)
            try:
                imgs = sorted(list(item.artwork.images), key=lambda i: i.sort_order)
                cover_map[item.series_id] = imgs[0].file_url if imgs else None
            except Exception:
                cover_map[item.series_id] = None
    result = []
    for s in series_list:
        entry = {
            "id": s.id,
            "title": s.title,
            "description": s.description,
            "artwork_count": count_map[s.id],
            "cover_url": cover_map.get(s.id),
            "created_at": s.created_at.isoformat(),
        }
        if include_updated_at:
            entry["updated_at"] = s.updated_at.isoformat()
        result.append(entry)
    return result


@router.get("/series/my", response_model=ResponseBase[List[dict]])
async def get_my_series(
    current_user: User = Depends(get_current_user),
):
    """获取当前登录用户的所有系列（含封面图与作品数）"""
    from app.models.artwork import ArtworkSeries
    series_list = await ArtworkSeries.filter(author_id=current_user.id).order_by("-created_at")
    return ResponseBase(data=await _build_series_list(series_list, include_updated_at=True))


@router.post("/series/create", response_model=ResponseBase[ArtworkSeriesResponse])
async def create_series(
    series_in: ArtworkSeriesCreate,
    current_user: User = Depends(get_current_user),
):
    series = await ArtworkService.create_series(current_user.id, series_in.title, series_in.description)
    return ResponseBase(data=ArtworkSeriesResponse(
        id=series.id,
        author_id=series.author_id,
        title=series.title,
        description=series.description,
        created_at=series.created_at,
    ))


@router.get("/series/user/{user_id}", response_model=ResponseBase[list])
async def get_user_series(user_id: int):
    """获取指定用户的系列列表（公开）"""
    from app.models.artwork import ArtworkSeries
    series_qs = await ArtworkSeries.filter(author_id=user_id).order_by("-created_at")
    return ResponseBase(data=await _build_series_list(series_qs))


@router.get("/series/{series_id}", response_model=ResponseBase[dict])
async def get_series(series_id: int):
    series = await ArtworkService.get_series(series_id)
    items = await series.items.all().prefetch_related("artwork__images").order_by("order")
    return ResponseBase(data={
        "id": series.id,
        "title": series.title,
        "description": series.description,
        "author_id": series.author_id,
        "artworks": [serialize_artwork(item.artwork).model_dump() for item in items],
    })


@router.put("/series/{series_id}", response_model=ResponseBase[dict])
async def update_series(
    series_id: int,
    title: str = Body(...),
    description: Optional[str] = Body(None),
    current_user: User = Depends(get_current_user),
):
    """编辑系列标题/简介（仅作者）"""
    series = await ArtworkService.get_series(series_id)
    if series.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权编辑此系列")
    series.title = title.strip()
    series.description = description.strip() if description else None
    await series.save()
    return ResponseBase(data={"id": series.id, "title": series.title, "description": series.description})


@router.delete("/series/{series_id}", response_model=ResponseBase[dict])
async def delete_series(
    series_id: int,
    current_user: User = Depends(get_current_user),
):
    """删除系列（仅作者，不删作品本身）"""
    series = await ArtworkService.get_series(series_id)
    if series.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权删除此系列")
    await series.delete()
    return ResponseBase(data={"deleted": True})


@router.post("/series/{series_id}/follow", response_model=ResponseBase[dict])
async def toggle_series_follow(
    series_id: int,
    current_user: User = Depends(get_current_user),
):
    """加入/退出追更列表（toggle）"""
    from app.models.artwork import SeriesFollow
    series = await ArtworkService.get_series(series_id)
    follow = await SeriesFollow.get_or_none(user=current_user, series=series)
    if follow:
        await follow.delete()
        return ResponseBase(data={"following": False, "notify": False})
    follow = await SeriesFollow.create(user=current_user, series=series, notify=True)
    return ResponseBase(data={"following": True, "notify": follow.notify})


@router.get("/series/{series_id}/follow", response_model=ResponseBase[dict])
async def get_series_follow_status(
    series_id: int,
    current_user: User = Depends(get_current_user),
):
    """查询当前用户对该系列的追更状态"""
    from app.models.artwork import SeriesFollow
    follow = await SeriesFollow.get_or_none(user=current_user, series_id=series_id)
    if follow:
        return ResponseBase(data={"following": True, "notify": follow.notify})
    return ResponseBase(data={"following": False, "notify": False})


@router.put("/series/{series_id}/follow/notify", response_model=ResponseBase[dict])
async def toggle_series_notify(
    series_id: int,
    current_user: User = Depends(get_current_user),
):
    """切换新话通知开关（需已追更）"""
    from app.models.artwork import SeriesFollow
    follow = await SeriesFollow.get_or_none(user=current_user, series_id=series_id)
    if not follow:
        raise HTTPException(status_code=404, detail="尚未追更此系列")
    follow.notify = not follow.notify
    await follow.save()
    return ResponseBase(data={"following": True, "notify": follow.notify})


async def _notify_series_followers(series_id: int, series_title: str, artwork_title: str, artwork_id: int, author_id: int):
    """Fan-out: 通知所有开启了新话推送的追更用户"""
    try:
        from app.models.artwork import SeriesFollow
        from app.services.notification_service import push_notification
        follows = await SeriesFollow.filter(series_id=series_id, notify=True).exclude(user_id=author_id)
        for f in follows:
            try:
                await push_notification(
                    user_id=f.user_id,
                    actor_id=author_id,
                    type="series_update",
                    content=f"「{series_title}」更新了新话：{artwork_title}",
                    related_entity_id=str(artwork_id),
                )
            except Exception as e:
                logger.warning("series notify failed for user %s: %s", f.user_id, e)
    except Exception as e:
        logger.error("_notify_series_followers error: %s", e)


@router.post("/series/{series_id}/artworks/{artwork_id}", response_model=ResponseBase[dict])
async def add_to_series(
    series_id: int,
    artwork_id: int,
    order: int = 0,
    current_user: User = Depends(get_current_user),
):
    item = await ArtworkService.add_artwork_to_series(series_id, artwork_id, current_user.id, order)
    # 通知追更用户（ensure_future 保留 Tortoise ORM 连接上下文）
    try:
        from app.models.artwork import ArtworkSeries, Artwork as _Artwork
        series_obj = await ArtworkSeries.get_or_none(id=series_id)
        artwork_obj = await _Artwork.get_or_none(id=artwork_id)
        if series_obj and artwork_obj:
            import asyncio
            asyncio.ensure_future(_notify_series_followers(
                series_id, series_obj.title, artwork_obj.title, artwork_id, current_user.id,
            ))
    except Exception:
        pass
    return ResponseBase(data={"message": "Added to series"})


@router.delete("/series/{series_id}/artworks/{artwork_id}", response_model=ResponseBase[dict])
async def remove_from_series(
    series_id: int,
    artwork_id: int,
    current_user: User = Depends(get_current_user),
):
    await ArtworkService.remove_artwork_from_series(series_id, artwork_id, current_user.id)
    return ResponseBase(data={"message": "Removed from series"})

@router.get("/{artwork_id}/series", response_model=ResponseBase[List[dict]])
async def get_artwork_series(artwork_id: int):
    """获取某作品所属的所有系列，含位置与前后话 ID"""
    from app.models.artwork import ArtworkSeries, ArtworkSeriesItem
    items = await ArtworkSeriesItem.filter(artwork_id=artwork_id).prefetch_related("series")
    result = []
    for item in items:
        s = item.series
        all_items = await (
            ArtworkSeriesItem.filter(series_id=s.id)
            .order_by("order")
            .prefetch_related("artwork__images")
        )
        total = len(all_items)
        pos = next((i for i, si in enumerate(all_items) if si.artwork_id == artwork_id), 0)
        prev_id = all_items[pos - 1].artwork_id if pos > 0 else None
        next_id = all_items[pos + 1].artwork_id if pos < total - 1 else None
        cover = None
        try:
            imgs = sorted(list(all_items[0].artwork.images), key=lambda i: i.sort_order)
            if imgs:
                cover = imgs[0].file_url
        except Exception:
            pass
        WINDOW = 4
        ep_start = max(0, pos - WINDOW)
        ep_end = min(total, pos + WINDOW + 1)
        episodes = [
            {"id": all_items[i].artwork_id, "title": all_items[i].artwork.title}
            for i in range(ep_start, ep_end)
        ]
        result.append({
            "series_id": s.id,
            "series_title": s.title,
            "series_description": s.description,
            "author_id": s.author_id,
            "position": pos,
            "total": total,
            "prev_artwork_id": prev_id,
            "next_artwork_id": next_id,
            "cover_url": cover,
            "episodes": episodes,
            "ep_start": ep_start,
            "ep_end": ep_end,
            "prev_more": ep_start,
            "next_more": total - ep_end,
        })
    return ResponseBase(data=result)


# ------------------------------------------------------------------
# 众包打标（User Tagging）
# ------------------------------------------------------------------

class UserTagRequest(BaseModel):
    tag_name: str


@router.post("/{artwork_id}/tags", response_model=ResponseBase[dict])
async def add_user_tag(
    artwork_id: int,
    body: UserTagRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """众包打标：任意登录用户为公开作品补充标签（type='user'）"""
    from app.middleware.rate_limit import rate_limit
    await rate_limit(request, "tag")
    from app.models.artwork import Artwork
    from app.models.tag import ArtworkTag

    artwork = await Artwork.get_or_none(id=artwork_id, visibility="public")
    if not artwork:
        raise HTTPException(status_code=404, detail="Artwork not found or not public")

    tag_name = body.tag_name.strip().lower().replace(" ", "_")
    if not tag_name or len(tag_name) > 50:
        raise HTTPException(status_code=400, detail="标签名最长 50 个字符")

    if not artwork.allow_community_tagging:
        raise HTTPException(status_code=403, detail="作者已关闭社区标签功能")

    current_tag_count = await ArtworkTag.filter(artwork_id=artwork_id).count()
    if current_tag_count >= 100:
        raise HTTPException(status_code=400, detail="该作品标签数量已达上限（100个）")

    tag, created = await ArtworkTag.get_or_create(
        artwork_id=artwork_id,
        tag_name=tag_name,
        defaults={"type": "ai_unverified", "confidence": 0.0}
    )
    return ResponseBase(data={"created": created, "tag_id": tag.id, "tag_name": tag.tag_name, "type": tag.type})


@router.delete("/{artwork_id}/tags/{tag_name}", response_model=ResponseBase[dict])
async def remove_user_tag(
    artwork_id: int,
    tag_name: str,
    current_user: User = Depends(get_current_user),
):
    """删除众包标签（仅作者本人、admin 或 moderator 可操作；ai_verified 标签不可删）"""
    from app.models.artwork import Artwork
    from app.models.tag import ArtworkTag

    artwork = await Artwork.get_or_none(id=artwork_id)
    if not artwork:
        raise HTTPException(status_code=404, detail="Artwork not found")

    is_privileged = current_user.role in ("admin", "moderator") or artwork.author_id == current_user.id
    if not is_privileged:
        raise HTTPException(status_code=403, detail="Permission denied")

    tag = await ArtworkTag.get_or_none(artwork_id=artwork_id, tag_name=tag_name.lower())
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")

    await tag.delete()
    return ResponseBase(data={"deleted": True, "tag_name": tag_name})

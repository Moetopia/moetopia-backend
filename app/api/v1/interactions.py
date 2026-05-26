from typing import List, Optional
from fastapi import APIRouter, HTTPException, Depends, Request, Query
from app.schemas.interaction_schema import (
    CommentCreate, CommentResponse, BookmarkCreate,
    BookmarkFolderCreate, BookmarkFolderResponse,
)
from app.api.dependencies import get_current_user
from app.services.interaction_service import InteractionService
from app.models.user import User
from app.schemas.common import ResponseBase
from app.middleware.rate_limit import rate_limit

router = APIRouter()


# ------------------------------------------------------------------
# 点赞
# ------------------------------------------------------------------

@router.post("/artworks/{artwork_id}/like", response_model=ResponseBase[dict])
async def toggle_like(
    artwork_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """点赞或取消点赞"""
    await rate_limit(request, "like")
    is_liked = await InteractionService.toggle_like(current_user.id, artwork_id)
    from app.infrastructure.cache import invalidate_artwork
    await invalidate_artwork(artwork_id)
    return ResponseBase(data={"is_liked": is_liked})


# ------------------------------------------------------------------
# 收藏
# ------------------------------------------------------------------

@router.post("/artworks/{artwork_id}/bookmark", response_model=ResponseBase[dict])
async def bookmark_artwork(
    artwork_id: int,
    bookmark_in: BookmarkCreate,
    current_user: User = Depends(get_current_user),
):
    """收藏作品（支持私密、打标签、指定收藏夹）"""
    bookmark = await InteractionService.create_bookmark(current_user.id, artwork_id, bookmark_in)
    return ResponseBase(data={"message": "Bookmark created/updated", "bookmark_id": bookmark.id})


@router.delete("/artworks/{artwork_id}/bookmark", response_model=ResponseBase[dict])
async def delete_bookmark(
    artwork_id: int,
    current_user: User = Depends(get_current_user),
):
    """取消收藏"""
    await InteractionService.delete_bookmark(current_user.id, artwork_id)
    return ResponseBase(data={"message": "Bookmark removed"})


@router.get("/bookmarks/me", response_model=ResponseBase[list])
async def get_my_bookmarks(
    folder_id: Optional[int] = None,
    q: Optional[str] = Query(default=None, max_length=100),
    sort_by: str = Query(default="newest"),           # newest | oldest (oldest = 会员)
    public_only: bool = Query(default=False),          # 仅公开收藏
    rating: Optional[str] = Query(default=None),       # safe | r18 | r18g
    bookmark_tag: Optional[str] = Query(default=None), # 收藏标签（自定义）
    artwork_tag: Optional[str] = Query(default=None),  # 作品标签（会员）
    date_from: Optional[str] = Query(default=None),    # ISO date 起始（会员）
    date_to: Optional[str] = Query(default=None),      # ISO date 截止（会员）
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
):
    """获取我的收藏列表（含作品详情）。排序倒序、作品标签、收藏时间过滤需要会员。"""
    from app.schemas.artwork_schema import serialize_artwork
    from app.models.interaction import Bookmark

    # ── 会员鉴权（仅当使用高级筛选时检查）──────────────────────────────────
    premium_used = sort_by == "oldest" or bool(artwork_tag) or bool(date_from) or bool(date_to)
    if premium_used:
        from datetime import datetime, timezone
        from app.models.user_membership import UserMembership
        has_membership = await UserMembership.filter(
            user_id=current_user.id, status="active",
            expires_at__gt=datetime.now(timezone.utc)
        ).exists()
        if not has_membership:
            raise HTTPException(
                status_code=403,
                detail={"code": "requires_membership", "message": "此筛选功能需要会员"}
            )

    # ── 当有文字搜索时走 Meilisearch，其余纯 DB ─────────────────────────────
    meili_artwork_ids: Optional[set] = None
    meili_rank: dict = {}

    if q:
        import asyncio
        from app.services.search_engine import SearchEngine
        # 构建 Meilisearch 额外过滤（rating + artwork_tag；visibility 由 SearchEngine 注入）
        meili_parts = []
        if rating:
            safe_r = rating.replace("'", "")
            meili_parts.append(f"rating = '{safe_r}'")
        if artwork_tag:
            safe_t = artwork_tag.replace("'", "")
            meili_parts.append(f"tags = '{safe_t}'")
        meili_extra = " AND ".join(meili_parts) if meili_parts else None
        meili_result = await asyncio.to_thread(
            SearchEngine.search_artworks,
            current_user, q, None, meili_extra, 1000, 0,
        )
        hits = meili_result.get("hits", [])
        meili_artwork_ids = {int(h["id"]) for h in hits}
        meili_rank = {int(h["id"]): i for i, h in enumerate(hits)}
        if not meili_artwork_ids:
            return ResponseBase(data=[])

    qs = Bookmark.filter(user_id=current_user.id)
    if meili_artwork_ids is not None:
        qs = qs.filter(artwork_id__in=meili_artwork_ids)
    if folder_id is not None:
        qs = qs.filter(folder_id=folder_id)
    if public_only:
        qs = qs.filter(is_private=False)
    # rating / artwork_tag: only apply DB filters when NOT already filtered by Meilisearch
    if rating and meili_artwork_ids is None:
        qs = qs.filter(artwork__rating=rating)
    if artwork_tag and meili_artwork_ids is None:
        qs = qs.filter(artwork__tags__tag_name=artwork_tag)
    if date_from:
        try:
            from datetime import datetime
            qs = qs.filter(created_at__gte=datetime.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            from datetime import datetime
            qs = qs.filter(created_at__lte=datetime.fromisoformat(date_to))
        except ValueError:
            pass

    order = "created_at" if sort_by == "oldest" else "-created_at"

    if meili_artwork_ids is not None:
        # Fetch all matching (no DB-level offset/limit; we sort by meili rank then paginate in Python)
        bookmarks_raw = await (
            qs.prefetch_related("artwork__images", "artwork__tags", "artwork__author")
            .limit(1000)
        )
        # Apply Python-side filters
        filtered = []
        for b in bookmarks_raw:
            artwork = b.artwork
            if artwork.visibility == "private" and artwork.author_id != current_user.id:
                continue
            if bookmark_tag and bookmark_tag not in (b.user_custom_tags or []):
                continue
            filtered.append(b)
        # Sort: newest/oldest by bookmark date, or by meili relevance rank
        if sort_by == "oldest":
            filtered.sort(key=lambda b: b.created_at)
        else:
            filtered.sort(key=lambda b: meili_rank.get(b.artwork_id, 9999))
        paginated = filtered[offset: offset + limit]
    else:
        paginated = await (
            qs.order_by(order)
            .offset(offset)
            .limit(limit)
            .prefetch_related("artwork__images", "artwork__tags", "artwork__author")
        )

    result = []
    for b in paginated:
        artwork = b.artwork
        if artwork.visibility == "private" and artwork.author_id != current_user.id:
            continue
        if bookmark_tag and meili_artwork_ids is None and bookmark_tag not in (b.user_custom_tags or []):
            continue
        result.append({
            "bookmark_id": b.id,
            "is_private": b.is_private,
            "user_custom_tags": b.user_custom_tags,
            "folder_id": b.folder_id,
            "bookmarked_at": b.created_at.isoformat(),
            "artwork": serialize_artwork(artwork).model_dump(),
        })
    return ResponseBase(data=result)


# ------------------------------------------------------------------
# 收藏夹
# ------------------------------------------------------------------

@router.post("/bookmark-folders", response_model=ResponseBase[BookmarkFolderResponse])
async def create_bookmark_folder(
    folder_in: BookmarkFolderCreate,
    current_user: User = Depends(get_current_user),
):
    """创建收藏夹"""
    folder = await InteractionService.create_folder(current_user.id, folder_in)
    return ResponseBase(data=BookmarkFolderResponse(
        id=folder.id,
        user_id=folder.user_id,
        name=folder.name,
        is_private=folder.is_private,
        created_at=folder.created_at,
    ))


@router.get("/bookmark-folders/me", response_model=ResponseBase[List[BookmarkFolderResponse]])
async def get_my_bookmark_folders(current_user: User = Depends(get_current_user)):
    """获取我的收藏夹列表"""
    folders = await InteractionService.get_folders(current_user.id)
    return ResponseBase(data=[
        BookmarkFolderResponse(id=f.id, user_id=f.user_id, name=f.name, is_private=f.is_private, created_at=f.created_at)
        for f in folders
    ])


@router.delete("/bookmark-folders/{folder_id}", response_model=ResponseBase[dict])
async def delete_bookmark_folder(
    folder_id: int,
    current_user: User = Depends(get_current_user),
):
    """删除收藏夹"""
    await InteractionService.delete_folder(folder_id, current_user.id)
    return ResponseBase(data={"message": "Folder deleted"})


# ------------------------------------------------------------------
# 评论
# ------------------------------------------------------------------

@router.post("/artworks/{artwork_id}/comments", response_model=ResponseBase[CommentResponse])
async def post_comment(
    request: Request,
    artwork_id: int,
    comment_in: CommentCreate,
    current_user: User = Depends(get_current_user),
):
    """对作品发表评论"""
    await rate_limit(request, "comment")
    comment = await InteractionService.post_comment(current_user.id, artwork_id, comment_in)
    from app.models.interaction import Comment as CommentModel
    comment_full = await CommentModel.get(id=comment.id).prefetch_related("user", "reply_to")
    return ResponseBase(data=_serialize_comment(comment_full))


def _serialize_comment(c, depth: int = 0, liked_ids: Optional[set] = None) -> CommentResponse:
    """序列化评论（两层扁平模型：depth 0 = 顶层, depth 1 = 回复，不再继续递归）。"""
    try:
        uname = c.user.username
    except Exception:
        uname = None
    try:
        avatar = c.user.avatar_url
    except Exception:
        avatar = None
    try:
        reply_to_uname = c.reply_to.username
    except Exception:
        reply_to_uname = None
    # 只在顶层展开 replies；depth>=1 时 replies 永远为空（扁平结构）
    sub_replies: list = []
    if depth == 0:
        try:
            sub_replies = [_serialize_comment(r, depth=1, liked_ids=liked_ids) for r in c.replies]
        except Exception:
            sub_replies = []
    return CommentResponse(
        id=c.id,
        user_id=c.user_id,
        username=uname,
        user_avatar=avatar,
        artwork_id=c.artwork_id,
        parent_id=c.parent_id,
        reply_to_id=c.reply_to_id,
        reply_to_username=reply_to_uname,
        content=c.content,
        is_deleted=c.is_deleted,
        like_count=getattr(c, 'like_count', 0),
        is_liked=c.id in liked_ids if liked_ids is not None else False,
        created_at=c.created_at,
        replies=sub_replies,
    )


@router.get("/artworks/{artwork_id}/comments", response_model=ResponseBase[List[CommentResponse]])
async def get_comments(
    artwork_id: int,
    sort: str = Query(default="latest", pattern="^(latest|hot)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """获取作品下的顶层评论（楼中楼结构，含用户名）。sort=latest|hot"""
    comments = await InteractionService.get_comments(artwork_id, limit=limit, offset=offset, sort=sort)
    return ResponseBase(data=[_serialize_comment(c) for c in comments])


@router.get("/artworks/{artwork_id}/comments/authed", response_model=ResponseBase[List[CommentResponse]])
async def get_comments_authed(
    artwork_id: int,
    sort: str = Query(default="latest", pattern="^(latest|hot)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
):
    """获取作品评论（含当前用户是否已点赞，需登录）"""
    from app.models.interaction import CommentLike
    comments = await InteractionService.get_comments(artwork_id, limit=limit, offset=offset, sort=sort)
    all_ids = [c.id for c in comments] + [r.id for c in comments for r in getattr(c, 'replies', [])]
    liked_set: set = set()
    if all_ids:
        liked_set = {
            cl.comment_id
            for cl in await CommentLike.filter(user_id=current_user.id, comment_id__in=all_ids)
        }
    return ResponseBase(data=[_serialize_comment(c, liked_ids=liked_set) for c in comments])


@router.post("/comments/{comment_id}/like", response_model=ResponseBase[dict])
async def toggle_comment_like(
    comment_id: int,
    current_user: User = Depends(get_current_user),
):
    """点赞或取消点赞评论"""
    is_liked = await InteractionService.toggle_comment_like(current_user.id, comment_id)
    return ResponseBase(data={"is_liked": is_liked})


@router.get("/bookmark-tags", response_model=ResponseBase[List[dict]])
async def get_my_bookmark_tags(current_user: User = Depends(get_current_user)):
    """
    获取我在收藏中使用过的全部自定义标签，附带每个标签的使用次数。
    类似 Pixiv「収まったタグ」管理界面。
    """
    from app.models.interaction import Bookmark
    from collections import Counter

    bookmarks = await Bookmark.filter(user_id=current_user.id).values_list("user_custom_tags", flat=True)
    counter: Counter = Counter()
    for tag_list in bookmarks:
        if tag_list:
            for tag in tag_list:
                counter[tag] += 1

    return ResponseBase(data=[{"tag": tag, "count": cnt} for tag, cnt in counter.most_common()])


@router.put("/comments/{comment_id}", response_model=ResponseBase[CommentResponse])
async def edit_comment(
    comment_id: int,
    body: CommentCreate,
    current_user: User = Depends(get_current_user),
):
    """编辑自己的评论（5 分钟内可修改）"""
    from app.models.interaction import Comment as CommentModel
    from datetime import datetime, timedelta, timezone
    comment = await CommentModel.get_or_none(id=comment_id)
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    if comment.is_deleted:
        raise HTTPException(status_code=400, detail="Cannot edit deleted comment")
    edit_window = datetime.now(timezone.utc) - timedelta(minutes=5)
    if comment.created_at.replace(tzinfo=timezone.utc) < edit_window:
        raise HTTPException(status_code=403, detail="评论发布超过 5 分钟，不可修改")
    comment.content = body.content
    await comment.save(update_fields=["content"])
    comment_full = await CommentModel.get(id=comment.id).prefetch_related("user", "reply_to")
    return ResponseBase(data=_serialize_comment(comment_full))


@router.delete("/comments/{comment_id}", response_model=ResponseBase[dict])
async def delete_comment(
    comment_id: int,
    current_user: User = Depends(get_current_user),
):
    """删除评论（逻辑删除，内容替换为[已删除]）"""
    await InteractionService.delete_comment(comment_id, current_user.id, role=current_user.role)
    return ResponseBase(data={"message": "Comment deleted"})

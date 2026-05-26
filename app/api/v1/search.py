from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from typing import Any, List, Optional
from app.schemas.search_schema import SearchResultResponse
from app.services.search_service import SearchService
from app.models.user import User
from app.api.dependencies import get_current_user, get_optional_user
from app.schemas.artwork_schema import ArtworkResponse, serialize_artwork
from app.schemas.common import ResponseBase

router = APIRouter()


@router.post("/hybrid", response_model=ResponseBase[SearchResultResponse])
async def hybrid_search(
    query: Optional[str] = Form(None, max_length=200),
    image: Optional[UploadFile] = File(None),
    limit: int = Form(50, ge=1, le=200),
    offset: int = Form(0, ge=0),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """混合检索：传图走 Qdrant 图搜图，传 query 走 Meilisearch，全程强制安全注入"""
    result = await SearchService.hybrid_search(query, image, user=current_user, limit=limit, offset=offset)
    hits = result.get("hits") or []
    return ResponseBase(data=SearchResultResponse(
        hits=hits, total=len(hits), offset=offset, limit=limit,
        detected_tags=result.get("detected_tags"),
        matched_anchors=result.get("matched_anchors"),
    ))


@router.get("/keyword", response_model=ResponseBase[SearchResultResponse])
async def keyword_search(
    q: str = Query(..., max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort: Optional[str] = Query(None, max_length=50),
    origin: Optional[str] = Query(None, pattern="^(original|fanart|repost)$"),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """关键词模糊搜索（支持 sort=like_count:desc / created_at:desc）"""
    sort_by = [sort] if sort else None
    origin_filter = f"content_origin = '{origin}'" if origin else None
    res = await SearchService.keyword_search(q, user=current_user, sort_by=sort_by, extra_filter=origin_filter, limit=limit, offset=offset)
    return ResponseBase(data=SearchResultResponse(
        hits=res["hits"], total=res["estimated_total"], offset=offset, limit=limit
    ))


@router.get("/ranking", response_model=ResponseBase[List[ArtworkResponse]])
async def get_ranking(
    mode: str = "daily",
    limit: int = Query(default=50, ge=1, le=200),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """排行榜：mode=daily|weekly|monthly"""
    artworks = await SearchService.get_ranking(mode=mode, limit=limit, user=current_user)
    return ResponseBase(data=artworks)


@router.get("/feed/following", response_model=ResponseBase[List[ArtworkResponse]])
async def get_following_feed(
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    type: Optional[str] = None,
    rating: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """关注画师的最新作品流（需要登录）"""
    artworks = await SearchService.get_following_feed(current_user, limit=limit, offset=offset, artwork_type=type, rating=rating)
    return ResponseBase(data=artworks)


@router.get("/feed/tag", response_model=ResponseBase[List[ArtworkResponse]])
async def get_tag_feed(
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    type: Optional[str] = None,
    rating: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """关注标签的最新作品流（需要登录）"""
    artworks = await SearchService.get_tag_feed(current_user, limit=limit, offset=offset, artwork_type=type, rating=rating)
    return ResponseBase(data=artworks)


@router.get("/feed/recommended", response_model=ResponseBase[List[ArtworkResponse]])
async def get_recommended_feed(
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    type: Optional[str] = None,
    rating: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """个性化推荐 Feed（For You，基于浏览历史，冷启动回退到热榜）"""
    artworks = await SearchService.get_recommended_feed(current_user, limit=limit, offset=offset, artwork_type=type, rating=rating)
    return ResponseBase(data=artworks)


@router.get("/tags/trending", response_model=ResponseBase[List[dict]])
async def get_trending_tags(limit: int = Query(default=20, ge=1, le=50)):
    """近 7 天热门标签（按使用频次降序）"""
    tags = await SearchService.get_trending_tags(limit=limit)
    return ResponseBase(data=tags)


@router.get("/tags/autocomplete", response_model=ResponseBase[List[str]])
async def tag_autocomplete(q: str = Query(..., max_length=100), limit: int = Query(default=10, ge=1, le=30)):
    """标签自动补全（走 Meilisearch tags 索引，按使用频次降序）"""
    tags = await SearchService.tag_autocomplete(q, limit=limit)
    return ResponseBase(data=tags)


@router.get("/style/{style_ref_id}", response_model=ResponseBase[list])
async def search_by_style(
    style_ref_id: int,
    limit: int = Query(default=30, ge=1, le=100),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """以指定风格参考图 ID 搜索风格相似作品"""
    from app.models.artwork import StyleReference
    from app.infrastructure.qdrant_client import qdrant_client
    from app.services.search_engine import SearchEngine
    import asyncio

    ref = await StyleReference.get_or_none(id=style_ref_id)
    if not ref:
        raise HTTPException(status_code=404, detail="Style reference not found")

    query_filter = SearchEngine._build_qdrant_filter(current_user)
    points = await asyncio.to_thread(
        qdrant_client.search_by_style_ref, ref.qdrant_id, query_filter, limit
    )
    from app.models.artwork import Artwork
    hits = []
    seen: set = set()
    for p in points:
        aid = p.payload.get("artwork_id")
        if not aid or aid in seen:
            continue
        seen.add(aid)
        try:
            artwork = await Artwork.get(id=int(aid)).prefetch_related("images", "tags", "author")
            d = serialize_artwork(artwork).model_dump(mode="json")
            d["_score"] = round(p.score, 4)
            hits.append(d)
        except Exception:
            pass
    return ResponseBase(data=hits)


@router.get("/users", response_model=ResponseBase[dict])
async def search_users(q: str = Query(..., max_length=200), limit: int = Query(default=30, ge=1, le=100), offset: int = Query(default=0, ge=0)):
    """用户搜索（走 Meilisearch users 索引，按粉丝数降序）"""
    result = await SearchService.search_users(q, limit=limit, offset=offset)
    return ResponseBase(data=result)


@router.get("/advanced", response_model=ResponseBase[SearchResultResponse])
async def advanced_search(
    q: str = Query(..., max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort: Optional[str] = Query(None, max_length=50),
    min_bookmarks: Optional[int] = None,
    date_from: Optional[str] = Query(None, max_length=20),
    date_to: Optional[str] = Query(None, max_length=20),
    author_id: Optional[int] = None,
    type: Optional[str] = Query(None, max_length=30),
    is_ai: Optional[bool] = None,
    any_keywords: Optional[str] = Query(None, max_length=200),
    exclude_keywords: Optional[str] = Query(None, max_length=200),
    search_scope: Optional[str] = Query(None, max_length=50),
    origin: Optional[str] = Query(None, pattern="^(original|fanart|repost)$"),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """高级搜索（支持 min_bookmarks、date_from/to、author_id、type、is_ai、any/exclude keywords、scope、origin）"""
    sort_by = [sort] if sort else None
    res = await SearchService.advanced_keyword_search(
        query=q,
        user=current_user,
        sort_by=sort_by,
        min_bookmarks=min_bookmarks,
        date_from=date_from,
        date_to=date_to,
        author_id=author_id,
        artwork_type=type,
        is_ai=is_ai,
        any_keywords=any_keywords,
        exclude_keywords=exclude_keywords,
        search_scope=search_scope,
        content_origin=origin,
        limit=limit,
        offset=offset,
    )
    return ResponseBase(data=SearchResultResponse(hits=res["hits"], total=res["estimated_total"], offset=offset, limit=limit))

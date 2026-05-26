import os
import uuid
import asyncio
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Query
from pydantic import BaseModel, Field
from app.models.announcement import Announcement
from app.models.user import User
from app.api.dependencies import get_current_user, get_optional_user
from app.schemas.common import ResponseBase
from app.services.storage_service import storage

router = APIRouter()

ALLOWED_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


class AnnouncementCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1)
    cover_image: Optional[str] = None
    category: str = Field(default="notice", pattern="^(notice|event|update)$")
    is_pinned: bool = False


class AnnouncementUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    content: Optional[str] = Field(None, min_length=1)
    cover_image: Optional[str] = None
    category: Optional[str] = Field(None, pattern="^(notice|event|update)$")
    is_pinned: Optional[bool] = None


def _serialize(a: Announcement, author: Optional[User] = None) -> dict:
    return {
        "id": a.id,
        "title": a.title,
        "content": a.content,
        "cover_image": a.cover_image,
        "category": a.category,
        "is_pinned": a.is_pinned,
        "created_at": a.created_at.isoformat(),
        "updated_at": a.updated_at.isoformat(),
        "author": {
            "id": author.id if author else a.author_id,
            "username": author.username if author else "",
            "avatar_url": author.avatar_url if author else None,
        } if author else {"id": a.author_id, "username": "", "avatar_url": None},
    }


@router.get("/", response_model=ResponseBase[List[dict]])
async def list_announcements(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    category: Optional[str] = None,
):
    """公开公告列表（置顶优先，时间倒序）"""
    qs = Announcement.all().prefetch_related("author")
    if category:
        qs = qs.filter(category=category)
    items = await qs.order_by("-is_pinned", "-created_at").offset(offset).limit(limit)
    return ResponseBase(data=[_serialize(a, a.author) for a in items])


@router.get("/{announcement_id}", response_model=ResponseBase[dict])
async def get_announcement(announcement_id: int):
    """公告详情"""
    a = await Announcement.get_or_none(id=announcement_id).prefetch_related("author")
    if not a:
        raise HTTPException(status_code=404, detail="公告不存在")
    return ResponseBase(data=_serialize(a, a.author))


@router.post("/", response_model=ResponseBase[dict])
async def create_announcement(
    body: AnnouncementCreate,
    current_user: User = Depends(get_current_user),
):
    """发布公告（仅 admin）"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可发布公告")
    a = await Announcement.create(
        author=current_user,
        title=body.title,
        content=body.content,
        cover_image=body.cover_image,
        category=body.category,
        is_pinned=body.is_pinned,
    )
    return ResponseBase(data=_serialize(a, current_user))


@router.put("/{announcement_id}", response_model=ResponseBase[dict])
async def update_announcement(
    announcement_id: int,
    body: AnnouncementUpdate,
    current_user: User = Depends(get_current_user),
):
    """编辑公告（仅 admin）"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可编辑公告")
    a = await Announcement.get_or_none(id=announcement_id).prefetch_related("author")
    if not a:
        raise HTTPException(status_code=404, detail="公告不存在")
    if body.title is not None:
        a.title = body.title
    if body.content is not None:
        a.content = body.content
    if body.cover_image is not None:
        a.cover_image = body.cover_image if body.cover_image else None
    if body.category is not None:
        a.category = body.category
    if body.is_pinned is not None:
        a.is_pinned = body.is_pinned
    await a.save()
    return ResponseBase(data=_serialize(a, a.author))


@router.delete("/{announcement_id}", response_model=ResponseBase[dict])
async def delete_announcement(
    announcement_id: int,
    current_user: User = Depends(get_current_user),
):
    """删除公告（仅 admin）"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可删除公告")
    a = await Announcement.get_or_none(id=announcement_id)
    if not a:
        raise HTTPException(status_code=404, detail="公告不存在")
    await a.delete()
    return ResponseBase(data={"deleted": True})


@router.post("/upload-image", response_model=ResponseBase[dict])
async def upload_announcement_image(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """上传公告内嵌图片（admin only），返回图片 URL 供 Markdown 引用"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可上传公告图片")
    ext = os.path.splitext(file.filename or "")[1].lower() or ".jpg"
    if ext not in ALLOWED_IMG_EXTS:
        raise HTTPException(status_code=400, detail="不支持的图片格式，仅支持 jpg/png/webp/gif")
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片不能超过 5MB")
    filename = f"{uuid.uuid4().hex[:12]}{ext}"
    url = await storage.save(data, f"announcements/{filename}")
    return ResponseBase(data={"url": url})

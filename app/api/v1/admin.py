"""
后台管理模块 — 仅限 admin / moderator 角色访问
"""
import asyncio
import os
import uuid
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from pydantic import BaseModel, Field

from app.api.dependencies import require_role
from app.models.user import User
from app.models.artwork import Artwork
from app.models.commission import Commission
from app.models.report import ArtworkReport
from app.schemas.common import ResponseBase
from app.services.artwork_service import ArtworkService
from app.services.storage_service import storage

router = APIRouter()

_ADMIN = require_role(["admin"])
_STAFF = require_role(["admin", "moderator"])


# ──────────────────────────────────────────────────────────────────────
# 请求体 Schema（内联定义，避免额外文件）
# ──────────────────────────────────────────────────────────────────────

class RoleUpdateRequest(BaseModel):
    role: str = Field(..., pattern="^(admin|moderator|tag_validator|user)$")


class BanRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=1000)


# ──────────────────────────────────────────────────────────────────────
# 站点总览统计
# ──────────────────────────────────────────────────────────────────────

@router.get("/stats", response_model=ResponseBase[dict])
async def site_stats(current_user: User = Depends(_STAFF)):
    """站点概览数据"""
    from app.models.interaction import Like, Bookmark, Comment
    from app.models.social import Follow
    from app.models.artwork import ArtworkSeries
    from app.models.announcement import Announcement

    (
        total_users, total_artworks, total_likes, total_bookmarks,
        total_comments, total_follows, pending_reports,
        total_commissions, total_series, total_announcements,
    ) = await asyncio.gather(
        User.all().count(),
        Artwork.all().count(),
        Like.all().count(),
        Bookmark.all().count(),
        Comment.filter(is_deleted=False).count(),
        Follow.all().count(),
        ArtworkReport.filter(status="pending").count(),
        Commission.all().count(),
        ArtworkSeries.all().count(),
        Announcement.all().count(),
    )
    from app.models.moderation import ModerationQueue
    pending_moderation = await ModerationQueue.filter(status="pending").count()

    return ResponseBase(data={
        "total_users":         total_users,
        "total_artworks":      total_artworks,
        "total_likes":         total_likes,
        "total_bookmarks":     total_bookmarks,
        "total_comments":      total_comments,
        "total_follows":       total_follows,
        "pending_reports":     pending_reports,
        "pending_moderation":  pending_moderation,
        "total_commissions":   total_commissions,
        "total_series":        total_series,
        "total_announcements": total_announcements,
    })


# ──────────────────────────────────────────────────────────────────────
# 用户管理
# ──────────────────────────────────────────────────────────────────────

@router.get("/users", response_model=ResponseBase[List[dict]])
async def list_users(
    q: Optional[str] = Query(None, description="搜索用户名或邮箱"),
    role: Optional[str] = Query(None, description="按角色过滤"),
    is_banned: Optional[bool] = Query(None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(_STAFF),
):
    """列出所有用户（支持搜索 + 角色/封禁过滤，走 Meilisearch）"""
    from app.services.search_service import SearchService
    result = await SearchService.search_users_admin(
        query=q or "",
        role=role,
        is_banned=is_banned,
        limit=limit,
        offset=offset,
    )
    return ResponseBase(data=result["hits"])


@router.get("/users/{user_id}", response_model=ResponseBase[dict])
async def get_user_detail(
    user_id: int,
    current_user: User = Depends(_STAFF),
):
    """获取单个用户详细信息"""
    user = await User.get_or_none(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    from app.models.artwork import Artwork
    from app.models.social import Follow
    artwork_count  = await Artwork.filter(author_id=user_id).count()
    follower_count = await Follow.filter(followed_id=user_id).count()
    return ResponseBase(data={
        "id": user.id, "username": user.username, "email": user.email,
        "role": user.role, "is_creator": user.is_creator,
        "commission_enabled": user.commission_enabled,
        "is_banned": user.is_banned, "banned_reason": user.banned_reason,
        "banned_at": user.banned_at,
        "created_at": user.created_at,
        "artwork_count": artwork_count,
        "follower_count": follower_count,
    })


@router.put("/users/{user_id}/role", response_model=ResponseBase[dict])
async def update_user_role(
    user_id: int,
    body: RoleUpdateRequest,
    current_user: User = Depends(_ADMIN),
):
    """变更用户角色（仅 admin）"""
    valid_roles = {"admin", "moderator", "tag_validator", "user"}
    if body.role not in valid_roles:
        raise HTTPException(status_code=400, detail=f"role 必须是 {valid_roles} 之一")
    user = await User.get_or_none(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot change your own role")
    user.role = body.role
    await user.save(update_fields=["role"])
    from app.services.meili_sync import sync_user_to_meili
    await sync_user_to_meili(user)
    return ResponseBase(data={"id": user_id, "role": body.role})


@router.post("/users/{user_id}/ban", response_model=ResponseBase[dict])
async def ban_user(
    user_id: int,
    body: BanRequest,
    current_user: User = Depends(_STAFF),
):
    """封禁用户"""
    user = await User.get_or_none(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == "admin":
        raise HTTPException(status_code=403, detail="Cannot ban an admin")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot ban yourself")
    user.is_banned = True
    user.banned_reason = body.reason
    user.banned_at = datetime.now(timezone.utc)
    user.token_version += 1
    await user.save(update_fields=["is_banned", "banned_reason", "banned_at", "token_version"])
    from app.services.meili_sync import sync_user_to_meili
    await sync_user_to_meili(user)
    return ResponseBase(data={"message": f"User {user_id} banned"})


@router.delete("/users/{user_id}/ban", response_model=ResponseBase[dict])
async def unban_user(
    user_id: int,
    current_user: User = Depends(_STAFF),
):
    """解封用户"""
    user = await User.get_or_none(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_banned = False
    user.banned_reason = None
    user.banned_at = None
    await user.save(update_fields=["is_banned", "banned_reason", "banned_at"])
    from app.services.meili_sync import sync_user_to_meili
    await sync_user_to_meili(user)
    return ResponseBase(data={"message": f"User {user_id} unbanned"})


@router.post("/users/{user_id}/creator", response_model=ResponseBase[dict])
async def toggle_creator_status(
    user_id: int,
    current_user: User = Depends(_ADMIN),
):
    """切换用户画师认证状态（仅 admin）"""
    user = await User.get_or_none(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_creator = not user.is_creator
    await user.save(update_fields=["is_creator"])
    from app.services.meili_sync import sync_user_to_meili
    await sync_user_to_meili(user)
    return ResponseBase(data={"id": user_id, "is_creator": user.is_creator})


@router.post("/users/{user_id}/warn", response_model=ResponseBase[dict])
async def warn_user(
    user_id: int,
    current_user: User = Depends(_STAFF),
):
    """向用户发送违规警告系统通知"""
    from app.services.notification_service import push_notification
    user = await User.get_or_none(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await push_notification(
        user_id=user_id,
        actor_id=current_user.id,
        type="system",
        content="您的内容因违反社区规范已被管理员处置，请遵守平台使用规则，避免再次违规。",
    )
    return ResponseBase(data={"message": "Warning notification sent"})


@router.post("/users/{user_id}/commission", response_model=ResponseBase[dict])
async def toggle_commission_status(
    user_id: int,
    current_user: User = Depends(_ADMIN),
):
    """切换用户接稿开关（仅 admin，要求用户已是画师）"""
    user = await User.get_or_none(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.is_creator:
        raise HTTPException(status_code=400, detail="用户尚未获得画师认证，无法操作接稿状态")
    user.commission_enabled = not user.commission_enabled
    await user.save(update_fields=["commission_enabled"])
    from app.services.meili_sync import sync_user_to_meili
    await sync_user_to_meili(user)
    return ResponseBase(data={"id": user_id, "commission_enabled": user.commission_enabled})


@router.post("/users/{user_id}/impersonate", response_model=ResponseBase[dict])
async def impersonate_user(
    user_id: int,
    current_user: User = Depends(_ADMIN),
):
    """【管理员】临时登录为该用户（签发该用户的 JWT Token）"""
    from app.core.security import create_access_token
    from app.core.config import settings
    from datetime import timedelta
    
    target_user = await User.get_or_none(id=user_id)
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")
        
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": str(target_user.id), "token_version": target_user.token_version},
        expires_delta=access_token_expires
    )
    
    return ResponseBase(data={
        "access_token": access_token,
        "token_type": "bearer",
        "user_id": target_user.id,
        "username": target_user.username,
        "role": target_user.role,
    })


@router.post("/pixiv-users/{pixiv_user_id}/impersonate", response_model=ResponseBase[dict])
async def impersonate_pixiv_user(
    pixiv_user_id: int,
    current_user: User = Depends(_ADMIN),
):
    """【管理员】通过 pixiv_user_id 临时登录为该导入用户"""
    from app.core.security import create_access_token
    from app.core.config import settings
    from datetime import timedelta
    
    target_user = await User.get_or_none(pixiv_user_id=pixiv_user_id)
    if not target_user:
        raise HTTPException(status_code=404, detail="该 Pixiv 作者对应的账号不存在，可能尚未导入任何作品")
        
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": str(target_user.id), "token_version": target_user.token_version},
        expires_delta=access_token_expires
    )
    
    return ResponseBase(data={
        "access_token": access_token,
        "token_type": "bearer",
        "user_id": target_user.id,
        "username": target_user.username,
        "role": target_user.role,
    })


# ──────────────────────────────────────────────────────────────────────
# 作品管理（全量，含私密）
# ──────────────────────────────────────────────────────────────────────

@router.get("/artworks", response_model=ResponseBase[List[dict]])
async def list_all_artworks(
    q: Optional[str] = None,
    author_id: Optional[int] = None,
    rating: Optional[str] = None,
    visibility: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(_STAFF),
):
    """列出所有作品（走 Meilisearch，管理员不受可见性限制）"""
    from app.infrastructure.meilisearch_client import meili_client

    parts = []
    if author_id:
        parts.append(f"author_id = '{author_id}'")
    if rating:
        parts.append(f"rating = '{rating}'")
    if visibility:
        parts.append(f"visibility = '{visibility}'")
    extra_filter = " AND ".join(parts) if parts else None

    result = await asyncio.to_thread(
        meili_client.search,
        q or "",
        extra_filter,
        ["created_at:desc"],
        limit,
        offset,
    )
    hits = result.get("hits", [])
    artworks = [
        {
            "id": h.get("id"),
            "author_id": h.get("author_id"),
            "title": h.get("title"),
            "rating": h.get("rating"),
            "visibility": h.get("visibility"),
            "is_ai": h.get("is_ai"),
            "view_count": h.get("view_count"),
            "like_count": h.get("like_count"),
            "bookmark_count": h.get("bookmark_count"),
            "created_at": h.get("created_at"),
        }
        for h in hits
    ]
    return ResponseBase(data=artworks)


@router.delete("/artworks/{artwork_id}", response_model=ResponseBase[dict])
async def admin_delete_artwork(
    artwork_id: int,
    current_user: User = Depends(_STAFF),
):
    """强制删除任意作品（管理员权限）"""
    await ArtworkService.delete_artwork(artwork_id, current_user.id, role=current_user.role)
    return ResponseBase(data={"message": f"Artwork {artwork_id} deleted"})


class VisibilityUpdateRequest(BaseModel):
    visibility: str = Field(..., pattern="^(public|followers|private)$")


@router.put("/artworks/{artwork_id}/visibility", response_model=ResponseBase[dict])
async def admin_set_artwork_visibility(
    artwork_id: int,
    body: VisibilityUpdateRequest,
    current_user: User = Depends(_STAFF),
):
    """强制修改作品可见性（moderator/admin 权限）"""
    valid = {"public", "followers", "private"}
    if body.visibility not in valid:
        raise HTTPException(status_code=400, detail=f"visibility 必须是 {valid} 之一")
    artwork = await Artwork.get_or_none(id=artwork_id)
    if not artwork:
        raise HTTPException(status_code=404, detail="Artwork not found")
    artwork.visibility = body.visibility
    await artwork.save(update_fields=["visibility"])
    return ResponseBase(data={"id": artwork_id, "visibility": artwork.visibility})


@router.post("/artworks/ai-retag-missing", response_model=ResponseBase[dict])
async def trigger_ai_retag_for_missing(current_user: User = Depends(_STAFF)):
    """触发缺失 AI 标签作品的重新打标任务"""
    from app.models.artwork import Artwork
    from app.models.tag import ArtworkTag
    from app.worker.enqueue import enqueue

    artworks = await Artwork.filter(allow_ai_tagging=True).values_list("id", flat=True)
    
    tagged_artworks = set(await ArtworkTag.filter(
        type__in=["ai_unverified", "ai_verified"]
    ).values_list("artwork_id", flat=True))

    missing_ids = [aid for aid in artworks if aid not in tagged_artworks]

    enqueued = 0
    for aid in missing_ids:
        try:
            await enqueue("task_ai_tag_artwork", artwork_id=aid)
            enqueued += 1
        except Exception:
            pass

    return ResponseBase(data={
        "message": f"Successfully triggered retagging for {enqueued} artworks.",
        "enqueued_count": enqueued
    })


# ──────────────────────────────────────────────────────────────────────
# 约稿管理（全量）
# ──────────────────────────────────────────────────────────────────────

@router.get("/commissions", response_model=ResponseBase[List[dict]])
async def list_all_commissions(
    status: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(_STAFF),
):
    """查看全站约稿记录"""
    qs = Commission.all()
    if status:
        qs = qs.filter(status=status)
    items = await qs.prefetch_related("client", "creator").order_by("-created_at").offset(offset).limit(limit)
    data = []
    for c in items:
        try: client_uname = c.client.username
        except Exception: client_uname = None
        try: creator_uname = c.creator.username
        except Exception: creator_uname = None
        data.append({
            "id": c.id,
            "client_id": c.client_id,
            "client_username": client_uname,
            "creator_id": c.creator_id,
            "creator_username": creator_uname,
            "title": c.title,
            "price": float(c.price),
            "status": c.status,
            "deadline": c.deadline.isoformat() if c.deadline else None,
            "created_at": c.created_at.isoformat(),
        })
    return ResponseBase(data=data)


@router.get("/commissions/{commission_id}", response_model=ResponseBase[dict])
async def get_commission_detail(
    commission_id: int,
    current_user: User = Depends(_STAFF),
):
    """【管理员/版主】查看单个约稿完整详情"""
    commission = await Commission.get_or_none(id=commission_id).prefetch_related("client", "creator")
    if not commission:
        raise HTTPException(status_code=404, detail="Commission not found")
    try: client_uname = commission.client.username
    except Exception: client_uname = None
    try: creator_uname = commission.creator.username
    except Exception: creator_uname = None
    return ResponseBase(data={
        "id": commission.id,
        "client_id": commission.client_id,
        "client_username": client_uname,
        "creator_id": commission.creator_id,
        "creator_username": creator_uname,
        "title": commission.title,
        "description": commission.description,
        "price": float(commission.price),
        "status": commission.status,
        "deadline": commission.deadline.isoformat() if commission.deadline else None,
        "delivered_artwork_id": commission.delivered_artwork_id,
        "creator_note": commission.creator_note,
        "cancelled_reason": commission.cancelled_reason,
        "terminated_by": commission.terminated_by,
        "created_at": commission.created_at.isoformat(),
        "updated_at": commission.updated_at.isoformat(),
    })


class AdminTerminateCommissionRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=1000)


@router.post("/commissions/{commission_id}/terminate", response_model=ResponseBase[dict])
async def admin_terminate_commission(
    commission_id: int,
    body: AdminTerminateCommissionRequest,
    current_user: User = Depends(_STAFF),
):
    """【管理员/版主】强制终止任意进行中的约稿，并通知双方。"""
    commission = await Commission.get_or_none(id=commission_id).prefetch_related("client", "creator")
    if not commission:
        raise HTTPException(status_code=404, detail="Commission not found")

    terminable_statuses = {"pending", "accepted", "in_progress", "revision_requested"}
    if commission.status not in terminable_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"只能终止状态为 {terminable_statuses} 的约稿，当前状态：{commission.status}",
        )

    commission.status = "cancelled"
    commission.cancelled_reason = body.reason
    commission.terminated_by = "admin"
    await commission.save()

    from app.services.notification_service import push_notification
    notice = f"约稿《{commission.title}》已被管理员强制终止。原因：{body.reason}"
    await push_notification(
        user_id=commission.client_id,
        actor_id=current_user.id,
        type="commission",
        content=notice,
        related_entity_id=str(commission.id),
    )
    await push_notification(
        user_id=commission.creator_id,
        actor_id=current_user.id,
        type="commission",
        content=notice,
        related_entity_id=str(commission.id),
    )

    return ResponseBase(data={"message": "Commission terminated by admin", "commission_id": commission_id})



# ──────────────────────────────────────────────────────────────────────
# 画师认证申请审核（admin only）
# ──────────────────────────────────────────────────────────────────────

class CreatorApplicationReviewRequest(BaseModel):
    decision: str = Field(..., pattern="^(approved|rejected)$")
    note: Optional[str] = Field(None, max_length=1000)


@router.get("/creator-applications", response_model=ResponseBase[List[dict]])
async def list_creator_applications(
    status: Optional[str] = Query(None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(_ADMIN),
):
    """【管理员】查看画师认证申请列表"""
    from app.models.creator_application import CreatorApplication
    qs = CreatorApplication.all().prefetch_related("applicant", "reviewed_by")
    if status:
        qs = qs.filter(status=status)
    apps = await qs.order_by("-created_at").offset(offset).limit(limit)
    result = []
    for a in apps:
        try:
            uname = a.applicant.username
            uid = a.applicant.id
            avatar = a.applicant.avatar_url
        except Exception:
            uname, uid, avatar = None, None, None
        try:
            reviewer = a.reviewed_by.username
        except Exception:
            reviewer = None
        result.append({
            "id": a.id,
            "applicant_id": uid,
            "applicant_username": uname,
            "applicant_avatar": avatar,
            "reason": a.reason,
            "portfolio_url": a.portfolio_url,
            "status": a.status,
            "review_note": a.review_note,
            "reviewed_by": reviewer,
            "reviewed_at": a.reviewed_at.isoformat() if a.reviewed_at else None,
            "created_at": a.created_at.isoformat(),
        })
    return ResponseBase(data=result)


@router.post("/creator-applications/{app_id}/review", response_model=ResponseBase[dict])
async def review_creator_application(
    app_id: int,
    body: CreatorApplicationReviewRequest,
    current_user: User = Depends(_ADMIN),
):
    """【管理员】审批画师认证申请（approved/rejected）。批准后自动设置用户 is_creator=True。"""
    from datetime import datetime, timezone
    from app.models.creator_application import CreatorApplication
    from app.services.notification_service import push_notification
    from app.services.meili_sync import sync_user_to_meili

    app = await CreatorApplication.get_or_none(id=app_id).prefetch_related("applicant")
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    if app.status != "pending":
        raise HTTPException(status_code=400, detail="Application is not pending")

    app.status = body.decision
    app.review_note = body.note
    app.reviewed_by = current_user
    app.reviewed_at = datetime.now(timezone.utc)
    await app.save()

    user = app.applicant
    if body.decision == "approved":
        user.is_creator = True
        await user.save(update_fields=["is_creator"])
        await sync_user_to_meili(user)
        msg = "恭喜！你的画师认证申请已通过审核。"
    else:
        msg = f"你的画师认证申请未通过审核。{('原因：' + body.note) if body.note else ''}"

    await push_notification(
        user_id=user.id,
        actor_id=current_user.id,
        type="system",
        content=msg,
    )
    return ResponseBase(data={"id": app_id, "status": body.decision})


# ──────────────────────────────────────────────────────────────────────
# 举报管理（admin 可直接裁决，已在 reports.py 实现；此处提供批量查看）
# ──────────────────────────────────────────────────────────────────────

@router.get("/reports", response_model=ResponseBase[List[dict]])
async def list_reports(
    status: Optional[str] = "pending",
    appeal_status: Optional[str] = Query(None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(_STAFF),
):
    """查看所有作品举报（可按 status / appeal_status 过滤）"""
    qs = ArtworkReport.all()
    if status and status != "all":
        qs = qs.filter(status=status)
    if appeal_status:
        qs = qs.filter(appeal_status=appeal_status)
    reports = await qs.order_by("created_at").offset(offset).limit(limit).values(
        "id", "artwork_id", "reporter_id",
        "reason", "description", "status", "admin_note", "created_at",
        "artwork__author_id",
        "appeal_status", "appeal_text", "appeal_note",
    )
    return ResponseBase(data=reports)


@router.get("/user-reports", response_model=ResponseBase[List[dict]])
async def list_user_reports(
    status: Optional[str] = "pending",
    appeal_status: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(_STAFF),
):
    """【管理员/版主】查看所有用户举报（可按 status / appeal_status 过滤）"""
    from app.models.report import UserReport
    qs = UserReport.all()
    if status and status != "all":
        qs = qs.filter(status=status)
    if appeal_status:
        qs = qs.filter(appeal_status=appeal_status)
    reports = await qs.order_by("created_at").offset(offset).limit(limit).values(
        "id", "reporter_id", "reported_user_id",
        "reason", "description", "status", "admin_note", "created_at",
        "appeal_status", "appeal_text", "appeal_note",
        "reviewed_at",
    )
    return ResponseBase(data=reports)


# ──────────────────────────────────────────────────────────────────────
# 风格参考图管理（admin only）
# ──────────────────────────────────────────────────────────────────────



class StyleRefCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)


class StyleRefUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    work_name: Optional[str] = Field(None, max_length=200)
    faction_name: Optional[str] = Field(None, max_length=200)
    character_name: Optional[str] = Field(None, max_length=200)
    tags: Optional[List[str]] = Field(None, max_length=50)
    similarity_threshold: Optional[float] = None


@router.get("/style-references", response_model=ResponseBase[List[dict]])
async def list_style_references(current_user: User = Depends(_STAFF)):
    """列出所有锚点基准图"""
    from app.models.artwork import StyleReference
    refs = await StyleReference.all().order_by("-created_at").values(
        "id", "name", "description", "file_url", "qdrant_id", "created_at",
        "work_name", "faction_name", "character_name", "tags", "similarity_threshold"
    )
    return ResponseBase(data=refs)


@router.post("/style-references", response_model=ResponseBase[dict])
async def upload_style_reference(
    name: str = Form(...),
    description: str = Form(None),
    work_name: Optional[str] = Form(None),
    faction_name: Optional[str] = Form(None),
    character_name: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    similarity_threshold: float = Form(0.75),
    file: UploadFile = File(...),
    current_user: User = Depends(_ADMIN),
):
    """上传锚点基准图，提取 WD14 向量存入 Qdrant style_refs 集合；填写角色层级字段后可触发锚点自动打标"""
    from app.models.artwork import StyleReference
    from app.services.ai_engine import ai_engine
    from app.infrastructure.qdrant_client import qdrant_client

    ext = os.path.splitext(file.filename or "img.jpg")[1].lower() or ".jpg"
    allowed = {".jpg", ".jpeg", ".png", ".webp"}
    if ext not in allowed:
        raise HTTPException(status_code=400, detail="仅支持 jpg/png/webp 格式")

    import tempfile
    ref_uuid = str(uuid.uuid4())
    filename = f"{ref_uuid}{ext}"
    file_data = await file.read()
    if len(file_data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="风格参考图不能超过 10MB")

    # 先用临时文件进行 AI 提取，再将文件上传到存储后端
    fd, tmp_path = tempfile.mkstemp(suffix=ext)
    os.close(fd)
    try:
        await asyncio.to_thread(lambda: open(tmp_path, "wb").write(file_data))
        vector, _ = await asyncio.to_thread(ai_engine.extract_vector, tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    file_url = await storage.save(file_data, f"style_refs/{filename}")
    await asyncio.to_thread(
        qdrant_client.upsert_style_ref,
        ref_uuid,
        vector,
        {"name": name, "file_url": file_url},
    )


    tags_list = [t.strip() for t in (tags or "").split(",") if t.strip()] if tags else []
    ref = await StyleReference.create(
        name=name,
        description=description,
        file_url=file_url,
        qdrant_id=ref_uuid,
        uploaded_by_id=current_user.id,
        work_name=work_name or None,
        faction_name=faction_name or None,
        character_name=character_name or None,
        tags=tags_list,
        similarity_threshold=similarity_threshold,
    )
    return ResponseBase(data={
        "id": ref.id,
        "name": ref.name,
        "description": ref.description,
        "file_url": ref.file_url,
        "qdrant_id": ref.qdrant_id,
        "work_name": ref.work_name,
        "faction_name": ref.faction_name,
        "character_name": ref.character_name,
        "tags": ref.tags or [],
        "similarity_threshold": ref.similarity_threshold,
        "created_at": ref.created_at.isoformat(),
    })


@router.put("/style-references/{ref_id}", response_model=ResponseBase[dict])
async def update_style_reference(
    ref_id: int,
    body: StyleRefUpdate,
    current_user: User = Depends(_ADMIN),
):
    """更新锚点基准图的元数据（不替换图片/向量）"""
    from app.models.artwork import StyleReference
    ref = await StyleReference.get_or_none(id=ref_id)
    if not ref:
        raise HTTPException(status_code=404, detail="Style reference not found")

    update_fields = []
    if body.name is not None:
        ref.name = body.name; update_fields.append("name")
    if body.description is not None:
        ref.description = body.description; update_fields.append("description")
    if body.work_name is not None:
        ref.work_name = body.work_name or None; update_fields.append("work_name")
    if body.faction_name is not None:
        ref.faction_name = body.faction_name or None; update_fields.append("faction_name")
    if body.character_name is not None:
        ref.character_name = body.character_name or None; update_fields.append("character_name")
    if body.tags is not None:
        ref.tags = body.tags; update_fields.append("tags")
    if body.similarity_threshold is not None:
        ref.similarity_threshold = body.similarity_threshold; update_fields.append("similarity_threshold")

    if update_fields:
        await ref.save(update_fields=update_fields)

    return ResponseBase(data={
        "id": ref.id,
        "name": ref.name,
        "description": ref.description,
        "file_url": ref.file_url,
        "qdrant_id": ref.qdrant_id,
        "work_name": ref.work_name,
        "faction_name": ref.faction_name,
        "character_name": ref.character_name,
        "tags": ref.tags or [],
        "similarity_threshold": ref.similarity_threshold,
        "created_at": ref.created_at.isoformat(),
    })


# ──────────────────────────────────────────────────────────────────────
# 全站配置（admin only）
# ──────────────────────────────────────────────────────────────────────

SITE_CONFIG_DEFAULTS: dict = {
    "site_name":             "Moetopia",
    "site_description":      "二次元插画社区",
    "site_icon_url":         "",
    "site_favicon_url":      "",
    "registration_enabled":  True,
    "r18_global_enabled":    True,
    "smtp_host":             "",
    "smtp_port":             587,
    "smtp_user":             "",
    "smtp_password":         "",
    "smtp_from":             "",
    "smtp_tls":              True,
    "frontend_url":          "http://localhost:3000",
    "crowdin_project_id":    "",
    "crowdin_api_token":     "",
    # 支付配置
    "payment_provider":      "demo",
    "wechat_app_id":         "",
    "wechat_mch_id":         "",
    "wechat_api_v3_key":     "",
    "wechat_notify_url":     "",
    "alipay_app_id":         "",
    "alipay_private_key":    "",
    "alipay_public_key":     "",
    "alipay_notify_url":     "",
    # AI 功能开关（总开关 + 子功能独立开关）
    "enable_ai_features":        True,
    "enable_wd14_tagging":       True,
    "enable_qdrant":             True,
    "enable_content_moderation": True,
    # 翻译功能配置
    "translation_enabled":       False,
    "mit_server_url":            "http://localhost:5003",
    "mit_timeout":               300,
    "translation_target_langs":  ["CHS", "ENG"],
    "mit_translator":            "offline",
    "mit_detection_size":        1536,
    "mit_detector":              "default",
    "mit_direction":             "auto",
    "mit_inpainter":             "default",
    "mit_inpainting_size":       2048,
    "mit_unclip_ratio":          2.3,
    "mit_box_threshold":         0.7,
    "mit_mask_dilation_offset":  30,
    # 法律链接
    "tos_url":            "",
    "privacy_policy_url": "",
}


@router.get("/site-config", response_model=ResponseBase[dict])
async def get_site_config(current_user: User = Depends(_ADMIN)):
    """读取全站配置（仅 admin）"""
    from app.models.site_config import SiteConfig
    from app.infrastructure.cache import cache_get, cache_set, TTL_SITE_CONFIG
    cached = await cache_get("site_config")
    if cached is not None:
        return ResponseBase(data=cached)
    rows = await SiteConfig.all().values("key", "value")
    cfg = dict(SITE_CONFIG_DEFAULTS)
    for row in rows:
        cfg[row["key"]] = row["value"]
    await cache_set("site_config", cfg, TTL_SITE_CONFIG)
    return ResponseBase(data=cfg)


class SiteConfigUpdateRequest(BaseModel):
    config: dict


@router.put("/site-config", response_model=ResponseBase[dict])
async def update_site_config(
    body: SiteConfigUpdateRequest,
    current_user: User = Depends(_ADMIN),
):
    """更新全站配置（仅 admin）"""
    import json as _json
    from tortoise import Tortoise as _Tortoise
    allowed_keys = set(SITE_CONFIG_DEFAULTS.keys())
    conn = _Tortoise.get_connection("default")
    for key, value in body.config.items():
        if key not in allowed_keys:
            continue
        await conn.execute_query(
            "INSERT INTO site_configs (key, value) VALUES ($1, $2::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = $2::jsonb",
            [key, _json.dumps(value)],
        )
    from app.infrastructure.cache import invalidate_site_config, cache_set, TTL_SITE_CONFIG
    from app.models.site_config import SiteConfig
    await invalidate_site_config()
    rows = await SiteConfig.all().values("key", "value")
    cfg = dict(SITE_CONFIG_DEFAULTS)
    for row in rows:
        cfg[row["key"]] = row["value"]
    await cache_set("site_config", cfg, TTL_SITE_CONFIG)
    return ResponseBase(data=cfg)


class SmtpTestRequest(BaseModel):
    to: str


@router.post("/smtp/test", response_model=ResponseBase[dict])
async def test_smtp(
    body: SmtpTestRequest,
    current_user: User = Depends(_ADMIN),
):
    """发送 SMTP 测试邮件（使用当前站点配置中的 SMTP 参数，失败时返回具体错误）"""
    from app.models.site_config import SiteConfig
    from app.infrastructure.cache import cache_get
    from app.services.email_service import EmailService

    cfg = await cache_get("site_config") or {}
    if not cfg:
        rows = await SiteConfig.all().values("key", "value")
        cfg = {r["key"]: r["value"] for r in rows}

    host = str(cfg.get("smtp_host", "")).strip()
    if not host:
        raise HTTPException(status_code=400, detail="SMTP 服务器地址未配置，请先填写并保存 SMTP 配置")

    smtp = {
        "host":      host,
        "port":      int(cfg.get("smtp_port", 587)),
        "user":      str(cfg.get("smtp_user", "")),
        "password":  str(cfg.get("smtp_password", "")),
        "from_addr": str(cfg.get("smtp_from", "") or cfg.get("smtp_user", "")),
        "tls":       bool(cfg.get("smtp_tls", True)),
    }
    html = """
    <div style="font-family:sans-serif;max-width:480px;margin:auto;padding:32px;">
      <h2 style="color:#FF7FAB;">Moetopia SMTP 测试</h2>
      <p>这是来自 Moetopia 管理后台的 SMTP 连接测试邮件。</p>
      <p style="color:#666;font-size:13px;">
        如果你收到此邮件，说明邮件服务配置正确，可以正常发送邮件。
      </p>
      <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
      <p style="color:#999;font-size:12px;">Moetopia &copy; 2025</p>
    </div>
    """
    try:
        await asyncio.to_thread(
            EmailService._send_with_config,
            body.to, "Moetopia SMTP 连接测试", html, **smtp,
        )
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"邮件发送失败（{smtp['host']}:{smtp['port']} tls={smtp['tls']}）: {e}",
        )

    return ResponseBase(data={"message": f"测试邮件已成功发送至 {body.to}"})


@router.post("/site-config/upload-icon", response_model=ResponseBase[dict])
async def upload_site_icon(
    file: UploadFile = File(...),
    current_user: User = Depends(_ADMIN),
):
    """上传站点图标（返回文件 URL，需再调用 PUT /site-config 保存）"""
    allowed = {".jpg", ".jpeg", ".png", ".webp", ".svg", ".ico"}
    ext = os.path.splitext(file.filename or "icon.png")[1].lower() or ".png"
    if ext not in allowed:
        raise HTTPException(status_code=400, detail="仅支持 jpg/png/webp/svg/ico 格式")
    icon_data = await file.read()
    if len(icon_data) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图标文件不能超过 2MB")
    filename = f"{uuid.uuid4()}{ext}"
    url = await storage.save(icon_data, f"site/{filename}")
    return ResponseBase(data={"url": url})


@router.delete("/style-references/{ref_id}", response_model=ResponseBase[dict])
async def delete_style_reference(
    ref_id: int,
    current_user: User = Depends(_ADMIN),
):
    """删除风格参考图（同步清除 Qdrant 向量和本地文件）"""
    from app.models.artwork import StyleReference
    from app.infrastructure.qdrant_client import qdrant_client
    ref = await StyleReference.get_or_none(id=ref_id)
    if not ref:
        raise HTTPException(status_code=404, detail="Style reference not found")

    await asyncio.to_thread(qdrant_client.delete_style_ref, ref.qdrant_id)
    await storage.delete_by_url(ref.file_url)
    await ref.delete()
    return ResponseBase(data={"deleted": True})


# ──────────────────────────────────────────────────────────────────────────────
# 验证码题库管理
# ──────────────────────────────────────────────────────────────────────────────

class CaptchaQuestionCreate(BaseModel):
    question:       str             = Field(..., max_length=500)
    question_type:  str             = Field("text", pattern="^(text|choice|tile)$")
    answer:         str             = Field("", max_length=200)
    choices:        Optional[List[str]]  = None
    tile_images:    Optional[List[str]]  = None
    correct_indices:Optional[List[int]]  = None
    tile_rows:      int             = Field(3, ge=1, le=6)
    tile_cols:      int             = Field(3, ge=1, le=6)
    hint_image:     Optional[str]        = Field(None, max_length=500)
    is_active:      bool            = True


class CaptchaQuestionUpdate(BaseModel):
    question:       Optional[str]        = Field(None, max_length=500)
    question_type:  Optional[str]        = Field(None, pattern="^(text|choice|tile)$")
    answer:         Optional[str]        = Field(None, max_length=200)
    choices:        Optional[List[str]]  = None
    tile_images:    Optional[List[str]]  = None
    correct_indices:Optional[List[int]]  = None
    tile_rows:      Optional[int]        = Field(None, ge=1, le=6)
    tile_cols:      Optional[int]        = Field(None, ge=1, le=6)
    hint_image:     Optional[str]        = Field(None, max_length=500)
    is_active:      Optional[bool]       = None


def _serialize_captcha(q) -> dict:
    return {
        "id":             q.id,
        "question":       q.question,
        "question_type":  q.question_type,
        "answer":         q.answer,
        "choices":        q.choices,
        "tile_images":    q.tile_images,
        "correct_indices":q.correct_indices,
        "tile_rows":      q.tile_rows,
        "tile_cols":      q.tile_cols,
        "hint_image":     q.hint_image,
        "is_active":      q.is_active,
        "created_at":     q.created_at.isoformat(),
        "updated_at":     q.updated_at.isoformat(),
    }


@router.get("/captcha-questions", response_model=ResponseBase[dict])
async def list_captcha_questions(
    limit:  int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(_ADMIN),
):
    """列出所有验证码题目"""
    from app.models.captcha import CaptchaQuestion
    total = await CaptchaQuestion.all().count()
    items = await CaptchaQuestion.all().order_by("-created_at").offset(offset).limit(limit)
    return ResponseBase(data={"total": total, "items": [_serialize_captcha(q) for q in items]})


@router.post("/captcha-questions/upload-tile", response_model=ResponseBase[dict])
async def upload_captcha_tile(
    image: UploadFile = File(...),
    current_user: User = Depends(_ADMIN),
):
    """上传单张验证码拼图图片，返回可用 URL"""
    data = await image.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片不能超过 5MB")
    ext = os.path.splitext(image.filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        raise HTTPException(status_code=400, detail="仅支持 jpg/png/webp 格式")
    filename = f"captcha/{uuid.uuid4()}{ext}"
    url = await storage.save(data, filename)
    return ResponseBase(data={"url": url})


@router.post("/captcha-questions/slice-image", response_model=ResponseBase[dict])
async def slice_captcha_image(
    rows:  int        = Form(..., ge=1, le=6),
    cols:  int        = Form(..., ge=1, le=6),
    image: UploadFile = File(...),
    current_user: User = Depends(_ADMIN),
):
    """上传一张背景图并自动切割为 rows×cols 小图片，返回全部小图 URL"""
    import io
    import asyncio
    from PIL import Image as PILImage

    raw = await image.read()
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片不能超过 10MB")
    if rows * cols > 16:
        raise HTTPException(status_code=400, detail="格子数量不能超过 16")

    def _slice(raw_bytes: bytes) -> list[bytes]:
        src = PILImage.open(io.BytesIO(raw_bytes)).convert("RGB")
        w, h = src.size
        tw, th = w // cols, h // rows
        tiles = []
        for r in range(rows):
            for c in range(cols):
                box = (c * tw, r * th, (c + 1) * tw, (r + 1) * th)
                buf = io.BytesIO()
                src.crop(box).save(buf, format="JPEG", quality=88)
                tiles.append(buf.getvalue())
        return tiles

    tile_bytes_list = await asyncio.to_thread(_slice, raw)
    urls = []
    for tile_bytes in tile_bytes_list:
        fn  = f"captcha/{uuid.uuid4()}.jpg"
        url = await storage.save(tile_bytes, fn)
        urls.append(url)

    return ResponseBase(data={"urls": urls, "rows": rows, "cols": cols})


@router.post("/captcha-questions", response_model=ResponseBase[dict])
async def create_captcha_question(
    body: CaptchaQuestionCreate,
    current_user: User = Depends(_ADMIN),
):
    """新增验证码题目"""
    from app.models.captcha import CaptchaQuestion
    if body.question_type == "choice" and not body.choices:
        raise HTTPException(status_code=400, detail="选择题必须提供 choices")
    if body.question_type == "tile":
        if not body.tile_images or len(body.tile_images) < 2:
            raise HTTPException(status_code=400, detail="图块验证码至少需要 2 张图片")
        if not body.correct_indices or len(body.correct_indices) == 0:
            raise HTTPException(status_code=400, detail="图块验证码至少需要 1 个正确格子")
    q = await CaptchaQuestion.create(
        question=body.question,
        question_type=body.question_type,
        answer=body.answer or "",
        choices=body.choices,
        tile_images=body.tile_images,
        correct_indices=body.correct_indices,
        tile_rows=body.tile_rows,
        tile_cols=body.tile_cols,
        hint_image=body.hint_image,
        is_active=body.is_active,
    )
    return ResponseBase(data=_serialize_captcha(q))


@router.put("/captcha-questions/{q_id}", response_model=ResponseBase[dict])
async def update_captcha_question(
    q_id: int,
    body: CaptchaQuestionUpdate,
    current_user: User = Depends(_ADMIN),
):
    """更新验证码题目"""
    from app.models.captcha import CaptchaQuestion
    q = await CaptchaQuestion.get_or_none(id=q_id)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")
    if body.question        is not None: q.question        = body.question
    if body.question_type   is not None: q.question_type   = body.question_type
    if body.answer          is not None: q.answer          = body.answer
    if body.choices         is not None: q.choices         = body.choices
    if body.tile_images     is not None: q.tile_images     = body.tile_images
    if body.correct_indices is not None: q.correct_indices = body.correct_indices
    if body.tile_rows       is not None: q.tile_rows       = body.tile_rows
    if body.tile_cols       is not None: q.tile_cols       = body.tile_cols
    if body.hint_image      is not None: q.hint_image      = body.hint_image or None
    if body.is_active       is not None: q.is_active       = body.is_active
    await q.save()
    return ResponseBase(data=_serialize_captcha(q))


@router.delete("/captcha-questions/{q_id}", response_model=ResponseBase[dict])
async def delete_captcha_question(
    q_id: int,
    current_user: User = Depends(_ADMIN),
):
    """删除验证码题目"""
    from app.models.captcha import CaptchaQuestion
    q = await CaptchaQuestion.get_or_none(id=q_id)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")
    await q.delete()
    return ResponseBase(data={"deleted": True})


# ──────────────────────────────────────────────────────────────────────────────
# 会员档位管理（admin）
# ──────────────────────────────────────────────────────────────────────────────

class MembershipPlanCreate(BaseModel):
    name: str = Field(..., max_length=100)
    description: str = ""
    monthly_price: float = Field(..., ge=0)
    quarterly_price: Optional[float] = None
    semi_annual_price: Optional[float] = None
    yearly_price: Optional[float] = None
    permissions: dict = Field(default_factory=dict)
    is_active: bool = True
    sort_order: int = 0


class MembershipPlanUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = None
    monthly_price: Optional[float] = Field(None, ge=0)
    quarterly_price: Optional[float] = None
    semi_annual_price: Optional[float] = None
    yearly_price: Optional[float] = None
    permissions: Optional[dict] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None


def _serialize_plan(p) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "monthly_price": float(p.monthly_price),
        "quarterly_price": float(p.quarterly_price) if p.quarterly_price is not None else None,
        "semi_annual_price": float(p.semi_annual_price) if p.semi_annual_price is not None else None,
        "yearly_price": float(p.yearly_price) if p.yearly_price is not None else None,
        "permissions": p.permissions,
        "is_active": p.is_active,
        "sort_order": p.sort_order,
        "created_at": p.created_at.isoformat(),
    }


@router.get("/membership/plans", response_model=ResponseBase[list])
async def admin_list_membership_plans(current_user: User = Depends(_ADMIN)):
    """列出所有会员档位。"""
    from app.models.membership_plan import MembershipPlan
    plans = await MembershipPlan.all().order_by("sort_order", "id")
    return ResponseBase(data=[_serialize_plan(p) for p in plans])


@router.post("/membership/plans", response_model=ResponseBase[dict])
async def admin_create_membership_plan(
    body: MembershipPlanCreate,
    current_user: User = Depends(_ADMIN),
):
    """创建会员档位。"""
    from app.models.membership_plan import MembershipPlan
    from decimal import Decimal
    p = await MembershipPlan.create(
        name=body.name,
        description=body.description,
        monthly_price=Decimal(str(body.monthly_price)),
        quarterly_price=Decimal(str(body.quarterly_price)) if body.quarterly_price is not None else None,
        semi_annual_price=Decimal(str(body.semi_annual_price)) if body.semi_annual_price is not None else None,
        yearly_price=Decimal(str(body.yearly_price)) if body.yearly_price is not None else None,
        permissions=body.permissions,
        is_active=body.is_active,
        sort_order=body.sort_order,
    )
    return ResponseBase(data=_serialize_plan(p))


@router.get("/membership/plans/{plan_id}", response_model=ResponseBase[dict])
async def admin_get_membership_plan(
    plan_id: int,
    current_user: User = Depends(_ADMIN),
):
    """获取单个档位详情。"""
    from app.models.membership_plan import MembershipPlan
    p = await MembershipPlan.get_or_none(id=plan_id)
    if not p:
        raise HTTPException(status_code=404, detail="档位不存在")
    return ResponseBase(data=_serialize_plan(p))


@router.put("/membership/plans/{plan_id}", response_model=ResponseBase[dict])
async def admin_update_membership_plan(
    plan_id: int,
    body: MembershipPlanUpdate,
    current_user: User = Depends(_ADMIN),
):
    """更新会员档位。"""
    from app.models.membership_plan import MembershipPlan
    from decimal import Decimal
    p = await MembershipPlan.get_or_none(id=plan_id)
    if not p:
        raise HTTPException(status_code=404, detail="档位不存在")
    if body.name              is not None: p.name              = body.name
    if body.description       is not None: p.description       = body.description
    if body.monthly_price     is not None: p.monthly_price     = Decimal(str(body.monthly_price))
    if body.quarterly_price   is not None: p.quarterly_price   = Decimal(str(body.quarterly_price))
    if body.semi_annual_price is not None: p.semi_annual_price = Decimal(str(body.semi_annual_price))
    if body.yearly_price      is not None: p.yearly_price      = Decimal(str(body.yearly_price))
    if body.permissions       is not None: p.permissions       = body.permissions
    if body.is_active         is not None: p.is_active         = body.is_active
    if body.sort_order        is not None: p.sort_order        = body.sort_order
    await p.save()
    return ResponseBase(data=_serialize_plan(p))


@router.delete("/membership/plans/{plan_id}", response_model=ResponseBase[dict])
async def admin_delete_membership_plan(
    plan_id: int,
    current_user: User = Depends(_ADMIN),
):
    """删除会员档位（已有订阅不受影响）。"""
    from app.models.membership_plan import MembershipPlan
    p = await MembershipPlan.get_or_none(id=plan_id)
    if not p:
        raise HTTPException(status_code=404, detail="档位不存在")
    await p.delete()
    return ResponseBase(data={"deleted": True})


@router.get("/membership/subscribers", response_model=ResponseBase[dict])
async def admin_list_subscribers(
    plan_id: Optional[int] = Query(None),
    status: Optional[str]  = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(_ADMIN),
):
    """所有订阅记录（可按 plan/status 过滤）。"""
    from app.models.user_membership import UserMembership
    q = UserMembership.all().prefetch_related("user", "plan")
    if plan_id:
        q = q.filter(plan_id=plan_id)
    if status:
        q = q.filter(status=status)
    total = await q.count()
    items = await q.order_by("-created_at").offset(offset).limit(limit)
    return ResponseBase(data={
        "total": total,
        "items": [
            {
                "id": s.id,
                "user_id": s.user_id,
                "username": s.user.username,
                "plan_id": s.plan_id,
                "plan_name": s.plan.name,
                "status": s.status,
                "started_at": s.started_at.isoformat(),
                "expires_at": s.expires_at.isoformat(),
                "payment_ref": s.payment_ref,
                "created_at": s.created_at.isoformat(),
            }
            for s in items
        ],
    })


class ManageSubscriptionRequest(BaseModel):
    action: str = Field(..., pattern="^(extend|cancel)$")
    days: Optional[int] = Field(None, ge=1, le=3650)


@router.patch("/membership/subscriptions/{sub_id}", response_model=ResponseBase[dict])
async def admin_manage_subscription(
    sub_id: int,
    body: ManageSubscriptionRequest,
    current_user: User = Depends(_ADMIN),
):
    """延期或取消指定订阅。"""
    from app.models.user_membership import UserMembership
    from datetime import datetime, timezone, timedelta
    sub = await UserMembership.get_or_none(id=sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="订阅不存在")
    if body.action == "cancel":
        sub.status = "cancelled"
        await sub.save(update_fields=["status"])
        return ResponseBase(data={"id": sub.id, "status": "cancelled", "expires_at": sub.expires_at.isoformat()})
    # extend
    if not body.days:
        raise HTTPException(status_code=422, detail="延期需指定天数")
    now = datetime.now(timezone.utc)
    base = sub.expires_at if sub.expires_at > now else now
    sub.expires_at = base + timedelta(days=body.days)
    sub.status = "active"
    await sub.save(update_fields=["expires_at", "status"])
    return ResponseBase(data={"id": sub.id, "status": sub.status, "expires_at": sub.expires_at.isoformat()})


class GrantMembershipRequest(BaseModel):
    user_id: int
    plan_id: int
    days: int = Field(..., ge=1, le=3650)


@router.post("/membership/grant", response_model=ResponseBase[dict])
async def admin_grant_membership(
    body: GrantMembershipRequest,
    current_user: User = Depends(_ADMIN),
):
    """手动赠送会员。"""
    from app.models.membership_plan import MembershipPlan
    from app.models.user_membership import UserMembership
    from datetime import datetime, timezone, timedelta
    plan = await MembershipPlan.get_or_none(id=body.plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="档位不存在")
    user = await User.get_or_none(id=body.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=body.days)
    sub = await UserMembership.create(
        user_id=body.user_id,
        plan_id=body.plan_id,
        status="active",
        started_at=now,
        expires_at=expires,
        payment_ref="ADMIN-GRANT",
    )
    return ResponseBase(data={
        "id": sub.id,
        "user_id": body.user_id,
        "plan_name": plan.name,
        "expires_at": expires.isoformat(),
    })


# ──────────────────────────────────────────────────────────────────────────────
# 翻译管理（admin）
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/translation/tasks", response_model=ResponseBase[dict])
async def admin_translation_tasks(
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(_ADMIN),
):
    """翻译任务列表（可按 status 过滤）。"""
    from app.models.artwork_translation import ArtworkTranslation
    q = ArtworkTranslation.all().prefetch_related("artwork", "requested_by")
    if status:
        q = q.filter(status=status)
    total = await q.count()
    items = await q.order_by("-created_at").offset(offset).limit(limit)
    return ResponseBase(data={
        "total": total,
        "items": [
            {
                "id": t.id,
                "artwork_id": t.artwork_id,
                "artwork_title": t.artwork.title,
                "target_lang": t.target_lang,
                "status": t.status,
                "error_msg": t.error_msg,
                "requested_by_id": t.requested_by_id,
                "requested_by_username": t.requested_by.username if t.requested_by else None,
                "translated_image_url": t.translated_image_url,
                "created_at": t.created_at.isoformat(),
                "updated_at": t.updated_at.isoformat(),
            }
            for t in items
        ],
    })


@router.post("/translation/tasks/{task_id}/requeue", response_model=ResponseBase[dict])
async def admin_requeue_translation(
    task_id: int,
    current_user: User = Depends(_ADMIN),
):
    """将失败任务重新排队。"""
    from app.models.artwork_translation import ArtworkTranslation
    from app.worker.enqueue import enqueue
    t = await ArtworkTranslation.get_or_none(id=task_id)
    if not t:
        raise HTTPException(status_code=404, detail="任务不存在")
    if t.status not in ("failed", "pending"):
        raise HTTPException(status_code=400, detail="仅可对 failed/pending 任务重新排队")
    t.status = "pending"
    t.error_msg = None
    await t.save(update_fields=["status", "error_msg"])
    await enqueue("task_translate_artwork", translation_id=t.id)
    return ResponseBase(data={"queued": True})


@router.get("/translation/stats", response_model=ResponseBase[dict])
async def admin_translation_stats(current_user: User = Depends(_ADMIN)):
    """翻译用量统计：KPI + 30天每日趋势 + 用户排行。"""
    from app.models.artwork_translation import ArtworkTranslation
    from tortoise.functions import Count
    from datetime import datetime, timezone, timedelta
    from tortoise.expressions import Q

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since_30d = now - timedelta(days=30)

    total = await ArtworkTranslation.all().count()
    done_count = await ArtworkTranslation.filter(status="done").count()
    today_count = await ArtworkTranslation.filter(created_at__gte=today_start).count()
    success_rate = round(done_count / total * 100, 1) if total else 0.0

    # 近 30 天每日翻译量
    from tortoise import Tortoise as _T
    conn = _T.get_connection("default")
    rows = await conn.execute_query_dict(
        "SELECT DATE(created_at AT TIME ZONE 'UTC') AS day, COUNT(*) AS cnt "
        "FROM artwork_translations "
        "WHERE created_at >= $1 "
        "GROUP BY day ORDER BY day",
        [since_30d],
    )
    daily_series = [{"date": str(r["day"]), "count": int(r["cnt"])} for r in rows]

    # 用户调用量排行（前 20）
    top_rows = await conn.execute_query_dict(
        "SELECT requested_by_id, COUNT(*) AS cnt, MAX(created_at) AS last_at "
        "FROM artwork_translations "
        "WHERE requested_by_id IS NOT NULL "
        "GROUP BY requested_by_id "
        "ORDER BY cnt DESC LIMIT 20",
    )
    user_ids = [r["requested_by_id"] for r in top_rows]
    users_map = {u.id: u.username async for u in User.filter(id__in=user_ids)}
    top_users = [
        {
            "user_id": r["requested_by_id"],
            "username": users_map.get(r["requested_by_id"], "?"),
            "count": int(r["cnt"]),
            "last_at": r["last_at"].isoformat() if r["last_at"] else None,
        }
        for r in top_rows
    ]

    return ResponseBase(data={
        "total": total,
        "success_rate": success_rate,
        "today": today_count,
        "daily_series": daily_series,
        "top_users": top_users,
    })


@router.post("/translation/ping", response_model=ResponseBase[dict])
async def admin_ping_mit(
    current_user: User = Depends(_ADMIN),
):
    """测试 MIT Server 连接。"""
    from app.services.translate_service import TranslateService
    from app.infrastructure.cache import cache_get
    cfg = await cache_get("site_config") or {}
    server_url = cfg.get("mit_server_url", "http://localhost:5003")
    online = await TranslateService.ping(server_url)
    return ResponseBase(data={"online": online, "server_url": server_url})


# ──────────────────────────────────────────────────────────────────────
# 导入账号管理
# ──────────────────────────────────────────────────────────────────────

class ImportedUserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    pixiv_user_id: Optional[int] = None
    source_platform: str = Field(default="pixiv", max_length=20)
    bio: Optional[str] = None
    avatar_url: Optional[str] = None
    website_url: Optional[str] = None


@router.post("/imported-users", response_model=ResponseBase[dict])
async def create_imported_user(
    body: ImportedUserCreate,
    current_user: User = Depends(_ADMIN),
):
    """创建导入账号（如 Pixiv 作者）。若 pixiv_user_id 已存在则返回 409 + 现有账号 ID。"""
    import secrets
    from app.core.security import get_password_hash

    if body.pixiv_user_id is not None:
        existing = await User.get_or_none(pixiv_user_id=body.pixiv_user_id)
        if existing:
            raise HTTPException(
                status_code=409,
                detail={"message": "Pixiv user already imported", "user_id": existing.id}
            )

    fake_email = f"imported_{body.pixiv_user_id or secrets.token_hex(8)}@internal.moetopia"
    random_pw = get_password_hash(secrets.token_hex(32))  # 随机不可逆密码，无法登录

    user = await User.create(
        username=body.username,
        email=fake_email,
        password_hash=random_pw,
        is_imported=True,
        pixiv_user_id=body.pixiv_user_id,
        source_platform=body.source_platform,
        bio=body.bio,
        avatar_url=body.avatar_url,
        website_url=body.website_url,
        is_creator=True,
        commission_enabled=False,
        token_version=0,
    )
    return ResponseBase(data={"user_id": user.id, "username": user.username})


@router.get("/imported-users/by-pixiv/{pixiv_user_id}", response_model=ResponseBase[dict])
async def get_imported_user_by_pixiv(
    pixiv_user_id: int,
    current_user: User = Depends(_ADMIN),
):
    """通过 Pixiv 用户 ID 查找已导入账号"""
    user = await User.get_or_none(pixiv_user_id=pixiv_user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Imported user not found")
    return ResponseBase(data={"user_id": user.id, "username": user.username})


@router.post("/imported-users/{user_id}/impersonate", response_model=ResponseBase[dict])
async def impersonate_imported_user(
    user_id: int,
    current_user: User = Depends(_ADMIN),
):
    """为指定导入账号生成 2 小时临时访问 JWT（仅限 is_imported=True 账号）。
    脚本模式下用于以作者身份执行上传、更新头像、管理系列等操作。
    """
    from app.core.security import create_access_token
    from datetime import timedelta
    user = await User.get_or_none(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if not user.is_imported:
        raise HTTPException(status_code=403, detail="仅支持对导入账号（is_imported=True）生成临时授权")
    token = create_access_token(
        data={"sub": str(user.id), "username": user.username, "ver": user.token_version},
        expires_delta=timedelta(hours=2),
    )
    return ResponseBase(data={"access_token": token, "user_id": user.id, "username": user.username})


@router.post("/imported-users/{user_id}/avatar", response_model=ResponseBase[dict])
async def upload_imported_user_avatar(
    user_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(_ADMIN),
):
    """管理员直接为导入账号上传头像文件（无需 impersonate）。"""
    user = await User.get_or_none(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if not user.is_imported:
        raise HTTPException(status_code=403, detail="仅限导入账号使用此接口")
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="头像文件不能超过 5MB")
    ext = os.path.splitext(file.filename or "avatar.jpg")[1].lower() or ".jpg"
    filename = f"{user.id}_{uuid.uuid4().hex[:8]}{ext}"
    url = await storage.save(data, f"avatars/{filename}")
    user.avatar_url = url
    await user.save(update_fields=["avatar_url"])
    return ResponseBase(data={"avatar_url": url})


# ──────────────────────────────────────────────────────────────────────
# 账号认领申请管理
# ──────────────────────────────────────────────────────────────────────

@router.get("/claim-requests", response_model=ResponseBase[list])
async def list_claim_requests(
    status: Optional[str] = "pending",
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(_STAFF),
):
    """列出账号认领申请（默认只看 pending）"""
    from app.models.account_claim import AccountClaimRequest
    qs = AccountClaimRequest.all()
    if status:
        qs = qs.filter(status=status)
    claims = await qs.order_by("-created_at").offset(offset).limit(limit).prefetch_related(
        "imported_user", "claimant"
    )
    result = []
    for c in claims:
        try:
            imported_username = c.imported_user.username
        except Exception:
            imported_username = None
        try:
            claimant_username = c.claimant.username
            claimant_email = c.claimant.email
        except Exception:
            claimant_username = None
            claimant_email = None
        result.append({
            "id": c.id,
            "imported_user_id": c.imported_user_id,
            "imported_username": imported_username,
            "claimant_id": c.claimant_id,
            "claimant_username": claimant_username,
            "claimant_email": claimant_email,
            "status": c.status,
            "admin_note": c.admin_note,
            "created_at": c.created_at.isoformat(),
            "resolved_at": c.resolved_at.isoformat() if c.resolved_at else None,
        })
    return ResponseBase(data=result)


class ClaimResolveRequest(BaseModel):
    admin_note: Optional[str] = None


@router.post("/claim-requests/{claim_id}/approve", response_model=ResponseBase[dict])
async def approve_claim_request(
    claim_id: int,
    body: ClaimResolveRequest,
    current_user: User = Depends(_ADMIN),
):
    """
    批准认领申请：
    1. 将导入账号的全部作品转移到认领者账号
    2. 将导入账号的全部系列转移到认领者账号
    3. 封禁/停用导入账号
    4. 向认领者发送通知
    """
    from app.models.account_claim import AccountClaimRequest
    from app.models.artwork import Artwork, ArtworkSeries
    from app.models.social import Notification

    claim = await AccountClaimRequest.get_or_none(id=claim_id)
    if not claim:
        raise HTTPException(status_code=404, detail="Claim request not found")
    if claim.status != "pending":
        raise HTTPException(status_code=400, detail=f"申请状态已为 {claim.status}，无法重复处理")

    imported_user = await User.get_or_none(id=claim.imported_user_id)
    if not imported_user:
        raise HTTPException(status_code=404, detail="导入账号不存在")

    await Artwork.filter(author_id=claim.imported_user_id).update(author_id=claim.claimant_id)
    await ArtworkSeries.filter(author_id=claim.imported_user_id).update(author_id=claim.claimant_id)  # Tortoise FK col is author_id

    imported_user.is_banned = True
    imported_user.banned_reason = f"账号已被用户 {claim.claimant_id} 认领"
    imported_user.banned_at = datetime.now(timezone.utc)
    await imported_user.save(update_fields=["is_banned", "banned_reason", "banned_at"])

    claim.status = "approved"
    claim.admin_note = body.admin_note
    claim.resolved_at = datetime.now(timezone.utc)
    await claim.save(update_fields=["status", "admin_note", "resolved_at"])

    await Notification.create(
        user_id=claim.claimant_id,
        actor_id=current_user.id,
        type="system",
        content=f"你对账号「{imported_user.username}」的认领申请已通过，相关作品和系列已转移到你的账号。",
        related_entity_id=str(claim.imported_user_id),
    )

    return ResponseBase(data={"message": "认领申请已批准，作品和系列已迁移"})


@router.post("/claim-requests/{claim_id}/reject", response_model=ResponseBase[dict])
async def reject_claim_request(
    claim_id: int,
    body: ClaimResolveRequest,
    current_user: User = Depends(_ADMIN),
):
    """拒绝认领申请，可附管理员说明"""
    from app.models.account_claim import AccountClaimRequest
    from app.models.social import Notification

    claim = await AccountClaimRequest.get_or_none(id=claim_id)
    if not claim:
        raise HTTPException(status_code=404, detail="Claim request not found")
    if claim.status != "pending":
        raise HTTPException(status_code=400, detail=f"申请状态已为 {claim.status}，无法重复处理")

    imported_user = await User.get_or_none(id=claim.imported_user_id)
    imported_name = imported_user.username if imported_user else str(claim.imported_user_id)

    claim.status = "rejected"
    claim.admin_note = body.admin_note
    claim.resolved_at = datetime.now(timezone.utc)
    await claim.save(update_fields=["status", "admin_note", "resolved_at"])

    note_text = f"（原因：{body.admin_note}）" if body.admin_note else ""
    await Notification.create(
        user_id=claim.claimant_id,
        actor_id=current_user.id,
        type="system",
        content=f"你对账号「{imported_name}」的认领申请未通过。{note_text}",
        related_entity_id=str(claim.imported_user_id),
    )

    return ResponseBase(data={"message": "认领申请已拒绝"})

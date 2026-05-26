"""
内容审核模块 — 仅限 admin / moderator 角色访问
处理 Qdrant 向量撞车检测、AI R18/违法内容检测生成的人工审核队列。
"""
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from tortoise.functions import Count

from app.api.dependencies import require_role
from app.models.user import User
from app.models.artwork import Artwork
from app.models.moderation import ModerationQueue
from app.models.social import Notification
from app.schemas.common import ResponseBase

router = APIRouter()
_STAFF = require_role(["admin", "moderator"])


class ReviewAction(BaseModel):
    note: Optional[str] = None


# ── 原因标签映射 ──────────────────────────────────────────────────────────
REASON_LABELS = {
    "duplicate_suspected": "疑似撞车",
    "r18_detected":        "AI 检测 R18",
    "explicit_suspected":  "疑似露骨内容",
    "illegal_suspected":   "疑似违法内容",
    "manual":              "手动入队",
}


def _serialize_entry(entry, artwork) -> dict:
    first_image = None
    try:
        imgs = sorted(artwork.images, key=lambda x: x.sort_order)
        if imgs:
            first_image = imgs[0].file_url
    except Exception:
        pass

    return {
        "id": entry.id,
        "artwork_id": entry.artwork_id,
        "artwork_title": artwork.title if artwork else None,
        "artwork_rating": artwork.rating if artwork else None,
        "artwork_author_id": artwork.author_id if artwork else None,
        "first_image": first_image,
        "reason": entry.reason,
        "reason_label": REASON_LABELS.get(entry.reason, entry.reason),
        "confidence": entry.confidence,
        "duplicate_of_artwork_id": entry.duplicate_of_artwork_id,
        "status": entry.status,
        "reviewer_note": entry.reviewer_note,
        "reviewed_at": entry.reviewed_at.isoformat() if entry.reviewed_at else None,
        "created_at": entry.created_at.isoformat(),
    }


@router.get("/stats", response_model=ResponseBase[dict])
async def get_stats(current_user: User = Depends(_STAFF)):
    """审核队列数量统计（按原因分组）"""
    total_pending = await ModerationQueue.filter(status="pending").count()
    by_reason = await (
        ModerationQueue.filter(status="pending")
        .annotate(cnt=Count("id"))
        .group_by("reason")
        .values("reason", "cnt")
    )
    return ResponseBase(data={
        "total_pending": total_pending,
        "by_reason": {row["reason"]: row["cnt"] for row in by_reason},
    })


@router.get("/", response_model=ResponseBase[dict])
async def list_queue(
    status: str = Query(default="pending", description="pending|approved|rejected"),
    reason: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(_STAFF),
):
    """列出审核队列（支持按状态/原因过滤）"""
    qs = ModerationQueue.filter(status=status)
    if reason:
        qs = qs.filter(reason=reason)

    total = await qs.count()
    entries = await qs.prefetch_related("artwork__images").order_by("-created_at").offset(offset).limit(limit)

    items = []
    for e in entries:
        try:
            artwork = e.artwork
        except Exception:
            artwork = None
        items.append(_serialize_entry(e, artwork))

    return ResponseBase(data={"total": total, "items": items})


@router.post("/{item_id}/approve", response_model=ResponseBase[dict])
async def approve_item(
    item_id: int,
    body: ReviewAction,
    current_user: User = Depends(_STAFF),
):
    """审核通过：设置作品 moderation_status=approved，关闭队列条目"""
    entry = await ModerationQueue.get_or_none(id=item_id)
    if not entry:
        raise HTTPException(status_code=404, detail="审核条目不存在")
    if entry.status != "pending":
        raise HTTPException(status_code=400, detail="该条目已处理")

    entry.status = "approved"
    entry.reviewer_id = current_user.id
    entry.reviewer_note = body.note
    entry.reviewed_at = datetime.now(timezone.utc)
    await entry.save()

    artwork = await Artwork.get_or_none(id=entry.artwork_id)
    if artwork:
        # 检查该作品是否还有其他 pending 条目
        other_pending = await ModerationQueue.filter(
            artwork_id=artwork.id, status="pending"
        ).exists()
        if not other_pending:
            update_fields = ["moderation_status"]
            artwork.moderation_status = "approved"
            # 若作品因 auto-ban 被设为 private，审核通过后恢复为 public
            if artwork.visibility == "private":
                artwork.visibility = "public"
                update_fields.append("visibility")
            await artwork.save(update_fields=update_fields)
            from app.worker.enqueue import enqueue as _enqueue
            await _enqueue("task_sync_artwork_meili", artwork_id=artwork.id)
            # 通知作者审核通过
            reason_label = REASON_LABELS.get(entry.reason, entry.reason)
            note_text = f"，审核备注：{body.note}" if body.note else ""
            await Notification.create(
                user_id=artwork.author_id,
                type="system",
                content=f"你的作品「{artwork.title}」已通过{reason_label}审核并恢复公开{note_text}。",
                related_entity_id=artwork.id,
            )

    return ResponseBase(data={"message": "已通过审核"})


@router.post("/{item_id}/reject", response_model=ResponseBase[dict])
async def reject_item(
    item_id: int,
    body: ReviewAction,
    current_user: User = Depends(_STAFF),
):
    """审核拒绝：设置作品 moderation_status=rejected，强制改为私密，通知作者"""
    entry = await ModerationQueue.get_or_none(id=item_id)
    if not entry:
        raise HTTPException(status_code=404, detail="审核条目不存在")
    if entry.status != "pending":
        raise HTTPException(status_code=400, detail="该条目已处理")

    entry.status = "rejected"
    entry.reviewer_id = current_user.id
    entry.reviewer_note = body.note
    entry.reviewed_at = datetime.now(timezone.utc)
    await entry.save()

    artwork = await Artwork.get_or_none(id=entry.artwork_id)
    if artwork:
        artwork.moderation_status = "rejected"
        artwork.visibility = "private"
        await artwork.save(update_fields=["moderation_status", "visibility"])
        from app.worker.enqueue import enqueue as _enqueue
        await _enqueue("task_sync_artwork_meili", artwork_id=artwork.id)

        # 通知作者
        reason_label = REASON_LABELS.get(entry.reason, entry.reason)
        note_text = f"，审核备注：{body.note}" if body.note else ""
        await Notification.create(
            user_id=artwork.author_id,
            type="system",
            content=f"你的作品「{artwork.title}」因{reason_label}被审核拒绝并已设为私密{note_text}。",
            related_entity_id=artwork.id,
        )

    return ResponseBase(data={"message": "已拒绝并通知作者"})


@router.post("/{item_id}/delete-artwork", response_model=ResponseBase[dict])
async def delete_artwork_from_moderation(
    item_id: int,
    body: ReviewAction,
    current_user: User = Depends(_STAFF),
):
    """审核删除：彻底删除作品，通知作者，标记条目为 rejected"""
    entry = await ModerationQueue.get_or_none(id=item_id)
    if not entry:
        raise HTTPException(status_code=404, detail="审核条目不存在")
    if entry.status != "pending":
        raise HTTPException(status_code=400, detail="该条目已处理")

    entry.status = "rejected"
    entry.reviewer_id = current_user.id
    entry.reviewer_note = body.note
    entry.reviewed_at = datetime.now(timezone.utc)
    await entry.save()

    artwork = await Artwork.get_or_none(id=entry.artwork_id)
    if artwork:
        author_id = artwork.author_id
        artwork_title = artwork.title
        reason_label = REASON_LABELS.get(entry.reason, entry.reason)
        note_text = f"，审核备注：{body.note}" if body.note else ""
        await Notification.create(
            user_id=author_id,
            type="system",
            content=f"你的作品「{artwork_title}」因{reason_label}已被管理员删除{note_text}。",
            related_entity_id=artwork.id,
        )
        artwork_id = artwork.id
        await artwork.delete()
        from app.infrastructure.cache import invalidate_artwork, invalidate_related
        await invalidate_artwork(artwork_id)
        await invalidate_related(artwork_id)

    return ResponseBase(data={"message": "作品已删除并通知作者"})


@router.post("/manual", response_model=ResponseBase[dict])
async def manual_enqueue(
    artwork_id: int,
    note: Optional[str] = None,
    current_user: User = Depends(_STAFF),
):
    """手动将作品加入审核队列"""
    artwork = await Artwork.get_or_none(id=artwork_id)
    if not artwork:
        raise HTTPException(status_code=404, detail="作品不存在")

    await ModerationQueue.create(
        artwork_id=artwork_id,
        reason="manual",
        confidence=1.0,
        reviewer_note=note,
    )
    artwork.moderation_status = "under_review"
    await artwork.save(update_fields=["moderation_status"])
    return ResponseBase(data={"message": "已加入审核队列"})

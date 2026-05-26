from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List
from app.api.dependencies import get_current_user
from app.models.user import User
from app.models.social import Notification
from app.schemas.common import ResponseBase

router = APIRouter()


@router.get("/", response_model=ResponseBase[List[dict]])
async def get_notifications(
    current_user: User = Depends(get_current_user),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    unread_only: bool = False,
):
    """获取当前用户的通知列表（含 actor 用户名与头像，避免前端 N+1）"""
    qs = Notification.filter(user_id=current_user.id)
    if unread_only:
        qs = qs.filter(is_read=False)
    notifs = await (
        qs.prefetch_related("actor")
        .order_by("-created_at")
        .offset(offset)
        .limit(limit)
    )
    result = []
    for n in notifs:
        actor_username = None
        actor_avatar = None
        try:
            if n.actor:
                actor_username = n.actor.username
                actor_avatar = n.actor.avatar_url
        except Exception:
            pass
        result.append({
            "id": n.id,
            "type": n.type,
            "content": n.content,
            "is_read": n.is_read,
            "related_entity_id": n.related_entity_id,
            "actor_id": n.actor_id,
            "actor_username": actor_username,
            "actor_avatar": actor_avatar,
            "created_at": n.created_at.isoformat(),
        })
    return ResponseBase(data=result)


@router.get("/unread-count", response_model=ResponseBase[dict])
async def get_unread_count(current_user: User = Depends(get_current_user)):
    """获取未读通知数量（Redis 缓存 30s，避免高频轮询打爆 DB）"""
    from app.infrastructure.cache import cache_get, cache_set, TTL_NOTIF_COUNT
    cache_key = f"notif_unread:{current_user.id}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return ResponseBase(data={"unread_count": cached})
    count = await Notification.filter(user_id=current_user.id, is_read=False).count()
    await cache_set(cache_key, count, TTL_NOTIF_COUNT)
    return ResponseBase(data={"unread_count": count})


@router.post("/{notification_id}/read", response_model=ResponseBase[dict])
async def mark_single_read(
    notification_id: int,
    current_user: User = Depends(get_current_user),
):
    """将单条通知标为已读"""
    notif = await Notification.get_or_none(id=notification_id, user_id=current_user.id)
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    notif.is_read = True
    await notif.save(update_fields=["is_read"])
    from app.infrastructure.cache import cache_delete
    await cache_delete(f"notif_unread:{current_user.id}")
    return ResponseBase(data={"message": "Marked as read"})


@router.post("/read-all", response_model=ResponseBase[dict])
async def mark_all_read(current_user: User = Depends(get_current_user)):
    """将所有通知标为已读"""
    count = await Notification.filter(user_id=current_user.id, is_read=False).update(is_read=True)
    from app.infrastructure.cache import cache_delete
    await cache_delete(f"notif_unread:{current_user.id}")
    return ResponseBase(data={"marked": count})


@router.delete("/{notification_id}", response_model=ResponseBase[dict])
async def delete_notification(
    notification_id: int,
    current_user: User = Depends(get_current_user),
):
    """删除单条通知"""
    notif = await Notification.get_or_none(id=notification_id, user_id=current_user.id)
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    await notif.delete()
    from app.infrastructure.cache import cache_delete
    await cache_delete(f"notif_unread:{current_user.id}")
    return ResponseBase(data={"deleted": True})


@router.delete("/", response_model=ResponseBase[dict])
async def delete_all_notifications(current_user: User = Depends(get_current_user)):
    """清空当前用户的所有通知"""
    count = await Notification.filter(user_id=current_user.id).delete()
    from app.infrastructure.cache import cache_delete
    await cache_delete(f"notif_unread:{current_user.id}")
    return ResponseBase(data={"deleted": count})

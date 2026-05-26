import json
from typing import Optional
from app.models.social import Notification


def _parse_notif_pref(pref) -> tuple[bool, bool]:
    """解析通知偏好（兼容旧版 bool 格式和新版 {site, email} 格式）。
    返回 (site_enabled, email_enabled)。
    """
    if isinstance(pref, bool):
        return pref, False
    if isinstance(pref, dict):
        return pref.get("site", True) is not False, pref.get("email", False) is True
    return True, False


async def push_notification(
    *,
    user_id: int,
    actor_id: Optional[int] = None,
    type: str,
    content: str,
    related_entity_id: Optional[str] = None,
) -> Optional[Notification]:
    """创建通知记录，并通过 WebSocket 实时推送给在线用户。
    若用户已在 notification_prefs 中关闭该类型，则跳过。
    邮件通知会写入 Redis 队列，由定时任务批量发送。
    """
    from app.models.user import User as UserModel
    target = await UserModel.get_or_none(id=user_id)
    email_enabled = False
    if target:
        prefs: dict = target.notification_prefs or {}
        raw_pref = prefs.get(type, True)
        site_enabled, email_enabled = _parse_notif_pref(raw_pref)
        if not site_enabled:
            return None

    notif = await Notification.create(
        user_id=user_id,
        actor_id=actor_id,
        type=type,
        content=content,
        related_entity_id=related_entity_id,
    )

    # 使未读计数缓存失效
    try:
        from app.infrastructure.cache import cache_delete
        await cache_delete(f"notif_unread:{user_id}")
    except Exception:
        pass

    actor_username = None
    actor_avatar = None
    if actor_id:
        try:
            actor = await UserModel.get(id=actor_id)
            actor_username = actor.username
            actor_avatar = actor.avatar_url
        except Exception:
            pass

    from app.api.v1.ws import manager
    payload = json.dumps({
        "type": "notification",
        "data": {
            "id": notif.id,
            "notif_type": type,
            "content": content,
            "actor_id": actor_id,
            "actor_username": actor_username,
            "actor_avatar": actor_avatar,
            "related_entity_id": related_entity_id,
            "is_read": False,
            "created_at": notif.created_at.isoformat(),
        }
    })
    await manager.send_personal_message(payload, user_id)

    # 邮件通知队列（Redis List + Set，由定时任务批量发送）
    if email_enabled and target and getattr(target, "email", None):
        try:
            from app.infrastructure.redis_client import get_redis
            r = get_redis()
            item = json.dumps({
                "type": type,
                "content": content,
                "actor_username": actor_username or "",
            })
            key = f"email_notif:{user_id}"
            await r.rpush(key, item)
            await r.expire(key, 86400)  # 最长保留 24h
            await r.sadd("email_notif_pending", str(user_id))
        except Exception:
            pass

    return notif

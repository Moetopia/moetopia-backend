from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Request, Query
from typing import List, Optional
from pydantic import BaseModel, Field
import asyncio
from app.models.message import DirectMessage
from app.models.user import User
from app.api.dependencies import get_current_user
from app.middleware.rate_limit import rate_limit
from app.schemas.common import ResponseBase
import os
import uuid
import json
from app.services.storage_service import storage

ALLOWED_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

router = APIRouter()


class MessageCreate(BaseModel):
    content: Optional[str] = Field(None, max_length=2000)
    image_url: Optional[str] = None
    commission_id: Optional[int] = None


class MessageResponse(BaseModel):
    id: int
    sender_id: int
    sender_username: str
    sender_avatar: Optional[str] = None
    recipient_id: int
    content: Optional[str] = None
    image_url: Optional[str] = None
    is_read: bool
    commission_id: Optional[int] = None
    created_at: str

    class Config:
        from_attributes = True


class ConversationSummary(BaseModel):
    user_id: int
    username: str
    avatar_url: Optional[str] = None
    last_message: Optional[str] = None
    last_message_time: str
    unread_count: int


def serialize_message(msg: DirectMessage) -> dict:
    try:
        sender_username = msg.sender.username
        sender_avatar = msg.sender.avatar_url
    except Exception:
        sender_username = ""
        sender_avatar = None
    return {
        "id": msg.id,
        "sender_id": msg.sender_id,
        "sender_username": sender_username,
        "sender_avatar": sender_avatar,
        "recipient_id": msg.recipient_id,
        "content": msg.content,
        "image_url": msg.image_url,
        "is_read": msg.is_read,
        "commission_id": msg.commission_id,
        "created_at": msg.created_at.isoformat(),
    }


@router.get("/conversations", response_model=ResponseBase[List[ConversationSummary]])
async def list_conversations(current_user: User = Depends(get_current_user)):
    """列出当前用户的所有会话（与每个对话方的最新消息）"""
    from tortoise.expressions import Q
    from tortoise.functions import Max

    # 只取最近 500 条推导会话列表，避免全表扫描
    msgs = await DirectMessage.filter(
        Q(sender_id=current_user.id) | Q(recipient_id=current_user.id)
    ).prefetch_related("sender", "recipient").order_by("-created_at").limit(500)

    seen_users: dict[int, ConversationSummary] = {}
    for msg in msgs:
        other_id = msg.recipient_id if msg.sender_id == current_user.id else msg.sender_id
        if other_id in seen_users:
            continue
        try:
            other = msg.recipient if msg.sender_id == current_user.id else msg.sender
            other_username = other.username
            other_avatar = other.avatar_url
        except Exception:
            other_username = f"user_{other_id}"
            other_avatar = None

        unread = await DirectMessage.filter(
            sender_id=other_id, recipient_id=current_user.id, is_read=False
        ).count()

        last_msg_preview = msg.content or ("图片" if msg.image_url else "")
        seen_users[other_id] = ConversationSummary(
            user_id=other_id,
            username=other_username,
            avatar_url=other_avatar,
            last_message=last_msg_preview,
            last_message_time=msg.created_at.isoformat(),
            unread_count=unread,
        )

    return ResponseBase(data=list(seen_users.values()))


@router.get("/conversations/{user_id}", response_model=ResponseBase[List[dict]])
async def get_conversation(
    user_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    before_id: Optional[int] = None,
    current_user: User = Depends(get_current_user),
):
    """获取与指定用户的消息历史"""
    if not await User.exists(id=user_id):
        raise HTTPException(status_code=404, detail="User not found")

    from tortoise.expressions import Q
    qs = DirectMessage.filter(
        (Q(sender_id=current_user.id) & Q(recipient_id=user_id))
        | (Q(sender_id=user_id) & Q(recipient_id=current_user.id))
    ).prefetch_related("sender", "recipient")
    if before_id:
        qs = qs.filter(id__lt=before_id)
    msgs = await qs.order_by("-created_at").limit(limit)
    msgs = list(reversed(msgs))

    # 自动标记已读
    await DirectMessage.filter(
        sender_id=user_id, recipient_id=current_user.id, is_read=False
    ).update(is_read=True)

    return ResponseBase(data=[serialize_message(m) for m in msgs])


@router.post("/upload", response_model=ResponseBase[dict])
async def upload_message_image(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """上传聊天图片附件，返回 URL"""
    ext = os.path.splitext(file.filename or "")[1].lower() or ".jpg"
    if ext not in ALLOWED_IMG_EXTS:
        raise HTTPException(status_code=400, detail="不支持的图片格式")
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片不能超过 5MB")
    filename = f"{current_user.id}_{uuid.uuid4().hex[:8]}{ext}"
    url = await storage.save(data, f"messages/{filename}")
    return ResponseBase(data={"image_url": url})


@router.post("/conversations/{user_id}", response_model=ResponseBase[dict])
async def send_message(
    user_id: int,
    body: MessageCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """向指定用户发送消息"""
    await rate_limit(request, "message")
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot message yourself")
    if not await User.exists(id=user_id):
        raise HTTPException(status_code=404, detail="User not found")
    if not body.content and not body.image_url:
        raise HTTPException(status_code=400, detail="消息内容不能为空")

    # 拉黑检查：双向均不可发送
    from app.models.social import UserBlock
    if await UserBlock.exists(blocker_id=user_id, blocked_id=current_user.id):
        raise HTTPException(status_code=403, detail="无法发送消息")
    if await UserBlock.exists(blocker_id=current_user.id, blocked_id=user_id):
        raise HTTPException(status_code=403, detail="无法发送消息")

    msg = await DirectMessage.create(
        sender_id=current_user.id,
        recipient_id=user_id,
        content=body.content.strip() if body.content else None,
        image_url=body.image_url,
        commission_id=body.commission_id,
    )
    await msg.fetch_related("sender", "recipient")
    data = serialize_message(msg)

    # 通过 WebSocket 实时推送给接收方
    from app.api.v1.ws import manager
    await manager.send_personal_message(
        json.dumps({"type": "new_message", "data": data}),
        user_id,
    )

    # 站内通知（保证离线用户铃铛计数正确）
    from app.services.notification_service import push_notification
    preview = (body.content or "").strip()[:50] or "发来了一张图片"
    await push_notification(
        user_id=user_id,
        actor_id=current_user.id,
        type="new_message",
        content=f"{current_user.username}：{preview}",
        related_entity_id=str(current_user.id),
    )

    return ResponseBase(data=data)


@router.delete("/{message_id}", response_model=ResponseBase[dict])
async def delete_message(
    message_id: int,
    current_user: User = Depends(get_current_user),
):
    """撤回消息（仅发送者本人可撤回，5 分钟内有效）"""
    from datetime import datetime, timedelta, timezone
    msg = await DirectMessage.get_or_none(id=message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg.sender_id != current_user.id:
        raise HTTPException(status_code=403, detail="只能撤回自己发送的消息")
    recall_window = datetime.now(timezone.utc) - timedelta(minutes=5)
    if msg.created_at.replace(tzinfo=timezone.utc) < recall_window:
        raise HTTPException(status_code=403, detail="消息发送超过 5 分钟，无法撤回")
    recipient_id = msg.recipient_id
    await msg.delete()

    # 通知接收方更新消息列表
    from app.api.v1.ws import manager
    await manager.send_personal_message(
        json.dumps({"type": "message_recalled", "data": {"message_id": message_id}}),
        recipient_id,
    )
    return ResponseBase(data={"recalled": True})


@router.get("/unread-count", response_model=ResponseBase[dict])
async def get_unread_message_count(current_user: User = Depends(get_current_user)):
    """获取未读消息总数"""
    count = await DirectMessage.filter(recipient_id=current_user.id, is_read=False).count()
    return ResponseBase(data={"unread_count": count})

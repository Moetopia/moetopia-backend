from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Query, Body
import asyncio
from datetime import datetime, timezone
from typing import List, Optional
from pydantic import BaseModel, Field
from app.schemas.user_schema import UserResponse, UserPublicResponse, UserPreferenceUpdate, UserProfileUpdate, CreatorProfileUpdate
import os
import uuid
from app.schemas.artwork_schema import ArtworkResponse, serialize_artwork
from app.services.storage_service import storage
from app.models.user import User
from app.models.artwork import Artwork, ArtworkSeriesItem
from app.models.social import Follow, UserBlock, FollowTag, FollowGroup, FollowGroupMember
from app.api.dependencies import get_current_user, get_optional_user
from app.services.auth_service import UserService
from app.services.social_service import SocialService
from app.core.security import verify_password, get_password_hash
from app.schemas.common import ResponseBase


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(..., max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)
    email_code: str


class LoginIdChangeRequest(BaseModel):
    new_login_id: str = Field(..., min_length=3, max_length=50)
    current_password: str = Field(..., max_length=128)
    email_code: str

router = APIRouter()


ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


@router.get("/me", response_model=ResponseBase[UserResponse])
async def get_current_user_profile(current_user: User = Depends(get_current_user)):
    """获取当前登录用户完整信息"""
    followers_count = await Follow.filter(followed_id=current_user.id).count()
    following_count = await Follow.filter(follower_id=current_user.id).count()
    resp = UserResponse.model_validate(current_user)
    resp.followers_count = followers_count
    resp.following_count = following_count
    from app.models.user_membership import UserMembership
    resp.has_membership = await UserMembership.filter(
        user_id=current_user.id, status="active", expires_at__gte=datetime.now(timezone.utc)
    ).exists()
    return ResponseBase(data=resp)


@router.put("/me/profile", response_model=ResponseBase[UserResponse])
async def update_profile(
    profile: UserProfileUpdate,
    current_user: User = Depends(get_current_user),
):
    """更新个人资料（头像、背景、简介、链接）"""
    updated_user = await UserService.update_profile(current_user, profile)
    return ResponseBase(data=UserResponse.model_validate(updated_user))


@router.put("/me/preferences", response_model=ResponseBase[UserResponse])
async def update_user_preferences(
    prefs: UserPreferenceUpdate,
    current_user: User = Depends(get_current_user),
):
    """更新内容偏好（R18 开关、屏蔽标签、屏蔽 AI 等）"""
    updated_user = await UserService.update_preferences(current_user, prefs)
    return ResponseBase(data=UserResponse.model_validate(updated_user))


@router.put("/me/creator", response_model=ResponseBase[UserResponse])
async def update_creator_profile(
    data: CreatorProfileUpdate,
    current_user: User = Depends(get_current_user),
):
    """配置创作者约稿设置（仅已认证画师可调用）"""
    if not current_user.is_creator:
        raise HTTPException(status_code=403, detail="仅认证画师可更新此设置")
    updated_user = await UserService.update_creator_profile(current_user, data)
    return ResponseBase(data=UserResponse.model_validate(updated_user))


@router.get("/me/feedback", response_model=ResponseBase[List[dict]])
async def get_my_feedback(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
):
    """获取所有评论了我作品的反馈（含评论者信息和作品封面）"""
    from app.models.interaction import Comment
    from app.models.artwork import ArtworkImage
    comments = await (
        Comment.filter(artwork__author_id=current_user.id, parent_id=None, is_deleted=False)
        .order_by("-created_at")
        .offset(offset)
        .limit(limit)
        .prefetch_related("user", "artwork__images")
    )
    result = []
    for c in comments:
        cover = None
        try:
            imgs = sorted(c.artwork.images, key=lambda i: i.sort_order)
            if imgs:
                cover = imgs[0].file_url
        except Exception:
            pass
        result.append({
            "id": c.id,
            "content": c.content,
            "created_at": c.created_at.isoformat(),
            "user_id": c.user_id,
            "username": c.user.username if c.user else None,
            "avatar_url": c.user.avatar_url if c.user else None,
            "artwork_id": c.artwork_id,
            "artwork_title": c.artwork.title if c.artwork else None,
            "artwork_cover": cover,
        })
    return ResponseBase(data=result)


@router.get("/me/blocked", response_model=ResponseBase[List[UserPublicResponse]])
async def get_blocked_users(current_user: User = Depends(get_current_user)):
    """获取我的拉黑列表"""
    users = await SocialService.get_blocked_users(current_user.id)
    return ResponseBase(data=[UserPublicResponse.model_validate(u) for u in users])


@router.get("/suggestions", response_model=ResponseBase[list])
async def get_user_suggestions(
    limit: int = Query(default=5, ge=1, le=20),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """推荐用户：活跃画师中未关注的（按作品数排序）"""
    from tortoise.functions import Count

    if current_user:
        following_ids = list(await Follow.filter(
            follower_id=current_user.id
        ).values_list("followed_id", flat=True))
        excluded = set(following_ids) | {current_user.id}
    else:
        excluded = set()

    qs = User.filter(is_creator=True, is_banned=False)
    if excluded:
        qs = qs.exclude(id__in=excluded)
    users = await qs.order_by("-created_at").limit(limit * 4)

    if not users:
        return ResponseBase(data=[])

    user_ids = [u.id for u in users]
    artwork_counts = await Artwork.filter(
        author_id__in=user_ids, visibility="public"
    ).group_by("author_id").annotate(cnt=Count("id")).values("author_id", "cnt")
    count_map = {r["author_id"]: r["cnt"] for r in artwork_counts}

    users_sorted = sorted(users, key=lambda u: count_map.get(u.id, 0), reverse=True)[:limit]
    return ResponseBase(data=[{
        "id": u.id,
        "username": u.username,
        "avatar_url": u.avatar_url,
        "bio": u.bio,
        "artwork_count": count_map.get(u.id, 0),
    } for u in users_sorted])


@router.get("/{user_id}", response_model=ResponseBase[UserPublicResponse])
async def get_user_profile(
    user_id: int,
    current_user: Optional[User] = Depends(get_optional_user),
):
    """获取画师公开主页信息"""
    user = await User.get_or_none(id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    followers_count = await Follow.filter(followed_id=user.id).count()
    following_count = await Follow.filter(follower_id=user.id).count()
    resp = UserPublicResponse.model_validate(user)
    resp.followers_count = followers_count
    resp.following_count = following_count
    # 被目标拉黑时：返回基本信息 + is_blocked_by=True，而非 403（避免用户误以为网站故障）
    if current_user and await UserBlock.exists(blocker_id=user_id, blocked_id=current_user.id):
        resp.is_blocked_by = True
    from app.models.user_membership import UserMembership
    resp.has_membership = await UserMembership.filter(
        user_id=user.id, status="active", expires_at__gte=datetime.now(timezone.utc)
    ).exists()
    return ResponseBase(data=resp)


@router.get("/{user_id}/artworks", response_model=ResponseBase[List[ArtworkResponse]])
async def get_user_artworks(
    user_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """获取某位画师的公开投稿流"""
    if not await User.exists(id=user_id):
        raise HTTPException(status_code=404, detail="User not found")

    # 被目标拉黑时返回空列表（主页会通过 is_blocked_by 显示屏蔽提示）
    if current_user and await UserBlock.exists(blocker_id=user_id, blocked_id=current_user.id):
        return ResponseBase(data=[])

    # 自己可见全部，关注者可见 followers 作品，其他人只见 public
    if current_user and current_user.id == user_id:
        qs = Artwork.filter(author_id=user_id).prefetch_related("images", "tags", "author")
    elif current_user and await Follow.filter(follower_id=current_user.id, followed_id=user_id).exists():
        qs = Artwork.filter(author_id=user_id, visibility__in=["public", "followers"]).prefetch_related("images", "tags", "author")
    else:
        qs = Artwork.filter(author_id=user_id, visibility="public").prefetch_related("images", "tags", "author")
    if current_user is None or not current_user.r18_enabled:
        qs = qs.filter(rating="safe")
    artworks = await qs.order_by("-created_at").offset(offset).limit(limit)
    return ResponseBase(data=[serialize_artwork(a) for a in artworks])


@router.get("/{user_id}/follow-status", response_model=ResponseBase[dict])
async def get_follow_status(
    user_id: int,
    current_user: User = Depends(get_current_user),
):
    """查询当前用户是否已关注目标用户"""
    follow = await Follow.get_or_none(follower_id=current_user.id, followed_id=user_id)
    return ResponseBase(data={
        "is_following": follow is not None,
        "is_private": follow.is_private if follow else False,
    })


class FollowRequest(BaseModel):
    is_private: bool = False


@router.post("/{user_id}/follow", response_model=ResponseBase[dict])
async def toggle_follow(
    user_id: int,
    body: FollowRequest = Body(default_factory=FollowRequest),
    current_user: User = Depends(get_current_user),
):
    """关注或取消关注（支持非公开关注）"""
    result = await SocialService.toggle_follow(current_user.id, user_id, body.is_private)
    return ResponseBase(data=result)


@router.get("/{user_id}/block-status", response_model=ResponseBase[dict])
async def get_block_status(
    user_id: int,
    current_user: User = Depends(get_current_user),
):
    """查询当前用户是否已拉黑目标用户"""
    is_blocked = await UserBlock.filter(blocker_id=current_user.id, blocked_id=user_id).exists()
    return ResponseBase(data={"is_blocked": is_blocked})


@router.post("/{user_id}/block", response_model=ResponseBase[dict])
async def toggle_block(
    user_id: int,
    current_user: User = Depends(get_current_user),
):
    """拉黑或取消拉黑用户"""
    is_blocked = await SocialService.toggle_block(current_user.id, user_id)
    return ResponseBase(data={"is_blocked": is_blocked})


class DeleteAccountRequest(BaseModel):
    password:               str
    captcha_verified_token: Optional[str] = None


@router.delete("/me", response_model=ResponseBase[dict])
async def delete_my_account(
    body: DeleteAccountRequest,
    current_user: User = Depends(get_current_user),
):
    """注销账号（需验证密码，删除所有个人数据）"""
    from app.core.security import verify_password
    from app.api.v1.captcha import check_captcha
    await check_captcha(body.captcha_verified_token)
    if not verify_password(body.password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="密码不正确")
    await current_user.delete()
    return ResponseBase(data={"message": "Account deleted"})


@router.get("/{user_id}/followers", response_model=ResponseBase[List[UserPublicResponse]])
async def get_followers(
    user_id: int,
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """获取粉丝列表"""
    target = await User.get_or_none(id=user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if not target.show_followers_public and (not current_user or current_user.id != user_id):
        raise HTTPException(status_code=403, detail="该用户已设置粉丝列表不公开")
    users = await SocialService.get_followers(user_id, limit=limit, offset=offset)
    return ResponseBase(data=[UserPublicResponse.model_validate(u) for u in users])


@router.get("/{user_id}/following", response_model=ResponseBase[List[UserPublicResponse]])
async def get_following(
    user_id: int,
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    group_id: Optional[int] = Query(default=None),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """获取关注列表（支持按分组过滤，分组只有本人可用）"""
    target = await User.get_or_none(id=user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if not target.show_following_public and (not current_user or current_user.id != user_id):
        raise HTTPException(status_code=403, detail="该用户已设置关注列表不公开")
    if group_id is not None:
        if not current_user or current_user.id != user_id:
            raise HTTPException(status_code=403, detail="只有本人可按分组查看关注列表")
        group = await FollowGroup.get_or_none(id=group_id, user_id=user_id)
        if not group:
            raise HTTPException(status_code=404, detail="分组不存在")
        members = await (
            FollowGroupMember.filter(group_id=group_id)
            .offset(offset).limit(limit)
            .prefetch_related("followed")
        )
        users = [m.followed for m in members]
        return ResponseBase(data=[UserPublicResponse.model_validate(u) for u in users])
    # 非本人查看时隐藏私密关注
    exclude_private = (not current_user or current_user.id != user_id)
    users = await SocialService.get_following(user_id, limit=limit, offset=offset, exclude_private=exclude_private)
    return ResponseBase(data=[UserPublicResponse.model_validate(u) for u in users])


@router.get("/me/following-feed", response_model=ResponseBase[List[dict]])
async def get_my_following_feed(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    only_accepting_commissions: bool = Query(default=False),
    show_private: Optional[bool] = Query(default=None, description="None=全部, true=仅非公开, false=仅公开"),
    current_user: User = Depends(get_current_user),
):
    """获取当前用户的丰富关注列表（含最新4件作品）。"""
    qs = Follow.filter(follower_id=current_user.id)
    if show_private is True:
        qs = qs.filter(is_private=True)
    elif show_private is False:
        qs = qs.filter(is_private=False)
    if only_accepting_commissions:
        qs = qs.filter(followed__commission_enabled=True)
    follows = await qs.prefetch_related("followed").order_by("-id").offset(offset).limit(limit)

    # 收集关注者 ID 以批量查询互关状态（好友判断）
    followed_ids = [f.followed_id for f in follows]
    back_follow_ids = set(
        await Follow.filter(follower_id__in=followed_ids, followed_id=current_user.id, is_private=False)
        .values_list("follower_id", flat=True)
    )

    # 批量获取每个被关注者最新4件作品
    r18_ok = getattr(current_user, "r18_enabled", False)
    artwork_qs = Artwork.filter(
        author_id__in=followed_ids, visibility="public"
    ).prefetch_related("images")
    if not r18_ok:
        artwork_qs = artwork_qs.filter(rating="safe")
    recent_artworks_all = await artwork_qs.order_by("-created_at").limit(len(followed_ids) * 4)
    artworks_by_user: dict[int, list] = {}
    for aw in recent_artworks_all:
        lst = artworks_by_user.setdefault(aw.author_id, [])
        if len(lst) < 4:
            lst.append({
                "id": aw.id,
                "title": aw.title,
                "thumb": (aw.images[0].file_url if aw.images else None),
            })

    result = []
    for f in follows:
        u = f.followed
        result.append({
            "user": {
                "id": u.id,
                "username": u.username,
                "avatar_url": u.avatar_url,
                "bio": getattr(u, "bio", None),
                "commission_enabled": getattr(u, "commission_enabled", False),
            },
            "recent_artworks": artworks_by_user.get(u.id, []),
            "is_private": f.is_private,
            "is_friend": u.id in back_follow_ids,
        })
    return ResponseBase(data=result)


@router.get("/me/friends", response_model=ResponseBase[List[dict]])
async def get_my_friends(
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
):
    """好萌友列表（双向互关）"""
    users = await SocialService.get_friends(current_user.id, limit=limit, offset=offset)
    r18_ok = getattr(current_user, "r18_enabled", False)
    user_ids = [u.id for u in users]
    artwork_qs = Artwork.filter(author_id__in=user_ids, visibility="public").prefetch_related("images")
    if not r18_ok:
        artwork_qs = artwork_qs.filter(rating="safe")
    recent_all = await artwork_qs.order_by("-created_at").limit(len(user_ids) * 4)
    artworks_by_user: dict[int, list] = {}
    for aw in recent_all:
        lst = artworks_by_user.setdefault(aw.author_id, [])
        if len(lst) < 4:
            lst.append({"id": aw.id, "title": aw.title, "thumb": (aw.images[0].file_url if aw.images else None)})
    result = []
    for u in users:
        result.append({
            "user": {
                "id": u.id,
                "username": u.username,
                "avatar_url": u.avatar_url,
                "bio": getattr(u, "bio", None),
                "commission_enabled": getattr(u, "commission_enabled", False),
            },
            "recent_artworks": artworks_by_user.get(u.id, []),
            "is_private": False,
            "is_friend": True,
        })
    return ResponseBase(data=result)


@router.get("/me/likes", response_model=ResponseBase[List[ArtworkResponse]])
async def get_my_likes(
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
):
    """获取我点赞过的作品列表"""
    from app.models.interaction import Like
    likes = await Like.filter(user_id=current_user.id).order_by("-created_at").offset(offset).limit(limit).prefetch_related("artwork__images", "artwork__author", "artwork__tags")
    artworks = [lk.artwork for lk in likes]
    return ResponseBase(data=[serialize_artwork(a) for a in artworks])


@router.get("/{user_id}/likes", response_model=ResponseBase[List[ArtworkResponse]])
async def get_user_likes(
    user_id: int,
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """获取用户公开点赞的作品列表"""
    target = await User.get_or_none(id=user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if not target.show_likes_public and (not current_user or current_user.id != user_id):
        raise HTTPException(status_code=403, detail="该用户已设置点赞列表不公开")
    from app.models.interaction import Like
    liked_ids = await (
        Like.filter(user_id=user_id)
        .order_by("-created_at")
        .offset(offset)
        .limit(limit)
        .values_list("artwork_id", flat=True)
    )
    if not liked_ids:
        return ResponseBase(data=[])
    artworks = await (
        Artwork.filter(id__in=list(liked_ids), visibility="public")
        .prefetch_related("images", "tags", "author")
    )
    if current_user is None or not current_user.r18_enabled:
        artworks = [a for a in artworks if a.rating == "safe"]
    id_order = {aid: i for i, aid in enumerate(liked_ids)}
    artworks = sorted(artworks, key=lambda a: id_order.get(a.id, 999))
    return ResponseBase(data=[serialize_artwork(a) for a in artworks])


# ------------------------------------------------------------------
# 修改密码
# ------------------------------------------------------------------

@router.post("/me/avatar", response_model=ResponseBase[dict])
async def upload_avatar(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """上传用户头像（图片文件）"""
    ext = os.path.splitext(file.filename or "")[1].lower() or ".jpg"
    if ext not in ALLOWED_IMAGE_EXTS:
        raise HTTPException(status_code=400, detail="不支持的图片格式")
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="头像文件不能超过 5MB")
    filename = f"{current_user.id}_{uuid.uuid4().hex[:8]}{ext}"
    url = await storage.save(data, f"avatars/{filename}")
    current_user.avatar_url = url
    await current_user.save(update_fields=["avatar_url"])
    return ResponseBase(data={"avatar_url": url})


@router.post("/me/background", response_model=ResponseBase[dict])
async def upload_background(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """上传用户背景图（图片文件）"""
    ext = os.path.splitext(file.filename or "")[1].lower() or ".jpg"
    if ext not in ALLOWED_IMAGE_EXTS:
        raise HTTPException(status_code=400, detail="不支持的图片格式")
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="背景图文件不能超过 10MB")
    filename = f"{current_user.id}_{uuid.uuid4().hex[:8]}{ext}"
    url = await storage.save(data, f"backgrounds/{filename}")
    current_user.background_url = url
    await current_user.save(update_fields=["background_url"])
    return ResponseBase(data={"background_url": url})


@router.put("/me/password", response_model=ResponseBase[dict])
async def change_password(
    body: PasswordChangeRequest,
    current_user: User = Depends(get_current_user),
):
    """修改账号密码"""
    if not verify_password(body.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="当前密码不正确")
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="新密码至少需要 8 位")
    from app.api.v1.auth import verify_and_consume_code
    await verify_and_consume_code(current_user.email, "password_change", body.email_code)
    current_user.password_hash = get_password_hash(body.new_password)
    current_user.token_version += 1
    await current_user.save(update_fields=["password_hash", "token_version"])
    return ResponseBase(data={"message": "密码已修改，请重新登录"})


@router.put("/me/login-id", response_model=ResponseBase[dict])
async def change_login_id(
    body: LoginIdChangeRequest,
    current_user: User = Depends(get_current_user),
):
    """修改登录 ID（30 天冷却期，需验证当前密码）"""
    from datetime import datetime, timezone, timedelta
    import re
    from app.api.v1.auth import verify_and_consume_code
    await verify_and_consume_code(current_user.email, "login_id_change", body.email_code)
    if not verify_password(body.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="当前密码不正确")
    new_id = body.new_login_id.strip()
    if len(new_id) < 3 or len(new_id) > 50:
        raise HTTPException(status_code=400, detail="登录 ID 长度需在 3~50 个字符之间")
    if not re.match(r'^[a-zA-Z0-9_\-\.]+$', new_id):
        raise HTTPException(status_code=400, detail="登录 ID 只能包含字母、数字、下划线、连字符和点")
    if current_user.login_id_changed_at:
        days_since = (datetime.now(timezone.utc) - current_user.login_id_changed_at).days
        if days_since < 30:
            raise HTTPException(status_code=429, detail=f"登录 ID 修改需要 30 天冷却期，距离上次修改还有 {30 - days_since} 天")
    exists = await User.filter(login_id=new_id).exclude(id=current_user.id).exists()
    if exists:
        raise HTTPException(status_code=409, detail="该登录 ID 已被使用")
    current_user.login_id = new_id
    current_user.login_id_changed_at = datetime.now(timezone.utc)
    current_user.token_version += 1
    await current_user.save(update_fields=["login_id", "login_id_changed_at", "token_version"])
    return ResponseBase(data={"message": "登录 ID 已更新，请重新登录", "new_login_id": new_id})


@router.get("/{user_id}/bookmarks", response_model=ResponseBase[list])
async def get_user_public_bookmarks(
    user_id: int,
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """获取某用户的公开收藏列表"""
    if not await User.exists(id=user_id):
        raise HTTPException(status_code=404, detail="User not found")
    from app.models.interaction import Bookmark
    bookmarks = await (
        Bookmark.filter(user_id=user_id, is_private=False)
        .order_by("-created_at")
        .offset(offset)
        .limit(limit)
        .prefetch_related("artwork__images", "artwork__author", "artwork__tags")
    )
    artworks = [b.artwork for b in bookmarks if b.artwork.visibility == "public"]
    return ResponseBase(data=[serialize_artwork(a) for a in artworks])


# ------------------------------------------------------------------
# 浏览历史
# ------------------------------------------------------------------

@router.get("/me/history", response_model=ResponseBase[list])
async def get_my_history(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
):
    """获取我的浏览历史（SQL 层 DISTINCT ON 去重，每件作品只保留最新一条）"""
    from app.models.interaction import ViewHistory
    from app.models.user_membership import UserMembership
    from tortoise import connections

    has_membership = await UserMembership.filter(
        user_id=current_user.id, status="active", expires_at__gte=datetime.now(timezone.utc)
    ).exists()
    cap = 10000 if has_membership else 500

    # 如果请求的 offset 超出上限，返回空
    if offset >= cap:
        return ResponseBase(data=[], extra={"cap": cap, "has_membership": has_membership})

    # 限制 limit 不超过上限内剩余量
    actual_limit = min(limit, cap - offset)

    conn = connections.get("default")
    # DISTINCT ON 取每件作品最新记录的 id，再在外层按 viewed_at 排序分页（在 cap 范围内）
    rows = await conn.execute_query_dict(
        """
        SELECT id FROM (
            SELECT DISTINCT ON (artwork_id) id, viewed_at
            FROM view_histories
            WHERE user_id = $1
            ORDER BY artwork_id, viewed_at DESC
        ) t
        ORDER BY viewed_at DESC
        LIMIT $2 OFFSET $3
        """,
        [current_user.id, actual_limit, offset],
    )
    deduped_ids = [r["id"] for r in rows]

    if not deduped_ids:
        return ResponseBase(data=[], extra={"cap": cap, "has_membership": has_membership})

    records = await (
        ViewHistory.filter(id__in=deduped_ids)
        .order_by("-viewed_at")
        .prefetch_related("artwork__images", "artwork__author", "artwork__tags")
    )
    result = [
        {
            "viewed_at": r.viewed_at.isoformat(),
            "artwork": serialize_artwork(r.artwork).model_dump(),
        }
        for r in records
    ]
    return ResponseBase(data=result, extra={"cap": cap, "has_membership": has_membership})


@router.delete("/me/history", response_model=ResponseBase[dict])
async def clear_my_history(
    current_user: User = Depends(get_current_user),
):
    """清空我的浏览历史"""
    from app.models.interaction import ViewHistory
    deleted = await ViewHistory.filter(user_id=current_user.id).delete()
    return ResponseBase(data={"deleted": deleted})


# ------------------------------------------------------------------
# 关注标签（Follow Tag）
# ------------------------------------------------------------------

@router.get("/me/follow-tags", response_model=ResponseBase[List[str]])
async def get_followed_tags(current_user: User = Depends(get_current_user)):
    """获取我关注的标签列表"""
    tags = await FollowTag.filter(user_id=current_user.id).values_list("tag_name", flat=True)
    return ResponseBase(data=list(tags))


@router.post("/me/follow-tags/{tag_name}", response_model=ResponseBase[dict])
async def follow_tag(
    tag_name: str,
    current_user: User = Depends(get_current_user),
):
    """关注一个标签"""
    _, created = await FollowTag.get_or_create(user_id=current_user.id, tag_name=tag_name)
    if created:
        from app.infrastructure.cache import invalidate_following
        await invalidate_following(current_user.id)
    return ResponseBase(data={"is_following": True, "created": created})


@router.delete("/me/follow-tags/{tag_name}", response_model=ResponseBase[dict])
async def unfollow_tag(
    tag_name: str,
    current_user: User = Depends(get_current_user),
):
    """取消关注标签"""
    deleted = await FollowTag.filter(user_id=current_user.id, tag_name=tag_name).delete()
    if not deleted:
        raise HTTPException(status_code=404, detail="Tag not followed")
    from app.infrastructure.cache import invalidate_following
    await invalidate_following(current_user.id)
    return ResponseBase(data={"is_following": False})


@router.get("/me/following_series", response_model=ResponseBase[List[dict]])
async def get_following_series(
    current_user: User = Depends(get_current_user),
):
    """获取当前用户追更的所有系列"""
    from app.models.artwork import SeriesFollow
    from collections import defaultdict
    follows = await (
        SeriesFollow.filter(user=current_user)
        .prefetch_related("series")
        .order_by("-created_at")
    )
    if not follows:
        return ResponseBase(data=[])
    series_ids = [f.series.id for f in follows]
    # 单次批量加载所有系列作品（封面 + 计数）
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
                imgs = list(item.artwork.images)
                cover_map[item.series_id] = imgs[0].file_url if imgs else None
            except Exception:
                cover_map[item.series_id] = None
    result = []
    for f in follows:
        s = f.series
        result.append({
            "series_id": s.id,
            "series_title": s.title,
            "series_description": s.description,
            "author_id": s.author_id,
            "artwork_count": count_map[s.id],
            "cover_url": cover_map.get(s.id),
            "notify": f.notify,
            "followed_at": f.created_at.isoformat(),
        })
    return ResponseBase(data=result)


# ------------------------------------------------------------------
# 关注用户分组
# ------------------------------------------------------------------

class FollowGroupCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)

class FollowGroupRename(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)


@router.get("/me/follow-groups", response_model=ResponseBase[list])
async def list_follow_groups(current_user: User = Depends(get_current_user)):
    """列出我的所有关注分组（含成员数）"""
    groups = await FollowGroup.filter(user_id=current_user.id).order_by("sort_order", "created_at")
    result = []
    for g in groups:
        count = await FollowGroupMember.filter(group_id=g.id).count()
        result.append({"id": g.id, "name": g.name, "sort_order": g.sort_order,
                        "member_count": count, "created_at": g.created_at.isoformat()})
    return ResponseBase(data=result)


@router.post("/me/follow-groups", response_model=ResponseBase[dict])
async def create_follow_group(
    body: FollowGroupCreate,
    current_user: User = Depends(get_current_user),
):
    """创建关注分组（上限 20 个）"""
    count = await FollowGroup.filter(user_id=current_user.id).count()
    if count >= 20:
        raise HTTPException(status_code=400, detail="最多创建 20 个分组")
    if await FollowGroup.exists(user_id=current_user.id, name=body.name):
        raise HTTPException(status_code=400, detail="已存在同名分组")
    g = await FollowGroup.create(user_id=current_user.id, name=body.name)
    return ResponseBase(data={"id": g.id, "name": g.name, "member_count": 0})


@router.put("/me/follow-groups/{group_id}", response_model=ResponseBase[dict])
async def rename_follow_group(
    group_id: int,
    body: FollowGroupRename,
    current_user: User = Depends(get_current_user),
):
    """重命名分组"""
    g = await FollowGroup.get_or_none(id=group_id, user_id=current_user.id)
    if not g:
        raise HTTPException(status_code=404, detail="分组不存在")
    conflict = await FollowGroup.get_or_none(user_id=current_user.id, name=body.name)
    if conflict and conflict.id != g.id:
        raise HTTPException(status_code=400, detail="已存在同名分组")
    g.name = body.name
    await g.save(update_fields=["name"])
    return ResponseBase(data={"id": g.id, "name": g.name})


@router.delete("/me/follow-groups/{group_id}", response_model=ResponseBase[dict])
async def delete_follow_group(
    group_id: int,
    current_user: User = Depends(get_current_user),
):
    """删除分组（成员关系一并删除）"""
    g = await FollowGroup.get_or_none(id=group_id, user_id=current_user.id)
    if not g:
        raise HTTPException(status_code=404, detail="分组不存在")
    await FollowGroupMember.filter(group_id=g.id).delete()
    await g.delete()
    return ResponseBase(data={"deleted": True})


@router.put("/me/follow-groups/{group_id}/members/{user_id}", response_model=ResponseBase[dict])
async def add_follow_group_member(
    group_id: int,
    user_id: int,
    current_user: User = Depends(get_current_user),
):
    """将已关注的用户加入分组"""
    g = await FollowGroup.get_or_none(id=group_id, user_id=current_user.id)
    if not g:
        raise HTTPException(status_code=404, detail="分组不存在")
    if not await Follow.exists(follower_id=current_user.id, followed_id=user_id):
        raise HTTPException(status_code=400, detail="只能将已关注的用户加入分组")
    await FollowGroupMember.get_or_create(group_id=group_id, followed_id=user_id)
    return ResponseBase(data={"added": True})


@router.delete("/me/follow-groups/{group_id}/members/{user_id}", response_model=ResponseBase[dict])
async def remove_follow_group_member(
    group_id: int,
    user_id: int,
    current_user: User = Depends(get_current_user),
):
    """从分组移除用户"""
    g = await FollowGroup.get_or_none(id=group_id, user_id=current_user.id)
    if not g:
        raise HTTPException(status_code=404, detail="分组不存在")
    deleted = await FollowGroupMember.filter(group_id=group_id, followed_id=user_id).delete()
    if not deleted:
        raise HTTPException(status_code=404, detail="该用户不在此分组")
    return ResponseBase(data={"removed": True})


@router.get("/me/follow-groups/user/{user_id}/groups", response_model=ResponseBase[list])
async def get_groups_for_followed_user(
    user_id: int,
    current_user: User = Depends(get_current_user),
):
    """获取当前用户将某个被关注者放入了哪些分组"""
    if not await Follow.exists(follower_id=current_user.id, followed_id=user_id):
        return ResponseBase(data=[])
    memberships = await FollowGroupMember.filter(
        followed_id=user_id, group__user_id=current_user.id
    ).prefetch_related("group")
    return ResponseBase(data=[{"id": m.group.id, "name": m.group.name} for m in memberships])


@router.get("/{user_id}/commissions/public", response_model=ResponseBase[list])
async def get_user_public_commissions(
    user_id: int,
    limit: int = Query(default=12, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
):
    """获取画师已完成且有关联公开作品的约稿列表（展示于画师主页约稿 tab）"""
    if not await User.exists(id=user_id):
        raise HTTPException(status_code=404, detail="User not found")
    from app.models.commission import Commission
    commissions = await Commission.filter(
        creator_id=user_id,
        status="completed",
        delivered_artwork_id__not_isnull=True,
    ).order_by("-updated_at").offset(offset).limit(limit)
    if not commissions:
        return ResponseBase(data=[])
    # 批量拉取关联作品（仅 public）
    artwork_ids = [c.delivered_artwork_id for c in commissions if c.delivered_artwork_id]
    artworks = await Artwork.filter(id__in=artwork_ids, visibility="public").prefetch_related("images")
    artwork_map = {a.id: a for a in artworks}
    result = []
    for c in commissions:
        art = artwork_map.get(c.delivered_artwork_id)
        if not art:
            continue
        thumb = art.images[0].file_url if art.images else None
        result.append({
            "commission_id": c.id,
            "title": c.title,
            "rating": c.rating if hasattr(c, "rating") else None,
            "artwork_id": art.id,
            "artwork_title": art.title,
            "thumbnail": thumb,
        })
    return ResponseBase(data=result)



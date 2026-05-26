from datetime import timedelta
from typing import Dict, Any
from fastapi import HTTPException
from app.models.user import User
from app.schemas.user_schema import UserCreate, UserLogin, UserPreferenceUpdate, UserProfileUpdate, CreatorProfileUpdate
from app.core.security import get_password_hash, verify_password, create_access_token, decode_access_token

# Refresh Token 有效期：30 天
REFRESH_TOKEN_EXPIRE_DAYS = 30


class AuthService:

    @staticmethod
    async def register(user_in: UserCreate) -> User:
        # login_id 和 username 初始设置为同一值，注册后可分别修改
        if await User.filter(login_id=user_in.username).exists():
            raise HTTPException(status_code=400, detail="Username already registered")
        if await User.filter(email=user_in.email).exists():
            raise HTTPException(status_code=400, detail="Email already registered")

        new_user = await User.create(
            login_id=user_in.username,
            username=user_in.username,
            email=user_in.email,
            password_hash=get_password_hash(user_in.password),
        )
        from app.services.meili_sync import sync_user_to_meili
        await sync_user_to_meili(new_user)
        return new_user

    @staticmethod
    async def login(user_in: UserLogin) -> Dict[str, Any]:
        # 优先通过 login_id 查找，兼容旧数据用 username 备选
        user = await User.filter(login_id=user_in.username).first()
        if not user:
            user = await User.filter(username=user_in.username, login_id=None).first()
        if not user or not verify_password(user_in.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Incorrect login ID or password")
        if user.is_banned:
            raise HTTPException(status_code=403, detail=f"Account is banned: {user.banned_reason or 'No reason provided'}")

        token_data = {"sub": str(user.id), "username": user.username, "ver": user.token_version}
        access_token = create_access_token(data=token_data)
        refresh_token = create_access_token(
            data={"sub": str(user.id), "type": "refresh", "ver": user.token_version},
            expires_delta=timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        )
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
        }

    @staticmethod
    async def refresh(refresh_token: str) -> Dict[str, Any]:
        payload = decode_access_token(refresh_token)
        if payload is None or payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

        user_id = payload.get("sub")
        user = await User.get_or_none(id=int(user_id))
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        if user.is_banned:
            raise HTTPException(status_code=403, detail="Account is banned")
        if payload.get("ver", 0) != user.token_version:
            raise HTTPException(status_code=401, detail="Token has been invalidated")

        new_access_token = create_access_token(
            data={"sub": str(user.id), "username": user.username, "ver": user.token_version}
        )
        return {"access_token": new_access_token, "token_type": "bearer"}


class UserService:

    @staticmethod
    async def update_preferences(user: User, prefs: UserPreferenceUpdate) -> User:
        if prefs.r18_enabled is not None:
            user.r18_enabled = prefs.r18_enabled
        if prefs.hide_ai_generated is not None:
            user.hide_ai_generated = prefs.hide_ai_generated
        if prefs.muted_tags is not None:
            user.muted_tags = prefs.muted_tags
        if prefs.muted_user_ids is not None:
            user.muted_user_ids = prefs.muted_user_ids
        if prefs.show_likes_public is not None:
            user.show_likes_public = prefs.show_likes_public
        if prefs.show_followers_public is not None:
            user.show_followers_public = prefs.show_followers_public
        if prefs.show_following_public is not None:
            user.show_following_public = prefs.show_following_public
        if prefs.notification_prefs is not None:
            user.notification_prefs = prefs.notification_prefs
        if hasattr(prefs, 'preferred_translation_lang') and prefs.preferred_translation_lang is not None:
            user.preferred_translation_lang = prefs.preferred_translation_lang
        await user.save()
        from app.services.meili_sync import sync_user_to_meili
        await sync_user_to_meili(user)
        return user

    @staticmethod
    async def update_profile(user: User, profile: UserProfileUpdate) -> User:
        simple_fields = [
            "username", "avatar_url", "background_url", "bio", "website_url", "twitter_url",
            "gender", "birth_year", "birth_month", "birth_day",
            "location", "occupation", "social_links",
        ]
        for field in simple_fields:
            val = getattr(profile, field, None)
            if val is not None:
                setattr(user, field, val)
        await user.save()
        from app.services.meili_sync import sync_user_to_meili
        await sync_user_to_meili(user)
        return user

    @staticmethod
    async def update_creator_profile(user: User, data: CreatorProfileUpdate) -> User:
        if data.commission_enabled is not None:
            user.commission_enabled = data.commission_enabled
        if data.commission_info is not None:
            user.commission_info = data.commission_info
        if data.commission_max_revisions is not None:
            user.commission_max_revisions = data.commission_max_revisions
        await user.save()
        from app.services.meili_sync import sync_user_to_meili
        await sync_user_to_meili(user)
        return user

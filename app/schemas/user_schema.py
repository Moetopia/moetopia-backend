from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List
from datetime import datetime


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    email_code: str = Field(..., min_length=6, max_length=6)


class UserLogin(BaseModel):
    username: str = Field(..., max_length=100)
    password: str = Field(..., max_length=128)


class UserResponse(BaseModel):
    id: int
    login_id: Optional[str] = None
    login_id_changed_at: Optional[datetime] = None
    username: str
    email: str
    avatar_url: Optional[str] = None
    background_url: Optional[str] = None
    bio: Optional[str] = None
    website_url: Optional[str] = None
    twitter_url: Optional[str] = None
    gender: Optional[str] = None
    birth_year: Optional[int] = None
    birth_month: Optional[int] = None
    birth_day: Optional[int] = None
    location: Optional[str] = None
    occupation: Optional[str] = None
    social_links: List[dict] = []
    role: str
    r18_enabled: bool
    hide_ai_generated: bool
    muted_tags: List[str] = []
    muted_user_ids: List[int] = []
    show_likes_public: bool = True
    show_followers_public: bool = True
    show_following_public: bool = True
    is_creator: bool
    commission_enabled: bool
    commission_info: Optional[str] = None
    notification_prefs: dict = {}
    preferred_translation_lang: Optional[str] = None
    created_at: datetime

    followers_count: int = 0
    following_count: int = 0
    has_membership: bool = False
    is_imported: bool = False
    pixiv_user_id: Optional[int] = None

    class Config:
        from_attributes = True


class UserPublicResponse(BaseModel):
    """面向公开访问的用户主页信息（不含敏感偏好字段）"""
    id: int
    login_id: Optional[str] = None
    username: str
    avatar_url: Optional[str] = None
    background_url: Optional[str] = None
    bio: Optional[str] = None
    website_url: Optional[str] = None
    twitter_url: Optional[str] = None
    gender: Optional[str] = None
    birth_year: Optional[int] = None
    birth_month: Optional[int] = None
    birth_day: Optional[int] = None
    location: Optional[str] = None
    occupation: Optional[str] = None
    social_links: List[dict] = []
    is_creator: bool
    commission_enabled: bool
    commission_info: Optional[str] = None
    show_likes_public: bool = True
    show_followers_public: bool = True
    show_following_public: bool = True
    created_at: datetime

    followers_count: int = 0
    following_count: int = 0
    is_blocked_by: bool = False
    has_membership: bool = False
    is_imported: bool = False
    pixiv_user_id: Optional[int] = None

    class Config:
        from_attributes = True


class UserProfileUpdate(BaseModel):
    """用户编辑个人资料"""
    username: Optional[str] = Field(None, min_length=1, max_length=50)
    avatar_url: Optional[str] = Field(None, max_length=500)
    background_url: Optional[str] = Field(None, max_length=500)
    bio: Optional[str] = Field(None, max_length=500)
    website_url: Optional[str] = Field(None, max_length=500)
    twitter_url: Optional[str] = Field(None, max_length=500)
    gender: Optional[str] = Field(None, pattern="^(male|female|other)$")
    birth_year: Optional[int] = Field(None, ge=1900, le=2099)
    birth_month: Optional[int] = Field(None, ge=1, le=12)
    birth_day: Optional[int] = Field(None, ge=1, le=31)
    location: Optional[str] = Field(None, max_length=100)
    occupation: Optional[str] = Field(None, max_length=100)
    social_links: Optional[List[dict]] = Field(None, max_length=10)


class UserPreferenceUpdate(BaseModel):
    r18_enabled: Optional[bool] = None
    hide_ai_generated: Optional[bool] = None
    muted_tags: Optional[List[str]] = Field(None, max_length=500)
    muted_user_ids: Optional[List[int]] = Field(None, max_length=500)
    show_likes_public: Optional[bool] = None
    show_followers_public: Optional[bool] = None
    preferred_translation_lang: Optional[str] = Field(None, max_length=10)
    show_following_public: Optional[bool] = None
    notification_prefs: Optional[dict] = None


class CreatorProfileUpdate(BaseModel):
    """创作者配置约稿设置（is_creator 仅通过管理员审批设置，不可自行修改）"""
    commission_enabled: Optional[bool] = None
    commission_info: Optional[str] = Field(None, max_length=2000)
    commission_max_revisions: Optional[int] = Field(None, ge=0, le=20)

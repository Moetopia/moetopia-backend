from pydantic import BaseModel, Field, field_validator
from typing import List, Optional
from uuid import UUID
from datetime import datetime


class ArtworkTagResponse(BaseModel):
    id: int
    tag_name: str
    type: str
    confidence: float
    upvotes: int
    downvotes: int

    class Config:
        from_attributes = True


class ArtworkImageResponse(BaseModel):
    id: UUID
    file_url: str
    has_original: bool = False
    width: Optional[int] = None
    height: Optional[int] = None
    sort_order: int

    class Config:
        from_attributes = True


# 1. 接收前端创建作品时的请求体结构
class ArtworkCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=100, description="作品标题")
    description: Optional[str] = Field(None, max_length=2000, description="作品描述")
    tags: List[str] = Field(default_factory=list, max_length=20, description="用户手打的标签数组（最多20个）")
    artwork_type: str = Field(default="illustration", pattern="^(illustration|manga|animated|novel)$", description="作品类型")
    is_ai: bool = Field(default=False, description="是否包含 AI 生成内容")
    rating: str = Field(default="safe", pattern="^(safe|r18|r18g)$", description="年龄分级")
    visibility: str = Field(default="public", pattern="^(public|private|followers|scheduled)$", description="可见性")
    allow_ai_tagging: bool = Field(default=True, description="允许 AI 自动打标")
    allow_community_tagging: bool = Field(default=True, description="允许社区众包标签")
    content_origin: str = Field(default='original', pattern='^(original|fanart|repost)$', description="内容来源声明")
    pixiv_id: Optional[int] = Field(None, description="Pixiv 作品 ID（用于去重）")
    source: Optional[str] = Field(None, max_length=500, description="原始来源 URL")
    original_author_name: Optional[str] = Field(None, max_length=200, description="原作者名（转载时填写）")

    @field_validator("tags", mode="before")
    @classmethod
    def validate_tags(cls, v: list) -> list:
        for tag in v:
            if len(str(tag)) > 50:
                raise ValueError("每个标签不能超过 50 个字符")
        return v


# 2. 返回给前端的作品展示结构
class TopCommentPreview(BaseModel):
    user_id: int
    username: Optional[str] = None
    user_avatar: Optional[str] = None
    content: str
    like_count: int = 0


class ArtworkResponse(BaseModel):
    id: int
    author_id: int
    author_username: Optional[str] = None
    author_avatar: Optional[str] = None
    title: str
    description: Optional[str]
    images: List[ArtworkImageResponse] = []
    tags: List[ArtworkTagResponse] = []
    artwork_type: str = 'illustration'
    is_ai: bool
    rating: str
    visibility: str
    view_count: int
    like_count: int
    bookmark_count: int
    comment_count: int = 0
    top_comment: Optional[TopCommentPreview] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    is_liked: bool = False
    is_bookmarked: bool = False
    allow_ai_tagging: bool = True
    allow_community_tagging: bool = True
    content_origin: str = 'original'
    moderation_status: str = 'approved'
    pixiv_id: Optional[int] = None
    source: Optional[str] = None
    original_author_name: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    # 导入账号相关
    author_is_imported: bool = False
    author_pixiv_user_id: Optional[int] = None

    class Config:
        from_attributes = True


class ArtworkUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=2000)
    tags: Optional[List[str]] = Field(None, max_length=20)
    rating: Optional[str] = Field(None, pattern="^(safe|r18|r18g)$")
    visibility: Optional[str] = Field(None, pattern="^(public|private|followers)$")

    @field_validator("tags", mode="before")
    @classmethod
    def validate_tags(cls, v: list) -> list:
        if v is None:
            return v
        for tag in v:
            if len(str(tag)) > 50:
                raise ValueError("每个标签不能超过 50 个字符")
        return v


class ArtworkSeriesCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)


class ArtworkSeriesResponse(BaseModel):
    id: int
    author_id: int
    title: str
    description: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


def serialize_artwork(artwork, top_comment=None, comment_count: int = 0) -> ArtworkResponse:
    from tortoise.exceptions import NoValuesFetched

    images_list = []
    try:
        images_list = [ArtworkImageResponse.model_validate(img) for img in sorted(artwork.images, key=lambda x: x.sort_order)]
    except (NoValuesFetched, AttributeError):
        pass

    tags_list = []
    try:
        tags_list = [ArtworkTagResponse.model_validate(t) for t in artwork.tags]
    except (NoValuesFetched, AttributeError):
        pass

    author_username = None
    author_avatar = None
    author_is_imported = False
    author_pixiv_user_id = None
    try:
        author_username = artwork.author.username
        author_avatar = artwork.author.avatar_url
        author_is_imported = getattr(artwork.author, 'is_imported', False)
        author_pixiv_user_id = getattr(artwork.author, 'pixiv_user_id', None)
    except (NoValuesFetched, AttributeError):
        pass

    top_comment_preview = None
    if top_comment is not None:
        try:
            tc_username = None
            tc_avatar = None
            try:
                tc_username = top_comment.user.username
                tc_avatar = top_comment.user.avatar_url
            except Exception:
                pass
            top_comment_preview = TopCommentPreview(
                user_id=top_comment.user_id,
                username=tc_username,
                user_avatar=tc_avatar,
                content=top_comment.content,
                like_count=getattr(top_comment, 'like_count', 0),
            )
        except Exception:
            pass

    return ArtworkResponse(
        id=artwork.id,
        author_id=artwork.author_id,
        author_username=author_username,
        author_avatar=author_avatar,
        title=artwork.title,
        description=artwork.description,
        images=images_list,
        tags=tags_list,
        artwork_type=getattr(artwork, 'artwork_type', 'illustration'),
        is_ai=artwork.is_ai,
        rating=artwork.rating,
        visibility=artwork.visibility,
        view_count=artwork.view_count,
        like_count=artwork.like_count,
        bookmark_count=artwork.bookmark_count,
        comment_count=comment_count,
        top_comment=top_comment_preview,
        created_at=artwork.created_at,
        updated_at=getattr(artwork, "updated_at", None),
        allow_ai_tagging=getattr(artwork, "allow_ai_tagging", True),
        allow_community_tagging=getattr(artwork, "allow_community_tagging", True),
        content_origin=getattr(artwork, "content_origin", "original"),
        moderation_status=getattr(artwork, "moderation_status", "approved"),
        pixiv_id=getattr(artwork, "pixiv_id", None),
        source=getattr(artwork, "source", None),
        original_author_name=getattr(artwork, "original_author_name", None),
        scheduled_at=getattr(artwork, "scheduled_at", None),
        author_is_imported=author_is_imported,
        author_pixiv_user_id=author_pixiv_user_id,
    )
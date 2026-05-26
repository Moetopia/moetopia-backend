from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class CommentCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=1000)
    parent_id: Optional[int] = None
    reply_to_user_id: Optional[int] = None


class CommentResponse(BaseModel):
    id: int
    user_id: int
    username: Optional[str] = None
    user_avatar: Optional[str] = None
    artwork_id: int
    parent_id: Optional[int] = None
    reply_to_id: Optional[int] = None
    reply_to_username: Optional[str] = None
    content: str
    is_deleted: bool
    like_count: int = 0
    is_liked: bool = False
    created_at: datetime
    replies: List["CommentResponse"] = []

    class Config:
        from_attributes = True


CommentResponse.model_rebuild()


class BookmarkCreate(BaseModel):
    is_private: bool = False
    user_custom_tags: List[str] = []
    folder_id: Optional[int] = None


class BookmarkFolderCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    is_private: bool = False


class BookmarkFolderResponse(BaseModel):
    id: int
    user_id: int
    name: str
    is_private: bool
    created_at: datetime

    class Config:
        from_attributes = True

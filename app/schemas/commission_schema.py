from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class CommissionTierRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=2000)
    price: float = Field(..., gt=0, le=1_000_000)
    allow_custom_amount: bool = False
    min_custom_amount: Optional[float] = Field(None, gt=0)
    sort_order: int = Field(default=0, ge=0)
    is_active: bool = True


class CommissionTierResponse(BaseModel):
    id: int
    creator_id: int
    title: str
    description: Optional[str]
    price: float
    allow_custom_amount: bool
    min_custom_amount: Optional[float]
    sort_order: int
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class CommissionRequest(BaseModel):
    creator_id: int
    title: str = Field(..., min_length=2, max_length=200)
    description: str = Field(..., min_length=10, max_length=5000)
    # tier_id: 选择挂位时使用；自定义金额时传 price，两者至少需要一个
    tier_id: Optional[int] = None
    price: Optional[float] = Field(None, gt=0, le=1_000_000)
    deadline: Optional[datetime] = None


class CommissionActionRequest(BaseModel):
    """画师接单/拒单时的操作体"""
    note: Optional[str] = Field(None, max_length=1000)


class CommissionCancelRequest(BaseModel):
    """客户取消或画师终止约稿时的操作体"""
    reason: Optional[str] = Field(None, max_length=1000)


class CommissionDeliverRequest(BaseModel):
    """画师交付时指定作品 ID"""
    artwork_id: int


class RevisionCreateRequest(BaseModel):
    description: str = Field(..., min_length=5, max_length=2000)


class StartRevisionRequest(BaseModel):
    creator_reply: Optional[str] = Field(None, max_length=1000)


class CommissionReviewRequest(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    comment: Optional[str] = Field(None, max_length=2000)
    is_anonymous: bool = False


class CommissionResponse(BaseModel):
    id: int
    client_id: int
    client_username: Optional[str] = None
    creator_id: int
    creator_username: Optional[str] = None
    title: str
    description: str
    tier_id: Optional[int] = None
    price: float
    status: str
    max_revisions: int = 3
    payment_status: str = 'unpaid'
    deadline: Optional[datetime]
    delivered_artwork_id: Optional[int]
    delivered_file_url: Optional[str] = None
    delivered_file_name: Optional[str] = None
    creator_note: Optional[str]
    cancelled_reason: Optional[str] = None
    terminated_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

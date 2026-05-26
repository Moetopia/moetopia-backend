import asyncio
import os
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from typing import List, Optional
from pydantic import BaseModel, Field
from tortoise.functions import Avg, Count
from app.api.dependencies import get_current_user, get_optional_user
from app.models.user import User
from app.models.commission import Commission
from app.models.commission_tier import CommissionTier
from app.models.social import Notification, Follow
from app.services.notification_service import push_notification
from app.models.artwork import Artwork
from app.schemas.commission_schema import CommissionRequest, CommissionResponse, CommissionActionRequest, CommissionCancelRequest, CommissionDeliverRequest, RevisionCreateRequest, StartRevisionRequest, CommissionReviewRequest, CommissionTierRequest, CommissionTierResponse
from app.schemas.artwork_schema import ArtworkResponse, serialize_artwork
from app.schemas.common import ResponseBase
from app.services.storage_service import storage

router = APIRouter()


class CreatorApplyRequest(BaseModel):
    """申请成为认证画师"""
    portfolio_url: Optional[str] = Field(None, max_length=500, description="作品集链接")
    reason: str = Field(..., min_length=10, max_length=2000, description="申请理由")


@router.get("/stats", response_model=ResponseBase[dict])
async def get_creator_stats(current_user: User = Depends(get_current_user)):
    """获取当前登录画师的真实统计数据"""
    from app.models.social import Follow
    from tortoise.functions import Sum, Count

    # 用聚合函数，避免将所有作品对象加载进内存
    _agg_list = await (
        Artwork.filter(author_id=current_user.id)
        .annotate(
            total_views=Sum("view_count"),
            total_likes=Sum("like_count"),
            total_bookmarks=Sum("bookmark_count"),
            total_artworks=Count("id"),
        )
        .values("total_views", "total_likes", "total_bookmarks", "total_artworks")
    )
    agg = _agg_list[0] if _agg_list else None

    total_followers, pending_commissions, in_progress_commissions = await asyncio.gather(
        Follow.filter(followed_id=current_user.id).count(),
        Commission.filter(creator_id=current_user.id, status="pending").count(),
        Commission.filter(creator_id=current_user.id, status__in=["accepted", "in_progress"]).count(),
    )

    return ResponseBase(data={
        "total_artworks":      agg["total_artworks"] or 0 if agg else 0,
        "total_views":         agg["total_views"] or 0 if agg else 0,
        "total_likes":         agg["total_likes"] or 0 if agg else 0,
        "total_bookmarks":     agg["total_bookmarks"] or 0 if agg else 0,
        "total_followers":     total_followers,
        "pending_commissions": pending_commissions,
        "in_progress_commissions": in_progress_commissions,
    })


# ------------------------------------------------------------------
# 约稿档位（CommissionTier）管理
# ------------------------------------------------------------------

@router.get("/tiers", response_model=ResponseBase[List[CommissionTierResponse]])
async def list_my_tiers(current_user: User = Depends(get_current_user)):
    """获取当前画师自己的所有档位"""
    tiers = await CommissionTier.filter(creator_id=current_user.id).order_by("sort_order", "id")
    return ResponseBase(data=[CommissionTierResponse.model_validate(t) for t in tiers])


@router.get("/{user_id}/tiers", response_model=ResponseBase[List[CommissionTierResponse]])
async def list_creator_tiers(user_id: int):
    """公开获取指定画师的有效档位"""
    tiers = await CommissionTier.filter(creator_id=user_id, is_active=True).order_by("sort_order", "id")
    return ResponseBase(data=[CommissionTierResponse.model_validate(t) for t in tiers])


@router.post("/tiers", response_model=ResponseBase[CommissionTierResponse])
async def create_tier(
    tier_in: CommissionTierRequest,
    current_user: User = Depends(get_current_user),
):
    """创建约稿档位"""
    if not current_user.is_creator:
        raise HTTPException(status_code=403, detail="仅认证画师可创建档位")
    tier = await CommissionTier.create(
        creator_id=current_user.id,
        title=tier_in.title,
        description=tier_in.description,
        price=tier_in.price,
        allow_custom_amount=tier_in.allow_custom_amount,
        min_custom_amount=tier_in.min_custom_amount,
        sort_order=tier_in.sort_order,
        is_active=tier_in.is_active,
    )
    return ResponseBase(data=CommissionTierResponse.model_validate(tier))


@router.put("/tiers/{tier_id}", response_model=ResponseBase[CommissionTierResponse])
async def update_tier(
    tier_id: int,
    tier_in: CommissionTierRequest,
    current_user: User = Depends(get_current_user),
):
    """更新约稿档位"""
    tier = await CommissionTier.get_or_none(id=tier_id)
    if not tier:
        raise HTTPException(status_code=404, detail="档位不存在")
    if tier.creator_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权修改此档位")
    await tier.update_from_dict(tier_in.model_dump()).save()
    return ResponseBase(data=CommissionTierResponse.model_validate(tier))


@router.delete("/tiers/{tier_id}", response_model=ResponseBase[dict])
async def delete_tier(
    tier_id: int,
    current_user: User = Depends(get_current_user),
):
    """删除约稿档位（不影响已有约稿记录）"""
    tier = await CommissionTier.get_or_none(id=tier_id)
    if not tier:
        raise HTTPException(status_code=404, detail="档位不存在")
    if tier.creator_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权删除此档位")
    await tier.delete()
    return ResponseBase(data={"deleted": True})


@router.post("/commissions", response_model=ResponseBase[CommissionResponse])
async def create_commission(
    commission_in: CommissionRequest,
    current_user: User = Depends(get_current_user),
):
    """客户发起约稿请求"""
    creator = await User.get_or_none(id=commission_in.creator_id)
    if not creator:
        raise HTTPException(status_code=404, detail="Creator not found")
    if not creator.is_creator or not creator.commission_enabled:
        raise HTTPException(status_code=400, detail="This user is not accepting commissions")

    # 确定最终金额：优先从档位取价，否则使用客户传入的 price
    final_price = commission_in.price
    tier_id = None
    if commission_in.tier_id:
        tier = await CommissionTier.get_or_none(id=commission_in.tier_id, creator_id=commission_in.creator_id, is_active=True)
        if not tier:
            raise HTTPException(status_code=404, detail="档位不存在或已下线")
        tier_id = tier.id
        if tier.allow_custom_amount and commission_in.price:
            min_price = float(tier.min_custom_amount or tier.price)
            if float(commission_in.price) < min_price:
                raise HTTPException(status_code=400, detail=f"自定义金额不能低于最低限额 {min_price}")
            final_price = commission_in.price
        else:
            final_price = float(tier.price)
    elif not commission_in.price:
        raise HTTPException(status_code=400, detail="必须选择档位或指定价格")

    commission = await Commission.create(
        client_id=current_user.id,
        creator_id=commission_in.creator_id,
        tier_id=tier_id,
        title=commission_in.title,
        description=commission_in.description,
        price=final_price,
        deadline=commission_in.deadline,
        max_revisions=getattr(creator, 'commission_max_revisions', 3),
    )

    await push_notification(
        user_id=commission_in.creator_id,
        actor_id=current_user.id,
        type="commission",
        content=f"{current_user.username} 发起了一个新约稿：{commission_in.title}",
        related_entity_id=str(commission.id),
    )

    return ResponseBase(data=CommissionResponse.model_validate(commission))


def _serialize_commission(c) -> CommissionResponse:
    """序列化约稿，填充 client_username / creator_username。"""
    try:
        client_uname = c.client.username
    except Exception:
        client_uname = None
    try:
        creator_uname = c.creator.username
    except Exception:
        creator_uname = None
    data = CommissionResponse.model_validate(c)
    return data.model_copy(update={"client_username": client_uname, "creator_username": creator_uname})


@router.get("/commissions", response_model=ResponseBase[List[CommissionResponse]])
async def list_commissions(
    role: str = "creator",
    current_user: User = Depends(get_current_user),
):
    """获取约稿列表（role=creator: 我收到的，role=client: 我发出的）"""
    if role == "creator":
        items = await Commission.filter(creator_id=current_user.id).prefetch_related("client", "creator").order_by("-created_at")
    else:
        items = await Commission.filter(client_id=current_user.id).prefetch_related("client", "creator").order_by("-created_at")
    return ResponseBase(data=[_serialize_commission(c) for c in items])


@router.get("/commissions/{commission_id}", response_model=ResponseBase[CommissionResponse])
async def get_commission(
    commission_id: int,
    current_user: User = Depends(get_current_user),
):
    """获取单个约稿详情（仅当事人可查看）"""
    commission = await Commission.get_or_none(id=commission_id).prefetch_related("client", "creator")
    if not commission:
        raise HTTPException(status_code=404, detail="Commission not found")
    if commission.creator_id != current_user.id and commission.client_id != current_user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return ResponseBase(data=_serialize_commission(commission))


@router.post("/commissions/{commission_id}/accept", response_model=ResponseBase[CommissionResponse])
async def accept_commission(
    commission_id: int,
    body: CommissionActionRequest,
    current_user: User = Depends(get_current_user),
):
    """画师接单"""
    commission = await Commission.get_or_none(id=commission_id, creator_id=current_user.id)
    if not commission:
        raise HTTPException(status_code=404, detail="Commission not found")
    if commission.status != "pending":
        raise HTTPException(status_code=400, detail="Commission is not in pending state")
    if commission.payment_status != "paid":
        raise HTTPException(status_code=400, detail="Cannot accept commission before payment is received")

    commission.status = "accepted"
    commission.creator_note = body.note
    await commission.save()

    await push_notification(
        user_id=commission.client_id,
        actor_id=current_user.id,
        type="commission",
        content=f"你的约稿《{commission.title}》已被接受",
        related_entity_id=str(commission.id),
    )
    return ResponseBase(data=CommissionResponse.model_validate(commission))


@router.post("/commissions/{commission_id}/reject", response_model=ResponseBase[CommissionResponse])
async def reject_commission(
    commission_id: int,
    body: CommissionActionRequest,
    current_user: User = Depends(get_current_user),
):
    """画师拒单"""
    commission = await Commission.get_or_none(id=commission_id, creator_id=current_user.id)
    if not commission:
        raise HTTPException(status_code=404, detail="Commission not found")
    if commission.status not in ("pending", "accepted"):
        raise HTTPException(status_code=400, detail="Cannot reject commission in current state")

    commission.status = "rejected"
    commission.creator_note = body.note
    await commission.save()

    await push_notification(
        user_id=commission.client_id,
        actor_id=current_user.id,
        type="commission",
        content=f"你的约稿《{commission.title}》已被拒绝",
        related_entity_id=str(commission.id),
    )
    return ResponseBase(data=CommissionResponse.model_validate(commission))


@router.post("/commissions/{commission_id}/start", response_model=ResponseBase[CommissionResponse])
async def start_commission(
    commission_id: int,
    current_user: User = Depends(get_current_user),
):
    """画师开始制作（accepted → in_progress）"""
    commission = await Commission.get_or_none(id=commission_id, creator_id=current_user.id)
    if not commission:
        raise HTTPException(status_code=404, detail="Commission not found")
    if commission.status != "accepted":
        raise HTTPException(status_code=400, detail="Commission must be in accepted state to start")
    if commission.payment_status != "paid":
        raise HTTPException(status_code=400, detail="Cannot start commission before payment is received")

    commission.status = "in_progress"
    await commission.save()

    await push_notification(
        user_id=commission.client_id,
        actor_id=current_user.id,
        type="commission",
        content=f"你的约稿《{commission.title}》画师已开始制作",
        related_entity_id=str(commission.id),
    )
    return ResponseBase(data=CommissionResponse.model_validate(commission))


@router.post("/commissions/{commission_id}/deliver", response_model=ResponseBase[CommissionResponse])
async def deliver_commission(
    commission_id: int,
    body: CommissionDeliverRequest,
    current_user: User = Depends(get_current_user),
):
    """画师交付作品"""
    commission = await Commission.get_or_none(id=commission_id, creator_id=current_user.id)
    if not commission:
        raise HTTPException(status_code=404, detail="Commission not found")
    if commission.status not in ("accepted", "in_progress", "revision_requested"):
        raise HTTPException(status_code=400, detail="Commission not in deliverable state")

    artwork = await Artwork.get_or_none(id=body.artwork_id, author_id=current_user.id)
    if not artwork:
        raise HTTPException(status_code=404, detail="Artwork not found or not owned by you")

    commission.status = "completed"
    commission.delivered_artwork_id = body.artwork_id
    await commission.save()
    await _resolve_pending_revisions(commission.id)

    await push_notification(
        user_id=commission.client_id,
        actor_id=current_user.id,
        type="commission",
        content=f"你的约稿《{commission.title}》已完成交付",
        related_entity_id=str(commission.id),
    )
    return ResponseBase(data=CommissionResponse.model_validate(commission))


@router.post("/commissions/{commission_id}/cancel", response_model=ResponseBase[CommissionResponse])
async def cancel_commission(
    commission_id: int,
    body: CommissionCancelRequest = CommissionCancelRequest(),
    current_user: User = Depends(get_current_user),
):
    """客户取消约稿（pending 或 accepted 阶段均可取消）"""
    commission = await Commission.get_or_none(id=commission_id, client_id=current_user.id)
    if not commission:
        raise HTTPException(status_code=404, detail="Commission not found")
    if commission.status not in ("pending", "accepted"):
        raise HTTPException(status_code=400, detail="Only pending or accepted commissions can be cancelled")

    commission.status = "cancelled"
    commission.cancelled_reason = body.reason
    commission.terminated_by = "client"
    await commission.save()

    reason_text = f"：{body.reason}" if body.reason else ""
    await push_notification(
        user_id=commission.creator_id,
        actor_id=current_user.id,
        type="commission",
        content=f"约稿《{commission.title}》已被客户取消{reason_text}",
        related_entity_id=str(commission.id),
    )
    return ResponseBase(data=CommissionResponse.model_validate(commission))


@router.post("/commissions/{commission_id}/terminate", response_model=ResponseBase[CommissionResponse])
async def terminate_commission(
    commission_id: int,
    body: CommissionCancelRequest = CommissionCancelRequest(),
    current_user: User = Depends(get_current_user),
):
    """画师终止约稿（展示原因，状态变为 cancelled）"""
    commission = await Commission.get_or_none(id=commission_id, creator_id=current_user.id)
    if not commission:
        raise HTTPException(status_code=404, detail="Commission not found")
    if commission.status not in ("pending", "accepted", "in_progress", "revision_requested"):
        raise HTTPException(status_code=400, detail="Cannot terminate commission in current state")

    commission.status = "cancelled"
    commission.cancelled_reason = body.reason
    commission.terminated_by = "creator"
    await commission.save()

    reason_text = f"：{body.reason}" if body.reason else ""
    await push_notification(
        user_id=commission.client_id,
        actor_id=current_user.id,
        type="commission",
        content=f"约稿《{commission.title}》已被画师终止{reason_text}",
        related_entity_id=str(commission.id),
    )
    return ResponseBase(data=CommissionResponse.model_validate(commission))


# ------------------------------------------------------------------
# 修改申请
# ------------------------------------------------------------------

@router.post("/commissions/{commission_id}/request-revision", response_model=ResponseBase[dict])
async def request_revision(
    commission_id: int,
    body: RevisionCreateRequest,
    current_user: User = Depends(get_current_user),
):
    """客户申请修改（in_progress → revision_requested）"""
    from app.models.commission_revision import CommissionRevision
    commission = await Commission.get_or_none(id=commission_id, client_id=current_user.id)
    if not commission:
        raise HTTPException(status_code=404, detail="Commission not found")
    if commission.status != "in_progress":
        raise HTTPException(status_code=400, detail="Commission must be in_progress to request revision")

    used = await CommissionRevision.filter(commission_id=commission_id).count()
    if used >= commission.max_revisions:
        raise HTTPException(status_code=400, detail=f"Revision limit ({commission.max_revisions}) reached")

    revision = await CommissionRevision.create(
        commission_id=commission_id,
        requested_by_id=current_user.id,
        description=body.description,
    )
    commission.status = "revision_requested"
    await commission.save()

    await push_notification(
        user_id=commission.creator_id,
        actor_id=current_user.id,
        type="commission",
        content=f"约稿《{commission.title}》客户申请了第 {used + 1} 次修改",
        related_entity_id=str(commission.id),
    )
    return ResponseBase(data={"revision_id": revision.id, "revision_count": used + 1})


@router.post("/commissions/{commission_id}/start-revision", response_model=ResponseBase[CommissionResponse])
async def start_revision(
    commission_id: int,
    body: StartRevisionRequest = StartRevisionRequest(),
    current_user: User = Depends(get_current_user),
):
    """画师确认开始修改（revision_requested → in_progress）"""
    from app.models.commission_revision import CommissionRevision
    commission = await Commission.get_or_none(id=commission_id, creator_id=current_user.id)
    if not commission:
        raise HTTPException(status_code=404, detail="Commission not found")
    if commission.status != "revision_requested":
        raise HTTPException(status_code=400, detail="Commission is not awaiting revision")

    active_rev = await CommissionRevision.filter(
        commission_id=commission_id, status="pending"
    ).order_by("-created_at").first()
    if active_rev:
        active_rev.status = "in_progress"
        active_rev.creator_reply = body.creator_reply
        await active_rev.save()

    commission.status = "in_progress"
    await commission.save()

    await push_notification(
        user_id=commission.client_id,
        actor_id=current_user.id,
        type="commission",
        content=f"约稿《{commission.title}》画师已开始修改",
        related_entity_id=str(commission.id),
    )
    return ResponseBase(data=CommissionResponse.model_validate(commission))


@router.get("/commissions/{commission_id}/revisions", response_model=ResponseBase[list])
async def list_revisions(
    commission_id: int,
    current_user: User = Depends(get_current_user),
):
    """列出约稿的所有修改记录（双方可查看）"""
    from app.models.commission_revision import CommissionRevision
    commission = await Commission.get_or_none(id=commission_id)
    if not commission:
        raise HTTPException(status_code=404, detail="Commission not found")
    if current_user.id not in (commission.client_id, commission.creator_id):
        raise HTTPException(status_code=403, detail="Access denied")

    revs = await CommissionRevision.filter(commission_id=commission_id).order_by("created_at")
    return ResponseBase(data=[
        {
            "id": r.id,
            "description": r.description,
            "status": r.status,
            "creator_reply": r.creator_reply,
            "created_at": r.created_at.isoformat(),
        } for r in revs
    ])


# ------------------------------------------------------------------
# 私密文件交付
# ------------------------------------------------------------------

@router.post("/commissions/{commission_id}/deliver-file", response_model=ResponseBase[CommissionResponse])
async def deliver_file_commission(
    commission_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """画师上传私密文件交付约稿（文件仅限约稿双方下载，状态设为 completed）"""
    commission = await Commission.get_or_none(id=commission_id, creator_id=current_user.id)
    if not commission:
        raise HTTPException(status_code=404, detail="Commission not found")
    if commission.status not in ("accepted", "in_progress", "revision_requested"):
        raise HTTPException(status_code=400, detail="Commission not in deliverable state")

    MAX_FILE_SIZE = 100 * 1024 * 1024
    file_data = await file.read()
    if len(file_data) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File size exceeds 100MB limit")

    orig_name = file.filename or "delivery"
    ext = os.path.splitext(orig_name)[1] or ""
    filename = f"{uuid.uuid4()}{ext}"
    file_url = await storage.save(file_data, f"commission_files/{commission_id}/{filename}")

    commission.delivered_file_url = file_url
    commission.delivered_file_name = orig_name
    commission.status = "completed"
    await commission.save()
    await _resolve_pending_revisions(commission_id)

    await push_notification(
        user_id=commission.client_id,
        actor_id=current_user.id,
        type="commission",
        content=f"你的约稿《{commission.title}》已完成，画师上传了交付文件",
        related_entity_id=str(commission.id),
    )
    return ResponseBase(data=CommissionResponse.model_validate(commission))


@router.get("/commissions/{commission_id}/download-file")
async def download_commission_file(
    commission_id: int,
    current_user: User = Depends(get_current_user),
):
    """约稿双方下载私密交付文件"""
    commission = await Commission.get_or_none(id=commission_id)
    if not commission:
        raise HTTPException(status_code=404, detail="Commission not found")
    if current_user.id not in (commission.client_id, commission.creator_id):
        raise HTTPException(status_code=403, detail="Access denied")
    if not commission.delivered_file_url:
        raise HTTPException(status_code=404, detail="No file delivered for this commission")
    return await storage.make_download_response(
        commission.delivered_file_url,
        commission.delivered_file_name or "delivery",
    )


# ------------------------------------------------------------------
# 约稿评价
# ------------------------------------------------------------------

@router.post("/commissions/{commission_id}/review", response_model=ResponseBase[dict])
async def submit_commission_review(
    commission_id: int,
    body: CommissionReviewRequest,
    current_user: User = Depends(get_current_user),
):
    """客户为已完成约稿留下评价（每个约稿仅可评价一次）"""
    from app.models.commission_review import CommissionReview
    commission = await Commission.get_or_none(id=commission_id, client_id=current_user.id)
    if not commission:
        raise HTTPException(status_code=404, detail="Commission not found")
    if commission.status != "completed":
        raise HTTPException(status_code=400, detail="Can only review completed commissions")

    exists = await CommissionReview.filter(commission_id=commission_id).exists()
    if exists:
        raise HTTPException(status_code=400, detail="Review already submitted")

    review = await CommissionReview.create(
        commission_id=commission_id,
        reviewer_id=current_user.id,
        creator_id=commission.creator_id,
        rating=body.rating,
        comment=body.comment,
        is_anonymous=body.is_anonymous,
    )
    await push_notification(
        user_id=commission.creator_id,
        actor_id=current_user.id,
        type="commission",
        content=f"你的约稿《{commission.title}》收到了一条 {'★' * body.rating} 评价",
        related_entity_id=str(commission.id),
    )
    return ResponseBase(data={"id": review.id, "rating": review.rating})


@router.get("/commissions/{commission_id}/review", response_model=ResponseBase[dict])
async def get_commission_review(
    commission_id: int,
    current_user: User = Depends(get_current_user),
):
    """获取约稿的评价（双方均可查看）"""
    from app.models.commission_review import CommissionReview
    commission = await Commission.get_or_none(id=commission_id)
    if not commission:
        raise HTTPException(status_code=404, detail="Commission not found")
    if current_user.id not in (commission.client_id, commission.creator_id):
        raise HTTPException(status_code=403, detail="Access denied")

    review = await CommissionReview.get_or_none(
        commission_id=commission_id
    ).prefetch_related("reviewer")
    if not review:
        return ResponseBase(data=None)

    reviewer_name = None if review.is_anonymous else review.reviewer.username
    return ResponseBase(data={
        "id": review.id,
        "rating": review.rating,
        "comment": review.comment,
        "is_anonymous": review.is_anonymous,
        "reviewer_username": reviewer_name,
        "created_at": review.created_at.isoformat(),
    })


async def _resolve_pending_revisions(commission_id: int):
    """将该约稿所有未完结的修改记录标记为 resolved"""
    from app.models.commission_revision import CommissionRevision
    await CommissionRevision.filter(
        commission_id=commission_id,
        status__in=["pending", "in_progress"]
    ).update(status="resolved")


# ------------------------------------------------------------------
# 画师公开主页
# ------------------------------------------------------------------

@router.get("/{user_id}/reviews", response_model=ResponseBase[dict])
async def get_creator_reviews(
    user_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """获取画师公开评价列表（安全过滤：匿名评价隐藏评分者身份）"""
    from app.models.commission_review import CommissionReview
    reviews = await CommissionReview.filter(creator_id=user_id).order_by("-created_at").prefetch_related("reviewer").offset(offset).limit(limit)
    agg_list = await CommissionReview.filter(creator_id=user_id).annotate(
        avg=Avg('rating'), cnt=Count('id')
    ).values('avg', 'cnt')
    agg = agg_list[0] if agg_list else None
    result = []
    for r in reviews:
        result.append({
            "id": r.id,
            "rating": r.rating,
            "comment": r.comment,
            "is_anonymous": r.is_anonymous,
            "reviewer_username": None if r.is_anonymous else r.reviewer.username,
            "created_at": r.created_at.isoformat(),
        })
    return ResponseBase(data={
        "reviews": result,
        "avg_rating": round(agg["avg"] or 0, 1) if agg else 0,
        "review_count": agg["cnt"] or 0 if agg else 0,
    })


@router.get("/{user_id}", response_model=ResponseBase[dict])
async def get_creator_public_profile(
    user_id: int,
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """
    画师公开主页：基本信息 + 公开作品列表 + 粉丝/关注数 + 约稿状态。
    仅返回公开作品，R-18 按访客偏好过滤。
    """
    creator = await User.get_or_none(id=user_id)
    if not creator:
        raise HTTPException(status_code=404, detail="Creator not found")

    # 检查访客是否被该画师拉黑
    from app.models.social import UserBlock
    is_blocked_by = False
    if current_user:
        is_blocked_by = await UserBlock.exists(blocker_id=user_id, blocked_id=current_user.id)

    follower_count = await Follow.filter(followed_id=user_id).count()
    following_count = await Follow.filter(follower_id=user_id).count()

    is_following = False
    if current_user:
        is_following = await Follow.exists(follower_id=current_user.id, followed_id=user_id)

    # 公开作品列表（安全过滤）
    qs = Artwork.filter(author_id=user_id, visibility="public").prefetch_related("images", "tags", "author")
    if current_user is None or not current_user.r18_enabled:
        qs = qs.filter(rating="safe")
    if current_user is not None and current_user.hide_ai_generated:
        qs = qs.filter(is_ai=False)

    artworks = [] if is_blocked_by else await qs.order_by("-created_at").offset(offset).limit(limit)

    return ResponseBase(data={
        "id": creator.id,
        "username": creator.username,
        "avatar_url": creator.avatar_url,
        "background_url": creator.background_url,
        "bio": creator.bio,
        "website_url": creator.website_url,
        "twitter_url": creator.twitter_url,
        "is_creator": creator.is_creator,
        "commission_enabled": creator.commission_enabled,
        "commission_info": creator.commission_info,
        "follower_count": follower_count,
        "following_count": following_count,
        "is_following": is_following,
        "is_blocked_by": is_blocked_by,
        "artworks": [serialize_artwork(a).model_dump() for a in artworks],
    })


# ------------------------------------------------------------------
# 画师认证申请
# ------------------------------------------------------------------

@router.post("/apply", response_model=ResponseBase[dict])
async def apply_for_creator(
    body: CreatorApplyRequest,
    current_user: User = Depends(get_current_user),
):
    """
    普通用户申请成为认证画师。
    申请记录写入 creator_applications 表，同时通知所有 admin。
    """
    from app.models.creator_application import CreatorApplication
    if current_user.is_creator:
        raise HTTPException(status_code=400, detail="You are already a creator")

    # 如有已 pending 的申请，覆盖更新而非重复创建
    existing = await CreatorApplication.filter(
        applicant=current_user, status="pending"
    ).first()
    if existing:
        existing.reason = body.reason
        existing.portfolio_url = body.portfolio_url
        await existing.save(update_fields=["reason", "portfolio_url", "updated_at"])
        return ResponseBase(data={"message": "申请已更新，等待审核", "application_id": existing.id})

    app = await CreatorApplication.create(
        applicant=current_user,
        reason=body.reason,
        portfolio_url=body.portfolio_url,
    )

    # 通知所有管理员审核
    admins = await User.filter(role="admin")
    for admin in admins:
        await Notification.create(
            user_id=admin.id,
            actor_id=current_user.id,
            type="system",
            content=(
                f"用户 [{current_user.username}] 申请成为认证画师（申请ID: {app.id}）。\n"
                f"申请理由：{body.reason[:200]}\n"
                f"作品集：{body.portfolio_url or '未提供'}"
            ),
            related_entity_id=str(current_user.id),
        )

    return ResponseBase(data={"message": "申请已提交，等待审核", "application_id": app.id})


@router.get("/my-application", response_model=ResponseBase[dict])
async def get_my_creator_application(current_user: User = Depends(get_current_user)):
    """查看自己最近一次画师认证申请状态"""
    from app.models.creator_application import CreatorApplication
    app = await CreatorApplication.filter(applicant=current_user).order_by("-created_at").first()
    if not app:
        return ResponseBase(data={"status": "none"})
    return ResponseBase(data={
        "id": app.id,
        "status": app.status,
        "reason": app.reason,
        "portfolio_url": app.portfolio_url,
        "review_note": app.review_note,
        "created_at": app.created_at.isoformat(),
    })

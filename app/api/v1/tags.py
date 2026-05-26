import json
from fastapi import APIRouter, HTTPException, Depends, Query, Body
from tortoise.expressions import F
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from datetime import datetime, timezone
from app.schemas.tag_schema import TagResponse, AnchorCreate, TagVoteRequest, ArtworkTagValidationRequest
from app.models.tag import ArtworkTag, TagVote, TagValidatorApplication, TagTranslation
from app.models.user import User
from app.models.social import Notification
from app.api.dependencies import get_current_user, require_role, get_optional_user
from app.schemas.common import ResponseBase
from app.schemas.artwork_schema import ArtworkResponse, serialize_artwork

router = APIRouter()


@router.get("/{tag_name}/artworks", response_model=ResponseBase[dict])
async def get_artworks_by_tag(
    tag_name: str,
    sort: str = "bookmark_count:desc",
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    type: Optional[str] = None,
    rating: Optional[str] = None,
    is_ai: Optional[bool] = None,
    current_user: Optional[User] = Depends(get_optional_user),
):
    """通过标签名直接查询关联作品（PostgreSQL 原生，不依赖 MeiliSearch）"""
    from app.models.artwork import Artwork

    qs = Artwork.filter(tags__tag_name=tag_name, visibility="public").distinct()
    if type:
        qs = qs.filter(artwork_type=type)
    if current_user is None or not current_user.r18_enabled:
        qs = qs.filter(rating="safe")
    elif rating:
        qs = qs.filter(rating=rating)
    if is_ai is not None:
        qs = qs.filter(is_ai=is_ai)

    total = await qs.count()

    sort_field, sort_dir = (sort.split(":") + ["desc"])[:2]
    allowed_sorts = {"bookmark_count", "like_count", "view_count", "created_at"}
    if sort_field not in allowed_sorts:
        sort_field = "bookmark_count"
    order_by = f"-{sort_field}" if sort_dir == "desc" else sort_field

    artworks = await qs.prefetch_related("images", "tags", "author").order_by(order_by).offset(offset).limit(limit)

    return ResponseBase(data={
        "hits": [serialize_artwork(a).model_dump() for a in artworks],
        "total": total,
    })

TAG_VOTE_THRESHOLD = 5

@router.post("/{tag_id}/vote", response_model=ResponseBase[dict])
async def vote_on_tag(
    tag_id: int, 
    vote_in: TagVoteRequest, 
    current_user: User = Depends(get_current_user)
):
    """【普通用户】对某个 AI 预测出的标签进行赞同或反对投票"""
    tag = await ArtworkTag.get_or_none(id=tag_id).prefetch_related('artwork')
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
        
    if tag.type != 'ai_unverified':
        raise HTTPException(status_code=400, detail="Only unverified AI tags can be voted on")

    # 检查是否已经投过票
    existing_vote = await TagVote.get_or_none(user=current_user, artwork_tag=tag)
    
    if existing_vote:
        if existing_vote.is_upvote == vote_in.is_upvote:
            return ResponseBase(data={"message": "Already voted"})
        existing_vote.is_upvote = vote_in.is_upvote
        await existing_vote.save()
        if vote_in.is_upvote:
            await ArtworkTag.filter(id=tag_id).update(upvotes=F("upvotes") + 1, downvotes=F("downvotes") - 1)
        else:
            await ArtworkTag.filter(id=tag_id).update(downvotes=F("downvotes") + 1, upvotes=F("upvotes") - 1)
    else:
        await TagVote.create(user=current_user, artwork_tag=tag, is_upvote=vote_in.is_upvote)
        if vote_in.is_upvote:
            await ArtworkTag.filter(id=tag_id).update(upvotes=F("upvotes") + 1)
        else:
            await ArtworkTag.filter(id=tag_id).update(downvotes=F("downvotes") + 1)

    # 重新读取最新计数
    tag = await ArtworkTag.get(id=tag_id).prefetch_related("artwork")

    # 检查是否触发阈值（用 Redis SETNX 保证只通知一次）
    if tag.upvotes - tag.downvotes >= TAG_VOTE_THRESHOLD:
        from app.infrastructure.redis_client import get_redis
        from app.api.v1.ws import manager
        try:
            r = get_redis()
            notif_key = f"tag_threshold_notified:{tag_id}"
            # setex 返回 True 表示首次设置（未通知过），False 表示已存在
            is_first = await r.set(notif_key, "1", ex=86400, nx=True)
        except Exception:
            is_first = True  # Redis 不可用时降级为总是发送
        if is_first:
            validators = await User.filter(role="tag_validator")
            ws_payload = json.dumps({
                "type": "tag_validation_ready",
                "tag_id": tag.id,
                "artwork_id": tag.artwork.id,
                "tag_name": tag.tag_name,
            })
            for v in validators:
                await Notification.create(
                    user_id=v.id,
                    actor_id=current_user.id,
                    type="system",
                    content=f"作品 {tag.artwork.id} 的标签「{tag.tag_name}」已达到验证阈值，请审核。",
                    related_entity_id=str(tag.id),
                )
                await manager.send_personal_message(ws_payload, v.id)

    return ResponseBase(data={"upvotes": tag.upvotes, "downvotes": tag.downvotes})

@router.post("/{tag_id}/validate", response_model=ResponseBase[dict])
async def validate_tag(
    tag_id: int,
    validation_in: ArtworkTagValidationRequest,
    current_user: User = Depends(require_role(["admin", "tag_validator"]))
):
    """【打标验证员/管理员】对达到阈值的标签进行最终裁决"""
    tag = await ArtworkTag.get_or_none(id=tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
        
    if tag.type != 'ai_unverified':
        raise HTTPException(status_code=400, detail="Only unverified AI tags can be validated")
        
    if validation_in.is_approved:
        tag.type = 'ai_verified'
        await tag.save()
        return ResponseBase(data={"message": "Tag officially verified", "status": "ai_verified"})
    else:
        # 如果审核拒绝，可以直接删除这个标签或者将其标记为废弃
        await tag.delete()
        return ResponseBase(data={"message": "Tag rejected and removed"})

@router.get("/pending", response_model=ResponseBase[List[dict]])
async def get_pending_tags(current_user: User = Depends(require_role(["admin", "tag_validator"]))):
    """【打标验证员】获取所有需要人工核验的推荐标签（按争议度/票数排序）"""
    # 找出所有未验证且净赞同数较高的标签
    tags = await ArtworkTag.filter(type='ai_unverified').order_by('-upvotes').limit(50).values('id', 'tag_name', 'upvotes', 'downvotes', 'artwork_id')
    return ResponseBase(data=tags)


# ──────────────────────────────────────────────────────────────────────
# Tag Validator 申请机制
# ──────────────────────────────────────────────────────────────────────

class TagValidatorApplyRequest(BaseModel):
    reason: str = Field(..., min_length=20, max_length=2000, description="申请理由")


class TagValidatorReviewRequest(BaseModel):
    # approved | rejected
    decision: str = Field(..., pattern="^(approved|rejected)$")
    note: Optional[str] = Field(None, max_length=1000, description="审批意见")


@router.post("/apply-validator", response_model=ResponseBase[dict])
async def apply_for_tag_validator(
    body: TagValidatorApplyRequest,
    current_user: User = Depends(get_current_user),
):
    """【普通用户】申请成为打标验证员。如有待审申请则覆盖，已是该角色则拒绝。"""
    if current_user.role == 'tag_validator':
        raise HTTPException(status_code=400, detail="您已经是打标验证员")
    if current_user.role == 'admin':
        raise HTTPException(status_code=400, detail="管理员无需申请")

    # 如有旧申请（pending 或 rejected）则覆盖
    existing = await TagValidatorApplication.filter(applicant=current_user).order_by("-created_at").first()
    if existing and existing.status == 'pending':
        # 已有待审申请，更新理由
        existing.reason = body.reason
        await existing.save(update_fields=["reason", "updated_at"])
        return ResponseBase(data={"message": "申请已更新，等待审核", "application_id": existing.id})

    app = await TagValidatorApplication.create(
        applicant=current_user,
        reason=body.reason,
    )

    # 通知所有管理员
    admins = await User.filter(role="admin")
    for admin in admins:
        await Notification.create(
            user_id=admin.id,
            actor_id=current_user.id,
            type="system",
            content=(
                f"用户 [{current_user.username}] 申请成为打标验证员。\n"
                f"申请理由：{body.reason[:200]}"
            ),
            related_entity_id=str(app.id),
        )

    return ResponseBase(data={"message": "申请已提交，等待审核", "application_id": app.id})


@router.get("/my-validator-application", response_model=ResponseBase[dict])
async def get_my_validator_application(current_user: User = Depends(get_current_user)):
    """【普通用户】查看自己最近一次 tag_validator 申请的状态"""
    app = await TagValidatorApplication.filter(applicant=current_user).order_by("-created_at").first()
    if not app:
        return ResponseBase(data={"status": "none"})
    return ResponseBase(data={
        "id": app.id,
        "status": app.status,
        "reason": app.reason,
        "review_note": app.review_note,
        "created_at": app.created_at.isoformat(),
    })


@router.get("/validator-applications", response_model=ResponseBase[List[dict]])
async def list_validator_applications(
    status: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(require_role(["admin"])),
):
    """【管理员】查看 tag_validator 申请列表"""
    qs = TagValidatorApplication.all().prefetch_related("applicant", "reviewed_by")
    if status:
        qs = qs.filter(status=status)
    apps = await qs.order_by("-created_at").offset(offset).limit(limit)
    result = []
    for a in apps:
        try: applicant_name = a.applicant.username
        except Exception: applicant_name = None
        try: reviewer_name = a.reviewed_by.username if a.reviewed_by_id else None
        except Exception: reviewer_name = None
        result.append({
            "id": a.id,
            "applicant_id": a.applicant_id,
            "applicant_username": applicant_name,
            "reason": a.reason,
            "status": a.status,
            "reviewer_username": reviewer_name,
            "reviewed_at": a.reviewed_at.isoformat() if a.reviewed_at else None,
            "review_note": a.review_note,
            "created_at": a.created_at.isoformat(),
        })
    return ResponseBase(data=result)


@router.post("/validator-applications/{app_id}/review", response_model=ResponseBase[dict])
async def review_validator_application(
    app_id: int,
    body: TagValidatorReviewRequest,
    current_user: User = Depends(require_role(["admin"])),
):
    """【管理员】审批 tag_validator 申请（approved/rejected）。批准后自动升级用户角色。"""
    valid = {"approved", "rejected"}
    if body.decision not in valid:
        raise HTTPException(status_code=400, detail=f"decision 必须是 {valid} 之一")

    app = await TagValidatorApplication.get_or_none(id=app_id).prefetch_related("applicant")
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    if app.status != 'pending':
        raise HTTPException(status_code=400, detail="该申请已经处理过")

    app.status = body.decision
    app.reviewed_by_id = current_user.id
    app.reviewed_at = datetime.now(timezone.utc)
    app.review_note = body.note
    await app.save()

    applicant = app.applicant
    if body.decision == 'approved':
        # 升级用户角色
        applicant.role = 'tag_validator'
        await applicant.save(update_fields=["role"])
        from app.services.meili_sync import sync_user_to_meili
        await sync_user_to_meili(applicant)
        notice = f"恭喜！您的打标验证员申请已通过。" + (f" 管理员说明：{body.note}" if body.note else "")
    else:
        notice = f"您的打标验证员申请未通过。" + (f" 说明：{body.note}" if body.note else "")

    await Notification.create(
        user_id=applicant.id,
        actor_id=current_user.id,
        type="system",
        content=notice,
        related_entity_id=str(app.id),
    )

    return ResponseBase(data={"message": f"Application {body.decision}", "applicant_id": applicant.id})


# ──────────────────────────────────────────────────────────────────────
# 标签 i18n 翻译系统
# ──────────────────────────────────────────────────────────────────────

SUPPORTED_LOCALES = {"zh", "ja", "en", "ko", "zh-TW"}


class TranslationSubmitRequest(BaseModel):
    locale: str = Field(..., max_length=10)
    translated_name: str = Field(..., min_length=1, max_length=200)


class TranslationApproveRequest(BaseModel):
    translated_name: Optional[str] = Field(None, max_length=200)


@router.get("/translations/export", response_model=ResponseBase[Dict[str, Any]])
async def export_translations(
    status: str = Query("approved", pattern="^(approved|pending|all)$"),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """
    导出所有标签翻译，返回 Crowdin 兼容 JSON。

    格式：
    ```json
    {
      "一原みか": {"zh": "一原美香", "en": "Ichihara Mika"},
      "碧蓝档案": {"zh": "碧蓝档案", "ja": "ブルーアーカイブ"}
    }
    ```
    """
    qs = TagTranslation.all()
    if status != "all":
        qs = qs.filter(status=status)
    records = await qs.values("tag_name", "locale", "translated_name", "status")
    result: Dict[str, Dict[str, str]] = {}
    for r in records:
        result.setdefault(r["tag_name"], {})[r["locale"]] = r["translated_name"]
    return ResponseBase(data=result)


@router.post("/translations/import", response_model=ResponseBase[dict])
async def import_translations(
    translations: Dict[str, Dict[str, str]] = Body(..., description="Crowdin JSON 格式翻译数据"),
    auto_approve: bool = Query(True),
    current_user: User = Depends(require_role(["admin"])),
):
    """
    批量导入翻译（管理员）。接受 Crowdin 导出格式：
    `{"tag_name": {"zh": "...", "en": "..."}}`
    """
    created = updated = skipped = 0
    now = datetime.now(timezone.utc)

    for tag_name, locales in translations.items():
        for locale, translated_name in locales.items():
            if not translated_name.strip():
                continue
            existing = await TagTranslation.get_or_none(tag_name=tag_name, locale=locale)
            if existing:
                if existing.translated_name != translated_name:
                    existing.translated_name = translated_name
                    if auto_approve:
                        existing.status = "approved"
                        existing.approved_by_id = current_user.id
                        existing.approved_at = now
                    await existing.save()
                    updated += 1
                else:
                    skipped += 1
            else:
                await TagTranslation.create(
                    tag_name=tag_name,
                    locale=locale,
                    translated_name=translated_name,
                    status="approved" if auto_approve else "pending",
                    submitted_by_id=current_user.id,
                    approved_by_id=current_user.id if auto_approve else None,
                    approved_at=now if auto_approve else None,
                )
                created += 1

    return ResponseBase(data={"created": created, "updated": updated, "skipped": skipped})


@router.get("/{tag_name}/translations", response_model=ResponseBase[List[dict]])
async def get_tag_translations(tag_name: str):
    """获取某标签的所有翻译（已批准 + 待审）"""
    records = await TagTranslation.filter(tag_name=tag_name).prefetch_related("submitted_by").order_by("locale")
    result = []
    for r in records:
        try: submitter = r.submitted_by.username if r.submitted_by_id else None
        except Exception: submitter = None
        result.append({
            "id": r.id,
            "locale": r.locale,
            "translated_name": r.translated_name,
            "status": r.status,
            "submitted_by": submitter,
            "approved_at": r.approved_at.isoformat() if r.approved_at else None,
            "created_at": r.created_at.isoformat(),
        })
    return ResponseBase(data=result)


@router.post("/{tag_name}/translations", response_model=ResponseBase[dict])
async def submit_translation(
    tag_name: str,
    body: TranslationSubmitRequest,
    current_user: User = Depends(get_current_user),
):
    """
    社区提交标签翻译（任意登录用户）。
    - 已有待审/批准记录时：更新 translated_name 并重置为 pending（供重新审核）
    - 管理员/tag_validator 提交时：直接批准
    """
    if body.locale not in SUPPORTED_LOCALES:
        raise HTTPException(status_code=400, detail=f"不支持的语言代码，允许：{SUPPORTED_LOCALES}")

    auto_approve = current_user.role in ("admin", "tag_validator")
    now = datetime.now(timezone.utc)

    existing = await TagTranslation.get_or_none(tag_name=tag_name, locale=body.locale)
    if existing:
        if existing.status == "approved" and not auto_approve:
            raise HTTPException(status_code=409, detail="该翻译已批准，如需修改请联系打标验证员")
        existing.translated_name = body.translated_name
        existing.submitted_by_id = current_user.id
        existing.status = "approved" if auto_approve else "pending"
        if auto_approve:
            existing.approved_by_id = current_user.id
            existing.approved_at = now
        await existing.save()
        return ResponseBase(data={"message": "翻译已更新", "status": existing.status})

    record = await TagTranslation.create(
        tag_name=tag_name,
        locale=body.locale,
        translated_name=body.translated_name,
        status="approved" if auto_approve else "pending",
        submitted_by_id=current_user.id,
        approved_by_id=current_user.id if auto_approve else None,
        approved_at=now if auto_approve else None,
    )
    return ResponseBase(data={"message": "翻译已提交" + ("并自动批准" if auto_approve else "，等待审核"), "id": record.id, "status": record.status})


@router.patch("/{tag_name}/translations/{locale}/approve", response_model=ResponseBase[dict])
async def approve_translation(
    tag_name: str,
    locale: str,
    body: TranslationApproveRequest = TranslationApproveRequest(),
    current_user: User = Depends(require_role(["admin", "tag_validator"])),
):
    """批准（或修改后批准）标签翻译"""
    record = await TagTranslation.get_or_none(tag_name=tag_name, locale=locale)
    if not record:
        raise HTTPException(status_code=404, detail="翻译记录不存在")
    if body.translated_name:
        record.translated_name = body.translated_name
    record.status = "approved"
    record.approved_by_id = current_user.id
    record.approved_at = datetime.now(timezone.utc)
    await record.save()
    return ResponseBase(data={"message": "翻译已批准", "translated_name": record.translated_name})


@router.get("/translations/pending", response_model=ResponseBase[List[dict]])
async def list_pending_translations(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(require_role(["admin", "tag_validator"])),
):
    """列出待审翻译（供 tag_validator/admin 批量审核）"""
    records = await TagTranslation.filter(status="pending").prefetch_related("submitted_by").order_by("-created_at").offset(offset).limit(limit)
    result = []
    for r in records:
        try: submitter = r.submitted_by.username if r.submitted_by_id else None
        except Exception: submitter = None
        result.append({
            "id": r.id,
            "tag_name": r.tag_name,
            "locale": r.locale,
            "translated_name": r.translated_name,
            "submitted_by": submitter,
            "created_at": r.created_at.isoformat(),
        })
    return ResponseBase(data=result)

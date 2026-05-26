from fastapi import APIRouter, Depends, HTTPException, Request, Query
from typing import List
from pydantic import BaseModel, Field
from typing import Optional
from app.api.dependencies import get_current_user, require_role
from app.models.user import User
from app.models.report import ArtworkReport, UserReport, CommentReport
from app.models.interaction import Comment
from app.models.artwork import Artwork
from app.schemas.common import ResponseBase
from app.middleware.rate_limit import rate_limit

router = APIRouter()


class ReportCreate(BaseModel):
    # spam | inappropriate | copyright | ai_mislabeled | other
    reason: str = Field(..., max_length=50)
    description: Optional[str] = Field(None, max_length=1000)


class ReportReviewRequest(BaseModel):
    # reviewed | dismissed | actioned
    status: str = Field(..., max_length=50)
    note: Optional[str] = Field(None, max_length=1000)


class AppealSubmitRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


class AppealReviewRequest(BaseModel):
    # accepted | rejected
    decision: str = Field(..., max_length=50)
    note: Optional[str] = Field(None, max_length=1000)


@router.post("/comments/{comment_id}", response_model=ResponseBase[dict])
async def report_comment(
    request: Request,
    comment_id: int,
    body: ReportCreate,
    current_user: User = Depends(get_current_user),
):
    """举报评论（每人对同一评论只能举报一次）"""
    await rate_limit(request, "report")
    if not await Comment.exists(id=comment_id):
        raise HTTPException(status_code=404, detail="Comment not found")

    valid_reasons = {"pornographic", "hostile", "privacy", "minors", "ads", "political", "rumor", "spam", "other"}
    if body.reason not in valid_reasons:
        raise HTTPException(status_code=400, detail=f"reason 必须是 {valid_reasons} 之一")

    exists = await CommentReport.exists(reporter_id=current_user.id, comment_id=comment_id)
    if exists:
        raise HTTPException(status_code=409, detail="You have already reported this comment")
    await CommentReport.create(reporter_id=current_user.id, comment_id=comment_id, reason=body.reason, description=body.description)
    return ResponseBase(data={"message": "Comment report submitted"})


@router.post("/artworks/{artwork_id}", response_model=ResponseBase[dict])
async def report_artwork(
    request: Request,
    artwork_id: int,
    body: ReportCreate,
    current_user: User = Depends(get_current_user),
):
    """举报作品（每人对同一作品只能举报一次）"""
    await rate_limit(request, "report")
    if not await Artwork.exists(id=artwork_id):
        raise HTTPException(status_code=404, detail="Artwork not found")

    valid_reasons = {"spam", "inappropriate", "copyright", "ai_mislabeled", "other"}
    if body.reason not in valid_reasons:
        raise HTTPException(status_code=400, detail=f"reason 必须是 {valid_reasons} 之一")

    _, created = await ArtworkReport.get_or_create(
        reporter_id=current_user.id,
        artwork_id=artwork_id,
        defaults={"reason": body.reason, "description": body.description},
    )
    if not created:
        raise HTTPException(status_code=409, detail="You have already reported this artwork")

    return ResponseBase(data={"message": "Report submitted"})


@router.post("/users/{user_id}", response_model=ResponseBase[dict])
async def report_user(
    request: Request,
    user_id: int,
    body: ReportCreate,
    current_user: User = Depends(get_current_user),
):
    """举报用户（每人对同一用户只能举报一次）"""
    await rate_limit(request, "report")
    if not await User.exists(id=user_id):
        raise HTTPException(status_code=404, detail="User not found")
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="不能举报自己")

    valid_reasons = {"harassment", "spam", "impersonation", "inappropriate", "other"}
    if body.reason not in valid_reasons:
        raise HTTPException(status_code=400, detail=f"reason 必须是 {valid_reasons} 之一")

    _, created = await UserReport.get_or_create(
        reporter_id=current_user.id,
        reported_user_id=user_id,
        defaults={"reason": body.reason, "description": body.description},
    )
    if not created:
        raise HTTPException(status_code=409, detail="You have already reported this user")

    return ResponseBase(data={"message": "User report submitted"})


@router.get("/pending", response_model=ResponseBase[List[dict]])
async def get_pending_reports(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(require_role(["admin", "moderator"])),
):
    """【管理员/审核员】获取待处理的举报列表"""
    reports = await (
        ArtworkReport.filter(status="pending")
        .order_by("created_at")
        .offset(offset)
        .limit(limit)
        .values(
            "id", "artwork_id", "reporter_id",
            "reason", "description", "created_at",
        )
    )
    return ResponseBase(data=reports)


@router.post("/{report_id}/review", response_model=ResponseBase[dict])
async def review_report(
    report_id: int,
    body: ReportReviewRequest,
    current_user: User = Depends(require_role(["admin", "moderator"])),
):
    """【管理员/审核员】裁决或重新裁决举报（dismissed / actioned）"""
    report = await ArtworkReport.get_or_none(id=report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    valid_statuses = {"reviewed", "dismissed", "actioned"}
    if body.status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"status 必须是 {valid_statuses} 之一")

    from datetime import datetime, timezone
    prev_status = report.status
    report.status = body.status
    report.reviewed_by_id = current_user.id
    report.reviewed_at = datetime.now(timezone.utc)
    if body.note is not None:
        report.admin_note = body.note
    await report.save()

    # 若 actioned（执行处置），隐藏作品
    if body.status == "actioned":
        await Artwork.filter(id=report.artwork_id).update(visibility="private")
    # 若从 actioned 改为 dismissed（重新裁决撤销处置），恢复作品可见性
    elif prev_status == "actioned" and body.status == "dismissed":
        await Artwork.filter(id=report.artwork_id).update(visibility="public")
        # 通知作品作者
        from app.services.notification_service import push_notification
        artwork = await Artwork.get_or_none(id=report.artwork_id)
        if artwork:
            await push_notification(
                user_id=artwork.author_id,
                actor_id=current_user.id,
                type="system",
                content="经重新审核，针对你作品的举报已被撤销，作品已恢复公开。",
                related_entity_id=str(artwork.id),
            )

    return ResponseBase(data={"message": f"Report {body.status}"})


@router.post("/{report_id}/appeal", response_model=ResponseBase[dict])
async def submit_appeal(
    report_id: int,
    body: AppealSubmitRequest,
    current_user: User = Depends(get_current_user),
):
    """【作品作者】对举报裁决提出申诉"""
    report = await ArtworkReport.get_or_none(id=report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    # 只有作品作者可以申诉
    artwork = await Artwork.get_or_none(id=report.artwork_id)
    if not artwork or artwork.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="只有作品作者可以申诉")
    # 只有已裁决的举报才能申诉
    if report.status not in ("actioned", "reviewed"):
        raise HTTPException(status_code=400, detail="只有已裁决的举报才能申诉")
    # 每个举报只能申诉一次
    if report.appeal_status is not None:
        raise HTTPException(status_code=409, detail="已提交过申诉")

    from datetime import datetime, timezone
    report.appeal_text = body.text
    report.appeal_submitted_at = datetime.now(timezone.utc)
    report.appeal_status = "pending_appeal"
    await report.save()

    return ResponseBase(data={"message": "申诉已提交，等待审核"})


@router.post("/{report_id}/appeal/review", response_model=ResponseBase[dict])
async def review_appeal(
    report_id: int,
    body: AppealReviewRequest,
    current_user: User = Depends(require_role(["admin", "moderator"])),
):
    """【管理员/审核员】处理申诉（accepted/rejected）"""
    report = await ArtworkReport.get_or_none(id=report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    if report.appeal_status != "pending_appeal":
        raise HTTPException(status_code=400, detail="该举报没有待审核的申诉")

    valid = {"accepted", "rejected"}
    if body.decision not in valid:
        raise HTTPException(status_code=400, detail=f"decision 必须是 {valid} 之一")

    from datetime import datetime, timezone
    report.appeal_status = body.decision
    report.appeal_reviewed_by_id = current_user.id
    report.appeal_reviewed_at = datetime.now(timezone.utc)
    if body.note is not None:
        report.appeal_note = body.note
    await report.save()

    artwork = await Artwork.get_or_none(id=report.artwork_id)
    if body.decision == "accepted" and artwork:
        # 申诉通过 → 恢复作品可见性，将举报状态改为 dismissed
        report.status = "dismissed"
        await report.save()
        await Artwork.filter(id=report.artwork_id).update(visibility="public")
        from app.services.notification_service import push_notification
        await push_notification(
            user_id=artwork.author_id,
            actor_id=current_user.id,
            type="system",
            content=f"你的申诉已通过！作品《{artwork.title}》已恢复公开。" + (f" 管理员说明：{body.note}" if body.note else ""),
            related_entity_id=str(artwork.id),
        )
    elif body.decision == "rejected" and artwork:
        from app.services.notification_service import push_notification
        await push_notification(
            user_id=artwork.author_id,
            actor_id=current_user.id,
            type="system",
            content=f"你对作品《{artwork.title}》的申诉未通过。" + (f" 说明：{body.note}" if body.note else ""),
            related_entity_id=str(artwork.id),
        )

    return ResponseBase(data={"message": f"Appeal {body.decision}"})


@router.get("/my", response_model=ResponseBase[List[dict]])
async def get_my_reports(
    current_user: User = Depends(get_current_user),
):
    """【用户】查看我提交的举报"""
    reports = await ArtworkReport.filter(reporter_id=current_user.id).order_by("-created_at").values(
        "id", "artwork_id", "reason", "description", "status",
        "admin_note", "appeal_status", "appeal_text", "appeal_note", "created_at",
    )
    return ResponseBase(data=reports)


@router.get("/artwork/{artwork_id}/status", response_model=ResponseBase[dict])
async def get_artwork_report_status(
    artwork_id: int,
    current_user: User = Depends(get_current_user),
):
    """【作品作者】查看自己作品上的举报裁决状态（用于申诉入口）"""
    artwork = await Artwork.get_or_none(id=artwork_id)
    if not artwork or artwork.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    report = await ArtworkReport.filter(artwork_id=artwork_id, status="actioned").first()
    if not report:
        return ResponseBase(data={"has_actioned_report": False})
    return ResponseBase(data={
        "has_actioned_report": True,
        "report_id": report.id,
        "admin_note": report.admin_note,
        "appeal_status": report.appeal_status,
        "appeal_note": report.appeal_note,
    })


# ──────────────────────────────────────────────────────────────────────
# UserReport：重新裁决 + 申诉
# ──────────────────────────────────────────────────────────────────────

@router.post("/users/{report_id}/review", response_model=ResponseBase[dict])
async def review_user_report(
    report_id: int,
    body: ReportReviewRequest,
    current_user: User = Depends(require_role(["admin", "moderator"])),
):
    """【管理员/审核员】裁决或重新裁决用户举报（dismissed / actioned）。
    actioned 时可选封禁被举报用户；从 actioned 改为 dismissed 时可选解封。
    """
    report = await UserReport.get_or_none(id=report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    valid_statuses = {"reviewed", "dismissed", "actioned"}
    if body.status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"status 必须是 {valid_statuses} 之一")

    from datetime import datetime, timezone
    prev_status = report.status
    report.status = body.status
    report.reviewed_by_id = current_user.id
    report.reviewed_at = datetime.now(timezone.utc)
    if body.note is not None:
        report.admin_note = body.note
    await report.save()

    from app.services.notification_service import push_notification
    # 若 actioned，通知被举报用户
    if body.status == "actioned":
        await push_notification(
            user_id=report.reported_user_id,
            actor_id=current_user.id,
            type="system",
            content="针对您账号的举报已被管理员裁定属实，请遵守社区规范。" + (f" 说明：{body.note}" if body.note else ""),
            related_entity_id=str(report.id),
        )
    # 若从 actioned 改为 dismissed（重新裁决撤销），通知被举报用户
    elif prev_status == "actioned" and body.status == "dismissed":
        await push_notification(
            user_id=report.reported_user_id,
            actor_id=current_user.id,
            type="system",
            content="经重新审核，针对您账号的举报已被撤销。" + (f" 说明：{body.note}" if body.note else ""),
            related_entity_id=str(report.id),
        )

    return ResponseBase(data={"message": f"User report {body.status}"})


@router.post("/users/{report_id}/appeal", response_model=ResponseBase[dict])
async def submit_user_report_appeal(
    report_id: int,
    body: AppealSubmitRequest,
    current_user: User = Depends(get_current_user),
):
    """【被举报用户】对用户举报裁决提出申诉（只有已裁决且未申诉的举报可申诉）"""
    report = await UserReport.get_or_none(id=report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    # 只有被举报用户本人可以申诉
    if report.reported_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="只有被举报用户可以申诉")
    # 只有已裁决的举报才能申诉
    if report.status not in ("actioned", "reviewed"):
        raise HTTPException(status_code=400, detail="只有已裁决的举报才能申诉")
    # 每个举报只能申诉一次
    if report.appeal_status is not None:
        raise HTTPException(status_code=409, detail="已提交过申诉")

    from datetime import datetime, timezone
    report.appeal_text = body.text
    report.appeal_submitted_at = datetime.now(timezone.utc)
    report.appeal_status = "pending_appeal"
    await report.save()

    return ResponseBase(data={"message": "申诉已提交，等待审核"})


@router.post("/users/{report_id}/appeal/review", response_model=ResponseBase[dict])
async def review_user_report_appeal(
    report_id: int,
    body: AppealReviewRequest,
    current_user: User = Depends(require_role(["admin", "moderator"])),
):
    """【管理员/审核员】处理用户举报申诉（accepted/rejected）"""
    report = await UserReport.get_or_none(id=report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    if report.appeal_status != "pending_appeal":
        raise HTTPException(status_code=400, detail="该举报没有待审核的申诉")

    valid = {"accepted", "rejected"}
    if body.decision not in valid:
        raise HTTPException(status_code=400, detail=f"decision 必须是 {valid} 之一")

    from datetime import datetime, timezone
    report.appeal_status = body.decision
    report.appeal_reviewed_by_id = current_user.id
    report.appeal_reviewed_at = datetime.now(timezone.utc)
    if body.note is not None:
        report.appeal_note = body.note
    await report.save()

    from app.services.notification_service import push_notification
    if body.decision == "accepted":
        # 申诉通过 → 将举报状态改为 dismissed，通知被举报用户
        report.status = "dismissed"
        await report.save()
        await push_notification(
            user_id=report.reported_user_id,
            actor_id=current_user.id,
            type="system",
            content="您的申诉已通过！针对您账号的举报已撤销。" + (f" 管理员说明：{body.note}" if body.note else ""),
            related_entity_id=str(report.id),
        )
    else:
        await push_notification(
            user_id=report.reported_user_id,
            actor_id=current_user.id,
            type="system",
            content="您的申诉未通过。" + (f" 说明：{body.note}" if body.note else ""),
            related_entity_id=str(report.id),
        )

    return ResponseBase(data={"message": f"User report appeal {body.decision}"})

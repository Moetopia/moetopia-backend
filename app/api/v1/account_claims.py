"""
账号认领 API — 允许普通用户申请认领导入账号（如 Pixiv 导入的作者账号）。
认领流程：captcha 验证 → 邮箱 OTP 验证 → 提交申请 → 管理员审核 → 批准后迁移作品/系列
"""
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.dependencies import get_current_user
from app.models.user import User
from app.schemas.common import ResponseBase

router = APIRouter()


# ──────────────────────────────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────────────────────────────

class ClaimRequestCreate(BaseModel):
    imported_user_id: int
    email_code: str = Field(..., min_length=6, max_length=6)
    captcha_verified_token: Optional[str] = None


class ClaimRequestResponse(BaseModel):
    id: int
    imported_user_id: int
    imported_username: Optional[str] = None
    claimant_id: int
    status: str
    admin_note: Optional[str] = None
    created_at: datetime
    resolved_at: Optional[datetime] = None


# ──────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────

@router.post("", response_model=ResponseBase[ClaimRequestResponse])
async def submit_claim_request(
    body: ClaimRequestCreate,
    current_user: User = Depends(get_current_user),
):
    """
    提交认领申请（需登录）。
    前置条件：
    1. captcha 人机验证
    2. 当前账号邮箱的 OTP 验证码
    3. 目标账号必须是 is_imported=True
    4. 该导入账号当前没有 pending 申请
    """
    from app.api.v1.captcha import check_captcha
    from app.api.v1.auth import verify_and_consume_code
    from app.models.account_claim import AccountClaimRequest

    await check_captcha(body.captcha_verified_token)
    await verify_and_consume_code(current_user.email, "account_claim", body.email_code)

    # 验证目标是导入账号
    target = await User.get_or_none(id=body.imported_user_id)
    if not target or not target.is_imported:
        raise HTTPException(status_code=400, detail="目标账号不是可认领的导入账号")

    # 不能认领自己
    if target.id == current_user.id:
        raise HTTPException(status_code=400, detail="无法认领自己的账号")

    # 同一导入账号只能有一个 pending 申请
    existing = await AccountClaimRequest.filter(
        imported_user_id=body.imported_user_id, status="pending"
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="该账号已有待审核的认领申请")

    # 同一用户不能重复申请同一账号
    my_existing = await AccountClaimRequest.filter(
        imported_user_id=body.imported_user_id, claimant_id=current_user.id
    ).first()
    if my_existing and my_existing.status in ("pending", "approved"):
        raise HTTPException(status_code=409, detail="你已提交过对该账号的认领申请")

    claim = await AccountClaimRequest.create(
        imported_user_id=body.imported_user_id,
        claimant_id=current_user.id,
    )

    return ResponseBase(data=ClaimRequestResponse(
        id=claim.id,
        imported_user_id=claim.imported_user_id,
        imported_username=target.username,
        claimant_id=claim.claimant_id,
        status=claim.status,
        admin_note=claim.admin_note,
        created_at=claim.created_at,
        resolved_at=claim.resolved_at,
    ))


@router.get("/me", response_model=ResponseBase[list])
async def get_my_claim_requests(
    current_user: User = Depends(get_current_user),
):
    """查询当前用户提交的所有认领申请"""
    from app.models.account_claim import AccountClaimRequest
    claims = await AccountClaimRequest.filter(
        claimant_id=current_user.id
    ).order_by("-created_at").prefetch_related("imported_user")

    result = []
    for c in claims:
        try:
            imported_username = c.imported_user.username
        except Exception:
            imported_username = None
        result.append({
            "id": c.id,
            "imported_user_id": c.imported_user_id,
            "imported_username": imported_username,
            "status": c.status,
            "admin_note": c.admin_note,
            "created_at": c.created_at.isoformat(),
            "resolved_at": c.resolved_at.isoformat() if c.resolved_at else None,
        })
    return ResponseBase(data=result)

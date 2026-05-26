"""
会员体系 API（用户侧）
- GET  /api/v1/membership/plans      公开：所有激活档位
- GET  /api/v1/membership/my         需登录：当前激活订阅
- POST /api/v1/membership/subscribe  需登录：demo 订阅
- POST /api/v1/membership/cancel     需登录：取消续费
"""
import uuid
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.dependencies import get_current_user, get_optional_user
from app.models.user import User
from app.models.membership_plan import MembershipPlan
from app.models.user_membership import UserMembership
from app.schemas.common import ResponseBase

router = APIRouter()


# ── 工具 ─────────────────────────────────────────────────────────────────────

async def get_active_membership(user: User) -> UserMembership | None:
    """返回用户当前有效的订阅，无则返回 None。"""
    now = datetime.now(timezone.utc)
    return await UserMembership.filter(
        user_id=user.id,
        status="active",
        expires_at__gt=now,
    ).prefetch_related("plan").first()


# ── 端点 ─────────────────────────────────────────────────────────────────────

@router.get("/plans", response_model=ResponseBase[list])
async def list_plans():
    """公开：返回所有激活档位。"""
    plans = await MembershipPlan.filter(is_active=True).order_by("sort_order", "id")
    return ResponseBase(data=[
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "monthly_price": float(p.monthly_price),
            "quarterly_price": float(p.quarterly_price) if p.quarterly_price is not None else None,
            "semi_annual_price": float(p.semi_annual_price) if p.semi_annual_price is not None else None,
            "yearly_price": float(p.yearly_price) if p.yearly_price is not None else None,
            "permissions": p.permissions,
            "sort_order": p.sort_order,
        }
        for p in plans
    ])


@router.get("/my", response_model=ResponseBase[dict])
async def my_membership(current_user: User = Depends(get_current_user)):
    """需登录：返回当前激活的订阅信息。"""
    sub = await get_active_membership(current_user)
    if not sub:
        return ResponseBase(data={"active": False})
    return ResponseBase(data={
        "active": True,
        "plan": {
            "id": sub.plan.id,
            "name": sub.plan.name,
            "permissions": sub.plan.permissions,
        },
        "status": sub.status,
        "started_at": sub.started_at.isoformat(),
        "expires_at": sub.expires_at.isoformat(),
        "payment_ref": sub.payment_ref,
    })


class SubscribeRequest(BaseModel):
    plan_id: int
    period: str = Field("monthly", pattern="^(monthly|quarterly|semi_annual|yearly)$")


@router.post("/subscribe", response_model=ResponseBase[dict])
async def subscribe(
    body: SubscribeRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Demo 模式：立即激活会员，生成 mock 订单号。
    period=monthly → +30 天；period=yearly → +365 天。
    """
    plan = await MembershipPlan.get_or_none(id=body.plan_id, is_active=True)
    if not plan:
        raise HTTPException(status_code=404, detail="会员档位不存在或已下线")

    now = datetime.now(timezone.utc)
    days_map = {"monthly": 30, "quarterly": 90, "semi_annual": 180, "yearly": 365}
    days = days_map.get(body.period, 30)
    expires = now + timedelta(days=days)
    ref = f"DEMO-{body.period.upper()[:2]}-{uuid.uuid4().hex[:10].upper()}"

    existing = await get_active_membership(current_user)
    if existing:
        existing.status = "cancelled"
        await existing.save(update_fields=["status"])

    sub = await UserMembership.create(
        user_id=current_user.id,
        plan_id=plan.id,
        status="active",
        started_at=now,
        expires_at=expires,
        payment_ref=ref,
    )
    return ResponseBase(data={
        "plan_id": plan.id,
        "plan_name": plan.name,
        "period": body.period,
        "expires_at": expires.isoformat(),
        "payment_ref": ref,
    })


@router.post("/cancel", response_model=ResponseBase[dict])
async def cancel_membership(current_user: User = Depends(get_current_user)):
    """取消续费：将当前激活订阅标记为 cancelled（到期时间不变）。"""
    sub = await get_active_membership(current_user)
    if not sub:
        raise HTTPException(status_code=404, detail="当前没有激活的会员订阅")
    sub.status = "cancelled"
    await sub.save(update_fields=["status"])
    return ResponseBase(data={"expires_at": sub.expires_at.isoformat()})

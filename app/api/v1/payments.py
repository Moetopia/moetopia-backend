"""
支付网关（演示级实现）
- 不接入真实支付渠道
- 所有支付均立即成功，生成 mock 交易号
- 端点：POST /pay/{id}  GET /pay/{id}
"""
import uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.dependencies import get_current_user
from app.models.user import User
from app.models.commission import Commission
from app.models.payment import PaymentRecord
from app.services.notification_service import push_notification
from app.schemas.common import ResponseBase

router = APIRouter()


class PayRequest(BaseModel):
    method: str = Field(..., pattern="^(demo|wechat|alipay)$")


@router.post("/{commission_id}", response_model=ResponseBase[dict])
async def mock_pay(
    commission_id: int,
    body: PayRequest,
    current_user: User = Depends(get_current_user),
):
    """演示支付：立即标记为已支付，生成 mock 交易号"""
    commission = await Commission.get_or_none(id=commission_id, client_id=current_user.id)
    if not commission:
        raise HTTPException(status_code=404, detail="Commission not found")
    if commission.payment_status == "paid":
        raise HTTPException(status_code=400, detail="Already paid")

    existing = await PaymentRecord.filter(commission_id=commission_id).first()
    if existing:
        commission.payment_status = "paid"
        await commission.save()
        return ResponseBase(data={
            "transaction_id": existing.transaction_id,
            "amount": float(commission.price),
            "method": existing.method,
            "paid_at": existing.paid_at.isoformat() if existing.paid_at else None,
        })

    method_labels = {"demo": "演示支付", "wechat": "微信支付", "alipay": "支付宝"}
    tx_id = f"MOCK-{body.method.upper()[:2]}-{uuid.uuid4().hex[:12].upper()}"
    now = datetime.now(timezone.utc)

    await PaymentRecord.create(
        commission_id=commission_id,
        payer_id=current_user.id,
        amount=commission.price,
        method=body.method,
        status="paid",
        transaction_id=tx_id,
        paid_at=now,
    )
    commission.payment_status = "paid"
    await commission.save()

    await push_notification(
        user_id=commission.creator_id,
        actor_id=current_user.id,
        type="commission",
        content=f"约稿《{commission.title}》客户已通过{method_labels.get(body.method, body.method)}完成支付，等待你接单",
        related_entity_id=str(commission.id),
    )

    return ResponseBase(data={
        "transaction_id": tx_id,
        "amount": float(commission.price),
        "method": body.method,
        "paid_at": now.isoformat(),
    })


@router.get("/{commission_id}", response_model=ResponseBase[dict])
async def get_payment_status(
    commission_id: int,
    current_user: User = Depends(get_current_user),
):
    """获取约稿支付状态（双方均可查询）"""
    commission = await Commission.get_or_none(id=commission_id)
    if not commission:
        raise HTTPException(status_code=404, detail="Commission not found")
    if current_user.id not in (commission.client_id, commission.creator_id):
        raise HTTPException(status_code=403, detail="Access denied")

    record = await PaymentRecord.get_or_none(commission_id=commission_id)
    return ResponseBase(data={
        "payment_status": commission.payment_status,
        "amount": float(commission.price),
        "method": record.method if record else None,
        "transaction_id": record.transaction_id if record else None,
        "paid_at": record.paid_at.isoformat() if record and record.paid_at else None,
    })

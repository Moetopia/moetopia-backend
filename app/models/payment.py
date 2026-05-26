from tortoise import fields, models


class PaymentRecord(models.Model):
    """约稿支付记录（演示级实现，不接入真实支付网关）"""
    id = fields.IntField(pk=True)
    commission = fields.ForeignKeyField('models.Commission', related_name='payment_record', unique=True)
    payer = fields.ForeignKeyField('models.User', related_name='payment_records')
    amount = fields.DecimalField(max_digits=10, decimal_places=2)
    # demo | wechat | alipay
    method = fields.CharField(max_length=20)
    # pending | paid | refunded
    status = fields.CharField(max_length=20, default='pending')
    transaction_id = fields.CharField(max_length=100, null=True)
    paid_at = fields.DatetimeField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "payment_records"

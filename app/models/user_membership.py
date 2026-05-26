from tortoise import fields, models


class UserMembership(models.Model):
    """用户会员订阅记录"""
    id = fields.IntField(pk=True)
    user = fields.ForeignKeyField('models.User', related_name='memberships')
    plan = fields.ForeignKeyField('models.MembershipPlan', related_name='subscriptions')
    # active | expired | cancelled
    status = fields.CharField(max_length=20, default='active')
    started_at = fields.DatetimeField()
    expires_at = fields.DatetimeField()
    # 预留真实支付订单号
    payment_ref = fields.CharField(max_length=200, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "user_memberships"
        ordering = ["-created_at"]

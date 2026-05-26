from tortoise import fields, models


class MembershipPlan(models.Model):
    """会员档位（由管理员动态配置）"""
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=100)
    description = fields.TextField(default="")
    monthly_price = fields.DecimalField(max_digits=10, decimal_places=2)
    quarterly_price = fields.DecimalField(max_digits=10, decimal_places=2, null=True)
    semi_annual_price = fields.DecimalField(max_digits=10, decimal_places=2, null=True)
    yearly_price = fields.DecimalField(max_digits=10, decimal_places=2, null=True)
    # {"translation": true, ...}
    permissions = fields.JSONField(default=dict)
    is_active = fields.BooleanField(default=True)
    sort_order = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "membership_plans"
        ordering = ["sort_order", "id"]

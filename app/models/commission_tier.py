from tortoise import fields, models


class CommissionTier(models.Model):
    """
    画师约稿档位：每个认证画师可创建多个固定价位套餐，
    或开启「自定义金额」并设置最低金额。
    """
    id      = fields.IntField(pk=True)
    creator = fields.ForeignKeyField('models.User', related_name='commission_tiers', on_delete=fields.CASCADE)

    title       = fields.CharField(max_length=100)
    description = fields.TextField(null=True)
    price       = fields.DecimalField(max_digits=10, decimal_places=2)

    # 是否允许买家自定义金额（高于 price 即可）
    allow_custom_amount = fields.BooleanField(default=False)
    # 买家自定义金额时的最低限额（None 表示以 price 为下限）
    min_custom_amount   = fields.DecimalField(max_digits=10, decimal_places=2, null=True)

    sort_order = fields.IntField(default=0)
    is_active  = fields.BooleanField(default=True)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "commission_tiers"

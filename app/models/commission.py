from tortoise import fields, models


class Commission(models.Model):
    id = fields.IntField(pk=True)
    client = fields.ForeignKeyField('models.User', related_name='commission_requests')
    creator = fields.ForeignKeyField('models.User', related_name='commissions')

    title = fields.CharField(max_length=200)
    description = fields.TextField()
    price = fields.DecimalField(max_digits=10, decimal_places=2)

    # 关联到画师档位（可选，自定义金额约稿时为空）
    tier = fields.ForeignKeyField('models.CommissionTier', related_name='commissions', null=True, on_delete=fields.SET_NULL)

    # pending, accepted, in_progress, revision_requested, completed, rejected, cancelled
    status = fields.CharField(max_length=50, default='pending')

    # unpaid | paid | refunded（新约稿默认 unpaid，迁移旧数据时填 paid）
    payment_status = fields.CharField(max_length=20, default='unpaid')

    # 最大允许修改次数（创建时约定）
    max_revisions = fields.IntField(default=3)

    deadline = fields.DatetimeField(null=True)

    # 交付的艺术品 ID（整数型，关联 Artwork.id）
    delivered_artwork_id = fields.IntField(null=True)

    # 私密交付文件（路径 + 原始文件名，仅双方可访问）
    delivered_file_url = fields.CharField(max_length=500, null=True)
    delivered_file_name = fields.CharField(max_length=500, null=True)

    # 画师备注（接单或拒单时填写）
    creator_note = fields.TextField(null=True)

    # 取消/终止原因（客户取消或画师终止时填写）
    cancelled_reason = fields.TextField(null=True)
    # 终止方：client | creator
    terminated_by = fields.CharField(max_length=20, null=True)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "commissions"

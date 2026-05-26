from tortoise import fields, models


class AccountClaimRequest(models.Model):
    """用户申请认领导入账号的请求"""
    id = fields.IntField(pk=True)

    imported_user = fields.ForeignKeyField(
        'models.User', related_name='claim_requests_received',
        description="被认领的导入账号"
    )
    claimant = fields.ForeignKeyField(
        'models.User', related_name='claim_requests_sent',
        description="发起认领的正常用户"
    )

    status = fields.CharField(max_length=20, default='pending')  # pending | approved | rejected
    admin_note = fields.TextField(null=True)

    created_at = fields.DatetimeField(auto_now_add=True)
    resolved_at = fields.DatetimeField(null=True)

    class Meta:
        table = "account_claim_requests"

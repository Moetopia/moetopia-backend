from tortoise import models, fields


class CreatorApplication(models.Model):
    """用户申请成为认证画师的记录"""
    id = fields.IntField(pk=True)
    applicant = fields.ForeignKeyField('models.User', related_name='creator_applications')
    portfolio_url = fields.CharField(max_length=500, null=True)
    reason = fields.TextField()
    # pending | approved | rejected
    status = fields.CharField(max_length=20, default='pending')
    reviewed_by = fields.ForeignKeyField(
        'models.User', related_name='creator_application_reviews', null=True
    )
    reviewed_at = fields.DatetimeField(null=True)
    review_note = fields.TextField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "creator_applications"

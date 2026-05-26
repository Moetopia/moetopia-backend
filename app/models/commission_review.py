from tortoise import models, fields


class CommissionReview(models.Model):
    """客户对约稿的评价/评分"""
    id = fields.IntField(pk=True)
    commission = fields.ForeignKeyField('models.Commission', related_name='reviews', unique=True)
    reviewer = fields.ForeignKeyField('models.User', related_name='reviews_given')
    creator = fields.ForeignKeyField('models.User', related_name='reviews_received')
    rating = fields.IntField()  # 1-5
    comment = fields.TextField(null=True)
    is_anonymous = fields.BooleanField(default=False)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "commission_reviews"

from tortoise import models, fields


class CommissionRevision(models.Model):
    """客户申请的约稿修改记录"""
    id = fields.IntField(pk=True)
    commission = fields.ForeignKeyField('models.Commission', related_name='revisions')
    requested_by = fields.ForeignKeyField('models.User', related_name='revision_requests')
    description = fields.TextField()
    # pending | in_progress | resolved
    status = fields.CharField(max_length=20, default='pending')
    creator_reply = fields.TextField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "commission_revisions"

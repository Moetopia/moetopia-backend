from tortoise import fields, models


class UserReport(models.Model):
    """用户举报"""
    id = fields.IntField(pk=True)
    reporter = fields.ForeignKeyField('models.User', related_name='user_reports_submitted')
    reported_user = fields.ForeignKeyField('models.User', related_name='reports_received')

    # harassment, spam, impersonation, inappropriate, other
    reason = fields.CharField(max_length=50)
    description = fields.TextField(null=True)

    # pending, reviewed, dismissed, actioned
    status = fields.CharField(max_length=20, default='pending')
    reviewed_by = fields.ForeignKeyField('models.User', related_name='user_reports_reviewed', null=True)
    reviewed_at = fields.DatetimeField(null=True)
    admin_note = fields.TextField(null=True)

    # 被举报用户的申诉字段
    appeal_text = fields.TextField(null=True)
    appeal_submitted_at = fields.DatetimeField(null=True)
    # pending_appeal | accepted | rejected
    appeal_status = fields.CharField(max_length=20, null=True)
    appeal_reviewed_by = fields.ForeignKeyField(
        'models.User', related_name='user_report_appeals_reviewed', null=True
    )
    appeal_reviewed_at = fields.DatetimeField(null=True)
    appeal_note = fields.TextField(null=True)

    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "user_reports"
        unique_together = (("reporter", "reported_user"),)


class CommentReport(models.Model):
    """评论举报"""
    id = fields.IntField(pk=True)
    reporter = fields.ForeignKeyField('models.User', related_name='comment_reports_submitted')
    comment_id = fields.IntField()

    # pornographic | hostile | privacy | minors | ads | political | rumor | spam | other
    reason = fields.CharField(max_length=50)
    description = fields.TextField(null=True)

    # pending, reviewed, dismissed, actioned
    status = fields.CharField(max_length=20, default='pending')
    reviewed_by = fields.ForeignKeyField('models.User', related_name='comment_reports_reviewed', null=True)
    reviewed_at = fields.DatetimeField(null=True)
    admin_note = fields.TextField(null=True)

    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "comment_reports"
        unique_together = (("reporter_id", "comment_id"),)


class ArtworkReport(models.Model):
    """作品举报"""
    id = fields.IntField(pk=True)
    reporter = fields.ForeignKeyField('models.User', related_name='reports_submitted')
    artwork = fields.ForeignKeyField('models.Artwork', related_name='reports')

    # spam, inappropriate, copyright, ai_mislabeled, other
    reason = fields.CharField(max_length=50)
    description = fields.TextField(null=True)

    # pending, reviewed, dismissed, actioned
    status = fields.CharField(max_length=20, default='pending')
    reviewed_by = fields.ForeignKeyField('models.User', related_name='reports_reviewed', null=True)
    reviewed_at = fields.DatetimeField(null=True)
    admin_note = fields.TextField(null=True)

    # 用户申诉字段
    appeal_text = fields.TextField(null=True)
    appeal_submitted_at = fields.DatetimeField(null=True)
    # pending_appeal | accepted | rejected | None
    appeal_status = fields.CharField(max_length=20, null=True)
    appeal_reviewed_by = fields.ForeignKeyField('models.User', related_name='appeals_reviewed', null=True)
    appeal_reviewed_at = fields.DatetimeField(null=True)
    appeal_note = fields.TextField(null=True)

    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "artwork_reports"
        unique_together = (("reporter", "artwork"),)

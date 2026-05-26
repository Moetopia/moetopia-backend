from tortoise import fields, models


class Announcement(models.Model):
    """站内公告 / 文章（仅 admin 可发布）"""
    id = fields.IntField(pk=True)
    author = fields.ForeignKeyField('models.User', related_name='announcements')
    title = fields.CharField(max_length=200)
    content = fields.TextField()
    cover_image = fields.CharField(max_length=500, null=True, default=None)
    category = fields.CharField(max_length=50, default='notice')  # notice / event / update
    is_pinned = fields.BooleanField(default=False)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "announcements"
        ordering = ["-is_pinned", "-created_at"]

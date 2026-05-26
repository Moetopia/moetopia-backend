from tortoise import fields, models


class BookmarkFolder(models.Model):
    """收藏夹（类 Pixiv 收藏集）"""
    id = fields.IntField(pk=True)
    user = fields.ForeignKeyField('models.User', related_name='bookmark_folders')
    name = fields.CharField(max_length=100)
    is_private = fields.BooleanField(default=False)
    cover_artwork = fields.ForeignKeyField('models.Artwork', related_name='folder_covers', null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "bookmark_folders"


class Bookmark(models.Model):
    id = fields.IntField(pk=True)
    user = fields.ForeignKeyField('models.User', related_name='bookmarks')
    artwork = fields.ForeignKeyField('models.Artwork', related_name='bookmarked_by')
    folder = fields.ForeignKeyField('models.BookmarkFolder', related_name='bookmarks', null=True)

    is_private = fields.BooleanField(default=False)
    user_custom_tags = fields.JSONField(default=list)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "bookmarks"
        unique_together = (("user", "artwork"),)


class Comment(models.Model):
    id = fields.IntField(pk=True)
    user = fields.ForeignKeyField('models.User', related_name='comments')
    artwork = fields.ForeignKeyField('models.Artwork', related_name='comments')
    content = fields.TextField()

    # 楼中楼支持
    parent = fields.ForeignKeyField('models.Comment', related_name='replies', null=True)
    reply_to = fields.ForeignKeyField('models.User', related_name='received_replies', null=True)

    is_deleted = fields.BooleanField(default=False)
    like_count = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "comments"


class CommentLike(models.Model):
    id = fields.IntField(pk=True)
    user = fields.ForeignKeyField('models.User', related_name='comment_likes')
    comment = fields.ForeignKeyField('models.Comment', related_name='liked_by')
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "comment_likes"
        unique_together = (("user", "comment"),)


class Like(models.Model):
    id = fields.IntField(pk=True)
    user = fields.ForeignKeyField('models.User', related_name='likes')
    artwork = fields.ForeignKeyField('models.Artwork', related_name='liked_by')
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "likes"
        unique_together = (("user", "artwork"),)


class ViewHistory(models.Model):
    id = fields.IntField(pk=True)
    user = fields.ForeignKeyField('models.User', related_name='view_history', null=True)
    artwork = fields.ForeignKeyField('models.Artwork', related_name='views')

    # 记录匿名浏览者的 IP，方便做简单的防刷和访客统计
    ip_address = fields.CharField(max_length=45, null=True)

    viewed_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "view_histories"

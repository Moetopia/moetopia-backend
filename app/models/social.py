from tortoise import fields, models


class Follow(models.Model):
    id = fields.IntField(pk=True)
    follower = fields.ForeignKeyField('models.User', related_name='following')
    followed = fields.ForeignKeyField('models.User', related_name='followers')
    is_private = fields.BooleanField(default=False)  # 非公开关注（普通用户≤100，会员≤1000）
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "follows"
        unique_together = (("follower", "followed"),)


class UserBlock(models.Model):
    """用户拉黑"""
    id = fields.IntField(pk=True)
    blocker = fields.ForeignKeyField('models.User', related_name='blocking')
    blocked = fields.ForeignKeyField('models.User', related_name='blocked_by')
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "user_blocks"
        unique_together = (("blocker", "blocked"),)


class FollowTag(models.Model):
    """关注标签（类 Pixiv 标签订阅）"""
    id = fields.IntField(pk=True)
    user = fields.ForeignKeyField('models.User', related_name='followed_tags')
    tag_name = fields.CharField(max_length=100, index=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "follow_tags"
        unique_together = (("user", "tag_name"),)


class FollowGroup(models.Model):
    """用户自定义关注分组"""
    id = fields.IntField(pk=True)
    user = fields.ForeignKeyField('models.User', related_name='follow_groups')
    name = fields.CharField(max_length=50)
    sort_order = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "follow_groups"
        unique_together = (("user", "name"),)


class FollowGroupMember(models.Model):
    """关注分组成员（必须已关注该用户）"""
    id = fields.IntField(pk=True)
    group = fields.ForeignKeyField('models.FollowGroup', related_name='members')
    followed = fields.ForeignKeyField('models.User', related_name='in_follow_groups')
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "follow_group_members"
        unique_together = (("group", "followed"),)


class Notification(models.Model):
    id = fields.IntField(pk=True)
    user = fields.ForeignKeyField('models.User', related_name='notifications')
    actor = fields.ForeignKeyField('models.User', related_name='caused_notifications', null=True)

    # like, comment, follow, commission, system, tag_validation
    type = fields.CharField(max_length=50)
    content = fields.TextField()
    is_read = fields.BooleanField(default=False)

    # 相关的实体ID，改为 CharField 以兼容 Int (Artwork) 和 UUID (Commission/User)
    related_entity_id = fields.CharField(max_length=255, null=True)

    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "notifications"

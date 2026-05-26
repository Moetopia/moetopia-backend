from tortoise import fields, models


class DirectMessage(models.Model):
    id = fields.IntField(pk=True)
    sender = fields.ForeignKeyField('models.User', related_name='sent_messages')
    recipient = fields.ForeignKeyField('models.User', related_name='received_messages')
    content = fields.TextField(null=True)
    image_url = fields.CharField(max_length=500, null=True)
    is_read = fields.BooleanField(default=False)
    # 可选：关联某个约稿订单，方便在聊天界面显示约稿上下文
    commission = fields.ForeignKeyField('models.Commission', related_name='messages', null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "direct_messages"
        ordering = ["created_at"]

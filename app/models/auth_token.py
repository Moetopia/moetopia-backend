from tortoise import fields, models


class PasswordResetToken(models.Model):
    """密码重置令牌（有效期 1 小时，使用后立即删除）"""
    id = fields.IntField(pk=True)
    user = fields.ForeignKeyField('models.User', related_name='reset_tokens', on_delete=fields.CASCADE)
    token = fields.CharField(max_length=64, unique=True, index=True)
    expires_at = fields.DatetimeField()
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "password_reset_tokens"

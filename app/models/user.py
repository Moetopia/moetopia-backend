from tortoise import fields, models


class User(models.Model):
    id = fields.IntField(pk=True)
    # 登录所用 ID（如 pixiv id，唯一，更改需冷却期+二次验证）—— null=True 以兼容旧数据
    login_id = fields.CharField(max_length=50, unique=True, index=True, null=True)
    # 显示名称（可随意修改，不强制唯一）
    username = fields.CharField(max_length=50, index=True)
    email = fields.CharField(max_length=255, unique=True, index=True)
    password_hash = fields.CharField(max_length=255)
    # login_id 最近修改时间（冒冷期校验）
    login_id_changed_at = fields.DatetimeField(null=True)

    avatar_url = fields.CharField(max_length=500, null=True)
    background_url = fields.CharField(max_length=500, null=True)
    bio = fields.TextField(null=True)
    website_url = fields.CharField(max_length=500, null=True)
    twitter_url = fields.CharField(max_length=500, null=True)

    # 扩展资料
    gender = fields.CharField(max_length=10, null=True)       # male / female / other
    birth_year = fields.IntField(null=True)
    birth_month = fields.IntField(null=True)
    birth_day = fields.IntField(null=True)
    location = fields.CharField(max_length=100, null=True)
    occupation = fields.CharField(max_length=100, null=True)
    social_links = fields.JSONField(default=list)              # [{"platform": "twitter", "handle": "xxx"}]

    # 权限组：'admin', 'tag_validator', 'moderator', 'user'
    role = fields.CharField(max_length=20, default='user')

    # 内容偏好
    r18_enabled = fields.BooleanField(default=False)
    hide_ai_generated = fields.BooleanField(default=False)
    muted_tags = fields.JSONField(default=list)
    muted_user_ids = fields.JSONField(default=list)

    # 个人主页隐私设置
    show_likes_public = fields.BooleanField(default=True)
    show_followers_public = fields.BooleanField(default=True)
    show_following_public = fields.BooleanField(default=True)

    # 创作者设置
    is_creator = fields.BooleanField(default=False)
    commission_enabled = fields.BooleanField(default=False)
    commission_info = fields.TextField(null=True)
    commission_max_revisions = fields.IntField(default=3)

    # 通知偏好：{"like": true, "comment": true, "follow": true, "commission": true, "system": true, "new_artwork": true, "series_update": true}
    notification_prefs = fields.JSONField(default=dict)

    # 账号封禁
    is_banned = fields.BooleanField(default=False)
    banned_reason = fields.CharField(max_length=500, null=True)
    banned_at = fields.DatetimeField(null=True)

    # Token 版本号：封禁或修改密码时 +1，使所有旧 JWT 立即失效
    token_version = fields.IntField(default=0)

    # 翻译偏好
    preferred_translation_lang = fields.CharField(max_length=10, null=True)

    # 导入账号（如 Pixiv 导入作者）
    is_imported = fields.BooleanField(default=False)
    pixiv_user_id = fields.BigIntField(null=True, unique=True, index=True)
    source_platform = fields.CharField(max_length=20, null=True)  # "pixiv" 等

    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "users"
from tortoise import fields, models


class Artwork(models.Model):
    # 修改为自增整数以提供形如 pixiv.net/artworks/123456 的短链接结构
    id = fields.IntField(pk=True)
    # 注意这里的外键关联
    author = fields.ForeignKeyField('models.User', related_name='artworks')

    title = fields.CharField(max_length=200)
    description = fields.TextField(null=True)

    # 标签已拆分为 ArtworkTag 关系表，不再使用 JSONField

    artwork_type = fields.CharField(max_length=20, default='illustration')  # illustration, manga, animated, novel
    is_ai = fields.BooleanField(default=False)
    rating = fields.CharField(max_length=20, default='safe')  # safe, r18, r18g

    # 可见性：public=公开, private=私密(仅自己), followers=仅关注者
    visibility = fields.CharField(max_length=20, default='public')

    view_count = fields.IntField(default=0)
    like_count = fields.IntField(default=0)
    bookmark_count = fields.IntField(default=0)

    allow_ai_tagging = fields.BooleanField(default=True)
    allow_community_tagging = fields.BooleanField(default=True)

    # 来源信息（Pixiv 导入用）
    content_origin = fields.CharField(max_length=20, default='original')  # original, fanart, repost
    moderation_status = fields.CharField(max_length=20, default='approved')  # approved | under_review | rejected
    pixiv_id = fields.IntField(null=True, index=True)
    source = fields.CharField(max_length=500, null=True)
    original_author_name = fields.CharField(max_length=200, null=True)

    scheduled_at = fields.DatetimeField(null=True)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "artworks"


class ArtworkImage(models.Model):
    id = fields.UUIDField(pk=True)
    artwork = fields.ForeignKeyField('models.Artwork', related_name='images', on_delete=fields.CASCADE)

    file_url = fields.CharField(max_length=500)      # 展示用（≤1200px 压缩版）
    original_url = fields.CharField(max_length=500, null=True)  # 原始全分辨率文件（仅会员可下载）
    width = fields.IntField(null=True)
    height = fields.IntField(null=True)
    sort_order = fields.IntField(default=0)

    @property
    def has_original(self) -> bool:
        return bool(self.original_url)

    # 注意：这里没有 vector 字段！AI 提取的 9083 维特征将直接存入 Qdrant。
    # 撞车检测也由 Qdrant 向量相似度完成，无需额外的 pHash 字段。
    # Qdrant 那边的 Payload 中会存入此处的 Image ID，实现跨库绑定。

    class Meta:
        table = "artwork_images"


class ArtworkSeries(models.Model):
    """作品系列（漫画/连载作品集）"""
    id = fields.IntField(pk=True)
    author = fields.ForeignKeyField('models.User', related_name='series')
    title = fields.CharField(max_length=200)
    description = fields.TextField(null=True)
    cover_artwork = fields.ForeignKeyField('models.Artwork', related_name='series_covers', null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "artwork_series"


class ArtworkSeriesItem(models.Model):
    """系列中的单个作品项"""
    id = fields.IntField(pk=True)
    series = fields.ForeignKeyField('models.ArtworkSeries', related_name='items', on_delete=fields.CASCADE)
    artwork = fields.ForeignKeyField('models.Artwork', related_name='series_items')
    order = fields.IntField(default=0)

    class Meta:
        table = "artwork_series_items"
        unique_together = (("series", "artwork"),)


class StyleReference(models.Model):
    """管理员上传的锚点基准图（存入 Qdrant style_refs 集合）
    双重用途：
    1. 风格相似搜索（search/style/{id}）
    2. 锚点基准图解包：新作品向量与锚点相似度超过阈值时，自动注入 work_name/faction_name/character_name 层级标签
    """
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=100)
    description = fields.TextField(null=True)
    file_url = fields.CharField(max_length=500)
    qdrant_id = fields.CharField(max_length=100, unique=True)  # Qdrant 中的点 ID
    uploaded_by = fields.ForeignKeyField('models.User', related_name='style_references', null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    # 锚点基准图解包 — 角色层级标签（留空则仅用于风格搜索，不参与自动打标）
    work_name      = fields.CharField(max_length=100, null=True)   # 如 "碧蓝档案"
    faction_name   = fields.CharField(max_length=100, null=True)   # 如 "FOX小队"（可选）
    character_name = fields.CharField(max_length=100, null=True)   # 如 "音葵"
    tags           = fields.JSONField(default=list, null=True)     # 自定义多标签列表，如 ["角色名", "系列名", ...]
    similarity_threshold = fields.FloatField(default=0.75)         # 触发自动打标的余弦相似度阈值

    class Meta:
        table = "style_references"


class SeriesFollow(models.Model):
    """用户追更系列"""
    id = fields.IntField(pk=True)
    user = fields.ForeignKeyField('models.User', related_name='series_follows', on_delete=fields.CASCADE)
    series = fields.ForeignKeyField('models.ArtworkSeries', related_name='followers', on_delete=fields.CASCADE)
    notify = fields.BooleanField(default=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "series_follows"
        unique_together = (("user", "series"),)
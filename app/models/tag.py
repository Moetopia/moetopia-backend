from tortoise import fields, models


class TagTranslation(models.Model):
    """标签多语言翻译（支持社区提交 + 管理员审批 + Crowdin 导出/导入）"""
    id = fields.IntField(pk=True)
    tag_name = fields.CharField(max_length=100, index=True)  # 原始标签名（通常为日语）
    locale = fields.CharField(max_length=10)                 # zh | ja | en | ko …
    translated_name = fields.CharField(max_length=200)
    # pending | approved
    status = fields.CharField(max_length=20, default='pending')
    submitted_by = fields.ForeignKeyField(
        'models.User', related_name='tag_translations', null=True, on_delete=fields.SET_NULL
    )
    approved_by = fields.ForeignKeyField(
        'models.User', related_name='approved_tag_translations', null=True, on_delete=fields.SET_NULL
    )
    approved_at = fields.DatetimeField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "tag_translations"
        unique_together = (("tag_name", "locale"),)


class ConceptAnchor(models.Model):
    """AI 概念基准库：专门存你官方图的特征，物理隔离防污染"""
    id = fields.IntField(pk=True)
    tag_name = fields.CharField(max_length=100, unique=True, index=True) # 如 "碧蓝档案", "佩丽卡"
    namespace = fields.CharField(max_length=50, default='general')       # copyright, character, etc.
    # We no longer store the fused vector in Postgres, we store it in Qdrant, linking by ID/tag_name
    
    class Meta:
        table = "concept_anchors"

class ArtworkTag(models.Model):
    id = fields.IntField(pk=True)
    artwork = fields.ForeignKeyField('models.Artwork', related_name='tags')
    tag_name = fields.CharField(max_length=100, index=True)
    type = fields.CharField(max_length=50, default='author') # author, ai_unverified, ai_verified
    confidence = fields.FloatField(default=1.0)
    
    upvotes = fields.IntField(default=0)
    downvotes = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)
    
    class Meta:
        table = "artwork_tags"
        unique_together = (("artwork", "tag_name"),)

class TagVote(models.Model):
    id = fields.IntField(pk=True)
    user = fields.ForeignKeyField('models.User', related_name='tag_votes')
    artwork_tag = fields.ForeignKeyField('models.ArtworkTag', related_name='votes')
    is_upvote = fields.BooleanField()
    created_at = fields.DatetimeField(auto_now_add=True)
    
    class Meta:
        table = "tag_votes"
        unique_together = (("user", "artwork_tag"),)


class TagValidatorApplication(models.Model):
    """用户申请成为 tag_validator 的申请记录"""
    id = fields.IntField(pk=True)
    applicant = fields.ForeignKeyField('models.User', related_name='tag_validator_applications')
    # 申请理由（用于管理员判断申请人是否有足够能力）
    reason = fields.TextField()
    # pending | approved | rejected
    status = fields.CharField(max_length=20, default='pending')
    reviewed_by = fields.ForeignKeyField(
        'models.User', related_name='tag_validator_reviews', null=True
    )
    reviewed_at = fields.DatetimeField(null=True)
    # 审批意见（拒绝时说明原因，通过时可留言）
    review_note = fields.TextField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "tag_validator_applications"

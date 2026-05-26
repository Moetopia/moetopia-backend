from tortoise import fields, models


class ArtworkTranslation(models.Model):
    """翻译结果缓存，按 (artwork_id, target_lang) 唯一"""
    id = fields.IntField(pk=True)
    artwork = fields.ForeignKeyField('models.Artwork', related_name='translations')
    target_lang = fields.CharField(max_length=10)
    # pending | processing | done | failed
    status = fields.CharField(max_length=20, default='pending')
    translated_image_url = fields.CharField(max_length=500, null=True)
    requested_by = fields.ForeignKeyField('models.User', related_name='requested_translations', null=True)
    image_index = fields.IntField(default=0)       # which image in the artwork (0-based)
    is_manual = fields.BooleanField(default=False)  # True = author uploaded; False = AI generated
    error_msg = fields.CharField(max_length=500, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "artwork_translations"
        unique_together = [("artwork_id", "target_lang", "image_index")]
        ordering = ["-created_at"]

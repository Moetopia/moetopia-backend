from tortoise import fields, models


class SiteConfig(models.Model):
    """全站配置 — 键值对形式，支持 JSON 值"""
    key   = fields.CharField(max_length=100, unique=True, index=True)
    value = fields.JSONField(null=True)

    class Meta:
        table = "site_configs"

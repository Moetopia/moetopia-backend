from tortoise import fields, models


class CaptchaQuestion(models.Model):
    """自定义验证码题库"""
    question       = fields.TextField()
    question_type  = fields.CharField(max_length=20, default="text")  # "text" | "choice" | "tile"
    answer         = fields.CharField(max_length=200, default="")
    choices        = fields.JSONField(null=True)   # list[str]，仅 choice 类型使用
    tile_images    = fields.JSONField(null=True)   # list[str]，仅 tile 类型使用（图片 URL 列表）
    correct_indices= fields.JSONField(null=True)   # list[int]，tile 类型正确格子的下标
    hint_image     = fields.CharField(max_length=500, null=True)  # tile 类型的提示参考图 URL
    tile_rows      = fields.IntField(default=3)    # 网格行数
    tile_cols      = fields.IntField(default=3)    # 网格列数
    is_active      = fields.BooleanField(default=True)
    created_at    = fields.DatetimeField(auto_now_add=True)
    updated_at    = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "captcha_questions"

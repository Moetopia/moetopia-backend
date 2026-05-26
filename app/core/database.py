"""
Tortoise ORM 配置 — 供 aerich 迁移工具使用。
aerich 命令须在 backend/ 目录下运行，.env 会自动加载。
"""
from app.core.config import settings

TORTOISE_ORM = {
    "connections": {"default": settings.DATABASE_URL},
    "apps": {
        "models": {
            "models": ["app.models", "aerich.models"],
            "default_connection": "default",
        }
    },
}

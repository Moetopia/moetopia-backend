from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    PROJECT_NAME: str
    VERSION: str
    API_V1_STR: str = "/api/v1"

    # 数据库 URL
    DATABASE_URL: str
    MEILI_URL: str
    MEILI_MASTER_KEY: str
    QDRANT_URL: str
    HF_ENDPOINT: str

    SECRET_KEY: str

    # 邮件服务（SMTP）
    SMTP_HOST: str = "smtp.example.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "noreply@moetopia.app"
    SMTP_TLS: bool = True

    # 站点前端地址（生成重置密码链接用）
    FRONTEND_URL: str = "http://localhost:3000"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # CORS 允许的源（逗号分隔字符串，env 中配置；默认允许所有）
    ALLOWED_ORIGINS: list = ["*"]
    LOG_LEVEL: str = "INFO"

    # AI 功能总开关：false 时跳过 WD14 模型加载和 Qdrant 初始化（适合无 GPU / NAS 私有部署）
    ENABLE_AI_FEATURES: bool = True

    # 文件存储后端：local（默认） | s3
    STORAGE_BACKEND: str = "local"
    # S3 兼容存储配置（STORAGE_BACKEND=s3 时生效）
    S3_BUCKET: str = ""
    S3_REGION: str = ""
    S3_ACCESS_KEY_ID: str = ""
    S3_SECRET_ACCESS_KEY: str = ""
    S3_ENDPOINT_URL: str = ""   # 自定义端点，如 MinIO / Cloudflare R2
    S3_BASE_URL: str = ""       # 公开访问基础 URL，如 https://cdn.example.com

    # 首次部署自动创建管理员账号（三项均填写时生效）
    INITIAL_ADMIN_USERNAME: str = ""
    INITIAL_ADMIN_EMAIL: str = ""
    INITIAL_ADMIN_PASSWORD: str = ""

    class Config:
        env_file = ".env"
        # 允许额外字段，防止 pydantic 报错
        extra = "ignore"


settings = Settings()
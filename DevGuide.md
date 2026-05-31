## ⚙️ 环境变量与环境启动指南 (Deployment Guide)

为了让 **Moetopia（萌托邦）** 平台的各个微服务（Backend, Frontend, Pixiv Agent）以及基础设施（PostgreSQL, Redis, Garage 等）正常协同工作，请在启动前按照本指南配置各部分的 `.env` 文件。

---

### 1. 基础设施准备 (Docker Compose)

在启动任何应用服务之前，请确保你已经使用 Docker 启动了所有底层依赖。根据前文的修复，你的 `docker-compose.yml` 中已经包含了完整的数据库、搜索引擎、向量库以及 **Garage S3 存储与 WebUI**。

```bash
# 一键启动所有基础设施
docker compose up -d

```

---

### 2. 后端配置 (Backend `.env`)

在 `backend/` 目录下创建 `.env` 文件（可参考下方的完整生产/开发示例）。

#### 📋 完整的 `.env` 字段详解与模板

```env
# ==========================================
# 1. 基础项目配置
# ==========================================
PROJECT_NAME="Moetopia · 萌托邦"
VERSION="1.0.0"
API_V1_STR="/api/v1"
LOG_LEVEL="INFO"
# 密钥：用于 JWT 签名，生产环境请务必更换为随机长字符串（例如使用 openssl rand -hex 32 生成）
SECRET_KEY="your_super_secret_jwt_key_here"

# ==========================================
# 2. 基础设施连接 URL
# ==========================================
# 关系型数据库（PostgreSQL 18）
DATABASE_URL="postgres://moetopia:moetopia@localhost:5432/moetopia"

# 缓存与任务队列（Redis 7）
REDIS_URL="redis://localhost:6379/0"

# 文本模糊搜索引擎（Meilisearch）
MEILI_URL="http://localhost:7700"
MEILI_MASTER_KEY="moetopia"

# 向量检索数据库（Qdrant）
QDRANT_URL="http://localhost:6333"

# HuggingFace 镜像端点（解决国内下载 WD14 模型网络问题）
# 默认使用 hf-mirror，若在海外可填写 https://huggingface.co
HF_ENDPOINT="https://hf-mirror.com"

# ==========================================
# 3. AI 功能开关 (核心特性)
# ==========================================
# 如果部署在无 GPU、低算力 NAS 或轻量服务器上，请将其设置为 false
# 设置为 false 时，系统将跳过 WD14 模型加载和 Qdrant 向量库的初始化
ENABLE_AI_FEATURES=true

# ==========================================
# 4. 文件存储后端配置 (Local 本地存储 / S3 兼容存储)
# ==========================================
# 可选值：local 或 s3
STORAGE_BACKEND="s3"

# --- 当 STORAGE_BACKEND=s3 时，以下配置生效（针对绑定的 Garage 服务） ---
S3_BUCKET="moetopia-media"
S3_REGION="garage"
S3_ACCESS_KEY_ID="通过_garage_cli_或_webui_生成的_access_key"
S3_SECRET_ACCESS_KEY="通过_garage_cli_或_webui_生成的_secret_key"
# 后端容器访问 Garage 的 S3 API 接口
S3_ENDPOINT_URL="http://localhost:3900"
# 前端用户或 CDN 实际访问图片的公开 URL（对应 Garage 的 s3_web 或 域名）
S3_BASE_URL="http://localhost:3902/moetopia-media"

# ==========================================
# 5. 邮件服务配置 (SMTP)
# ==========================================
SMTP_HOST="smtp.exmail.qq.com"
SMTP_PORT=587
SMTP_USER="noreply@moetopia.app"
SMTP_PASSWORD="your_smtp_password"
SMTP_FROM="noreply@moetopia.app"
SMTP_TLS=true

# ==========================================
# 6. 安全与前后台交互
# ==========================================
# 站点前端地址（用于给用户发送密码重置链接等邮件时拼接 URL）
FRONTEND_URL="http://localhost:3000"

# CORS 跨域允许的源（JSON 数组格式，Pydantic 会自动解析）
# 默认情况下 ["*"] 允许所有。生产环境建议缩窄，例如 ["https://moetopia.app", "http://localhost:3000"]
ALLOWED_ORIGINS=["*"]

# ==========================================
# 7. 首次部署自动创建管理员（可选）
# ==========================================
# 三项同时填写时，系统在首次迁移启动后会自动在数据库内生成该管理员账号
INITIAL_ADMIN_USERNAME="admin"
INITIAL_ADMIN_EMAIL="admin@moetopia.app"
INITIAL_ADMIN_PASSWORD="ChooseAnExtremelyStrongPassword123!"

```

#### 🚀 后端启动命令顺序

```bash
cd backend
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate  # Windows

pip install -r requirements.txt

# 1. 执行数据库迁移（Tortoise ORM / Aerich）
aerich upgrade

# 2. 启动 API 主服务
uvicorn main:app --reload --reload-exclude "docker-data/*"

```

---

### 3. 前端配置 (Frontend `.env`)

在 `frontend/` 目录下创建 `.env` 文件。作为 Nuxt 3 项目，它需要知道后端的 API 地址。

```env
# 后端 API 的基础请求路径
PUBLIC_API_BASE_URL="http://localhost:8000/api/v1"

# 如果前端有独立的 SSR 内部网络通信，可以额外配置（可选）
# NITRO_API_BASE_URL="http://backend:8000/api/v1"

```

#### 🚀 前端启动命令

```bash
cd frontend
pnpm install
pnpm dev --port 3000

```

---

### 4. Pixiv 同步节点配置 (Pixiv Agent `.env`)

如果你启用了 Pixiv 分布式同步节点，请在 `pixiv-agent/` 目录下创建 `.env`。

```env
# 核心凭证（通过 OAuth 登录或网页抓取获取的长期有效 Refresh Token）
PIXIV_REFRESH_TOKEN="your_pixiv_refresh_token_here"

# 节点与主后端通信的安全校验密钥（需与后端验证机制匹配）
API_KEY="your_node_api_key_for_authentication"
NODE_NAME="shanghai-node-01"

# 速率限制：每次请求 Pixiv API 的最小间隔时间（单位：秒），防止触发 429 风控
RATE_LIMIT=0.5

# 并发下载插画原图的协程/线程数量
DOWNLOAD_CONCURRENCY=3

```

#### 🚀 节点启动命令

```bash
cd pixiv-agent
pip install -r requirements.txt
uvicorn app.main:app --port 8001

```

---

### 🔍 启动后自检清单 (Verification Checklist)

1. **Web 管理界面**：访问 `http://localhost:3909` 进入 Garage UI，确认 Cluster 状态为 `OK`（副本数与节点数已按前文修复匹配）。
2. **API 文档确认**：访问 `http://localhost:8000/docs` （FastAPI Swagger UI），如果能正常打开并测试 `/api/v1/health` 接口，说明后端与 Postgres/Redis 连接成功。
3. **AI 推理检查**：若 `ENABLE_AI_FEATURES=true`，请观察后端启动日志。首次启动会通过 `HF_ENDPOINT` 自动下载 `wd-v1-4-convnext-tagger-v2` 模型。如看见 `Model loaded successfully` 相关的 INFO 日志，说明以图搜图及打标引擎就绪。
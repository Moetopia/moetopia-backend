# 🌸 Moetopia · 萌托邦

> 一个面向 ACG 创作者与爱好者的，AI 驱动的高性能二次元图片社区平台。  
> An AI-powered, high-performance ACG art community platform for creators and enthusiasts.

<div align="center">

![FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688?logo=fastapi&logoColor=white)
![Nuxt](https://img.shields.io/badge/Frontend-Nuxt3-00DC82?logo=nuxt.js&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/Database-PostgreSQL-4169E1?logo=postgresql&logoColor=white)
![Meilisearch](https://img.shields.io/badge/Search-Meilisearch-FF5CAA?logo=meilisearch&logoColor=white)
![Qdrant](https://img.shields.io/badge/Vector-Qdrant-FF3A3A)
![Redis](https://img.shields.io/badge/Queue-Redis-DC382D?logo=redis&logoColor=white)
</div>

---

## ✨ 功能特性

### 核心功能
- **作品发布** — 多图/漫画投稿，支持分级（全年龄 / R-18）、AI 生成标注
- **WD14 自动打标** — 上传即触发，基于 `wd-v1-4-convnext-tagger-v2` 提取 9083 维语义向量，零样本自动追加标签
- **以图搜图** — 上传本地图片，毫秒级检索站内相似构图、角色、风格的作品
- **文本模糊搜索** — 标题、标签、作者多维联合搜索，容忍拼写偏差
- **Pixiv 分布式同步** — 多节点 `pixiv-agent` 集群，自动拉取追踪作者的作品，带速率限制与 429 自动退避重试
- **接稿系统** — 创作者开放委托接稿，含稿件状态流转与支付标记
- **评论与点赞** — 多级评论、评论点赞、热评排序
- **公告系统** — 全站公告，支持置顶与 Markdown 富文本
- **账号认领** — 支持用户认领从 Pixiv 导入的作者账号
- **AI 内容安全** — 自动违规审核与内容审核队列
- **管理后台** — 用户管理、内容审核、节点监控、AI 开关

### 安全与隐私
- 服务端强制内容过滤 — R-18、AI 作品、屏蔽标签均在后端注入，不信任任何前端参数
- JWT 双 Token 鉴权（Access + Refresh）
- 内容分级严格隔离

---

## 🏗 系统架构

```
Moetopia/
├── frontend/          # Nuxt 3 · Vue 3 · TailwindCSS — 用户界面
├── backend/           # FastAPI · Tortoise ORM · ARQ — 核心 API 服务
└── pixiv-agent/       # FastAPI · aiosqlite — Pixiv 数据采集节点（可多实例）
```

```
                        ┌──────────────┐
                        │   Frontend   │  Nuxt 3 + TailwindCSS
                        └──────┬───────┘
                               │ HTTP / REST
                        ┌──────▼───────┐
                        │   Backend    │  FastAPI + ARQ Worker
                        │              │
              ┌─────────┤  PostgreSQL  ├─────────┐
              │         │  Meilisearch │         │
              │         │   Qdrant     │         │
              │         └──────┬───────┘         │
              │                │ HTTP + API Key  │
              │         ┌──────▼───────┐         │
              │         │ pixiv-agent  │  × N 节点│
              │         │  (SQLite)    │         │
              └─────────┴──────────────┴─────────┘
```

---

## 🛠 技术栈

| 层次 | 技术 | 版本 |
|------|------|------|
| **前端框架** | Nuxt 3 + Vue 3 | `^4.4` / `^3.5` |
| **前端样式** | TailwindCSS + Lucide Icons | `6.14` |
| **后端框架** | FastAPI + Uvicorn | `>=0.109` |
| **ORM** | Tortoise ORM + asyncpg | `>=0.20` |
| **主数据库** | PostgreSQL | `15` |
| **文本搜索** | Meilisearch | `>=1.6` |
| **向量检索** | Qdrant | `>=1.7` |
| **AI 推理** | ONNX Runtime + HuggingFace Hub | `>=1.26` |
| **AI 模型** | WD14 ConvNeXt Tagger v2 | — |
| **异步任务** | ARQ + Redis | `>=0.25` |
| **Pixiv 节点** | FastAPI + aiosqlite + pixivpy3 | — |

---

## 🚀 快速开始

### 前置依赖

- Python 3.11+
- Node.js 20+ + pnpm
- PostgreSQL 15
- Redis 7+
- Meilisearch（可 Docker 启动）
- Qdrant（可 Docker 启动）

### 基础设施（Docker 一键启动）

```bash
docker compose up -d
```

### Backend

```bash
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # Linux/macOS

# 配置环境变量（复制并编辑）
cp .env.example .env

# 运行数据库迁移
.venv/Scripts/python -m aerich upgrade

# 启动 API 服务
.venv/Scripts/python -m uvicorn app.main:app --reload

# 启动异步任务 Worker（新终端）
.venv/Scripts/python -m arq app.worker.worker.WorkerSettings
```

### Frontend

```bash
cd frontend
pnpm install
pnpm dev
```

### Pixiv Agent（可选，Pixiv 同步节点）

```bash
cd pixiv-agent
pip install -r requirements.txt
cp .env.example .env   # 填写 PIXIV_REFRESH_TOKEN 和 API_KEY

python -m uvicorn app.main:app --port 8001
```

---

## ⚙️ 环境变量

### Backend `.env` 关键配置

```env
DATABASE_URL=postgres://user:password@localhost:5432/moetopia
REDIS_URL=redis://localhost:6379/0
MEILISEARCH_URL=http://localhost:7700
MEILISEARCH_KEY=your_master_key
QDRANT_URL=http://localhost:6333
SECRET_KEY=your_jwt_secret
STORAGE_BACKEND=local          # local | s3
ENABLE_AI_FEATURES=true
```

### Pixiv Agent `.env` 关键配置

```env
PIXIV_REFRESH_TOKEN=your_pixiv_refresh_token
API_KEY=your_node_api_key
NODE_NAME=node-01
RATE_LIMIT=0.5                 # Pixiv API 请求速率（req/s）
DOWNLOAD_CONCURRENCY=3
```

---

## 📁 目录结构

```
backend/
├── app/
│   ├── api/v1/          # REST 路由层（仅参数接收与调用 services）
│   ├── models/          # Tortoise ORM 模型
│   ├── schemas/         # Pydantic Request/Response DTO
│   ├── services/        # 业务逻辑层（所有跨库操作在此）
│   ├── worker/          # ARQ 异步任务定义
│   └── core/            # 配置、安全、依赖注入
├── main.py
└── requirements.txt

frontend/
├── app/
│   ├── pages/           # 路由页面
│   ├── components/      # 通用组件
│   ├── composables/     # API 封装 / 状态逻辑
│   └── layouts/         # 页面布局
└── nuxt.config.ts

pixiv-agent/
├── app/
│   ├── routes/          # health / sync / artworks / logs
│   ├── worker.py        # 同步 Worker 主循环
│   ├── pixiv_client.py  # Pixiv API 封装（含限速 + 重试）
│   ├── queue.py         # 令牌桶速率限制 + 429 全局冷却
│   └── log_collector.py # 异步日志收集（SQLite + 内存缓冲）
└── requirements.txt
```

---

## 🔬 AI 检索系统

本项目的以图搜图基于 **WD14 ConvNeXt Tagger v2** 模型的语义特征向量方案。  
抛弃通用 CLIP 模型因「语义坍塌」导致的色调偏差问题，直接提取 **9083 维概念概率数组**作为图像语义特征向量。

在零样本（Zero-shot）条件下，针对二次元插画领域：

| 指标 | 数值 |
|------|------|
| 精确率 | 84.6% |
| 召回率 | 84.6% |
| **特异度** | **99.0%** |
| F1 分数 | 0.846 |

> 189 张完全不相关干扰图片无一越过 0.49 阈值线，证明模型真正理解了「角色语义维度」而非浅层色彩匹配。

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request。提交代码前请确保：

1. 遵循项目的 DDD 分层架构（`api/` 层不含业务逻辑）
2. 所有搜索查询经过服务端安全注入（不信任前端 filter 参数）
3. 通过现有 linter 检查

---

## 📄 许可证

本项目采用 [Source Available License](LICENSE.md) 许可证。

---

<div align="center">
Made with ❤️ for the SakuraKoi Society & misaka10843
</div>

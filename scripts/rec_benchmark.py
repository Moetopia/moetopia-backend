"""
Moetopia 推荐算法性能测试脚本
================================

生成论文图表所需数据：
  图7-1  不同推荐场景下的接口响应时间对比   (HTTP 压测模式)
  图7-2  推荐流程各阶段耗时组成              (服务层直连模式)
  图7-3  推荐候选来源数量统计                (服务层直连模式)
  图7-4  多样性过滤前后推荐结果对比          (服务层直连模式)
  图7-5  个性化推荐结果得分构成              (服务层直连模式)

用法:
  # 两种模式都运行（默认）
  python scripts/rec_benchmark.py --mode all \\
      --base-url http://localhost:8000 --token <JWT> \\
      --user-id 1 --cold-user-id 99 --artwork-id 42

  # 仅 HTTP 压测
  python scripts/rec_benchmark.py --mode http \\
      --base-url http://localhost:8000 --token <JWT> \\
      --user-id 1 --cold-user-id 99 --artwork-id 42

  # 仅服务层测试（无需启动服务器）
  python scripts/rec_benchmark.py --mode service \\
      --user-id 1 --cold-user-id 99 --artwork-id 42

依赖:
  pip install httpx matplotlib tortoise-orm asyncpg
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import math
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── 输出目录 ──────────────────────────────────────────────────────────────────
OUTPUT_DIR = Path(__file__).parent / "rec_test_output"
OUTPUT_DIR.mkdir(exist_ok=True)

# ── 颜色 (matplotlib) ─────────────────────────────────────────────────────────
PALETTE = ["#FF7FAB", "#7FB4FF", "#7FFFB4", "#FFB47F", "#C97FFF", "#FF7F7F"]


def _setup_mpl_cjk() -> None:
    """设置 matplotlib 中文字体（Windows 优先使用 Microsoft YaHei / SimHei）。"""
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = [
        "Microsoft YaHei", "SimHei", "SimSun",
        "Arial Unicode MS", "DejaVu Sans",
    ]
    plt.rcParams["axes.unicode_minus"] = False

# ─────────────────────────────────────────────────────────────────────────────
# HTTP 压测模式 (图7-1)
# ─────────────────────────────────────────────────────────────────────────────

SCENARIOS_HTTP = [
    ("HVCR-S 首次请求",       "hvcr_s_cold",    "s"),
    ("HVCR-S 缓存命中",       "hvcr_s_cache",   "s"),
    ("HVCR-U 首次请求",       "hvcr_u_cold",    "u"),
    ("HVCR-U 用户画像缓存命中", "hvcr_u_cache",  "u"),
    ("冷启动推荐",             "cold_start",     "u_cold"),
    ("纯标签 Fallback",       "tag_fallback",   "s_notag"),
]


async def _clear_cache_keys(redis_url: str, user_id: int, artwork_id: int) -> None:
    """清除与测试相关的 Redis 缓存键。"""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(redis_url)
        keys = [
            f"related:{artwork_id}:12",
            f"rec_uvec:{user_id}",
            f"rec_tags:{user_id}",
        ]
        await r.delete(*keys)
        await r.aclose()
    except Exception as e:
        print(f"  [warn] 无法清除缓存: {e}")


async def run_http_benchmark(
    base_url: str,
    token: str,
    user_id: int,
    cold_user_id: int,
    artwork_id: int,
    artwork_id_notag: Optional[int],
    redis_url: str,
    n_requests: int = 20,
) -> List[Dict]:
    """对运行中的服务发 HTTP 请求，测量端到端响应时间。"""
    try:
        import httpx
    except ImportError:
        print("[error] 请安装 httpx: pip install httpx")
        return []

    headers = {"Authorization": f"Bearer {token}"}
    results = []

    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=60) as client:
        for label, key, stype in SCENARIOS_HTTP:
            url, extra_headers = _scenario_url(
                stype, user_id, cold_user_id, artwork_id,
                artwork_id_notag or artwork_id, base_url, token
            )
            latencies: List[float] = []
            success = 0

            for i in range(n_requests):
                # Cold 场景每次请求前清缓存
                if "cold" in key:
                    uid = cold_user_id if stype == "u_cold" else user_id
                    await _clear_cache_keys(redis_url, uid, artwork_id)

                req_headers = {**headers, **extra_headers}
                t0 = time.perf_counter()
                try:
                    resp = await client.get(url, headers=req_headers)
                    elapsed = (time.perf_counter() - t0) * 1000
                    if resp.status_code == 200:
                        latencies.append(elapsed)
                        success += 1
                    else:
                        print(f"  [warn] {label} #{i+1} 返回 {resp.status_code}")
                except Exception as e:
                    print(f"  [warn] {label} #{i+1} 请求失败: {e}")

                # Cache 场景第一次是冷的，后续是热的；只取 2 次之后的数据
                if key in ("hvcr_s_cache", "hvcr_u_cache") and i == 0:
                    latencies.clear()  # 丢弃第一次（实际上是冷请求）

            if latencies:
                row = {
                    "场景": label,
                    "key": key,
                    "请求次数": len(latencies),
                    "成功率": f"{success/n_requests*100:.1f}%",
                    "平均响应时间(ms)": round(statistics.mean(latencies), 2),
                    "最小响应时间(ms)": round(min(latencies), 2),
                    "最大响应时间(ms)": round(max(latencies), 2),
                    "标准差(ms)": round(statistics.stdev(latencies) if len(latencies) > 1 else 0, 2),
                }
                results.append(row)
                print(f"  {label}: avg={row['平均响应时间(ms)']}ms  "
                      f"min={row['最小响应时间(ms)']}ms  max={row['最大响应时间(ms)']}ms")
            else:
                print(f"  [skip] {label}: 无有效响应")

    return results


def _scenario_url(
    stype: str, user_id: int, cold_user_id: int,
    artwork_id: int, artwork_notag_id: int,
    base_url: str, token: str
) -> Tuple[str, dict]:
    """返回 (path, extra_headers)"""
    if stype in ("s", "s_notag"):
        aid = artwork_notag_id if stype == "s_notag" else artwork_id
        return f"/api/v1/artworks/{aid}/related?limit=12", {}
    elif stype == "u_cold":
        # 使用 cold 用户 token 需要重新生成，此处用 query param 代替测试
        # 直接用 cold_user_id 的 token（用户需要提供）
        return "/api/v1/search/feed/recommended?limit=30", {}
    else:  # u, u_cache
        return "/api/v1/search/feed/recommended?limit=30", {}


def save_fig71(results: List[Dict]) -> None:
    """生成图7-1 柱状图 PNG。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("[warn] matplotlib 未安装，跳过图片生成")
        return
    _setup_mpl_cjk()

    labels = [r["场景"] for r in results]
    avgs = [r["平均响应时间(ms)"] for r in results]
    stds = [r["标准差(ms)"] for r in results]

    x = np.arange(len(labels))
    fig, ax = plt.subplots(figsize=(10, 5))
    bars = ax.bar(x, avgs, color=PALETTE[:len(labels)], width=0.55,
                  yerr=stds, capsize=4, error_kw={"elinewidth": 1.2})
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=15, ha="right", fontsize=9)
    ax.set_ylabel("平均响应时间 (ms)")
    ax.set_title("图 7-1  不同推荐场景下的接口响应时间对比")
    ax.bar_label(bars, fmt="%.1f", padding=3, fontsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    plt.tight_layout()
    out = OUTPUT_DIR / "fig7-1.png"
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"  → 已保存: {out}")


# ─────────────────────────────────────────────────────────────────────────────
# 服务层直连模式 (图7-2～7-5)
# ─────────────────────────────────────────────────────────────────────────────

async def _init_db() -> None:
    """初始化 Tortoise ORM。"""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")

    from tortoise import Tortoise
    from app.core.config import settings
    await Tortoise.init(
        db_url=settings.DATABASE_URL,
        modules={"models": ["app.models"]},
    )


async def _close_db() -> None:
    from tortoise import Tortoise
    await Tortoise.close_connections()


def _now_utc():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


# ── 图7-2: 阶段耗时 ────────────────────────────────────────────────────────────

async def timed_hvcr_u(user_id: int) -> Dict[str, float]:
    """执行一次 HVCR-U 推荐，返回各阶段耗时（ms）。"""
    import math
    from datetime import timedelta
    from app.models.user import User
    from app.models.artwork import Artwork
    from app.models.interaction import Bookmark, Like, ViewHistory
    from app.models.tag import ArtworkTag
    from app.models.artwork import ArtworkImage
    from app.infrastructure.cache import cache_get, cache_set, TTL_REC_TAGS, TTL_REC_UVEC
    from app.infrastructure.qdrant_client import qdrant_client
    from app.services.search_service import SearchService
    from collections import Counter
    import numpy as np

    stages: Dict[str, float] = {}
    T = time.perf_counter

    user = await User.get_or_none(id=user_id)
    if not user:
        raise ValueError(f"用户 {user_id} 不存在")

    now = _now_utc()
    cutoff = now - timedelta(days=90)
    LAMBDA = math.log(2) / 30

    # Stage 1: PostgreSQL - 交互历史
    t0 = T()
    bookmarks, likes, views = await asyncio.gather(
        Bookmark.filter(user_id=user.id, created_at__gte=cutoff).order_by("-created_at").limit(50).values("artwork_id", "created_at"),
        Like.filter(user_id=user.id, created_at__gte=cutoff).order_by("-created_at").limit(50).values("artwork_id", "created_at"),
        ViewHistory.filter(user_id=user.id, viewed_at__gte=cutoff).order_by("-viewed_at").limit(100).values("artwork_id", "viewed_at"),
    )
    stages["PostgreSQL Query"] = (T() - t0) * 1000

    total = len(bookmarks) + len(likes) + len(views)
    if total < 5:
        # 冷启动: fallback 到 daily ranking
        t0 = T()
        await SearchService.get_ranking("daily", limit=30, user=user)
        stages["Qdrant Search"] = 0.0
        stages["Redis Access"] = 0.0
        stages["Re-ranking"] = 0.0
        stages["Filtering"] = 0.0
        stages["Enrichment"] = (T() - t0) * 1000
        stages["_path"] = "cold_start"
        return stages

    interactions: Dict[int, float] = {}
    def _add(artwork_id, base_weight, ts):
        if not artwork_id:
            return
        from app.services.search_service import SearchService as SS
        dt_utc = SS._ensure_utc(ts)
        delta_days = (now - dt_utc).total_seconds() / 86400
        w = base_weight * math.exp(-LAMBDA * delta_days)
        interactions[artwork_id] = interactions.get(artwork_id, 0.0) + w

    for b in bookmarks:
        _add(b["artwork_id"], 3.0, b["created_at"])
    for l in likes:
        _add(l["artwork_id"], 2.0, l["created_at"])
    for v in views:
        _add(v["artwork_id"], 0.5, v["viewed_at"])
    interacted_ids = set(interactions.keys())

    # Stage 2: Redis - 读用户向量缓存
    t0 = T()
    uvec_key = f"rec_uvec:{user.id}"
    user_vector = await cache_get(uvec_key)
    tag_cache_key = f"rec_tags:{user.id}"
    user_tag_profile = await cache_get(tag_cache_key) or {}
    stages["Redis Access"] = (T() - t0) * 1000

    # Stage 3: Qdrant - 向量召回 (含构建用户向量)
    t0 = T()
    if user_vector is None:
        img_rows = await ArtworkImage.filter(artwork_id__in=list(interacted_ids)).values("id", "artwork_id")
        art_to_imgs: Dict[int, List] = {}
        for row in img_rows:
            art_to_imgs.setdefault(row["artwork_id"], []).append(str(row["id"]))
        all_img_ids = [iid for ids in art_to_imgs.values() for iid in ids]
        if all_img_ids:
            vector_map = await asyncio.to_thread(qdrant_client.retrieve_vectors_by_ids, all_img_ids)
            if vector_map:
                weighted_sum = None
                for aid, img_ids in art_to_imgs.items():
                    vecs = [np.array(vector_map[iid]) for iid in img_ids if iid in vector_map]
                    if not vecs:
                        continue
                    art_vec = np.mean(vecs, axis=0) * interactions.get(aid, 0.0)
                    weighted_sum = art_vec if weighted_sum is None else weighted_sum + art_vec
                if weighted_sum is not None:
                    norm = np.linalg.norm(weighted_sum)
                    if norm > 0:
                        user_vector = (weighted_sum / norm).tolist()
                        await cache_set(uvec_key, user_vector, TTL_REC_UVEC)

    candidate_scores: Dict[str, float] = {}
    if user_vector:
        pts = await asyncio.to_thread(qdrant_client.search_similar_to_vectors, user_vector, 100)
        for p in pts:
            aid = p.payload.get("artwork_id")
            if aid and int(aid) not in interacted_ids:
                candidate_scores[str(aid)] = max(candidate_scores.get(str(aid), 0.0), p.score)
    stages["Qdrant Search"] = (T() - t0) * 1000

    # Stage 4: PostgreSQL - 候选标签 + 元数据
    t0 = T()
    if not user_tag_profile:
        tag_rows = await ArtworkTag.filter(artwork_id__in=list(interacted_ids)).values("artwork_id", "tag_name")
        art_to_tags: Dict[int, List] = {}
        all_tag_set: set = set()
        for row in tag_rows:
            art_to_tags.setdefault(row["artwork_id"], []).append(row["tag_name"])
            all_tag_set.add(row["tag_name"])
        idf_prof = await SearchService._get_tag_idf_weights(list(all_tag_set))
        pref: Dict[str, float] = {}
        for aid, tags_i in art_to_tags.items():
            w_i = interactions.get(aid, 0.0)
            for t in tags_i:
                pref[t] = pref.get(t, 0.0) + w_i * idf_prof.get(t, 1.0)
        user_tag_profile = dict(sorted(pref.items(), key=lambda kv: kv[1], reverse=True)[:30])
        await cache_set(tag_cache_key, user_tag_profile, TTL_REC_TAGS)

    candidate_ids = list(candidate_scores.keys())
    if not candidate_ids:
        stages["Re-ranking"] = 0.0
        stages["Filtering"] = 0.0
        stages["Enrichment"] = 0.0
        stages["_path"] = "fallback"
        return stages

    cand_tag_rows, cand_artworks = await asyncio.gather(
        ArtworkTag.filter(artwork_id__in=[int(c) for c in candidate_ids]).values("artwork_id", "tag_name"),
        Artwork.filter(id__in=[int(c) for c in candidate_ids], visibility="public").values("id", "created_at", "author_id"),
    )
    stages["PostgreSQL Query"] += (T() - t0) * 1000

    cand_tags: Dict[str, List] = {}
    for row in cand_tag_rows:
        cand_tags.setdefault(str(row["artwork_id"]), []).append(row["tag_name"])
    cand_meta = {str(r["id"]): r for r in cand_artworks}

    # Stage 5: Re-ranking
    t0 = T()
    MU = math.log(2) / 7
    EPS = 1e-9
    raw_tag_scores = {
        cid: (sum(user_tag_profile.get(t, 0.0) for t in cand_tags.get(cid, [])) / len(cand_tags.get(cid, [])) if cand_tags.get(cid) else 0.0)
        for cid in candidate_ids
    }
    max_raw_tag = max(raw_tag_scores.values(), default=0.0)
    scored = []
    score_details = []
    for cid in candidate_ids:
        meta = cand_meta.get(cid)
        if not meta:
            continue
        qdrant_sim = candidate_scores.get(cid, 0.0)
        tag_match = raw_tag_scores.get(cid, 0.0) / (max_raw_tag + EPS)
        created_at = SearchService._ensure_utc(meta["created_at"])
        delta_days = (now - created_at).total_seconds() / 86400
        freshness = math.exp(-MU * delta_days)
        final_score = 0.50 * qdrant_sim + 0.35 * tag_match + 0.15 * freshness
        scored.append((final_score, cid, meta["author_id"]))
        score_details.append({
            "artwork_id": int(cid),
            "visual_score": round(qdrant_sim, 4),
            "tag_match": round(tag_match, 4),
            "freshness": round(freshness, 4),
            "weighted_visual": round(0.50 * qdrant_sim, 4),
            "weighted_tag": round(0.35 * tag_match, 4),
            "weighted_fresh": round(0.15 * freshness, 4),
            "final_score": round(final_score, 4),
        })
    scored.sort(key=lambda x: x[0], reverse=True)
    stages["Re-ranking"] = (T() - t0) * 1000
    stages["_score_details"] = score_details
    stages["_qdrant_count"] = len(candidate_scores)
    stages["_tag_supplement"] = max(0, len(candidate_ids) - len(candidate_scores))

    # Stage 6: Diversity filter
    t0 = T()
    author_count: Dict[int, int] = {}
    before_ids = [cid for _, cid, _ in scored]
    diverse_ids = []
    author_before = {}
    for _, cid, author_id in scored:
        author_before[author_id] = author_before.get(author_id, 0) + 1
    for _, cid, author_id in scored:
        cnt = author_count.get(author_id, 0)
        if cnt < 3:
            diverse_ids.append(cid)
            author_count[author_id] = cnt + 1
        if len(diverse_ids) >= 30:
            break
    stages["Filtering"] = (T() - t0) * 1000
    stages["_before_author_count"] = len(author_before)
    stages["_after_author_count"] = len(author_count)
    stages["_max_before"] = max(author_before.values(), default=0)
    stages["_max_after"] = max(author_count.values(), default=0)
    stages["_filtered"] = len(before_ids) - len(diverse_ids)

    # Stage 7: Enrichment
    t0 = T()
    from app.services.search_service import SearchService as SS
    await SS._enrich_meili_hits([{"id": int(cid)} for cid in diverse_ids[:30]])
    stages["Enrichment"] = (T() - t0) * 1000
    stages["_path"] = "hvcr_u"

    return stages


async def timed_hvcr_s(artwork_id: int, user_id: Optional[int] = None) -> Dict[str, float]:
    """执行一次 HVCR-S 相似作品推荐，返回各阶段耗时（ms）。"""
    from app.models.user import User
    from app.models.artwork import Artwork
    from app.models.tag import ArtworkTag
    from app.models.artwork import ArtworkImage
    from app.infrastructure.qdrant_client import qdrant_client
    from app.services.search_service import SearchService
    import numpy as np

    stages: Dict[str, float] = {}
    T = time.perf_counter

    user = await User.get_or_none(id=user_id) if user_id else None

    # Stage 1: PostgreSQL - 查作品标签和图片
    t0 = T()
    tag_names, image_ids = await asyncio.gather(
        ArtworkTag.filter(artwork_id=artwork_id).values_list("tag_name", flat=True),
        ArtworkImage.filter(artwork_id=artwork_id).values_list("id", flat=True),
    )
    tag_names = list(tag_names)
    image_ids = list(image_ids)
    stages["PostgreSQL Query"] = (T() - t0) * 1000

    # Stage 2: Qdrant - 向量召回
    t0 = T()
    query_vector = None
    qdrant_scores: Dict[str, float] = {}
    if image_ids:
        vector_map = await asyncio.to_thread(
            qdrant_client.retrieve_vectors_by_ids,
            [str(iid) for iid in image_ids]
        )
        if vector_map:
            vecs = [np.array(v) for v in vector_map.values()]
            mean_vec = np.mean(vecs, axis=0)
            norm = np.linalg.norm(mean_vec)
            if norm > 0:
                query_vector = (mean_vec / norm).tolist()
    if query_vector:
        pts = await asyncio.to_thread(
            qdrant_client.search_similar_to_vector, query_vector, str(artwork_id), 48
        )
        for p in pts:
            aid = p.payload.get("artwork_id")
            if aid and aid != str(artwork_id):
                qdrant_scores[str(aid)] = max(qdrant_scores.get(str(aid), 0.0), p.score)
    stages["Qdrant Search"] = (T() - t0) * 1000

    # Redis (no user-specific cache for HVCR-S, just the result cache check)
    stages["Redis Access"] = 0.0  # checked at outer level; within: IDF cache

    # Stage 3: Meilisearch 标签补充
    t0 = T()
    meili_ids: List[str] = []
    if tag_names:
        from app.services.search_engine import search_engine
        top_tags = tag_names[:15]
        tag_list = ", ".join(f'"{t}"' for t in top_tags)
        result = await asyncio.to_thread(
            search_engine.search_artworks,
            user, "", None, f"tags IN [{tag_list}]", 48, 0
        )
        meili_ids = [str(h["id"]) for h in result.get("hits", []) if str(h.get("id")) != str(artwork_id)]
    # (this time counted in PostgreSQL for simplicity, as it's a tag lookup)
    stages["PostgreSQL Query"] += (T() - t0) * 1000

    candidate_ids = list(dict.fromkeys(list(qdrant_scores.keys()) + meili_ids))
    if not candidate_ids:
        stages["Re-ranking"] = 0.0
        stages["Filtering"] = 0.0
        stages["Enrichment"] = 0.0
        stages["_path"] = "tag_fallback"
        return stages

    # Stage 4: PostgreSQL - 候选标签 + IDF
    t0 = T()
    cand_tag_rows = await ArtworkTag.filter(
        artwork_id__in=[int(c) for c in candidate_ids]
    ).values("artwork_id", "tag_name")
    cand_tags: Dict[str, List] = {}
    for row in cand_tag_rows:
        cand_tags.setdefault(str(row["artwork_id"]), []).append(row["tag_name"])
    all_tags = set(tag_names)
    for tags in cand_tags.values():
        all_tags.update(tags)
    idf = await SearchService._get_tag_idf_weights(list(all_tags))
    stages["PostgreSQL Query"] += (T() - t0) * 1000

    # Stage 5: Re-ranking (HVCR-S scoring)
    t0 = T()
    alpha = 0.65 if query_vector and qdrant_scores else 0.0
    beta = 1.0 - alpha
    scored = []
    for cid in candidate_ids:
        c_tags = cand_tags.get(cid, [])
        wj = SearchService._weighted_jaccard(tag_names, c_tags, idf)
        qdrant_sim = qdrant_scores.get(cid, 0.0)
        final_score = alpha * qdrant_sim + beta * wj
        if final_score > 0:
            scored.append((final_score, cid))
    scored.sort(key=lambda x: x[0], reverse=True)
    stages["Re-ranking"] = (T() - t0) * 1000
    stages["_qdrant_count"] = len(qdrant_scores)
    stages["_tag_supplement"] = max(0, len(candidate_ids) - len(qdrant_scores))
    stages["_path"] = "tag_fallback" if not query_vector else "hvcr_s"

    # Stage 6: Filtering (no diversity filter for HVCR-S)
    stages["Filtering"] = 0.0

    # Stage 7: Enrichment
    t0 = T()
    top_ids = [cid for _, cid in scored[:12]]
    await SearchService._enrich_meili_hits([{"id": int(cid)} for cid in top_ids])
    stages["Enrichment"] = (T() - t0) * 1000

    return stages


async def run_service_benchmark(
    user_id: int,
    cold_user_id: int,
    artwork_id: int,
    artwork_id_notag: Optional[int],
    redis_url: str,
    n_runs: int = 5,
) -> Dict[str, Any]:
    """运行服务层基准测试，返回各图表所需数据。"""
    print("\n[服务层模式] 初始化数据库连接...")
    await _init_db()

    from app.infrastructure.redis_client import init_redis, close_redis, get_redis
    await init_redis()

    results: Dict[str, Any] = {
        "stages": [],       # 图7-2
        "candidates": [],   # 图7-3
        "diversity": [],    # 图7-4
        "scores": [],       # 图7-5
    }

    async def _clear(uid: int, aid: int):
        r = get_redis()
        keys = [f"rec_uvec:{uid}", f"rec_tags:{uid}", f"related:{aid}:12"]
        await r.delete(*keys)

    # ── HVCR-U (warm) ────────────────────────────────────────────────────────
    print("  Running HVCR-U warm (user_id={})...".format(user_id))
    hvcr_u_stage_list = []
    for i in range(n_runs):
        try:
            s = await timed_hvcr_u(user_id)
            hvcr_u_stage_list.append(s)
        except Exception as e:
            print(f"    [warn] run {i}: {e}")

    if hvcr_u_stage_list:
        avg_s = {k: statistics.mean(d[k] for d in hvcr_u_stage_list if isinstance(d.get(k), float))
                 for k in ("PostgreSQL Query", "Qdrant Search", "Redis Access", "Re-ranking", "Filtering", "Enrichment")}
        avg_s["流程"] = "HVCR-U"
        avg_s["总耗时"] = sum(avg_s[k] for k in ("PostgreSQL Query", "Qdrant Search", "Redis Access", "Re-ranking", "Filtering", "Enrichment"))
        results["stages"].append(avg_s)

        # 图7-3: 候选来源 (取第一次运行数据)
        first = hvcr_u_stage_list[0]
        results["candidates"].append({
            "用户": f"User {user_id}",
            "Qdrant 候选数": first.get("_qdrant_count", 0),
            "标签补充数": first.get("_tag_supplement", 0),
            "热榜候选数": 0,
            "被过滤数": first.get("_filtered", 0),
            "最终返回数": 30,
        })
        # 图7-4: 多样性
        results["diversity"].append({
            "用户": f"User {user_id}",
            "过滤前不同作者数": first.get("_before_author_count", 0),
            "过滤后不同作者数": first.get("_after_author_count", 0),
            "过滤前最大单作者": first.get("_max_before", 0),
            "过滤后最大单作者": first.get("_max_after", 0),
        })
        # 图7-5: top-10 得分
        if "_score_details" in first:
            for rank, sd in enumerate(first["_score_details"][:10], 1):
                results["scores"].append({"排名": rank, **sd})

    # ── HVCR-U (cold) ────────────────────────────────────────────────────────
    print("  Running HVCR-U cold (cold_user_id={})...".format(cold_user_id))
    hvcr_u_cold_stages = []
    for i in range(min(n_runs, 3)):
        try:
            await _clear(cold_user_id, artwork_id)
            s = await timed_hvcr_u(cold_user_id)
            hvcr_u_cold_stages.append(s)
        except Exception as e:
            print(f"    [warn] cold run {i}: {e}")

    if hvcr_u_cold_stages:
        path = hvcr_u_cold_stages[0].get("_path", "cold_start")
        if path == "cold_start":
            fb_stages = {k: statistics.mean(d.get(k, 0.0) for d in hvcr_u_cold_stages)
                         for k in ("PostgreSQL Query", "Qdrant Search", "Redis Access", "Re-ranking", "Filtering", "Enrichment")}
            fb_stages["流程"] = "Fallback"
            fb_stages["总耗时"] = sum(fb_stages[k] for k in ("PostgreSQL Query", "Qdrant Search", "Redis Access", "Re-ranking", "Filtering", "Enrichment"))
            results["stages"].append(fb_stages)
            results["candidates"].append({
                "用户": "Cold Start",
                "Qdrant 候选数": 0, "标签补充数": 0, "热榜候选数": 30,
                "被过滤数": 0, "最终返回数": 30,
            })

    # ── HVCR-S (warm) ────────────────────────────────────────────────────────
    print("  Running HVCR-S warm (artwork_id={})...".format(artwork_id))
    hvcr_s_stages = []
    for i in range(n_runs):
        try:
            s = await timed_hvcr_s(artwork_id, user_id)
            hvcr_s_stages.append(s)
        except Exception as e:
            print(f"    [warn] hvcr_s run {i}: {e}")

    if hvcr_s_stages:
        avg_s = {k: statistics.mean(d.get(k, 0.0) for d in hvcr_s_stages)
                 for k in ("PostgreSQL Query", "Qdrant Search", "Redis Access", "Re-ranking", "Filtering", "Enrichment")}
        avg_s["流程"] = "HVCR-S"
        avg_s["总耗时"] = sum(avg_s[k] for k in ("PostgreSQL Query", "Qdrant Search", "Redis Access", "Re-ranking", "Filtering", "Enrichment"))
        results["stages"].append(avg_s)

        first_s = hvcr_s_stages[0]
        results["candidates"].append({
            "用户": f"Artwork {artwork_id}",
            "Qdrant 候选数": first_s.get("_qdrant_count", 0),
            "标签补充数": first_s.get("_tag_supplement", 0),
            "热榜候选数": 0,
            "被过滤数": 0,
            "最终返回数": min(12, first_s.get("_qdrant_count", 0) + first_s.get("_tag_supplement", 0)),
        })

    # ── Tag fallback (no-vector artwork) ─────────────────────────────────────
    nta_id = artwork_id_notag or artwork_id
    if nta_id != artwork_id:
        print("  Running tag fallback (artwork_id={})...".format(nta_id))
        try:
            s_fb = await timed_hvcr_s(nta_id, user_id)
            if s_fb.get("_path") == "tag_fallback":
                s_fb_avg = {k: s_fb.get(k, 0.0) for k in ("PostgreSQL Query", "Qdrant Search", "Redis Access", "Re-ranking", "Filtering", "Enrichment")}
                s_fb_avg["流程"] = "Tag Fallback"
                s_fb_avg["总耗时"] = sum(s_fb_avg[k] for k in ("PostgreSQL Query", "Qdrant Search", "Redis Access", "Re-ranking", "Filtering", "Enrichment"))
                results["stages"].append(s_fb_avg)
        except Exception as e:
            print(f"    [warn] tag fallback: {e}")

    await _close_db()
    await close_redis()
    return results


# ─────────────────────────────────────────────────────────────────────────────
# CSV + 图表生成
# ─────────────────────────────────────────────────────────────────────────────

def write_csv(filename: str, rows: List[Dict]) -> None:
    if not rows:
        return
    out = OUTPUT_DIR / filename
    fieldnames = list(rows[0].keys())
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"  → CSV: {out}")


def save_fig72(stage_rows: List[Dict]) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        return
    _setup_mpl_cjk()
    if not stage_rows:
        return

    phase_keys = ["PostgreSQL Query", "Qdrant Search", "Redis Access", "Re-ranking", "Filtering", "Enrichment"]
    labels = [r["流程"] for r in stage_rows]
    data = [[r.get(k, 0.0) for r in stage_rows] for k in phase_keys]
    x = np.arange(len(labels))
    width = 0.45
    fig, ax = plt.subplots(figsize=(9, 5))
    bottom = np.zeros(len(labels))
    for i, (phase, vals) in enumerate(zip(phase_keys, data)):
        bars = ax.bar(x, vals, width, bottom=bottom, label=phase, color=PALETTE[i % len(PALETTE)])
        bottom += np.array(vals)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=10)
    ax.set_ylabel("耗时 (ms)")
    ax.set_title("图 7-2  推荐流程各阶段耗时组成")
    ax.legend(loc="upper right", fontsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    plt.tight_layout()
    out = OUTPUT_DIR / "fig7-2.png"
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"  → 已保存: {out}")


def save_fig73(cand_rows: List[Dict]) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        return
    _setup_mpl_cjk()
    if not cand_rows:
        return

    keys = ["Qdrant 候选数", "标签补充数", "热榜候选数", "被过滤数"]
    labels = [r["用户"] for r in cand_rows]
    x = np.arange(len(labels))
    fig, ax = plt.subplots(figsize=(8, 5))
    bottom = np.zeros(len(labels))
    for i, k in enumerate(keys):
        vals = [r.get(k, 0) for r in cand_rows]
        ax.bar(x, vals, 0.45, bottom=bottom, label=k, color=PALETTE[i % len(PALETTE)])
        bottom += np.array(vals)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_ylabel("候选数量")
    ax.set_title("图 7-3  推荐候选来源数量统计")
    ax.legend(fontsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    plt.tight_layout()
    out = OUTPUT_DIR / "fig7-3.png"
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"  → 已保存: {out}")


def save_fig74(div_rows: List[Dict]) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        return
    _setup_mpl_cjk()
    if not div_rows:
        return

    labels = [r["用户"] for r in div_rows]
    before = [r["过滤前不同作者数"] for r in div_rows]
    after = [r["过滤后不同作者数"] for r in div_rows]
    x = np.arange(len(labels))
    w = 0.3
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.bar(x - w/2, before, w, label="过滤前", color=PALETTE[0])
    ax.bar(x + w/2, after, w, label="过滤后", color=PALETTE[1])
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_ylabel("不同作者数量")
    ax.set_title("图 7-4  多样性过滤前后推荐结果对比")
    ax.legend(fontsize=9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    plt.tight_layout()
    out = OUTPUT_DIR / "fig7-4.png"
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"  → 已保存: {out}")


def save_fig75(score_rows: List[Dict]) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        return
    _setup_mpl_cjk()
    if not score_rows:
        return

    ranks = [r["排名"] for r in score_rows]
    vis = [r["weighted_visual"] for r in score_rows]
    tag = [r["weighted_tag"] for r in score_rows]
    fresh = [r["weighted_fresh"] for r in score_rows]
    x = np.arange(len(ranks))
    fig, ax = plt.subplots(figsize=(10, 5))
    b1 = ax.bar(x, vis, 0.5, label="视觉得分 (×0.50)", color=PALETTE[0])
    b2 = ax.bar(x, tag, 0.5, bottom=vis, label="标签匹配 (×0.35)", color=PALETTE[1])
    b3 = ax.bar(x, fresh, 0.5, bottom=[v+t for v, t in zip(vis, tag)],
                label="新鲜度 (×0.15)", color=PALETTE[2])
    ax.set_xticks(x)
    ax.set_xticklabels([f"Top-{r}" for r in ranks], fontsize=8, rotation=30, ha="right")
    ax.set_ylabel("得分")
    ax.set_title("图 7-5  个性化推荐结果得分构成")
    ax.legend(fontsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    plt.tight_layout()
    out = OUTPUT_DIR / "fig7-5.png"
    plt.savefig(out, dpi=150)
    plt.close()
    print(f"  → 已保存: {out}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Moetopia 推荐算法性能测试脚本")
    p.add_argument("--mode", choices=["http", "service", "all"], default="all",
                   help="运行模式：http=仅HTTP压测, service=仅服务层, all=两者 (default: all)")
    p.add_argument("--base-url", default="http://localhost:8000",
                   help="后端服务地址 (HTTP 模式用)")
    p.add_argument("--token", default="",
                   help="用于 HVCR-U/S 请求的 JWT Bearer token")
    p.add_argument("--user-id", type=int, required=True,
                   help="有足够交互记录的测试用户 ID (HVCR-U)")
    p.add_argument("--cold-user-id", type=int,
                   help="交互记录不足5条的冷启动用户 ID（不提供则用 user-id）")
    p.add_argument("--artwork-id", type=int, required=True,
                   help="用于 HVCR-S 测试的作品 ID（应有 Qdrant 向量）")
    p.add_argument("--artwork-id-notag", type=int, default=None,
                   help="无向量/无标签的作品 ID（用于 tag fallback 场景，可选）")
    p.add_argument("--redis-url", default="redis://localhost:6379/0",
                   help="Redis 地址（用于清缓存）")
    p.add_argument("--n", type=int, default=20,
                   help="HTTP 模式每场景请求次数 (default: 20)")
    p.add_argument("--n-service", type=int, default=5,
                   help="服务层模式每场景运行次数 (default: 5)")
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    cold_uid = args.cold_user_id or args.user_id

    print(f"\n{'='*60}")
    print("  Moetopia 推荐算法基准测试")
    print(f"  输出目录: {OUTPUT_DIR}")
    print(f"{'='*60}\n")

    # ── HTTP 压测 (图7-1) ──────────────────────────────────────────────────────
    if args.mode in ("http", "all"):
        print("[HTTP 压测模式] 每场景 {} 次请求".format(args.n))
        if not args.token:
            print("  [warn] 未提供 --token，HVCR-U 端点可能返回 401")
        http_rows = await run_http_benchmark(
            base_url=args.base_url,
            token=args.token,
            user_id=args.user_id,
            cold_user_id=cold_uid,
            artwork_id=args.artwork_id,
            artwork_id_notag=args.artwork_id_notag,
            redis_url=args.redis_url,
            n_requests=args.n,
        )
        # 清除内部 key 字段再保存 CSV
        csv_rows = [{k: v for k, v in r.items() if k != "key"} for r in http_rows]
        write_csv("fig7-1_latency.csv", csv_rows)
        save_fig71(http_rows)

    # ── 服务层测试 (图7-2～7-5) ────────────────────────────────────────────────
    if args.mode in ("service", "all"):
        print("\n[服务层模式] 每场景 {} 次运行".format(args.n_service))
        svc_results = await run_service_benchmark(
            user_id=args.user_id,
            cold_user_id=cold_uid,
            artwork_id=args.artwork_id,
            artwork_id_notag=args.artwork_id_notag,
            redis_url=args.redis_url,
            n_runs=args.n_service,
        )

        # 图7-2 CSV（去掉内部 key）
        stage_csv = [
            {k: round(v, 2) if isinstance(v, float) else v
             for k, v in r.items() if not k.startswith("_")}
            for r in svc_results["stages"]
        ]
        write_csv("fig7-2_stages.csv", stage_csv)
        save_fig72(svc_results["stages"])

        write_csv("fig7-3_candidates.csv", svc_results["candidates"])
        save_fig73(svc_results["candidates"])

        write_csv("fig7-4_diversity.csv", svc_results["diversity"])
        save_fig74(svc_results["diversity"])

        write_csv("fig7-5_scores.csv", svc_results["scores"])
        save_fig75(svc_results["scores"])

    print("\n全部完成。结果保存在:", OUTPUT_DIR)


if __name__ == "__main__":
    asyncio.run(main())

"""
Pixiv → Moetopia 一键导入脚本
===============================

功能：
  读取本地目录中的图片文件（文件名含 Pixiv 作品 ID），
  从 Pixiv API 自动拉取元数据（标题/描述/标签/作者等），
  批量上传至 Moetopia 站点。

依赖安装：
  .venv\\Scripts\\pip install -r requirements-scripts.txt

文件名格式（支持）：
  12345678.jpg           → 作品 12345678，第 1 张图
  12345678_p0.jpg        → 作品 12345678，第 0 张图
  12345678_p0.png
  12345678_p1.webp       → 作品 12345678，第 1 张图（同作品多图）
  abc_12345678_p0.jpg    → 也能正确提取 12345678

用法示例：
  # 最简：交互式输入账号密码和 Pixiv token
  python scripts/import_pixiv.py ./my_images

  # 完整参数：
  python scripts/import_pixiv.py ./my_images \\
      --username admin \\
      --password yourpassword \\
      --pixiv-token REFRESH_TOKEN_HERE \\
      --site-url http://localhost:8000 \\
      --visibility public \\
      --rating auto \\
      --delay 1.5 \\
      --dry-run

  # 使用已有 JWT（跳过登录步骤）：
  python scripts/import_pixiv.py ./my_images --jwt YOUR_JWT_TOKEN

Pixiv Refresh Token 获取方法：
  参考：https://gist.github.com/ZipFile/c9ebedb224406f4f11845ab700124362

环境变量（可替代命令行参数）：
  MOETOPIA_URL          站点地址，默认 http://localhost:8000
  PIXIV_REFRESH_TOKEN   Pixiv refresh token
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ──────────────────────────────────────────────
# 检查依赖
# ──────────────────────────────────────────────
try:
    import requests
except ImportError:
    sys.exit("❌  缺少 requests 库：.venv\\Scripts\\pip install requests")

try:
    from tqdm import tqdm
except ImportError:
    # 降级为无进度条的简单迭代
    def tqdm(iterable, **kwargs):
        desc = kwargs.get("desc", "")
        total = kwargs.get("total", None)
        if desc:
            print(f"[{desc}]")
        return iterable

try:
    from pixivpy3 import AppPixivAPI
except ImportError:
    sys.exit("❌  缺少 pixivpy3：.venv\\Scripts\\pip install pixivpy3")


# ──────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────
SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# 匹配文件名中的 Pixiv 作品 ID（7-10 位数字）
# 支持：12345678.jpg / 12345678_p0.jpg / prefix_12345678_p1.png
PIXIV_ID_RE = re.compile(r"(?<!\d)(\d{7,10})(?:_p\d+)?(?!\d)")

RATING_MAP = {0: "safe", 1: "r18", 2: "r18g"}

# 导入状态记录文件
IMPORTED_LOG = Path("scripts/.pixiv_imported.json")

# Pixiv 元数据本地缓存（SQLite）
PIXIV_CACHE_DB = Path("scripts/.pixiv_cache.db")


# ──────────────────────────────────────────────
# Pixiv 元数据缓存
# ──────────────────────────────────────────────

class PixivMetaCache:
    """
    基于 SQLite 的 Pixiv 本地缓存。
    - pixiv_meta  表：作品元数据（fetch_pixiv_meta 返回的处理后字典）
    - pixiv_authors 表：作者资料（username, bio, avatar_url, website_url 等）
    重新导入时直接读取缓存，无需再消耗 Pixiv API 请求次数。
    """

    _CREATE_ARTWORKS = """
        CREATE TABLE IF NOT EXISTS pixiv_meta (
            pixiv_id  INTEGER PRIMARY KEY,
            data      TEXT    NOT NULL,
            cached_at TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """
    _CREATE_AUTHORS = """
        CREATE TABLE IF NOT EXISTS pixiv_authors (
            pixiv_user_id  INTEGER PRIMARY KEY,
            data           TEXT    NOT NULL,
            cached_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """

    def __init__(self, db_path: Path = PIXIV_CACHE_DB):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute(self._CREATE_ARTWORKS)
        self._conn.execute(self._CREATE_AUTHORS)
        self._conn.commit()

    # ── 作品元数据 ─────────────────────────────────────────

    def get(self, pixiv_id: int) -> Optional[dict]:
        """返回缓存的作品元数据，未命中返回 None。"""
        row = self._conn.execute(
            "SELECT data FROM pixiv_meta WHERE pixiv_id = ?", (pixiv_id,)
        ).fetchone()
        if row:
            try:
                return json.loads(row[0])
            except Exception:
                return None
        return None

    def put(self, pixiv_id: int, meta: dict) -> None:
        """写入或更新作品元数据缓存。"""
        self._conn.execute(
            """
            INSERT INTO pixiv_meta (pixiv_id, data, cached_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(pixiv_id) DO UPDATE SET data = excluded.data, cached_at = excluded.cached_at
            """,
            (pixiv_id, json.dumps(meta, ensure_ascii=False)),
        )
        self._conn.commit()

    # ── 作者资料 ─────────────────────────────────────────

    def get_author(self, pixiv_user_id: int) -> Optional[dict]:
        """返回缓存的作者资料，未命中返回 None。"""
        row = self._conn.execute(
            "SELECT data FROM pixiv_authors WHERE pixiv_user_id = ?", (pixiv_user_id,)
        ).fetchone()
        if row:
            try:
                return json.loads(row[0])
            except Exception:
                return None
        return None

    def put_author(self, pixiv_user_id: int, data: dict) -> None:
        """写入或更新作者资料缓存。"""
        self._conn.execute(
            """
            INSERT INTO pixiv_authors (pixiv_user_id, data, cached_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(pixiv_user_id) DO UPDATE SET data = excluded.data, cached_at = excluded.cached_at
            """,
            (pixiv_user_id, json.dumps(data, ensure_ascii=False)),
        )
        self._conn.commit()

    # ── 统计 ───────────────────────────────────────────

    def count(self) -> int:
        artworks = self._conn.execute("SELECT COUNT(*) FROM pixiv_meta").fetchone()[0]
        authors  = self._conn.execute("SELECT COUNT(*) FROM pixiv_authors").fetchone()[0]
        return artworks, authors

    def close(self) -> None:
        self._conn.close()


# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────

def extract_pixiv_id(filename: str) -> Optional[int]:
    """从文件名中提取 Pixiv 作品 ID"""
    m = PIXIV_ID_RE.search(Path(filename).stem)
    return int(m.group(1)) if m else None


def extract_page_num(filename: str) -> int:
    """提取分 P 编号，无则返回 0"""
    m = re.search(r"_p(\d+)", Path(filename).stem)
    return int(m.group(1)) if m else 0


def strip_html(text: str) -> str:
    """简单去除 Pixiv 描述中的 HTML 标签"""
    return re.sub(r"<[^>]+>", "", text or "").strip()


def load_imported() -> set:
    """读取已导入的 Pixiv ID 集合"""
    if IMPORTED_LOG.exists():
        try:
            return set(json.loads(IMPORTED_LOG.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_imported(ids: set) -> None:
    """持久化已导入 ID"""
    IMPORTED_LOG.parent.mkdir(exist_ok=True)
    IMPORTED_LOG.write_text(json.dumps(sorted(ids)), encoding="utf-8")


def group_files_by_pixiv_id(directory: Path) -> Dict[int, List[Path]]:
    """
    扫描目录，将图片按 Pixiv 作品 ID 分组，
    同一作品的多张图（p0/p1/p2...）归为一组并按页码排序。
    """
    groups: Dict[int, List[Path]] = defaultdict(list)
    for f in directory.iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() not in SUPPORTED_EXTS:
            continue
        pid = extract_pixiv_id(f.name)
        if pid:
            groups[pid].append(f)
        else:
            print(f"  ⚠  跳过无法识别 ID 的文件：{f.name}")
    # 每组内按页码排序
    for pid in groups:
        groups[pid].sort(key=lambda p: extract_page_num(p.name))
    return dict(groups)


# ──────────────────────────────────────────────
# Moetopia API
# ──────────────────────────────────────────────

class MoetopiaCLient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.token: Optional[str] = None

    def login(self, username: str, password: str) -> str:
        resp = self.session.post(
            f"{self.base_url}/api/v1/auth/login",
            json={"username": username, "password": password},
            timeout=15,
        )
        if resp.status_code != 200:
            sys.exit(f"❌  登录失败（{resp.status_code}）：{resp.text[:200]}")
        data = resp.json()
        token = data.get("data", {}).get("access_token")
        if not token:
            sys.exit(f"❌  响应中无 access_token：{resp.text[:200]}")
        self.token = token
        self.session.headers["Authorization"] = f"Bearer {token}"
        print(f"✅  已登录 Moetopia（{self.base_url}）")
        return token

    def set_jwt(self, token: str) -> None:
        self.token = token
        self.session.headers["Authorization"] = f"Bearer {token}"

    def check_pixiv_exists(self, pixiv_id: int) -> Optional[int]:
        """查询 Pixiv ID 是否已导入，返回本站作品 ID 或 None"""
        try:
            resp = self.session.get(
                f"{self.base_url}/api/v1/artworks/by-pixiv/{pixiv_id}",
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json().get("data")
                return data["id"] if data else None
        except Exception:
            pass
        return None

    def submit_tag_translations(self, tag_bilingual: list) -> int:
        """提交双语标签翻译，返回成功提交数"""
        count = 0
        for t in tag_bilingual:
            ja_name = t.get("ja", "").strip()
            translated = t.get("translated", "")
            if not ja_name or not translated:
                continue
            try:
                resp = self.session.post(
                    f"{self.base_url}/api/v1/tags/{ja_name}/translations",
                    json={"locale": "zh", "translated_name": translated},
                    timeout=10,
                )
                if resp.status_code in (200, 201):
                    count += 1
            except Exception:
                pass
        return count

    def create_or_get_imported_user(
        self,
        pixiv_user_id: int,
        username: str,
        bio: Optional[str] = None,
        avatar_url: Optional[str] = None,
        website_url: Optional[str] = None,
    ) -> int:
        """
        在 Moetopia 创建或获取 Pixiv 导入作者账号。
        返回本站用户 ID。
        """
        payload = {
            "username": username[:50],
            "pixiv_user_id": pixiv_user_id,
            "source_platform": "pixiv",
            "bio": bio,
            "avatar_url": avatar_url,
            "website_url": website_url,
        }
        resp = self.session.post(
            f"{self.base_url}/api/v1/admin/imported-users",
            json=payload,
            timeout=15,
        )
        if resp.status_code == 201 or resp.status_code == 200:
            return resp.json()["data"]["user_id"]
        if resp.status_code == 409:
            detail = resp.json().get("detail", {})
            if isinstance(detail, dict) and "user_id" in detail:
                return detail["user_id"]
            # Fallback: look up by pixiv_user_id
            get_resp = self.session.get(
                f"{self.base_url}/api/v1/admin/imported-users/by-pixiv/{pixiv_user_id}",
                timeout=10,
            )
            if get_resp.status_code == 200:
                return get_resp.json()["data"]["user_id"]
        sys.exit(f"❌  创建导入账号失败（{resp.status_code}）：{resp.text[:200]}")

    def impersonate_user(self, user_id: int) -> str:
        """获取指定导入账号的 2 小时临时 JWT。需要管理员身份。"""
        resp = self.session.post(
            f"{self.base_url}/api/v1/admin/imported-users/{user_id}/impersonate",
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["data"]["access_token"]

    def get_author_session(self, user_id: int) -> "MoetopiaCLient":
        """返回以指定导入账号临时 JWT 登录的新客户端实例。"""
        token = self.impersonate_user(user_id)
        client = MoetopiaCLient(self.base_url)
        client.set_jwt(token)
        return client

    def upload_avatar_from_url(self, url: str, pixiv_api=None) -> Optional[str]:
        """下载 Pixiv 头像并上传为当前账号头像，返回站内 URL。
        pixiv_api: 传入 AppPixivAPI 实例以使用其认证 session（避免 403）。"""
        try:
            data = _download_pixiv_image(url, pixiv_api)
            if not data:
                return None
            ext = "." + url.rsplit(".", 1)[-1].split("?")[0]
            if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                ext = ".jpg"
            resp = self.session.post(
                f"{self.base_url}/api/v1/users/me/avatar",
                files={"file": (f"avatar{ext}", data, _mime_by_ext(ext))},
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.json()["data"].get("avatar_url")
            print(f"        ⚠  头像上传响应 {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            print(f"        ⚠  头像上传失败：{e}")
        return None

    def create_or_get_series(self, title: str, description: Optional[str] = None) -> int:
        """在当前账号下创建系列，返回系列 ID。"""
        resp = self.session.post(
            f"{self.base_url}/api/v1/artworks/series/create",
            json={"title": title, "description": description},
            timeout=10,
        )
        if resp.status_code in (200, 201):
            return resp.json()["data"]["id"]
        raise RuntimeError(f"创建系列失败（{resp.status_code}）：{resp.text[:200]}")

    def add_artwork_to_series(self, series_id: int, artwork_id: int, order: int = 0):
        """将作品加入系列。"""
        resp = self.session.post(
            f"{self.base_url}/api/v1/artworks/series/{series_id}/artworks/{artwork_id}",
            params={"order": order},
            timeout=10,
        )
        resp.raise_for_status()

    def upload_artwork(
        self,
        title: str,
        description: str,
        tags: List[str],
        artwork_type: str,
        is_ai: bool,
        rating: str,
        visibility: str,
        image_paths: List[Path],
        pixiv_id: Optional[int] = None,
        source: Optional[str] = None,
        original_author_name: Optional[str] = None,
        content_origin: str = "repost",
        author_id: Optional[int] = None,
    ) -> dict:
        files = [
            ("files", (p.name, p.open("rb"), _mime(p)))
            for p in image_paths
        ]
        data = {
            "title": title,
            "description": description,
            "tags_str": json.dumps(tags),
            "artwork_type": artwork_type,
            "is_ai": str(is_ai).lower(),
            "rating": rating,
            "visibility": visibility,
            "allow_ai_tagging": "true",
            "allow_community_tagging": "true",
            "content_origin": content_origin,
        }
        if pixiv_id is not None:
            data["pixiv_id"] = str(pixiv_id)
        if source:
            data["source"] = source
        if original_author_name:
            data["original_author_name"] = original_author_name
        if author_id is not None:
            data["author_id"] = str(author_id)
        try:
            resp = self.session.post(
                f"{self.base_url}/api/v1/artworks/upload",
                data=data,
                files=files,
                timeout=120,
            )
            resp.raise_for_status()
            return resp.json().get("data", {})
        finally:
            for _, (_, fobj, _) in files:
                fobj.close()


def _mime(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "application/octet-stream")


def fetch_pixiv_author_meta(api: "AppPixivAPI", pixiv_user_id: int) -> Optional[dict]:
    """拉取 Pixiv 用户详情，返回标准化字典（username, bio, avatar_url, website_url 等）。"""
    try:
        result = api.user_detail(pixiv_user_id)
        if result.get("error"):
            return None
        user    = result.get("user", {})
        profile = result.get("profile", {})
        return {
            "pixiv_user_id":  pixiv_user_id,
            "username":       user.get("name", ""),
            "avatar_url":     user.get("profile_image_urls", {}).get("medium"),
            "bio":            (user.get("comment") or "").strip() or None,
            "website_url":    profile.get("webpage") or None,
            "twitter_url":    profile.get("twitter_url") or None,
        }
    except Exception as e:
        print(f"    ⚠  获取 Pixiv 用户 {pixiv_user_id} 详情失败：{e}")
        return None


def _mime_by_ext(ext: str) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext.lower(), "image/jpeg")


def _download_pixiv_image(url: str, pixiv_api=None) -> Optional[bytes]:
    """
    从 Pixiv CDN 下载图片，返回字节数据。
    必须使用 pixivpy3 的认证 session 才能避免 403。
    若未提供 api，回退到带 Referer 的普通请求（成功率较低）。
    """
    try:
        if pixiv_api is not None:
            # 使用 pixivpy3 认证 session（含 Authorization + User-Agent 等头）
            r = pixiv_api.session.get(
                url,
                headers={"Referer": "https://www.pixiv.net/"},
                timeout=15,
            )
        else:
            r = requests.get(
                url,
                headers={
                    "Referer": "https://www.pixiv.net/",
                    "User-Agent": "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)",
                },
                timeout=15,
            )
        r.raise_for_status()
        return r.content
    except Exception as e:
        print(f"        ⚠  下载 Pixiv 图片失败 {url}: {e}")
        return None


# ──────────────────────────────────────────────
# Pixiv API
# ──────────────────────────────────────────────

def build_pixiv_api(refresh_token: str) -> AppPixivAPI:
    api = AppPixivAPI()
    api.auth(refresh_token=refresh_token)
    return api


def fetch_pixiv_meta(api: AppPixivAPI, pixiv_id: int) -> Optional[dict]:
    """
    拉取 Pixiv 作品元数据，返回标准化字典。
    失败时返回 None（由调用方决定是否跳过）。
    """
    try:
        result = api.illust_detail(pixiv_id)
        if result.get("error"):
            print(f"    Pixiv API 错误：{result['error'].get('message')}")
            return None
        illust = result.get("illust")
        print(illust)
        if not illust:
            return None

        # 标签：name + translated_name（去重）
        tags = []
        seen = set()
        for t in illust.get("tags", []):
            for val in (t.get("name"), t.get("translated_name")):
                if val and val not in seen:
                    tags.append(val)
                    seen.add(val)

        # 评级
        x_restrict = illust.get("x_restrict", 0)
        rating = RATING_MAP.get(x_restrict, "safe")

        # AI 检测（illust_ai_type: 0=non-ai, 1=partial, 2=full-ai）
        is_ai = illust.get("illust_ai_type", 0) >= 2

        # 类型
        illust_type = illust.get("type", "illust")
        artwork_type = "manga" if illust_type == "manga" else "illustration"

        user_info = illust.get("user", {})
        pixiv_user_name = user_info.get("name", "")
        pixiv_user_id = user_info.get("id")
        pixiv_user_avatar = user_info.get("profile_image_urls", {}).get("medium")
        pixiv_series = illust.get("series")  # {"id": int, "title": str} or None
        source_url = f"https://www.pixiv.net/artworks/{pixiv_id}"

        # 整理双语标签映射：{原名(ja): 译名(zh/en)}
        tag_bilingual: list[dict] = []
        for t in illust.get("tags", []):
            name = t.get("name", "")
            trans = t.get("translated_name")
            if name:
                tag_bilingual.append({"ja": name, "translated": trans})

        # 给 Moetopia 上传用的标签列表（原始名，全小写）；schema 限制最多 20 个
        upload_tags = [t["ja"].strip().lower() for t in tag_bilingual if t["ja"]][:20]

        raw_title = illust.get("title", "") or f"Pixiv #{pixiv_id}"
        raw_desc  = strip_html(illust.get("caption", ""))
        return {
            "title": raw_title[:100],
            "description": raw_desc[:2000],
            "tags": upload_tags,
            "tag_bilingual": tag_bilingual,
            "artwork_type": artwork_type,
            "is_ai": is_ai,
            "rating": rating,
            "pixiv_user": pixiv_user_name,
            "pixiv_user_id": pixiv_user_id,
            "source_url": source_url,
            "page_count": illust.get("page_count", 1),
            "pixiv_user_avatar": pixiv_user_avatar,
            "pixiv_series": pixiv_series,
        }
    except Exception as e:
        print(f"    ⚠  获取 Pixiv #{pixiv_id} 元数据失败：{e}")
        return None


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="将本地 Pixiv 图片批量导入 Moetopia",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("directory", help="包含 Pixiv 图片的本地目录")
    parser.add_argument("--site-url",   default=os.environ.get("MOETOPIA_URL", "http://localhost:8000"), help="站点地址")
    parser.add_argument("--username",   help="Moetopia 用户名（不使用 --jwt 时需要）")
    parser.add_argument("--password",   help="Moetopia 密码")
    parser.add_argument("--jwt",        help="直接使用已有 JWT access_token（跳过登录）")
    parser.add_argument("--pixiv-token", default=os.environ.get("PIXIV_REFRESH_TOKEN"), help="Pixiv refresh token")
    parser.add_argument("--visibility", default="public", choices=["public", "private", "followers"], help="发布可见性")
    parser.add_argument("--rating",     default="auto", choices=["auto", "safe", "r18", "r18g"], help="评级（auto=跟随 Pixiv 原作）")
    parser.add_argument("--delay",      type=float, default=1.0, help="每次上传后的间隔秒数（防止触发限速）")
    parser.add_argument("--dry-run",    action="store_true", help="仅预览，不实际上传")
    parser.add_argument("--skip-existing", action="store_true", default=True, help="跳过已导入的 Pixiv ID（默认开启）")
    parser.add_argument("--no-skip",    action="store_true", help="强制重新导入已有记录")
    parser.add_argument("--content-origin", default="repost", choices=["repost", "original", "fanart"],
                        help="来源声明（默认 repost：转载他人作品；若导入自己的 Pixiv 作品请用 original）")
    parser.add_argument("--import-authors", action="store_true",
                        help="为每个 Pixiv 作者创建/复用导入账号，并以该账号名义上传作品（需要 admin JWT）")
    parser.add_argument("--impersonate", action="store_true",
                        help="配合 --import-authors：以导入作者临时 JWT 上传（支持头像同步 + 系列自动识别）")
    parser.add_argument("--no-pixiv-cache", action="store_true",
                        help="禁用 Pixiv 元数据本地缓存（默认启用，缓存至 scripts/.pixiv_cache.db）")
    args = parser.parse_args()

    directory = Path(args.directory)
    if not directory.is_dir():
        sys.exit(f"❌  目录不存在：{directory}")

    # ── 1. 扫描图片文件 ──────────────────────────
    print(f"\n🔍  扫描目录：{directory.resolve()}")
    groups = group_files_by_pixiv_id(directory)
    if not groups:
        sys.exit("❌  目录中没有找到含有 Pixiv ID 的图片文件")
    print(f"    找到 {len(groups)} 个 Pixiv 作品，共 {sum(len(v) for v in groups.values())} 张图片")

    # ── 2. 过滤已导入 ────────────────────────────
    imported_ids = set() if args.no_skip else load_imported()
    if imported_ids:
        before = len(groups)
        groups = {pid: imgs for pid, imgs in groups.items() if pid not in imported_ids}
        skipped = before - len(groups)
        if skipped:
            print(f"    ⏭  跳过已导入 {skipped} 个作品（使用 --no-skip 强制重导入）")
    if not groups:
        print("✅  所有作品均已导入，无需操作。")
        return

    print(f"    待导入：{len(groups)} 个作品\n")

    if args.dry_run:
        print("🔎  [DRY-RUN 模式] 仅预览，不实际上传\n")

    # ── 3. 初始化 Pixiv API ──────────────────────
    if not args.pixiv_token:
        args.pixiv_token = input("请输入 Pixiv refresh token：").strip()
    print("🎨  正在连接 Pixiv API...")
    pixiv_api = build_pixiv_api(args.pixiv_token)
    print("    ✅  Pixiv 认证成功\n")

    # ── 3b. 初始化元数据缓存 ─────────────────────
    meta_cache: Optional[PixivMetaCache] = None
    if not args.no_pixiv_cache:
        meta_cache = PixivMetaCache()
        n_artworks, n_authors = meta_cache.count()
        print(f"💾  Pixiv 元数据缓存已加载（作品 {n_artworks} 条 / 作者 {n_authors} 条，{PIXIV_CACHE_DB}）\n")

    # ── 4. 登录 Moetopia ─────────────────────────
    moetopia = MoetopiaCLient(args.site_url)
    if args.jwt:
        moetopia.set_jwt(args.jwt)
        print(f"✅  使用已有 JWT（{args.site_url}）")
    else:
        username = args.username or input("Moetopia 用户名：").strip()
        password = args.password or input("Moetopia 密码：").strip()
        moetopia.login(username, password)

    # ── 5. 批量导入 ──────────────────────────────
    success_count = 0
    fail_count = 0
    newly_imported: set = set()
    author_sessions: Dict[int, MoetopiaCLient] = {}  # moetopia_user_id → 临时 JWT 客户端
    series_cache: Dict[int, int] = {}  # pixiv_series_id → moetopia_series_id

    pixiv_ids = list(groups.keys())

    for pixiv_id in tqdm(pixiv_ids, desc="导入进度"):
        image_paths = groups[pixiv_id]
        print(f"\n  📦  Pixiv #{pixiv_id}  ({len(image_paths)} 张图)")

        # 拉取元数据（优先读缓存，缓存无标签时强制重拉）
        _TAG_RETRY_MAX   = 5
        _TAG_RETRY_DELAY = 10
        meta = meta_cache.get(pixiv_id) if meta_cache else None
        if meta is not None:
            if meta.get("tags"):
                print(f"      📦  元数据来自本地缓存")
            else:
                print(f"      ⚠  缓存中无标签，重新从 Pixiv 拉取")
                meta = None
        if meta is None:
            for _attempt in range(_TAG_RETRY_MAX + 1):
                meta = fetch_pixiv_meta(pixiv_api, pixiv_id)
                if meta is not None and meta.get("tags"):
                    if meta_cache:
                        meta_cache.put(pixiv_id, meta)
                    break
                _reason = "获取失败" if meta is None else "标签为空（Pixiv 可能尚未索引）"
                if _attempt < _TAG_RETRY_MAX:
                    print(f"      ⚠  {_reason}，等待 {_TAG_RETRY_DELAY}s 后重试（{_attempt + 1}/{_TAG_RETRY_MAX}）...")
                    time.sleep(_TAG_RETRY_DELAY)
        if meta is None:
            print(f"      ⚠  无法获取元数据，跳过")
            fail_count += 1
            time.sleep(0.5)
            continue
        if not meta.get("tags"):
            print(f"      ❌  重试 {_TAG_RETRY_MAX} 次后仍无标签，跳过此作品")
            fail_count += 1
            continue

        # 评级覆盖
        rating = meta["rating"] if args.rating == "auto" else args.rating

        # 预览
        print(f"      标题：{meta['title']}")
        print(f"      作者：{meta['pixiv_user']}")
        print(f"      评级：{rating}  AI：{meta['is_ai']}  类型：{meta['artwork_type']}")
        print(f"      标签：{', '.join(meta['tags'][:8])}{'...' if len(meta['tags']) > 8 else ''}")
        print(f"      图片：{[p.name for p in image_paths]}")

        if args.dry_run:
            print("      [DRY-RUN] 跳过上传")
            success_count += 1
            continue

        # 检查是否已通过 API 导入（比 JSON 文件更可靠）
        existing_id = moetopia.check_pixiv_exists(pixiv_id)
        if existing_id and not args.no_skip:
            print(f"      ⏭  已存在（本站 ID: {existing_id}），跳过")
            imported_ids.add(pixiv_id)
            continue

        # 作者信息
        author_note = ""
        if meta["pixiv_user"]:
            author_note = f"原作者：{meta['pixiv_user']}"
            if meta.get("pixiv_user_id"):
                author_note += f"（Pixiv @{meta['pixiv_user_id']}）"
        description = meta["description"]
        if author_note and description:
            description = f"{author_note}\n\n{description}"
        elif author_note:
            description = author_note

        # 导入作者账号
        upload_author_id: Optional[int] = None
        effective_client = moetopia  # 上传所用客户端（impersonate 模式下会切换）
        use_author_id_param = True   # 是否通过 author_id 参数指定作者（admin JWT 模式）
        if args.import_authors and meta.get("pixiv_user_id"):
            puid = meta["pixiv_user_id"]
            # 获取/缓存作者详细资料
            author_data: Optional[dict] = meta_cache.get_author(puid) if meta_cache else None
            if author_data is None:
                author_data = fetch_pixiv_author_meta(pixiv_api, puid)
                if author_data and meta_cache:
                    meta_cache.put_author(puid, author_data)
                    print(f"      📌  作者资料已缓存（{author_data.get('username')}）")
            else:
                print(f"      📌  作者资料来自本地缓存")
            # 合并元数据中已有的基础字段（author_data 优先，因为包含 bio/website 等完整信息）
            effective_author = author_data or {}
            try:
                upload_author_id = moetopia.create_or_get_imported_user(
                    pixiv_user_id=puid,
                    username=effective_author.get("username") or meta["pixiv_user"] or f"pixiv_{puid}",
                    bio=effective_author.get("bio"),
                    avatar_url=effective_author.get("avatar_url") or meta.get("pixiv_user_avatar"),
                    website_url=effective_author.get("website_url"),
                )
                print(f"      👤  作者账号 ID: {upload_author_id}（{effective_author.get('username') or meta['pixiv_user']}）")
            except Exception as e:
                print(f"      ⚠  无法创建作者账号：{e}，将使用当前登录账号上传")

            # impersonate 模式：获取/复用作者临时 JWT，切换上传客户端
            if upload_author_id and args.impersonate:
                if upload_author_id not in author_sessions:
                    try:
                        author_sessions[upload_author_id] = moetopia.get_author_session(upload_author_id)
                        print(f"      🔑  已获取作者临时 JWT（2h）")
                        # 同步头像（以作者身份上传，使用 pixivpy3 session 避免 403）
                        avatar_url = meta.get("pixiv_user_avatar")
                        if avatar_url:
                            uploaded = author_sessions[upload_author_id].upload_avatar_from_url(
                                avatar_url, pixiv_api=pixiv_api
                            )
                            if uploaded:
                                print(f"      🖼   头像已同步")
                    except Exception as e:
                        print(f"      ⚠  临时授权失败：{e}，降级为 admin JWT + author_id 上传")
                if upload_author_id in author_sessions:
                    effective_client = author_sessions[upload_author_id]
                    use_author_id_param = False  # JWT 本身已标识作者，无需 author_id
            elif upload_author_id and meta.get("pixiv_user_avatar"):
                # 非 impersonate：通过 admin 端点上传头像，使用 pixivpy3 session 避免 403
                try:
                    avatar_data = _download_pixiv_image(meta["pixiv_user_avatar"], pixiv_api)
                    if avatar_data:
                        ext_av = "." + meta["pixiv_user_avatar"].rsplit(".", 1)[-1].split("?")[0]
                        if ext_av not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                            ext_av = ".jpg"
                        av_resp = moetopia.session.post(
                            f"{args.site_url}/api/v1/admin/imported-users/{upload_author_id}/avatar",
                            files={"file": (f"avatar{ext_av}", avatar_data, _mime_by_ext(ext_av))},
                            timeout=30,
                        )
                        if av_resp.status_code == 200:
                            print(f"      🖼   头像已同步（admin 端点）")
                        else:
                            print(f"      ⚠  头像上传响应 {av_resp.status_code}: {av_resp.text[:100]}")
                except Exception as e:
                    print(f"      ⚠  头像同步失败：{e}")

        # 上传
        try:
            result = effective_client.upload_artwork(
                title=meta["title"],
                description=description,
                tags=meta["tags"],
                artwork_type=meta["artwork_type"],
                is_ai=meta["is_ai"],
                rating=rating,
                visibility=args.visibility,
                image_paths=image_paths,
                pixiv_id=pixiv_id,
                source=meta["source_url"],
                original_author_name=meta["pixiv_user"] or None,
                content_origin=args.content_origin,
                author_id=upload_author_id if use_author_id_param else None,
            )
            artwork_id = result.get("id")
            print(f"      ✅  已上传 → 本站作品 ID: {artwork_id}")

            # 系列识别与关联
            pixiv_series = meta.get("pixiv_series")
            if pixiv_series and artwork_id:
                psid = pixiv_series.get("id")
                series_title = pixiv_series.get("title") or f"Pixiv Series {psid}"
                if psid:
                    series_client = effective_client  # 必须用有权限的客户端（作者或 admin）
                    if psid not in series_cache:
                        try:
                            msid = series_client.create_or_get_series(series_title)
                            series_cache[psid] = msid
                            print(f"      📚  系列「{series_title}」→ 本站 ID: {msid}")
                        except Exception as e:
                            print(f"      ⚠  系列创建失败：{e}")
                    if psid in series_cache:
                        try:
                            series_client.add_artwork_to_series(series_cache[psid], artwork_id)
                            print(f"      📎  已加入系列「{series_title}」")
                        except Exception as e:
                            print(f"      ⚠  加入系列失败：{e}")

            # 提交双语标签翻译
            tag_count = moetopia.submit_tag_translations(meta.get("tag_bilingual", []))
            if tag_count:
                print(f"      🌐  已提交 {tag_count} 个标签翻译")

            newly_imported.add(pixiv_id)
            imported_ids.add(pixiv_id)
            save_imported(imported_ids)
            success_count += 1
        except Exception as e:
            print(f"      ❌  上传失败：{e}")
            fail_count += 1

        if args.delay > 0:
            time.sleep(args.delay)

    # ── 6. 汇总 ─────────────────────────────────
    print(f"\n{'='*50}")
    print(f"  ✅  成功：{success_count}  ❌  失败：{fail_count}  共计：{len(pixiv_ids)}")
    if not args.dry_run and newly_imported:
        print(f"  📝  导入记录已保存至：{IMPORTED_LOG}")
    print(f"{'='*50}\n")

    if meta_cache:
        meta_cache.close()


if __name__ == "__main__":
    main()

"""
文件存储抽象层。

支持两种后端，通过 .env STORAGE_BACKEND 切换：
  local  — 本地磁盘（默认，开发/轻量部署）
  s3     — S3 兼容对象存储（AWS S3 / MinIO / Cloudflare R2）

用法：
    from app.services.storage_service import storage

    url  = await storage.save(data, "artworks/123_0.jpg")
    resp = await storage.make_download_response(url, "file.zip")
    await storage.delete_by_url(url)

    # AI 推理专用（自动处理 S3 临时下载 + 清理）
    async with storage.open_for_processing(url) as local_path:
        vector = ai_engine.extract_vector(local_path)
"""
import os
import asyncio
import logging
import tempfile
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import Optional, AsyncIterator

from fastapi import Response
from fastapi.responses import FileResponse, RedirectResponse

from app.core.config import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Abstract base
# ─────────────────────────────────────────────────────────────────────────────

class StorageBackend(ABC):

    @abstractmethod
    async def save(self, data: bytes, key: str) -> str:
        """存储文件数据，返回公开 URL。key 格式如 'artworks/123_0.jpg'。"""

    @abstractmethod
    async def delete_by_url(self, url: str) -> None:
        """通过公开 URL 删除文件。"""

    @asynccontextmanager
    async def open_for_processing(self, url: str) -> AsyncIterator[str]:
        """
        异步上下文管理器：生成一个适合 CPU 密集型处理（AI 推理）的本地文件路径。
        对于 S3 后端会自动下载到临时文件并在退出时清理。
        """
        local = self._to_local_path(url)
        if local is not None:
            yield local
        else:
            tmp = await self._download_to_temp(url)
            try:
                yield tmp
            finally:
                try:
                    await asyncio.to_thread(os.unlink, tmp)
                except Exception:
                    pass

    def _to_local_path(self, url: str) -> Optional[str]:
        """若 URL 对应本地路径则返回，否则返回 None。"""
        return None

    async def _download_to_temp(self, url: str) -> str:
        raise NotImplementedError("Remote storage must implement _download_to_temp")

    @abstractmethod
    async def make_download_response(self, url_or_path: str, filename: str) -> Response:
        """构造文件下载 HTTP 响应（支持私密文件）。"""


# ─────────────────────────────────────────────────────────────────────────────
# Local backend
# ─────────────────────────────────────────────────────────────────────────────

class LocalStorageBackend(StorageBackend):
    """将文件保存到本地 uploads/ 目录，通过 FastAPI StaticFiles 提供服务。"""

    async def save(self, data: bytes, key: str) -> str:
        full = os.path.join("uploads", key)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        await asyncio.to_thread(self._sync_write, full, data)
        return f"/uploads/{key}"

    @staticmethod
    def _sync_write(path: str, data: bytes) -> None:
        with open(path, "wb") as f:
            f.write(data)

    async def delete_by_url(self, url: str) -> None:
        local = self._to_local_path(url)
        if local and os.path.exists(local):
            try:
                await asyncio.to_thread(os.unlink, local)
            except OSError as exc:
                logger.warning(f"⚠️ 文件删除失败 ({local}): {exc}")

    def _to_local_path(self, url: str) -> Optional[str]:
        if url.startswith("/uploads/"):
            return url.lstrip("/")
        clean = url.lstrip("./")
        if clean.startswith("uploads/"):
            return clean
        return None

    async def make_download_response(self, url_or_path: str, filename: str) -> Response:
        local = self._to_local_path(url_or_path) or url_or_path
        return FileResponse(
            path=local,
            filename=filename,
            media_type="application/octet-stream",
        )


# ─────────────────────────────────────────────────────────────────────────────
# S3 backend
# ─────────────────────────────────────────────────────────────────────────────

class S3StorageBackend(StorageBackend):
    """存储到 S3 兼容对象存储（AWS S3 / MinIO / Cloudflare R2）。"""

    def __init__(self) -> None:
        try:
            import boto3  # type: ignore[import]
        except ImportError:
            raise RuntimeError("S3 存储需要安装 boto3：pip install boto3")

        self._client = boto3.client(
            "s3",
            region_name=settings.S3_REGION or None,
            endpoint_url=settings.S3_ENDPOINT_URL or None,
            aws_access_key_id=settings.S3_ACCESS_KEY_ID,
            aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
        )
        self.bucket = settings.S3_BUCKET
        self.base_url = (
            settings.S3_BASE_URL.rstrip("/")
            if settings.S3_BASE_URL
            else f"https://{self.bucket}.s3.amazonaws.com"
        )

    def _key_from_url(self, url: str) -> Optional[str]:
        prefix = self.base_url + "/"
        return url[len(prefix):] if url.startswith(prefix) else None

    @staticmethod
    def _content_type(key: str) -> str:
        return {
            ".jpg":  "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png":  "image/png",
            ".gif":  "image/gif",
            ".webp": "image/webp",
            ".svg":  "image/svg+xml",
            ".ico":  "image/x-icon",
        }.get(os.path.splitext(key)[1].lower(), "application/octet-stream")

    async def save(self, data: bytes, key: str) -> str:
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=self._content_type(key),
        )
        return f"{self.base_url}/{key}"

    async def delete_by_url(self, url: str) -> None:
        key = self._key_from_url(url)
        if key:
            await asyncio.to_thread(
                self._client.delete_object, Bucket=self.bucket, Key=key
            )

    async def _download_to_temp(self, url: str) -> str:
        key = self._key_from_url(url)
        if not key:
            raise ValueError(f"无法从 URL 推断 S3 Key: {url}")
        ext = os.path.splitext(key)[1]
        fd, tmp_path = tempfile.mkstemp(suffix=ext)
        os.close(fd)
        await asyncio.to_thread(self._client.download_file, self.bucket, key, tmp_path)
        return tmp_path

    async def make_download_response(self, url_or_path: str, filename: str) -> Response:
        key = self._key_from_url(url_or_path)
        if not key:
            raise ValueError(f"无法从 URL 推断 S3 Key: {url_or_path}")
        presigned: str = await asyncio.to_thread(
            self._client.generate_presigned_url,
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
            },
            ExpiresIn=300,
        )
        return RedirectResponse(url=presigned, status_code=302)


# ─────────────────────────────────────────────────────────────────────────────
# Factory & singleton
# ─────────────────────────────────────────────────────────────────────────────

def _create_storage() -> StorageBackend:
    backend = getattr(settings, "STORAGE_BACKEND", "local").lower()
    if backend == "s3":
        logger.info("📦 文件存储后端: S3")
        return S3StorageBackend()
    logger.info("📦 文件存储后端: Local")
    return LocalStorageBackend()


storage: StorageBackend = _create_storage()

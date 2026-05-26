"""
Manga Image Translator HTTP 服务封装。
MIT 需以 --mode web 启动，默认监听 http://localhost:5003。

API：
  POST /translate/with-form/image/stream
    Body: multipart/form-data
      image  : 图片文件（二进制）
      config : JSON 字符串，包含翻译配置
    Response: 流式二进制协议
      [1 byte status_code][4 bytes data_size big-endian][data_size bytes data]
      status_code 0 = 翻译完成，data 为翻译后图片字节（PNG）
      status_code 1 = 状态文本更新
      status_code 2 = 错误信息
      status_code 3 = 排队中（data 为队列位置文字）
      status_code 4 = 排队中（无位置信息）
"""
import json
import logging
import struct
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://localhost:5003"
_DEFAULT_TIMEOUT = 300


_DEFAULT_MIT_PARAMS: dict = {
    "mit_translator":           "offline",
    "mit_detection_size":       1536,
    "mit_detector":             "default",
    "mit_direction":            "auto",
    "mit_inpainter":            "default",
    "mit_inpainting_size":      2048,
    "mit_unclip_ratio":         2.3,
    "mit_box_threshold":        0.7,
    "mit_mask_dilation_offset": 30,
}


async def _get_mit_config() -> tuple[str, int, dict]:
    """从 site_config 读取 MIT 配置，降级到默认值。返回 (server_url, timeout, params)。"""
    try:
        from app.infrastructure.cache import cache_get
        cfg = await cache_get("site_config")
        if not cfg:
            from app.models.site_config import SiteConfig
            rows = await SiteConfig.all().values("key", "value")
            cfg = {r["key"]: r["value"] for r in rows}
        url = cfg.get("mit_server_url", _DEFAULT_URL)
        timeout = int(cfg.get("mit_timeout", _DEFAULT_TIMEOUT))
        params = {k: cfg.get(k, v) for k, v in _DEFAULT_MIT_PARAMS.items()}
        return url, timeout, params
    except Exception:
        return _DEFAULT_URL, _DEFAULT_TIMEOUT, dict(_DEFAULT_MIT_PARAMS)


def _parse_stream(data: bytes) -> bytes:
    """
    解析 MIT 流式二进制响应，提取翻译结果图片字节。
    协议格式：[1B status_code][4B data_size BE][data_size B data] 反复。
    status_code=0 → 翻译完成，返回对应 data。
    status_code=2 → 错误，抛出异常。
    """
    buf = bytearray(data)
    while len(buf) >= 5:
        status_code = buf[0]
        data_size = struct.unpack(">I", buf[1:5])[0]
        total_size = 5 + data_size
        if len(buf) < total_size:
            break
        payload = bytes(buf[5:total_size])
        buf = buf[total_size:]

        if status_code == 0:
            return payload
        elif status_code == 2:
            raise ValueError(f"MIT 翻译失败: {payload.decode('utf-8', errors='replace')}")
        elif status_code == 1:
            logger.info(f"[MIT] 状态: {payload.decode('utf-8', errors='replace')}")
        elif status_code in (3, 4):
            pos = payload.decode('utf-8', errors='replace') if status_code == 3 else ""
            logger.info(f"[MIT] 排队中{f'（位置 {pos}）' if pos else ''}")

    raise ValueError("MIT 服务器未返回翻译图片（响应流提前结束）")


class TranslateService:
    @staticmethod
    async def translate(image_path: str, target_lang: str) -> bytes:
        """
        调用 MIT HTTP Server 翻译图片。
        image_path: 本地文件系统路径。
        target_lang: 目标语言代码（CHS / CHT / ENG / JPN / KOR / FRA / ...）。
        返回: 翻译后图片的原始字节（PNG）。
        """
        server_url, timeout, params = await _get_mit_config()
        url = f"{server_url.rstrip('/')}/translate/with-form/image/stream"

        with open(image_path, "rb") as f:
            image_bytes = f.read()

        ext = Path(image_path).suffix.lower() or ".png"
        mime = "image/png" if ext == ".png" else "image/jpeg"

        config = json.dumps({
            "detector": {
                "detector":       params["mit_detector"],
                "detection_size": int(params["mit_detection_size"]),
                "box_threshold":  float(params["mit_box_threshold"]),
                "unclip_ratio":   float(params["mit_unclip_ratio"]),
            },
            "render": {
                "direction": params["mit_direction"],
            },
            "translator": {
                "translator": params["mit_translator"],
                "target_lang": target_lang,
            },
            "inpainter": {
                "inpainter":       params["mit_inpainter"],
                "inpainting_size": int(params["mit_inpainting_size"]),
            },
            "mask_dilation_offset": int(params["mit_mask_dilation_offset"]),
        })

        logger.info(f"[MIT] POST {url}  lang={target_lang}  translator={params['mit_translator']}  size={len(image_bytes)} bytes")

        # 使用无读取超时 + 连接超时 10 秒；整体由 ARQ 任务超时管控
        http_timeout = httpx.Timeout(timeout=None, connect=10.0)
        async with httpx.AsyncClient(timeout=http_timeout) as client:
            resp = await client.post(
                url,
                data={"config": config},
                files={"image": (Path(image_path).name, image_bytes, mime)},
            )
            resp.raise_for_status()
            return _parse_stream(resp.content)

    @staticmethod
    async def ping(server_url: str | None = None) -> bool:
        """检测 MIT Server 是否在线（用于管理后台测试连接）。"""
        if server_url is None:
            server_url, _, _params = await _get_mit_config()
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(server_url.rstrip("/") + "/")
                return r.status_code < 500
        except Exception:
            return False

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from typing import Optional

from rich.logging import RichHandler
from rich.console import Console
from rich.theme import Theme


# 自定义终端主题
_THEME = Theme({
    "logging.level.debug":    "dim cyan",
    "logging.level.info":     "bold green",
    "logging.level.warning":  "bold yellow",
    "logging.level.error":    "bold red",
    "logging.level.critical": "bold white on red",
})

_console = Console(theme=_THEME, stderr=False)

# 按日期滚动的文件 formatter（纯文本，不含 ANSI 色码）
_FILE_FMT = logging.Formatter(
    fmt="%(asctime)s  [%(levelname)-8s]  %(name)s  |  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

LOG_DIR = "logs"


def setup_logging(log_level: str = "INFO", log_dir: Optional[str] = None):
    """
    初始化全局日志：
    - 终端：使用 Rich 彩色输出
    - 文件：每天零点滚动，保存到 {log_dir}/app_YYYY-MM-DD.log，保留 30 天
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    # ── 终端 Handler（Rich）──────────────────────────────────────────
    rich_handler = RichHandler(
        console=_console,
        show_time=True,
        show_level=True,
        show_path=True,
        rich_tracebacks=True,
        tracebacks_show_locals=False,
        markup=True,
    )
    rich_handler.setLevel(level)
    root.addHandler(rich_handler)

    # ── 文件 Handler（每日滚动）──────────────────────────────────────
    _log_dir = log_dir or LOG_DIR
    os.makedirs(_log_dir, exist_ok=True)

    log_path = os.path.join(_log_dir, "app.log")
    file_handler = TimedRotatingFileHandler(
        filename=log_path,
        when="midnight",       # 每天零点滚动
        interval=1,
        backupCount=30,        # 保留最近 30 天
        encoding="utf-8",
        utc=False,
    )
    # 滚动后的文件名格式：app.log.2026-05-06
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setFormatter(_FILE_FMT)
    file_handler.setLevel(level)
    root.addHandler(file_handler)

    # ── 降低第三方库噪音 ─────────────────────────────────────────────
    for noisy in ("uvicorn.access", "tortoise", "meilisearch", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        f"Logging ready  level=[bold]{log_level}[/bold]  "
        f"file=[cyan]{os.path.abspath(log_path)}[/cyan]"
    )

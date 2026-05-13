"""
结构化日志初始化（loguru）
========================
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def setup_logging(log_dir: str = "logs") -> None:
    """控制台 + 按日滚动文件。"""
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        enqueue=True,
        backtrace=False,
        diagnose=False,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    )
    logger.add(
        Path(log_dir) / "bot_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="14 days",
        encoding="utf-8",
        level="DEBUG",
        enqueue=True,
    )

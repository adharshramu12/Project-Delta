"""Structured logging: JSON lines to a rotating file, concise console output."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import structlog


def setup_logging(data_dir: Path, console_level: int = logging.WARNING) -> None:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "delta.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    console = logging.StreamHandler()
    console.setLevel(console_level)

    root = logging.getLogger()
    root.handlers[:] = [file_handler, console]
    root.setLevel(logging.DEBUG)
    for noisy in ("httpx", "httpcore", "openai", "primp", "urllib3", "trafilatura"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


log = structlog.get_logger("delta")

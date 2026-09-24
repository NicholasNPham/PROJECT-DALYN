"""Logging setup for DALYN.

One dated file per day in logs/, plus console output. Every module gets its
logger by calling get_logger(__name__) so each line records which file it
came from.
"""

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_dir: Path, console_level: int = logging.INFO) -> None:
    """Configure DALYN's root logger. Call once, at startup, before anything else.

    Args:
        log_dir: Directory for log files. Created if it does not exist.
        console_level: Minimum level printed to the console. The file always
            records DEBUG and above.
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger("dalyn")
    root_logger.setLevel(logging.DEBUG)

    if root_logger.handlers:
        return

    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT)

    file_handler = TimedRotatingFileHandler(
        filename=log_dir / "dalyn.log",
        when="midnight",
        interval=1,
        backupCount=90,
        encoding="utf-8",
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)


def get_logger(module_name: str) -> logging.Logger:
    """Return the logger for a module. Use get_logger(__name__) at the top of each file."""
    return logging.getLogger(f"dalyn.{module_name}")
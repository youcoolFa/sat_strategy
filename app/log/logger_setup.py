"""
app/log/logger_setup.py

參考 /Users/mac/fa_trade/Fa_Successful_trade/app/log/logger_setup.py 的結構改的:
    - console sink (INFO 以上,彩色)
    - rotating file sink (logs/sat_strategy.log,DEBUG 以上,含 backtrace/diagnose)
    - telegram sink (WARNING 以上,真的發送,不是 mock;斷線重連邏輯在 notifier.py)

使用方式:
    from app.log.logger_setup import setup_logger
    logger = setup_logger()
"""

from __future__ import annotations

import os
import sys
from typing import Any, cast

from loguru import logger
from loguru._logger import Logger

from app.notifier import notifier

_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs")


def _telegram_sink(message: Any) -> None:
    record = message.record
    text = f"[sat_strategy] {record['level'].name} | {record['message']}"
    notifier.send(text)


def setup_logger(quiet: bool = False) -> Logger:
    """
    quiet=True 用在臨時測試窗口模式 (test_window_minutes 有設定):dry-run 模式下
    進場/平倉會密集循環,一般的 INFO 等級會在短時間內灌爆 log。quiet 模式把
    console/file sink 的等級都拉高到 WARNING,只留 logger.warning()/error()/
    critical() 這些本來就是關鍵事件(啟動提示、_cleanup() 開始清理、真的異常)
    才會記錄的訊息。

    quiet=False (預設) 時行為跟原本完全一樣,不影響正式環境。
    """
    logger.remove()

    console_level = "WARNING" if quiet else "INFO"
    file_level = "WARNING" if quiet else "DEBUG"

    logger.add(
        sys.stdout,
        level=console_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<blue>{name}</blue>:<blue>{function}</blue>:<blue>{line}</blue> | "
            "<level>{message}</level>"
        ),
    )

    os.makedirs(_LOG_DIR, exist_ok=True)

    logger.add(
        os.path.join(_LOG_DIR, "sat_strategy.log"),
        level=file_level,
        rotation="10 MB",
        retention="14 days",
        backtrace=True,
        diagnose=True,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | "
            "{name}:{function}:{line} | {message}"
        ),
    )

    logger.add(
        _telegram_sink,
        level="WARNING",
    )

    return logger


if __name__ == "__main__":
    core = cast(Any, logger._core)  # type: ignore[attr-defined]
    setup_logger()
    logger.info("測試 log 設定")
    logger.warning("測試 WARNING (會嘗試發送到 Telegram,若 .env 沒設定 token 會略過)")

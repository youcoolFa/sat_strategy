"""
app/log/logger_setup.py

參考 /Users/mac/fa_trade/Fa_Successful_trade/app/log/logger_setup.py 的結構改的:
    - console sink (INFO 以上,彩色)
    - rotating file sink (logs/sat_strategy.log,DEBUG 以上,含 backtrace/diagnose)
    - telegram sink (WARNING 以上 + 標記 telegram=True 的重要事件;背景發送、冷卻時間、
      不會自己觸發自己,見 telegram_notifier.py——跟 Fa_Successful_trade 同一套)

使用方式:
    from app.log.logger_setup import setup_logger
    logger = setup_logger()
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional, cast

from loguru import logger
from loguru._logger import Logger

from app.log.log_limit import enforce_log_limit, max_total_mb_from_env, size_retention
from app.log.telegram_notifier import TelegramNotifier, telegram_filter

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LOG_DIR = os.path.join(_PROJECT_ROOT, "logs")

# log 總大小上限(MB),超過就從最舊的檔刪起,見 log_limit.py;可用 .env 的 LOG_MAX_TOTAL_MB 調整。
# 分兩組各自計算:logs/sat_strategy*.log(loguru 寫的),以及專案根目錄每次啟動一個的
# sat_strategy_<時間>.log(sat_strategy_start.sh 把終端機輸出導進去的)。
# logs/ 裡 launchd_*.log(排程啟動/停止紀錄)不在範圍內,不會被刪。
DEFAULT_LOG_MAX_TOTAL_MB = 300

# 每個 log 檔寫到這個大小就封存(輪替),封存的檔壓縮成 .log.gz(純文字約剩 1/10)。
# 正在寫的 sat_strategy.log 不壓縮。根目錄的 sat_strategy_<時間>.log 是 shell 導向的
# 終端機輸出,不經過 loguru,不會被壓縮(大小限制器照樣管)。
LOG_ROTATION = "10 MB"
LOG_COMPRESSION = "gz"


# Telegram 訊息標題用的名稱(對應 @sat_strategy_bot)
TELEGRAM_PROJECT = "sat_strategy"

_notifier: Optional[TelegramNotifier] = None


def get_notifier() -> TelegramNotifier:
    """全程式共用一個發送器(冷卻時間、連線狀態才會一致)。第一次用到才建立,
    這時 .env 已經載入;沒有 TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 就是停用狀態。"""
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier(project=TELEGRAM_PROJECT)
    return _notifier


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
    log_file = os.path.join(_LOG_DIR, "sat_strategy.log")
    max_total_mb = max_total_mb_from_env(DEFAULT_LOG_MAX_TOTAL_MB)
    enforce_log_limit(_LOG_DIR, max_total_mb, pattern="sat_strategy*.log*", protect=[log_file])
    enforce_log_limit(_PROJECT_ROOT, max_total_mb, pattern="sat_strategy_*.log")  # 正在寫的是剛建立的 → 不會刪

    logger.add(
        log_file,
        level=file_level,
        rotation=LOG_ROTATION,
        compression=LOG_COMPRESSION,
        retention=size_retention(_LOG_DIR, max_total_mb, pattern="sat_strategy*.log*", protect=[log_file]),
        backtrace=True,
        diagnose=True,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | "
            "{name}:{function}:{line} | {message}"
        ),
    )

    # Telegram:WARNING 以上(出問題)才發;重要事件用 logger.bind(telegram=True).info(...)
    # 也會發;不想發的 WARNING 用 logger.bind(telegram=False)。quiet 模式不影響這裡。
    logger.add(
        get_notifier().sink,
        level="INFO",
        filter=telegram_filter,
    )

    return logger


if __name__ == "__main__":
    core = cast(Any, logger._core)  # type: ignore[attr-defined]
    setup_logger()
    logger.info("測試 log 設定")
    logger.warning("測試 WARNING (會嘗試發送到 Telegram,若 .env 沒設定 token 會略過)")

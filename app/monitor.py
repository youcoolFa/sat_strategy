"""
app/monitor.py

監控包裝層:
    - 用 loguru 的 @logger.catch 包住 bot.run(),任何沒被內部處理到的例外
      都會被抓住、記錄完整堆疊(CRITICAL 等級,會經由 telegram sink 發送出去),
      不會讓 process 無聲無息地死掉
    - 背景執行緒定期呼叫 notifier.check_connection() 檢查 Telegram 連線,
      斷線/恢復狀態變化會記錄在本地 log (console + log 檔)

使用方式 (取代直接跑 app.bot):
    python -m app.monitor
"""

import threading

from app.bot import SatStrategyBot
from app.config import load_config
from app.log.logger_setup import setup_logger
from app.notifier import notifier

logger = setup_logger()

TELEGRAM_CHECK_INTERVAL_SECONDS = 300  # 5 分鐘檢查一次 Telegram 連線


def _telegram_health_check_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        notifier.check_connection()
        stop_event.wait(TELEGRAM_CHECK_INTERVAL_SECONDS)


@logger.catch(level="CRITICAL", reraise=False)
def _run_bot(bot: SatStrategyBot) -> None:
    bot.run()


def run_with_monitoring() -> None:
    config = load_config()
    if config.test_window_minutes is not None:
        setup_logger(quiet=True)
        logger.warning("=== 臨時測試窗口模式,log 等級調高到 WARNING,避免 dry-run 密集循環洗版 ===")
    logger.info(f"載入參數: {config.to_dict()}")

    if notifier.enabled:
        notifier.check_connection()
        logger.info(f"Telegram 通知已啟用,連線狀態: {notifier.connected}")
    else:
        logger.warning("Telegram 通知未設定 (缺 TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID),錯誤只會記錄在本地 log")

    stop_event = threading.Event()
    health_thread = threading.Thread(
        target=_telegram_health_check_loop, args=(stop_event,), daemon=True, name="telegram-health-check"
    )
    health_thread.start()

    try:
        bot = SatStrategyBot(config)
        _run_bot(bot)
    finally:
        stop_event.set()
        logger.info("monitor 結束")


if __name__ == "__main__":
    run_with_monitoring()

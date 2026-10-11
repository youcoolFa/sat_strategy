"""全部測試共用:確保測試絕對不會用 .env 裡的真 token 發 Telegram 訊息到手機。

要在「模組層級」就清空:app/bot.py 一被 import 就 load_dotenv() + setup_logger(),
等到 fixture 執行時發送器早就建好了。load_dotenv() 不會覆蓋已經存在的環境變數
(即使是空字串),所以先設成空字串就擋住了。"""

import os

import pytest

os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
os.environ["TELEGRAM_TRADE_BOT_TOKEN"] = ""  # 交易快報 / 系統警報(2026-10-11)也不能真的發
os.environ["TELEGRAM_TRADE_CHAT_ID"] = ""
os.environ["TELEGRAM_SYSTEM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_SYSTEM_CHAT_ID"] = ""
# log 大小限制器在測試裡不清任何東西;要測限制器的測試自己 monkeypatch。
os.environ["LOG_MAX_TOTAL_MB"] = "1000000"

# 所有測試的 log 寫到臨時資料夾,不寫真的 logs/sat_strategy.log(2026-10-11:app/bot.py 一 import 就
# setup_logger(),測試的假紀錄跟週末真錢 BTC 的 log 混在一起)。要在任何測試模組 import 前就換掉。
import tempfile  # noqa: E402

from app.log import logger_setup as _logger_setup  # noqa: E402

_logger_setup._LOG_DIR = tempfile.mkdtemp(prefix="sat_test_logs_")


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch):
    from app.log import logger_setup

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("TELEGRAM_TRADE_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_TRADE_CHAT_ID", "")
    monkeypatch.setenv("TELEGRAM_SYSTEM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_SYSTEM_CHAT_ID", "")
    monkeypatch.setattr(logger_setup, "_notifier", None)
    yield

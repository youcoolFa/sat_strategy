"""全部測試共用:確保測試絕對不會用 .env 裡的真 token 發 Telegram 訊息到手機。

要在「模組層級」就清空:app/bot.py 一被 import 就 load_dotenv() + setup_logger(),
等到 fixture 執行時發送器早就建好了。load_dotenv() 不會覆蓋已經存在的環境變數
(即使是空字串),所以先設成空字串就擋住了。"""

import os

import pytest

os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
# log 大小限制器在測試裡不清任何東西(有些測試會在真的 logs/ 設定 log);要測限制器的測試自己 monkeypatch。
os.environ["LOG_MAX_TOTAL_MB"] = "1000000"


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch):
    from app.log import logger_setup

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(logger_setup, "_notifier", None)
    yield

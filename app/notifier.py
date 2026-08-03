"""
app/notifier.py

Telegram 通知器:把 log 模組抓到的 WARNING/ERROR 發送到 Telegram。

斷線重連設計:
    - 每次發送失敗會重試 (指數退避),重試次數用完才放棄
    - 連線狀態 (self.connected) 會被追蹤,狀態改變 (斷線 <-> 恢復) 會記錄在本地 log,
      這樣即使 Telegram 本身送不出去,狀態變化在 bot 自己的 log/console 裡還是看得到
    - 絕對不會因為「發送失敗」又跑去呼叫 send() 通知 Telegram 自己壞了,避免無窮迴圈;
      失敗到底就只記錄本地 log

環境變數:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
"""

import os
import time

import requests
from loguru import logger


class TelegramNotifier:
    def __init__(self, bot_token=None, chat_id=None, max_retries=3, timeout=10):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = requests.Session()
        self.connected = None  # None = 還沒測試過連線狀態

    @property
    def enabled(self):
        return bool(self.bot_token and self.chat_id)

    def _api_url(self, method):
        return f"https://api.telegram.org/bot{self.bot_token}/{method}"

    def check_connection(self):
        """呼叫 getMe 驗證連線是否正常,狀態變化會記錄 log。給 monitor 定期呼叫用。"""
        if not self.enabled:
            return False
        try:
            resp = self.session.get(self._api_url("getMe"), timeout=self.timeout)
            resp.raise_for_status()
            ok = bool(resp.json().get("ok"))
        except requests.RequestException:
            ok = False
        self._update_connection_state(ok)
        return ok

    def _update_connection_state(self, ok):
        if self.connected is False and ok:
            logger.info("Telegram 連線已恢復")
        elif self.connected is not False and not ok:
            logger.warning("Telegram 連線中斷")
        self.connected = ok

    def send(self, message):
        """發送訊息到 Telegram,失敗會重試。回傳是否成功送出。"""
        if not self.enabled:
            logger.debug(f"Telegram 未設定 (缺 TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID),略過發送: {message}")
            return False

        for attempt in range(self.max_retries):
            try:
                resp = self.session.post(
                    self._api_url("sendMessage"),
                    data={"chat_id": self.chat_id, "text": message},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                self._update_connection_state(True)
                return True
            except requests.RequestException as exc:
                was_connected = self.connected is not False
                self.connected = False
                if was_connected:
                    logger.warning(f"Telegram 發送失敗(可能斷線),第 {attempt + 1} 次重試: {exc}")
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)

        logger.error(f"Telegram 訊息發送最終失敗(已重試 {self.max_retries} 次),只記錄在本地 log: {message}")
        return False


# 全域共用的 notifier instance,logger_setup / monitor 都用同一個,狀態才會一致
notifier = TelegramNotifier()

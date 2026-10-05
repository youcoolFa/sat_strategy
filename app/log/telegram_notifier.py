"""
app/log/telegram_notifier.py

Telegram 發送器:logger_setup.py 把它的 `sink` 接到 loguru,出問題(WARNING 以上)
或明確標記的重要事件(`logger.bind(telegram=True).info(...)`)會發到 Telegram。

設計重點(三個專案 Fa_Successful_trade / sat_strategy / strategy_lab 用同一套):
    - 背景 thread 發送:記 log 的地方只是把訊息丟進 queue,不會因為 Telegram
      很慢或斷線而卡住交易/收資料的主流程。queue 滿了就丟掉並計數,絕不阻塞。
    - 冷卻時間:同一個發生位置(模組:函式:行)在冷卻時間內只發一則,下一則會附上
      「期間略過 N 則」。問題持續(冷卻期間又有新的)冷卻時間就加倍,5 → 10 → 20 分鐘…
      最長 max_throttle_seconds(6 小時);安靜超過冷卻時間後恢復成 throttle_seconds。
      避免斷線一整個週末、每 5 秒一則的重試訊息洗版。明確標記的事件(telegram=True)
      不受冷卻限制。
    - 不會自己觸發自己:發送失敗只用 `logger.bind(telegram=False)` 記本地 log,
      telegram_filter 會擋掉,不會變成「發送失敗 → 記 WARNING → 再發送」的迴圈。
    - 程式結束前(atexit)最多等 10 秒把 queue 裡的訊息送完,崩潰訊息才不會遺失。
    - 類別樣式:Telegram 文字不能上色,改用「彩色圓點 + hashtag」當標題(點 hashtag 可以
      篩出同一類訊息)。事件用 logger.bind(telegram=True, category="fill") 指定類別,
      沒指定 = 系統;WARNING/ERROR/CRITICAL 依等級。用 HTML 模式(標題粗體),訊息內容
      一律轉義;萬一 Telegram 還是拒絕(400),改用純文字重送一次。

環境變數:TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID(在 .env,不進 git)。
"""

from __future__ import annotations

import atexit
import html
import os
import re
import queue
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

import requests
from loguru import logger

MAX_MESSAGE_CHARS = 4000  # Telegram 上限 4096,留一點餘裕
MAX_BODY_CHARS = 3500  # 訊息內容先截到這個長度再轉義,避免截斷時切壞 HTML
# 事件類別:logger.bind(telegram=True, category=<key>)。沒指定類別的事件 = system。
CATEGORY_STYLES = {
    "system": ("🔵", "系統"),  # 啟動、停止、重連
    "fill": ("🟢", "成交"),  # 買入、賣出、平倉成交
    "pnl": ("🟣", "損益"),  # 每輪/每個 event 損益、結束總結
    "position": ("🟠", "持倉"),  # 開倉、平倉、反手
    "equity": ("🟤", "權益"),  # 權益大幅變動
    "status": ("⚪", "狀態"),  # 定時狀態回報(心跳):收到 = 還活著
}
LEVEL_STYLES = {"WARNING": ("🟡", "警告"), "ERROR": ("🔴", "錯誤"), "CRITICAL": ("🆘", "嚴重")}
_TAG_RE = re.compile(r"<[^>]+>")
_WARNING_NO = 30


def telegram_filter(record: Dict[str, Any]) -> bool:
    """WARNING 以上預設發送;telegram=True 的 INFO 事件也發送;telegram=False 一律不發。"""
    flag = record["extra"].get("telegram")
    if flag is False:
        return False
    return flag is True or record["level"].no >= _WARNING_NO


class TelegramNotifier:
    def __init__(
        self,
        project: str,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        timeout: float = 10,
        max_retries: int = 3,
        retry_base_seconds: float = 1.0,
        throttle_seconds: float = 300,
        max_throttle_seconds: float = 6 * 3600,
        max_queue: int = 200,
        session: Any = None,
        clock: Callable[[], float] = time.monotonic,
        start_worker: bool = True,
    ) -> None:
        self.project = project
        self.bot_token = bot_token if bot_token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id if chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self.throttle_seconds = throttle_seconds
        self.max_throttle_seconds = max_throttle_seconds
        self.session = session or requests.Session()
        self.clock = clock
        self.connected: Optional[bool] = None  # None = 還沒測試過
        self.dropped = 0  # 沒送出去的訊息數(queue 滿、重試用完)
        self._queue: "queue.Queue[Tuple[str, Optional[str]]]" = queue.Queue(maxsize=max_queue)
        # 發生位置 -> (上次發送時間, 冷卻期間略過幾則, 目前冷卻秒數)
        self._throttle: Dict[Tuple[str, str, int], Tuple[float, int, float]] = {}
        self._lock = threading.Lock()
        if start_worker and self.enabled:
            threading.Thread(target=self._worker, daemon=True, name="telegram-notifier").start()
            atexit.register(self.flush, 10)

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    # ---------- 對外 ----------

    def notify(self, text: str, parse_mode: Optional[str] = None) -> bool:
        """直接發一則訊息(不經過 log;預設純文字)。不阻塞;回傳是否成功放進發送 queue。"""
        if not self.enabled:
            return False
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[: MAX_MESSAGE_CHARS - 20] + "\n…(訊息過長已截斷)"
        try:
            self._queue.put_nowait((text, parse_mode))
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def sink(self, message: Any) -> None:
        """loguru sink:格式化 + 冷卻時間,然後丟進 queue。永遠不拋例外。"""
        try:
            record = message.record
            event = record["extra"].get("telegram") is True
            suffix = ""
            if not event:
                suppressed = self._throttled(record)
                if suppressed is None:
                    return
                if suppressed:
                    suffix = f"\n<i>(上次通知後,同一位置的訊息另外略過 {suppressed} 則)</i>"
            self.notify(self.format(record) + suffix, parse_mode="HTML")
        except Exception:  # noqa: BLE001  sink 出錯不能影響主程式
            pass

    def style(self, record: Dict[str, Any]) -> Tuple[str, str]:
        """(彩色圓點, 類別標籤)。出問題依等級;事件依 category,沒指定 = 系統。"""
        level = record["level"].name
        if level in LEVEL_STYLES:
            return LEVEL_STYLES[level]
        return CATEGORY_STYLES.get(record["extra"].get("category"), CATEGORY_STYLES["system"])

    def format(self, record: Dict[str, Any]) -> str:
        """HTML:粗體標題(圓點 + #類別 + 專案)、斜體時間(出問題時再加程式位置)、內容。"""
        icon, tag = self.style(record)
        when = record["time"].strftime("%Y-%m-%d %H:%M:%S")
        meta = when
        if record["level"].name in LEVEL_STYLES:  # 出問題才需要知道是哪一行;事件不用
            meta += f"｜{record['name']}:{record['function']}:{record['line']}"
        body = str(record["message"])
        exc = record.get("exception")
        if exc is not None and exc.type is not None:
            body += f"\n\n{exc.type.__name__}: {exc.value}"
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS] + "\n…(訊息過長已截斷)"
        return (f"<b>{icon} #{tag}｜{html.escape(self.project)}</b>\n<i>{html.escape(meta)}</i>\n\n"
                f"{html.escape(body)}")

    @staticmethod
    def to_plain(text: str) -> str:
        return html.unescape(_TAG_RE.sub("", text))

    def check_connection(self) -> bool:
        """呼叫 getMe 驗證連線,狀態變化記本地 log。給定期健康檢查用。"""
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

    def flush(self, timeout: float = 10) -> bool:
        """等 queue 裡的訊息處理完(送出或放棄)。回傳是否在時限內完成。"""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.01)
        return True

    # ---------- 內部 ----------

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}/{method}"

    def _throttled(self, record: Dict[str, Any]) -> Optional[int]:
        """None = 冷卻中,這則不發;數字 = 可以發,數字是冷卻期間略過了幾則。"""
        key = (record["name"], record["function"], record["line"])
        now = self.clock()
        with self._lock:
            last, suppressed, window = self._throttle.get(key, (None, 0, self.throttle_seconds))
            if last is not None and now - last < window:
                self._throttle[key] = (last, suppressed + 1, window)
                return None
            # 冷卻期間有被略過 = 問題還在持續 → 下一輪冷卻加倍;沒有 = 安靜過了 → 恢復基本值
            window = min(window * 2, self.max_throttle_seconds) if suppressed else self.throttle_seconds
            self._throttle[key] = (now, 0, window)
            return suppressed

    def _worker(self) -> None:
        while True:
            text, parse_mode = self._queue.get()
            try:
                self._send(text, parse_mode)
            finally:
                self._queue.task_done()

    def _send(self, text: str, parse_mode: Optional[str] = None) -> None:
        local = logger.bind(telegram=False)
        for attempt in range(self.max_retries):
            data = {"chat_id": self.chat_id, "text": text}
            if parse_mode:
                data["parse_mode"] = parse_mode
            try:
                resp = self.session.post(self._api_url("sendMessage"), data=data, timeout=self.timeout)
                resp.raise_for_status()
                self._update_connection_state(True)
                return
            except requests.HTTPError as exc:
                status = getattr(exc.response, "status_code", None)
                if parse_mode and status == 400:
                    # 格式被拒(例如 HTML 解析失敗):改純文字重送,內容比格式重要
                    local.warning("Telegram 拒絕 HTML 格式,改用純文字重送")
                    text, parse_mode = self.to_plain(text), None
                    continue
                self._update_connection_state(False)
                local.warning(f"Telegram 發送失敗,第 {attempt + 1}/{self.max_retries} 次:HTTP {status}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_base_seconds * (2 ** attempt))
            except requests.RequestException as exc:
                self._update_connection_state(False)
                local.warning(f"Telegram 發送失敗,第 {attempt + 1}/{self.max_retries} 次:{type(exc).__name__}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_base_seconds * (2 ** attempt))
        self.dropped += 1
        local.error(f"Telegram 發送最終失敗,只記在本地 log:{text[:200]}")

    def _update_connection_state(self, ok: bool) -> None:
        if self.connected is False and ok:
            logger.bind(telegram=True).info("Telegram 連線已恢復")
        elif self.connected is not False and not ok:
            logger.bind(telegram=False).warning("Telegram 連線中斷")
        self.connected = ok

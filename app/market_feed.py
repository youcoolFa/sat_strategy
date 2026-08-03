"""
app/market_feed.py

訂閱 Fa_Successful_trade（獨立專案，負責接收/解析 Bybit WS 資料）透過 Redis 廣播出來的
即時事件，取代/補充 bot.py 原本用 ccxt 做的 REST 輪詢。

Fa_Successful_trade 那邊的分工（見 app/market_data/event_publisher.py）：
    - ticker（高頻、公開行情）  -> Redis Pub/Sub，channel "bybit:ticker"
    - wallet/position/order（低頻、帳戶私有資料）-> Redis Streams，
      stream "bybit:wallet" / "bybit:position" / "bybit:order"

這裡只實作 TickerFeed（Pub/Sub）：
    ticker 是公開市場資料，不管哪個 Bybit 帳戶都是同一份，接進來沒有風險。
    wallet/position/order 是「哪個帳戶」的資料，Fa_Successful_trade 目前接的帳戶
    (mainnet) 不一定是 sat_strategy 自己下單的帳戶（sat_strategy 預設 testnet）——
    兩邊帳戶對不上的話，接這些資料反而會誤導策略判斷（例如以為某張單成交了，其實
    那是另一個帳戶的單）。等確認兩邊真的是同一個帳戶/同一個網路，再照同樣的 Streams
    + consumer group 模式加 order/wallet feed，不要在還沒確認之前就先接上去。

用同步 redis client（不是 redis.asyncio）：bot.py 整個是同步、time.sleep() 輪詢的
架構，沒有 asyncio event loop，硬塞 async client 進來會需要重寫整個 bot，不划算。
"""

import json
import os
import threading

import redis
from loguru import logger

TICKER_CHANNEL = "bybit:ticker"


class TickerFeed:
    """
    背景 thread 訂閱 Redis Pub/Sub 的 bybit:ticker，把收到的最新價格存進
    thread-safe 的變數，供 bot.py 主迴圈呼叫 get_last_price() 讀取。

    連不上 Redis、或還沒收到任何訊息時，get_last_price() 回傳 None——呼叫端
    （bot.py）要自己決定 fallback 回 REST（不要讓 bot 完全依賴這個 feed 是否活著）。
    """

    def __init__(self, redis_url: str = None):
        self._redis_url = redis_url or os.getenv("REDIS_URL")
        self._lock = threading.Lock()
        self._last_price = None
        self._thread = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if not self._redis_url:
            logger.warning("[TickerFeed] REDIS_URL 未設定，不會啟動即時行情訂閱。")
            return

        self._thread = threading.Thread(
            target=self._run, daemon=True, name="ticker-feed"
        )
        self._thread.start()
        logger.info("[TickerFeed] 已啟動，訂閱 Redis channel: %s" % TICKER_CHANNEL)

    def stop(self) -> None:
        self._stop_event.set()

    def get_last_price(self):
        with self._lock:
            return self._last_price

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._subscribe_loop()
            except redis.RedisError as e:
                logger.warning(f"[TickerFeed] Redis 連線發生問題，5 秒後重試：{e}")
                self._stop_event.wait(5)

    def _subscribe_loop(self) -> None:
        client = redis.Redis.from_url(self._redis_url)
        pubsub = client.pubsub()
        pubsub.subscribe(TICKER_CHANNEL)

        for message in pubsub.listen():
            if self._stop_event.is_set():
                break
            if message["type"] != "message":
                continue

            try:
                data = json.loads(message["data"])
                price = float(data["last_price"])
            except (ValueError, KeyError, TypeError) as e:
                logger.warning(f"[TickerFeed] 收到的訊息格式不對，略過：{e}")
                continue

            with self._lock:
                self._last_price = price


# 全域共用的 instance，跟 notifier.py 的 `notifier` 同一種用法：
# bot.py 直接 import 這個 instance 使用，不用自己管生命週期。
ticker_feed = TickerFeed()

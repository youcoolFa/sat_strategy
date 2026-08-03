"""
app/account_feed.py

訂閱 Fa_Successful_trade 透過 Redis Streams 廣播的 wallet/position/order
(bybit:wallet / bybit:position / bybit:order)，用 consumer group 讀取，
離線期間漏掉的事件重啟後能接著讀（Redis consumer group 的 last-delivered-id
跨連線持久）。

跟 market_feed.py 的 TickerFeed 分開一個檔案：ticker 用 Pub/Sub（不需要
consumer group、不需要 ack，漏了就漏了、下一筆馬上又來）；wallet/position/
order 用 Streams + consumer group（每一筆帳戶事件都重要，不能漏），兩種訂閱
模型的取捨完全不同，混在同一個檔案裡容易搞混。

payload 欄位對照 Fa_Successful_trade 的
app/market_data/account_parser.py / event_publisher.py：
    wallet:   {coin, account_type, equity, wallet_balance,
               available_to_withdraw, unrealised_pnl, cum_realised_pnl,
               total_equity}
    position: {settle_coin, symbol, size, position_value, avg_price,
               unrealised_pnl}
    order:    {symbol, order_id, side, order_type, price, qty, order_status}

只保留「最新一筆」，跟 Fa_Successful_trade 的 AccountStateHolder 設計一致：
wallet/position 依 coin/settle_coin 分別保留最新狀態；order 只保留最新一筆。
"""

import json
import os
import threading

import redis
from loguru import logger

WALLET_STREAM = "bybit:wallet"
POSITION_STREAM = "bybit:position"
ORDER_STREAM = "bybit:order"

CONSUMER_GROUP = "sat_strategy"
CONSUMER_NAME = "sat_strategy-1"


class AccountFeed:
    """
    背景 thread 用 XREADGROUP 同時讀 wallet/position/order 三個 Streams，
    更新 thread-safe 的最新狀態，供 bot.py 讀取。

    consumer group 建立時從「當下最新一筆」開始讀（id="$"），不重播
    Fa_Successful_trade 啟動以來的完整歷史——跟 TickerFeed 的取捨一致：
    sat_strategy 只在意「現在的帳戶狀態」。group 一旦建立，之後 sat_strategy
    重啟不會重新從 "$" 開始，而是接著讀離線期間漏掉的事件。

    連不上 Redis、或還沒收到任何訊息時，get_latest_*() 回傳 None——呼叫端
    要自己決定 fallback（例如維持現有的 REST 輪詢），不要讓邏輯完全依賴
    這個 feed 是否活著。
    """

    def __init__(self, redis_url: str = None):
        self._redis_url = redis_url or os.getenv("REDIS_URL")
        self._lock = threading.Lock()
        self._wallets: dict[str, dict] = {}
        self._positions: dict[str, dict] = {}
        self._latest_order: dict | None = None
        self._thread = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if not self._redis_url:
            logger.warning("[AccountFeed] REDIS_URL 未設定，不會啟動帳戶事件訂閱。")
            return

        self._thread = threading.Thread(
            target=self._run, daemon=True, name="account-feed"
        )
        self._thread.start()
        logger.info(
            f"[AccountFeed] 已啟動，訂閱 Redis streams: "
            f"{WALLET_STREAM}, {POSITION_STREAM}, {ORDER_STREAM}"
        )

    def stop(self) -> None:
        self._stop_event.set()

    def get_latest_wallet(self, coin: str):
        with self._lock:
            fields = self._wallets.get(coin)
            return dict(fields) if fields else None

    def get_latest_position(self, settle_coin: str):
        with self._lock:
            fields = self._positions.get(settle_coin)
            return dict(fields) if fields else None

    def get_latest_order(self):
        with self._lock:
            return dict(self._latest_order) if self._latest_order else None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._consume_loop()
            except redis.RedisError as e:
                logger.warning(f"[AccountFeed] Redis 連線發生問題，5 秒後重試：{e}")
                self._stop_event.wait(5)

    def _ensure_groups(self, client) -> None:
        for stream in (WALLET_STREAM, POSITION_STREAM, ORDER_STREAM):
            try:
                client.xgroup_create(stream, CONSUMER_GROUP, id="$", mkstream=True)
            except redis.ResponseError as e:
                if "BUSYGROUP" not in str(e):
                    raise

    def _consume_loop(self) -> None:
        client = redis.Redis.from_url(self._redis_url, decode_responses=True)
        self._ensure_groups(client)
        self._drain_pending(client)

        streams = {WALLET_STREAM: ">", POSITION_STREAM: ">", ORDER_STREAM: ">"}

        while not self._stop_event.is_set():
            response = client.xreadgroup(
                CONSUMER_GROUP, CONSUMER_NAME, streams, count=50, block=5000
            )
            if not response:
                continue

            for stream_name, entries in response:
                for entry_id, entry_data in entries:
                    self._handle_entry(stream_name, entry_data)
                    client.xack(stream_name, CONSUMER_GROUP, entry_id)

    def _drain_pending(self, client) -> None:
        """
        補讀「上次已經投遞給這個 consumer、但還沒來得及 ack 就當機」的舊訊息。

        CONSUMER_NAME 是固定字串，重啟後的新 process 會接手同一個 consumer
        身分，Redis 那些訊息還卡在它名下的 pending list 裡——只查詢 ">"
        (從未投遞過的新訊息) 永遠不會拿到這些訊息，事件會被永久漏掉。這裡
        用 id="0" 明確要求「這個 consumer 名下所有還沒 ack 的舊訊息」，處理
        完再進入正常的 ">" 迴圈。
        """
        streams = {WALLET_STREAM: "0", POSITION_STREAM: "0", ORDER_STREAM: "0"}

        while not self._stop_event.is_set():
            response = client.xreadgroup(CONSUMER_GROUP, CONSUMER_NAME, streams, count=50)
            if not response or not any(entries for _, entries in response):
                break

            for stream_name, entries in response:
                for entry_id, entry_data in entries:
                    self._handle_entry(stream_name, entry_data)
                    client.xack(stream_name, CONSUMER_GROUP, entry_id)

    def _handle_entry(self, stream_name: str, entry_data: dict) -> None:
        try:
            data = json.loads(entry_data["data"])
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"[AccountFeed] 收到的訊息格式不對，略過（{stream_name}）：{e}")
            return

        if stream_name == WALLET_STREAM:
            coin = data.get("coin")
            if coin:
                with self._lock:
                    self._wallets[coin] = data
        elif stream_name == POSITION_STREAM:
            settle_coin = data.get("settle_coin")
            if settle_coin:
                with self._lock:
                    self._positions[settle_coin] = data
        elif stream_name == ORDER_STREAM:
            with self._lock:
                self._latest_order = data


# 全域共用的 instance，跟 market_feed.py 的 ticker_feed 同一種用法：
# bot.py 直接 import 這個 instance 使用，不用自己管生命週期。
account_feed = AccountFeed()

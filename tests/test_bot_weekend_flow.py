"""
整個週末流程的整合測試(2026-10-07):跟 test_bot_logic.py 不同,這裡**不**替換 bot 自己的方法
(_wait_until_filled_or_stop / _place_*_order / _cleanup / _window_end 全部是真的),只把兩樣東西換成假的:

    - 交易所:FakeExchange(ccxt 介面的子集),有掛單撮合(價格碰到限價才成交)、持倉、手續費、取消、市價單
    - 時鐘:從 HKT 週六 04:00 開始,bot 每次 time.sleep 就把假時鐘往前推,一個週末幾秒內跑完

Telegram 走真的 TelegramNotifier 格式化(假 HTTP、不開背景 thread),驗證手機會收到的內容。
"""

from datetime import datetime, timedelta

import ccxt
import pytest
from loguru import logger

import app.bot as bot_module
from app.bot import HKT, SatStrategyBot
from app.config import StrategyConfig
from app.log.telegram_notifier import TelegramNotifier, telegram_filter

SYMBOL = "BTC/USDT:USDT"
START = datetime(2026, 10, 10, 4, 0, tzinfo=HKT)  # 週六 04:00 HKT
FEE_RATE = 0.0002  # 假交易所回報的手續費率(掛單)
TAKER_RATE = 0.00055


class Clock:
    def __init__(self, start):
        self.now = start

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


class FakeExchange:
    """價格是時間的函數(price_at);每次查詢訂單時依當下價格撮合。"""

    def __init__(self, clock, price_at):
        self.clock = clock
        self.price_at = price_at
        self.orders = {}
        self.position = 0.0
        self.log = []  # (時間, 動作, 細節)
        self.no_market_fee = False  # True = 市價單查不到手續費(測 bot 的估算 fallback)
        self._next_id = 0

    def price(self):
        return self.price_at(self.clock.now)

    def _new(self, side, qty, price, reduce_only):
        self._next_id += 1
        oid = f"o{self._next_id}"
        self.orders[oid] = {"id": oid, "side": side, "amount": qty, "price": price, "reduceOnly": reduce_only,
                            "status": "open", "filled": 0.0, "fee": None}
        self.log.append((self.clock.now, f"{side}{'(reduceOnly)' if reduce_only else ''}", price, qty))
        return {"id": oid, "status": "open"}

    def _match(self, order):
        if order["status"] != "open":
            return
        p = self.price()
        hit = p <= order["price"] if order["side"] == "buy" else p >= order["price"]
        if hit:
            order.update(status="closed", filled=order["amount"],
                         fee={"cost": order["price"] * order["amount"] * FEE_RATE, "currency": "USDT"})
            self.position += order["amount"] if order["side"] == "buy" else -order["amount"]
            self.log.append((self.clock.now, "filled", order["id"], order["price"]))

    # ---- ccxt 介面 ----
    def fetch_ticker(self, symbol):
        return {"symbol": symbol, "last": self.price()}

    def create_limit_buy_order(self, symbol, qty, price, params=None):
        return self._new("buy", qty, price, False)

    def create_limit_sell_order(self, symbol, qty, price, params=None):
        assert params == {"reduceOnly": True}, "平倉單一定要 reduceOnly"
        assert qty <= self.position + 1e-12, "平倉數量不能超過持倉"
        return self._new("sell", qty, price, True)

    def fetch_open_order(self, oid, symbol):
        order = self.orders[oid]
        self._match(order)
        if order["status"] != "open":
            raise ccxt.OrderNotFound(oid)
        return dict(order)

    def fetch_closed_order(self, oid, symbol):
        return dict(self.orders[oid])

    def cancel_order(self, oid, symbol):
        order = self.orders.get(oid)
        if order is None or order["status"] != "open":
            raise ccxt.OrderNotFound(oid)
        order["status"] = "canceled"
        self.log.append((self.clock.now, "canceled", oid, order["price"]))

    def fetch_open_orders(self, symbol):
        return [dict(o) for o in self.orders.values() if o["status"] == "open"]

    def fetch_positions(self, symbols):
        return [{"symbol": SYMBOL, "contracts": abs(self.position) if self.position else 0}]

    def create_market_sell_order(self, symbol, qty, params=None):
        assert params == {"reduceOnly": True}
        self.position -= qty
        price = self.price()
        self.log.append((self.clock.now, "market sell", price, qty))
        # 跟真 Bybit 一樣:下單回應只有 id,成交均價/手續費要再查一次訂單才拿得到
        self.orders["m1"] = {"id": "m1", "side": "sell", "amount": qty, "price": None, "average": price,
                             "reduceOnly": True, "status": "closed", "filled": qty,
                             "fee": None if self.no_market_fee else {"cost": price * qty * TAKER_RATE, "currency": "USDT"}}
        return {"id": "m1", "status": "closed"}


def price_path(t):
    """週六 04:00 起點 80000 → 06:00 跌到 79300(買入成交)→ 10:00 回到 80100(平倉成交)
    → 14:00 再跌到 79000(第二次買入)→ 之後一直沒回來,週一收尾時市價平倉。"""
    hours = (t - START).total_seconds() / 3600
    if hours < 2:
        return 80000.0
    if hours < 6:
        return 79300.0
    if hours < 10:
        return 80100.0
    return 79000.0


def run_weekend(monkeypatch, no_market_fee=False):
    monkeypatch.setenv("STATUS_INTERVAL_MINUTES", "60")
    clock = Clock(START)
    monkeypatch.setattr(bot_module, "time", clock)  # bot 裡的 time.sleep → 推進假時鐘
    config = StrategyConfig(symbol=SYMBOL, order_qty=0.01, dry_run=False, testnet=False, poll_interval_seconds=60)
    bot = SatStrategyBot(config)
    bot.exchange = FakeExchange(clock, price_path)
    bot.exchange.no_market_fee = no_market_fee
    monkeypatch.setattr(bot, "_now_hkt", lambda: clock.now)

    notifier = TelegramNotifier(project="sat_strategy", bot_token="123:abc", chat_id="42", start_worker=False)
    sink = logger.add(notifier.sink, level="INFO", filter=telegram_filter)  # 跟 setup_logger 同樣的接法
    try:
        bot.run()
    finally:
        logger.remove(sink)
    messages = []
    while not notifier._queue.empty():
        messages.append(notifier._queue.get_nowait()[0])
    return bot, bot.exchange, clock, messages


@pytest.fixture
def weekend(monkeypatch):
    return run_weekend(monkeypatch)


def by_tag(messages, tag):
    return [m for m in messages if m.startswith(f"<b>{tag}｜sat_strategy</b>")]


def test_orders_follow_the_strategy(weekend):
    _, ex, _, _ = weekend
    placed = [(when, action, price, qty) for when, action, price, qty in ex.log if action in ("buy", "sell(reduceOnly)")]
    # 起點 80000 → 買入 80000 × (1 − 0.75%) = 79400;平倉回到起點 80000(reduceOnly)
    assert [(a, p) for _, a, p, _ in placed] == [
        ("buy", 79400.0), ("sell(reduceOnly)", 80000.0),  # 第 1 輪
        ("buy", 79400.0), ("sell(reduceOnly)", 80000.0),  # 第 2 輪:平倉成交後才補回買單
    ]
    assert placed[0][0] == START  # 一啟動就掛
    assert placed[2][0] >= START + timedelta(hours=6)  # 第一筆平倉成交前不會重複開倉


def test_cleanup_before_window_end_leaves_nothing_behind(weekend):
    _, ex, clock, _ = weekend
    cleanup_at = datetime(2026, 10, 12, 5, 55, tzinfo=HKT)  # 週一 06:00 − 5 分鐘
    assert cleanup_at <= clock.now < cleanup_at + timedelta(minutes=2)
    assert ex.fetch_open_orders(SYMBOL) == []
    assert ex.position == 0  # 第二輪的持倉在收尾時市價平掉
    canceled = [e for e in ex.log if e[1] == "canceled"]
    market = [e for e in ex.log if e[1] == "market sell"]
    assert len(canceled) == 1 and canceled[0][3] == 80000.0  # 取消的是還沒成交的平倉單
    assert len(market) == 1 and market[0][3] == 0.01


def test_telegram_start_fills_and_heartbeats(weekend):
    _, _, _, messages = weekend
    [start] = by_tag(messages, "🔵 #系統")
    assert "🚀 sat_strategy 啟動(實盤)" in start and "origin 80000.00 → 買入 79400.00(−0.75%)× 0.01" in start
    assert "窗口結束 2026-10-12 06:00 HKT" in start

    fills = by_tag(messages, "🟢 #成交")
    assert len(fills) == 3  # 買入、平倉(第 1 輪)、第 2 次買入
    assert "✅ 買入成交 0.01 @ 79400.00" in fills[0]
    # 毛利 (80000 − 79400) × 0.01 = 6.00;手續費用交易所回報的 79400×0.01×0.02% + 80000×0.01×0.02% = 0.3188
    closed = fills[1]
    assert "第 1 輪完成" in closed and "淨利 +5.68 USDT(毛利 +6.00 − 手續費 0.3188)" in closed
    assert "估算" not in closed and "手續費佔利益 5.31%" in closed

    beats = by_tag(messages, "⚪ #狀態")
    assert 48 <= len(beats) <= 50  # 週六 05:00 起每小時一則,到週一 05:55
    assert "狀態:掛買單等待成交 @ 79400.00" in beats[0]
    assert any("狀態:持倉 0.01 @ 79400.00" in b and "未實現 -4.00" in b for b in beats)
    assert "已完成 1 輪" in beats[-1]

    [warn] = by_tag(messages, "🟡 #警告")
    assert "還有 0.0100 未平倉,用市價平倉" in warn  # 收尾要動用市價單 → 手機會知道


def test_end_summary_is_sent(weekend):
    _, _, _, messages = weekend
    [end] = by_tag(messages, "🟣 #損益")
    assert "🏁 sat_strategy 結束(窗口到期)" in end and "完成 1 輪" in end


def test_end_summary_includes_the_forced_close(weekend):
    """2026-10-07 修正:結束總結以前只算完成的輪,收尾市價平倉的虧損和手續費都漏掉(報 +5.68,實際約 +1.09)。"""
    _, _, _, messages = weekend
    [end] = by_tag(messages, "🟣 #損益")
    # 第 2 輪:買 79400、收尾市價 79000 → 毛利 −4.00;全部毛利 6.00 − 4.00 = +2.00
    # 手續費:第 1 輪 0.3188 + 第 2 輪買入 79400×0.01×0.02% = 0.1588 + 市價平倉 79000×0.01×0.055% = 0.4345 → 0.9121
    assert "完成 1 輪" in end and "收尾市價平倉 1 筆" in end
    assert "淨利合計 +1.09 USDT(毛利 +2.00 − 手續費 0.9121)" in end
    assert "估算" not in end and "手續費佔利益 45.61%" in end
    assert "收尾市價平倉 0.01 @ 79000.00(買入 79400.00),毛利 -4.00" in end


def test_forced_close_fee_is_estimated_when_exchange_has_none(monkeypatch):
    _, _, _, messages = run_weekend(monkeypatch, no_market_fee=True)
    [end] = by_tag(messages, "🟣 #損益")
    # 查不到市價單手續費 → 用吃單費率估算(同樣 0.4345),並標註「含估算」
    assert "淨利合計 +1.09 USDT(毛利 +2.00 − 手續費 0.9121)(含估算)" in end

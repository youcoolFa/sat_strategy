"""
app/bot.py

週末效應均值回歸策略的實盤下單機器人 (Bybit)。

流程 (對應需求):
    1. 到達起始時間點 (預設 HKT 週六 04:00) 時,記錄當下市價當作「起點」,
       立刻放一個限價買入盤,價格 = 起點 * (1 - entry_deviation_pct / 100)
    2. 買入盤成交後,立刻放一個限價平倉盤,價格 = 起點 (回到起點就平倉)
    3. 平倉盤成交後,才會再補回一張新的買入盤 (同一個交易未平倉前不會重複開倉)
    4. 到達結束時間點前 cleanup_buffer_minutes 分鐘 (預設 5 分鐘):
       取消所有未成交的掛單,並用市價把還沒平倉的部位平掉,然後結束

安全預設:
    - dry_run=True: 只記錄「本來要下什麼單」,不會真的呼叫交易所 API 下單
    - testnet=True: 連 Bybit 測試網,不是正式環境
    這兩個都要手動在 sat_strategy_config.json 改成 false 才會動用真實資金,
    避免不小心直接對正式帳戶、真錢下單。

使用方式:
    python -m app.bot        (單純跑 bot,log 沒有 telegram/監控包裝)
    python -m app.monitor    (建議用這個,有例外監控 + telegram 通知)
"""

import os
import signal
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import ccxt
from dotenv import load_dotenv

load_dotenv()

from app.config import load_config
from app.log.logger_setup import setup_logger
from app.market_feed import ticker_feed

logger = setup_logger()

HKT = ZoneInfo("Asia/Hong_Kong")

DEFAULT_STATUS_INTERVAL_MINUTES = 60
# 交易所沒回報手續費時(dry-run、ccxt 沒解析到)用這個掛單費率估算,訊息會標「估算」
MAKER_FEE_ESTIMATE = 0.0002


def fee_ratio(gross, fees):
    """手續費佔利益(或虧損)的比率,小數點後兩位:手續費 ÷ |毛利|。"""
    if gross == 0:
        return "毛利為 0,無法算手續費比率"
    kind = "利益" if gross > 0 else "虧損"
    return f"手續費佔{kind} {fees / abs(gross) * 100:.2f}%"


def status_interval_minutes():
    """每小時狀態回報(⚪ #狀態)的間隔:.env 的 STATUS_INTERVAL_MINUTES,預設 60,0 = 關閉。"""
    raw = os.getenv("STATUS_INTERVAL_MINUTES")
    if not raw:
        return DEFAULT_STATUS_INTERVAL_MINUTES
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return DEFAULT_STATUS_INTERVAL_MINUTES


def _duration(delta):
    minutes = max(int(delta.total_seconds() // 60), 0)
    return f"{minutes // 60} 小時 {minutes % 60} 分"


def compute_entry_price(origin_price, entry_deviation_pct):
    """買入價 = 起點價格 * (1 - 偏離百分比 / 100),四捨五入到小數第 2 位。"""
    return round(origin_price * (1 - entry_deviation_pct / 100), 2)


class SatStrategyBot:
    def __init__(self, config):
        self.config = config
        self.exchange = self._build_exchange()
        self._stop_requested = False
        # 每小時狀態回報(心跳):run() 開始後才有 _status;_wait_until_filled_or_stop 每次輪詢檢查
        self._status_interval = status_interval_minutes()
        self._status = None
        self._started_at = None
        self._next_status = None
        self._last_fill_fee = None  # 最近一張成交單的 (手續費, 是否估算),_take_fill_fee() 取用後清空
        if self.config.use_live_ticker_feed:
            ticker_feed.start()
        signal.signal(signal.SIGTERM, self._handle_stop_signal)
        signal.signal(signal.SIGINT, self._handle_stop_signal)

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    def _build_exchange(self):
        if self.config.dry_run:
            logger.bind(telegram=False).warning("=== DRY RUN 模式:不會真的下單,只會記錄 log ===")
        exchange = ccxt.bybit({
            "apiKey": os.getenv("BYBIT_API_KEY", ""),
            "secret": os.getenv("BYBIT_API_SECRET", ""),
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })
        if self.config.testnet:
            exchange.set_sandbox_mode(True)
            logger.bind(telegram=False).warning("=== 使用 Bybit 測試網 (testnet) ===")
        else:
            logger.bind(telegram=False).warning("=== 使用 Bybit 正式環境,將動用真實資金! ===")
        return exchange

    def _handle_stop_signal(self, signum, frame):
        logger.warning(f"收到停止訊號 ({signum}),準備清理後結束")
        self._stop_requested = True

    # ------------------------------------------------------------------
    # 交易所 API 呼叫 (含重試,網路錯誤是常態,不是例外狀況)
    # ------------------------------------------------------------------
    def _call_with_retry(self, description, func, *args, **kwargs):
        for attempt in range(self.config.max_api_retries):
            try:
                result = func(*args, **kwargs)
                if attempt > 0:
                    # 只有「重試過才成功」才印,正常第一次就成功不用洗版
                    logger.info(f"{description} 重試後成功(第 {attempt + 1} 次嘗試)")
                return result
            except ccxt.NetworkError:
                if attempt == self.config.max_api_retries - 1:
                    raise
                wait = min(2 ** attempt, self.config.retry_backoff_cap_seconds)
                logger.warning(f"{description} 網路錯誤,第 {attempt + 1} 次重試,等待 {wait}s")
                time.sleep(wait)

    def _get_last_price(self):
        if self.config.use_live_ticker_feed:
            live_price = ticker_feed.get_last_price()
            if live_price is not None:
                return live_price
            logger.debug("[TickerFeed] 還沒有即時價格可用，fallback 回 REST。")

        ticker = self._call_with_retry("取得最新價格", self.exchange.fetch_ticker, self.config.symbol)
        return ticker["last"]

    # ------------------------------------------------------------------
    # 下單 / 平倉 / 清理
    # ------------------------------------------------------------------
    def _place_entry_order(self, entry_price):
        logger.info(f"放限價買入盤: {self.config.symbol} @ {entry_price:.2f}, qty={self.config.order_qty:.4f}")
        if self.config.dry_run:
            return {"id": "dry-run-entry", "status": "open"}
        return self._call_with_retry(
            "下買入限價單",
            self.exchange.create_limit_buy_order,
            self.config.symbol, self.config.order_qty, entry_price,
        )

    def _place_exit_order(self, exit_price, qty):
        logger.info(f"放限價平倉盤: {self.config.symbol} @ {exit_price:.2f}, qty={qty:.4f}")
        if self.config.dry_run:
            return {"id": "dry-run-exit", "status": "open"}
        return self._call_with_retry(
            "下平倉限價單",
            self.exchange.create_limit_sell_order,
            self.config.symbol, qty, exit_price, {"reduceOnly": True},
        )

    def _fetch_order_status(self, order):
        if self.config.dry_run:
            return order
        try:
            return self._call_with_retry(
                "查詢訂單狀態(open)",
                self.exchange.fetch_open_order,
                order["id"], self.config.symbol,
            )
        except ccxt.OrderNotFound:
            # 不在未成交列表裡了 → 已經成交或已被取消，查歷史紀錄拿最終狀態
            return self._call_with_retry(
                "查詢訂單狀態(closed)",
                self.exchange.fetch_closed_order,
                order["id"], self.config.symbol,
            )

    def _cancel_order(self, order):
        if self.config.dry_run:
            logger.info(f"[dry-run] 取消訂單 {order['id']}")
            return
        while True:
            try:
                self._call_with_retry("取消訂單", self.exchange.cancel_order, order["id"], self.config.symbol)
                return
            except ccxt.OrderNotFound:
                return
            except ccxt.NetworkError as e:
                # 網路持續失敗不能就這樣放棄一張真實訂單,寧可一直重試到成功或收到停止訊號。
                logger.warning(f"取消訂單網路持續失敗,{self.config.poll_interval_seconds} 秒後重試,不放棄: {e}")
                time.sleep(self.config.poll_interval_seconds)

    def _get_open_position_qty(self):
        if self.config.dry_run:
            return 0.0
        positions = self._call_with_retry("查詢持倉", self.exchange.fetch_positions, [self.config.symbol])
        for p in positions:
            if p.get("contracts"):
                return float(p["contracts"])
        return 0.0

    def _cleanup(self):
        logger.bind(telegram=False).warning("開始清理:取消所有未成交掛單,市價平掉未平倉部位")
        # 這是最後一道防線,絕對不能因為網路暫時失敗就放棄——寧可整段清理流程
        # 重來(冪等:重查一次未成交掛單/持倉,不會重複下單),也不要留下沒人管的
        # 真實掛單或部位。
        while True:
            try:
                if not self.config.dry_run:
                    open_orders = self._call_with_retry(
                        "查詢未成交掛單", self.exchange.fetch_open_orders, self.config.symbol
                    )
                    for o in open_orders:
                        self._call_with_retry("取消訂單", self.exchange.cancel_order, o["id"], self.config.symbol)
                        logger.info(f"已取消掛單 {o['id']}")

                remaining_qty = self._get_open_position_qty()
                if remaining_qty > 0:
                    logger.warning(f"還有 {remaining_qty:.4f} 未平倉,用市價平倉")
                    if not self.config.dry_run:
                        self._call_with_retry(
                            "市價平倉",
                            self.exchange.create_market_sell_order,
                            self.config.symbol, remaining_qty, {"reduceOnly": True},
                        )
                break
            except ccxt.NetworkError as e:
                logger.warning(f"清理過程網路持續失敗,{self.config.poll_interval_seconds} 秒後重新跑一次整個清理流程,不會放棄: {e}")
                time.sleep(self.config.poll_interval_seconds)
        logger.info("清理完成")

    # ------------------------------------------------------------------
    # 時間窗口判斷 (HKT)
    # ------------------------------------------------------------------
    def _now_hkt(self):
        return datetime.now(HKT)

    def _window_end(self, now):
        """回傳這次交易窗口的結束時間 (下一個符合 end_weekday/end_time 的時刻)。

        如果設定了 test_window_minutes (只能透過 SAT_STRATEGY_TEST_WINDOW_MINUTES
        環境變數開),忽略 start_weekday/end_weekday 那組計算,直接回傳「啟動後 N
        分鐘」,方便手動立刻測試完整的啟動->清理停止流程,不用等到下週六。

        如果設定了 manual_stop_only (只能透過 SAT_STRATEGY_MANUAL_STOP_ONLY 環境
        變數開,兩者都設的話 test_window_minutes 優先),回傳一個很遠的未來時間,
        讓 _should_stop_for_cleanup() 永遠不會自然觸發——只能靠手動送 SIGINT/
        SIGTERM 停止(跟 stop.plist 用的是同一套訊號機制,一樣會正常呼叫
        _cleanup() 收尾,不是強制中斷)。
        """
        if self.config.test_window_minutes is not None:
            return now + timedelta(minutes=self.config.test_window_minutes)

        if self.config.manual_stop_only:
            return now + timedelta(days=3650)

        end_h, end_m = map(int, self.config.end_time.split(":"))
        candidate = now
        for _ in range(8):
            if candidate.weekday() == self.config.end_weekday:
                end_dt = candidate.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
                if end_dt >= now:
                    return end_dt
            candidate += timedelta(days=1)
        raise RuntimeError("找不到窗口結束時間,設定可能有誤")

    def _should_stop_for_cleanup(self, window_end):
        return self._now_hkt() >= window_end - timedelta(minutes=self.config.cleanup_buffer_minutes)

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def run(self):
        now = self._now_hkt()
        window_end = self._window_end(now)
        if self.config.test_window_minutes is not None:
            logger.bind(telegram=False).warning(
                f"=== 使用臨時測試時間窗 ({self.config.test_window_minutes} 分鐘後結束),"
                f"不是正式週末排程 ==="
            )
        elif self.config.manual_stop_only:
            logger.bind(telegram=False).warning(
                "=== 手動啟停模式,沒有自動結束時間,只能手動 Ctrl+C 或 kill 停止 ==="
            )
        logger.info(f"啟動 sat_strategy bot,窗口結束時間: {window_end}")

        if self.config.cleanup_on_start:
            logger.warning("=== 崩潰後重啟:先清理上一個 process 留下的掛單/部位,再重新開始 ===")
            self._cleanup()

        origin_price = self._get_last_price()
        logger.info(f"起點價格 (origin_price) = {origin_price:.2f}")
        entry_price = compute_entry_price(origin_price, self.config.entry_deviation_pct)

        # Telegram 事件:啟動/結束一律發;每一輪的成交只在實盤發(dry-run 每 5 秒就一輪,會洗版)
        events = logger.bind(telegram=True)  # 類別:系統(🔵 #系統)
        trade_events = logger.bind(telegram=not self.config.dry_run, category="fill")  # 🟢 #成交
        qty = self.config.order_qty
        status_note = f"\n每 {self._status_interval:g} 分鐘回報一次狀態" if self._status_interval > 0 else ""
        events.info(
            f"🚀 sat_strategy 啟動({self._mode_label()})\n"
            f"{self.config.symbol}|窗口結束 {window_end:%Y-%m-%d %H:%M} HKT\n"
            f"origin {origin_price:.2f} → 買入 {entry_price:.2f}(−{self.config.entry_deviation_pct}%)× {qty}\n"
            f"平倉回到 origin {origin_price:.2f}{status_note}"
        )
        cycles = 0
        gross_total = 0.0
        fees_total = 0.0
        fees_estimated = False
        self._started_at = now
        self._next_status = now + timedelta(minutes=self._status_interval)
        self._status = {"origin": origin_price, "entry_price": entry_price, "qty": qty, "phase": "entry",
                        "cycles": 0, "gross_total": 0.0, "fees_total": 0.0, "window_end": window_end}

        while not self._stop_requested and not self._should_stop_for_cleanup(window_end):
            self._status["phase"] = "entry"
            entry_order = self._place_entry_order(entry_price)

            filled_qty = self._wait_until_filled_or_stop(entry_order, window_end)
            if filled_qty is None:
                if self._stop_requested or self._should_stop_for_cleanup(window_end):
                    break  # 真的要停止了,交給 _cleanup 處理
                # entry_order 被取消/拒絕/過期,但窗口還沒到——這時候還沒買到任何
                # 東西,重掛一張新的進場單是安全的,不需要整個 bot 提早收工。
                logger.warning("進場單被取消/拒絕/過期,還沒到清理時間,重新掛一張新的進場單")
                continue

            logger.info(f"買入盤已成交,qty={filled_qty:.4f}")
            entry_fee, entry_fee_est = self._take_fill_fee(entry_price, filled_qty)
            trade_events.info(f"✅ 買入成交 {filled_qty} @ {entry_price:.2f},已掛平倉單 @ {origin_price:.2f}")
            self._status.update(phase="exit", qty=filled_qty)

            # 已經買到手了,不管平倉單發生什麼事都不能回頭去補新的進場單——
            # 會在還沒平掉舊部位前又疊加一筆新部位。平倉單被取消就重掛一張新的
            # 平倉單,直到真的成交、或收到停止訊號/到了清理時間。
            while True:
                exit_order = self._place_exit_order(origin_price, filled_qty)
                exit_filled = self._wait_until_filled_or_stop(exit_order, window_end)
                if exit_filled is not None:
                    logger.info("平倉盤已成交,這次交易完成,準備補回新的買入盤")
                    cycles += 1
                    exit_fee, exit_fee_est = self._take_fill_fee(origin_price, filled_qty)
                    gross = (origin_price - entry_price) * filled_qty
                    fees = entry_fee + exit_fee
                    estimated = entry_fee_est or exit_fee_est
                    fees_estimated = fees_estimated or estimated
                    gross_total += gross
                    fees_total += fees
                    self._status.update(cycles=cycles, gross_total=gross_total, fees_total=fees_total, qty=qty)
                    note = "(手續費為估算)" if estimated else ""
                    trade_events.info(
                        f"💰 平倉成交 {filled_qty} @ {origin_price:.2f},第 {cycles} 輪完成\n"
                        f"淨利 {gross - fees:+.2f} USDT(毛利 {gross:+.2f} − 手續費 {fees:.4f}){note}|"
                        f"{fee_ratio(gross, fees)}\n補回買單 @ {entry_price:.2f}"
                    )
                    break
                if self._stop_requested or self._should_stop_for_cleanup(window_end):
                    break  # 平倉盤還沒成交就到清理時間了,交給 _cleanup 處理
                logger.warning("平倉單被取消/拒絕/過期,還沒到清理時間,重新掛一張新的平倉單")

        self._cleanup()
        if self.config.use_live_ticker_feed:
            ticker_feed.stop()
        logger.info("sat_strategy bot 結束")
        reason = "收到停止訊號" if self._stop_requested else "窗口到期"
        events.bind(category="pnl").info(  # 🟣 #損益
            f"🏁 sat_strategy 結束({reason})\n完成 {cycles} 輪,淨利合計 {gross_total - fees_total:+.2f} USDT"
            f"(毛利 {gross_total:+.2f} − 手續費 {fees_total:.4f}){'(含估算)' if fees_estimated else ''}|"
            f"{fee_ratio(gross_total, fees_total)}"
        )

    def _remember_fill_fee(self, status, filled):
        """成交時記下這張單的手續費:優先用交易所回報(ccxt 的 fee.cost,Bybit cumExecFee);
        沒有就留給 _take_fill_fee() 用掛單費率估算。"""
        cost = (status.get("fee") or {}).get("cost") if isinstance(status, dict) else None
        self._last_fill_fee = (float(cost), False) if cost is not None else None

    def _take_fill_fee(self, price, qty):
        """取出最近一張成交單的 (手續費, 是否估算);沒有交易所數字 → 價格 × 數量 × 掛單費率。"""
        fee, self._last_fill_fee = self._last_fill_fee, None
        if fee is not None:
            return fee
        return price * qty * MAKER_FEE_ESTIMATE, True

    def _maybe_report_status(self):
        """時間到就發一則 ⚪ #狀態(心跳)。只有這時候才查一次現價;出錯只記本地 log,不影響交易。"""
        if self._status is None or self._status_interval <= 0:
            return
        now = self._now_hkt()
        if now < self._next_status:
            return
        while self._next_status <= now:  # 電腦睡著醒來後不要一次補發好幾則
            self._next_status += timedelta(minutes=self._status_interval)
        try:
            price = self._get_last_price()
            st = self._status
            lines = [
                f"⏱ sat_strategy 運作中({self._mode_label()})|已運行 {_duration(now - self._started_at)}",
                f"{self.config.symbol}|現價 {price}(origin {st['origin']:.2f},{(price / st['origin'] - 1) * 100:+.2f}%)",
            ]
            if st["phase"] == "exit":
                lines.append(
                    f"狀態:持倉 {st['qty']} @ {st['entry_price']:.2f}|未實現 {(price - st['entry_price']) * st['qty']:+.2f}"
                    f"|平倉單 @ {st['origin']:.2f}(距現價 {(st['origin'] / price - 1) * 100:+.2f}%)"
                )
            else:
                lines.append(
                    f"狀態:掛買單等待成交 @ {st['entry_price']:.2f} × {st['qty']}"
                    f"(距現價 {(st['entry_price'] / price - 1) * 100:+.2f}%)"
                )
            gross, fees = st["gross_total"], st.get("fees_total", 0.0)
            lines.append(f"已完成 {st['cycles']} 輪|淨利合計 {gross - fees:+.2f} USDT(毛利 {gross:+.2f} − 手續費 "
                         f"{fees:.4f})|{fee_ratio(gross, fees)}")
            cleanup = st["window_end"] - timedelta(minutes=self.config.cleanup_buffer_minutes)
            lines.append(f"距強制收尾還有 {_duration(cleanup - now)}({cleanup:%m-%d %H:%M},到時取消掛單、市價平倉)")
        except Exception as e:  # noqa: BLE001  狀態回報出錯不能影響交易
            logger.bind(telegram=False).warning(f"狀態回報失敗:{e}")
            return
        logger.bind(telegram=True, category="status").info("\n".join(lines))

    def _mode_label(self):
        if self.config.dry_run:
            mode = "DRY RUN,不會真的下單"
        elif self.config.testnet:
            mode = "測試網"
        else:
            mode = "實盤"
        if self.config.test_window_minutes is not None:
            mode += f",測試窗口 {self.config.test_window_minutes} 分鐘"
        return mode

    def _wait_until_filled_or_stop(self, order, window_end):
        """輪詢訂單狀態,直到成交、或收到停止訊號、或進入清理時間。
        回傳成交數量;如果沒能等到成交就回傳 None (呼叫端會先取消該掛單,交給 _cleanup 統一處理)。
        """
        while True:
            self._maybe_report_status()
            if self._stop_requested or self._should_stop_for_cleanup(window_end):
                self._cancel_order(order)
                return None

            try:
                status = self._fetch_order_status(order)
            except ccxt.NetworkError as e:
                # 重試次數用完了,但這是一筆真實訂單,不能就這樣放棄不管——
                # 繼續在下一個輪詢週期再試,而不是讓整個 bot 當機、丟下真實部位。
                logger.warning(f"查詢訂單狀態持續失敗,{self.config.poll_interval_seconds} 秒後再試一次,不放棄這筆真實訂單: {e}")
                time.sleep(self.config.poll_interval_seconds)
                continue
            if self.config.dry_run:
                # dry-run 模式沒有真的交易所可以查,直接視為立即成交,方便測試整體流程。
                # 但還是要照 poll_interval_seconds 節流,不然 run() 的外層迴圈會變成
                # 沒有延遲的緊密迴圈,短時間內產生大量進場/平倉循環(灌爆 log、空轉 CPU)。
                time.sleep(self.config.poll_interval_seconds)
                return self.config.order_qty
            if status["status"] == "closed":
                self._remember_fill_fee(status, float(status["filled"]))
                return float(status["filled"])
            if status["status"] in ("canceled", "expired", "rejected"):
                logger.warning(f"訂單 {order['id']} 狀態異常: {status['status']}")
                return None

            time.sleep(self.config.poll_interval_seconds)


def main():
    config = load_config()
    if config.test_window_minutes is not None:
        setup_logger(quiet=True)
        logger.bind(telegram=False).warning("=== 臨時測試窗口模式,log 等級調高到 WARNING,避免 dry-run 密集循環洗版 ===")
    logger.info(f"載入參數: {config.to_dict()}")
    bot = SatStrategyBot(config)
    bot.run()


if __name__ == "__main__":
    main()

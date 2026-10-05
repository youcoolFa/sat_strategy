"""
tests/test_bot_logic.py

針對 app/bot.py 裡不需要真的連交易所的純邏輯做單元測試:
    - compute_entry_price(): 進場價格計算
    - _window_end(): 跨週的窗口結束時間計算
    - _should_stop_for_cleanup(): 清理時間判斷

執行方式:
    cd /Users/mac/sat_strategy
    pytest -v
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from zoneinfo import ZoneInfo

from app.bot import HKT, SatStrategyBot, compute_entry_price
from app.config import StrategyConfig


def make_bot(**overrides):
    config = StrategyConfig(dry_run=True, testnet=True)
    for key, value in overrides.items():
        setattr(config, key, value)
    return SatStrategyBot(config)


# ---------------------------------------------------------------------------
# compute_entry_price
# ---------------------------------------------------------------------------
class TestComputeEntryPrice:
    def test_basic_075_percent(self):
        assert compute_entry_price(100000, 0.75) == 99250.0

    def test_zero_deviation_returns_origin(self):
        assert compute_entry_price(50000, 0) == 50000.0

    def test_rounding_to_two_decimals(self):
        assert compute_entry_price(64977.6, 0.75) == round(64977.6 * (1 - 0.0075), 2)

    def test_larger_deviation(self):
        assert compute_entry_price(20000, 3.0) == 19400.0

    def test_negative_origin_price_raises_no_exception_but_is_caller_responsibility(self):
        # 純數學運算,不驗證輸入合理性 (現實中 origin_price 不會是負數,這裡只確認公式本身正確)
        assert compute_entry_price(-100, 10) == -90.0


# ---------------------------------------------------------------------------
# _window_end
# ---------------------------------------------------------------------------
class TestWindowEnd:
    def setup_method(self):
        # 預設 start=Sat 05:00, end=Mon 06:00 (HKT)
        self.bot = make_bot()

    def test_before_window_starts(self):
        # 2026-07-25 是週六,03:00 還沒到起始時間,應該回傳「這個即將到來的週末」的週一結束時間
        now = datetime(2026, 7, 25, 3, 0, tzinfo=HKT)
        window_end = self.bot._window_end(now)
        assert window_end == datetime(2026, 7, 27, 6, 0, tzinfo=HKT)

    def test_during_window_saturday(self):
        # 週六當天已經開始交易,結束時間仍是下一個週一 06:00
        now = datetime(2026, 7, 25, 10, 0, tzinfo=HKT)
        window_end = self.bot._window_end(now)
        assert window_end == datetime(2026, 7, 27, 6, 0, tzinfo=HKT)

    def test_during_window_sunday(self):
        now = datetime(2026, 7, 26, 12, 0, tzinfo=HKT)
        window_end = self.bot._window_end(now)
        assert window_end == datetime(2026, 7, 27, 6, 0, tzinfo=HKT)

    def test_during_window_monday_before_end(self):
        # 週一 05:00,還沒到 06:00 結束時間,應該回傳當天的 06:00
        now = datetime(2026, 7, 27, 5, 0, tzinfo=HKT)
        window_end = self.bot._window_end(now)
        assert window_end == datetime(2026, 7, 27, 6, 0, tzinfo=HKT)

    def test_exact_boundary_at_end_time(self):
        now = datetime(2026, 7, 27, 6, 0, tzinfo=HKT)
        window_end = self.bot._window_end(now)
        assert window_end == datetime(2026, 7, 27, 6, 0, tzinfo=HKT)

    def test_after_window_monday_afternoon_rolls_to_next_week(self):
        # 週一已經過了 06:00,應該回傳下一週的週一 06:00,不是今天
        now = datetime(2026, 7, 27, 15, 0, tzinfo=HKT)
        window_end = self.bot._window_end(now)
        assert window_end == datetime(2026, 8, 3, 6, 0, tzinfo=HKT)

    def test_midweek_returns_upcoming_monday(self):
        # 週三,離下個窗口結束(下週一 06:00)還有一段距離
        now = datetime(2026, 7, 29, 12, 0, tzinfo=HKT)
        window_end = self.bot._window_end(now)
        assert window_end == datetime(2026, 8, 3, 6, 0, tzinfo=HKT)

    def test_custom_start_end_weekday(self):
        # 自訂參數:改成 Sun 20:00 開始, Tue 08:00 結束,確認邏輯不是寫死 Sat/Mon
        bot = make_bot(start_weekday=6, start_time="20:00", end_weekday=1, end_time="08:00")
        now = datetime(2026, 7, 26, 21, 0, tzinfo=HKT)  # 週日晚上
        window_end = bot._window_end(now)
        assert window_end == datetime(2026, 7, 28, 8, 0, tzinfo=HKT)  # 下一個週二 08:00

    def test_test_window_minutes_overrides_weekday_calculation(self):
        # 設定 test_window_minutes 後,不管 start_weekday/end_weekday 是什麼,
        # 一律回傳「現在 + N 分鐘」,方便手動立刻測試,不用等到下週六。
        bot = make_bot(test_window_minutes=10)
        now = datetime(2026, 7, 29, 12, 0, tzinfo=HKT)  # 隨便一個週三中午
        window_end = bot._window_end(now)
        assert window_end == datetime(2026, 7, 29, 12, 10, tzinfo=HKT)

    def test_test_window_minutes_none_falls_back_to_normal_calculation(self):
        # 沒設定 (預設 None) 時,行為要跟原本完全一樣,不能有副作用
        bot = make_bot(test_window_minutes=None)
        now = datetime(2026, 7, 25, 3, 0, tzinfo=HKT)
        window_end = bot._window_end(now)
        assert window_end == datetime(2026, 7, 27, 6, 0, tzinfo=HKT)

    def test_manual_stop_only_returns_far_future(self):
        bot = make_bot(manual_stop_only=True)
        now = datetime(2026, 7, 29, 12, 0, tzinfo=HKT)  # 隨便一個平日中午
        window_end = bot._window_end(now)
        assert window_end > now + timedelta(days=365)

    def test_test_window_minutes_takes_precedence_over_manual_stop_only(self):
        bot = make_bot(manual_stop_only=True, test_window_minutes=10)
        now = datetime(2026, 7, 29, 12, 0, tzinfo=HKT)
        window_end = bot._window_end(now)
        assert window_end == now + timedelta(minutes=10)


# ---------------------------------------------------------------------------
# _should_stop_for_cleanup
# ---------------------------------------------------------------------------
class TestShouldStopForCleanup:
    def test_false_when_well_before_window_end(self):
        bot = make_bot(cleanup_buffer_minutes=5)
        window_end = datetime.now(HKT) + timedelta(hours=2)
        assert bot._should_stop_for_cleanup(window_end) is False

    def test_true_when_inside_buffer(self):
        bot = make_bot(cleanup_buffer_minutes=5)
        window_end = datetime.now(HKT) + timedelta(minutes=3)
        assert bot._should_stop_for_cleanup(window_end) is True

    def test_true_when_past_window_end(self):
        bot = make_bot(cleanup_buffer_minutes=5)
        window_end = datetime.now(HKT) - timedelta(minutes=1)
        assert bot._should_stop_for_cleanup(window_end) is True

    def test_false_just_outside_buffer(self):
        bot = make_bot(cleanup_buffer_minutes=5)
        window_end = datetime.now(HKT) + timedelta(minutes=5, seconds=30)
        assert bot._should_stop_for_cleanup(window_end) is False

    def test_zero_buffer_only_true_at_or_after_end(self):
        bot = make_bot(cleanup_buffer_minutes=0)
        future_end = datetime.now(HKT) + timedelta(seconds=30)
        past_end = datetime.now(HKT) - timedelta(seconds=1)
        assert bot._should_stop_for_cleanup(future_end) is False
        assert bot._should_stop_for_cleanup(past_end) is True


# ---------------------------------------------------------------------------
# run() 的重試邏輯:進場單/平倉單被取消/拒絕/過期時,不該讓整個 bot 提早收工
# ---------------------------------------------------------------------------
class TestRunRetryLogic:
    def _far_future_window_end(self):
        return datetime.now(HKT) + timedelta(hours=1)

    def test_entry_order_canceled_retries_without_placing_extra_exit(self):
        """進場單被取消(還沒買到東西)時,應該重掛一張新的進場單,
        不該多放一張平倉單、也不該提早結束整個 bot。"""
        bot = make_bot()
        with patch.object(bot, "_get_last_price", return_value=50000.0), \
             patch.object(bot, "_window_end", return_value=self._far_future_window_end()), \
             patch.object(bot, "_place_entry_order", return_value={"id": "e1"}) as mock_entry, \
             patch.object(bot, "_place_exit_order", return_value={"id": "x1"}) as mock_exit, \
             patch.object(bot, "_cleanup") as mock_cleanup:

            state = {"entry_calls": 0}

            def fake_wait(order, window_end):
                if order["id"] == "e1":
                    state["entry_calls"] += 1
                    if state["entry_calls"] == 1:
                        return None  # 第一張進場單被取消
                    return 0.001  # 第二張進場單成交
                bot._stop_requested = True  # 平倉單一成交就結束測試
                return 0.001

            with patch.object(bot, "_wait_until_filled_or_stop", side_effect=fake_wait):
                bot.run()

            assert mock_entry.call_count == 2
            assert mock_exit.call_count == 1
            mock_cleanup.assert_called_once()

    def test_exit_order_canceled_retries_without_placing_extra_entry(self):
        """平倉單被取消(已經買到手,部位還沒平掉)時,應該重掛一張新的平倉單,
        絕對不能回頭補一張新的進場單去疊加部位。"""
        bot = make_bot()
        with patch.object(bot, "_get_last_price", return_value=50000.0), \
             patch.object(bot, "_window_end", return_value=self._far_future_window_end()), \
             patch.object(bot, "_place_entry_order", return_value={"id": "e1"}) as mock_entry, \
             patch.object(bot, "_place_exit_order", return_value={"id": "x1"}) as mock_exit, \
             patch.object(bot, "_cleanup") as mock_cleanup:

            state = {"exit_calls": 0}

            def fake_wait(order, window_end):
                if order["id"] == "e1":
                    return 0.001  # 進場一次就成交
                state["exit_calls"] += 1
                if state["exit_calls"] == 1:
                    return None  # 第一張平倉單被取消
                bot._stop_requested = True
                return 0.001  # 第二張平倉單成交,結束測試

            with patch.object(bot, "_wait_until_filled_or_stop", side_effect=fake_wait):
                bot.run()

            assert mock_entry.call_count == 1  # 沒有因為平倉單被取消而多補一張進場單
            assert mock_exit.call_count == 2
            mock_cleanup.assert_called_once()

    def test_entry_order_canceled_and_stop_requested_ends_without_placing_exit(self):
        """進場單被取消,同時也收到停止訊號時,應該直接結束,不會再放平倉單。"""
        bot = make_bot()
        with patch.object(bot, "_get_last_price", return_value=50000.0), \
             patch.object(bot, "_window_end", return_value=self._far_future_window_end()), \
             patch.object(bot, "_place_entry_order", return_value={"id": "e1"}), \
             patch.object(bot, "_place_exit_order") as mock_exit, \
             patch.object(bot, "_cleanup") as mock_cleanup:

            def fake_wait(order, window_end):
                bot._stop_requested = True
                return None

            with patch.object(bot, "_wait_until_filled_or_stop", side_effect=fake_wait):
                bot.run()

            mock_exit.assert_not_called()
            mock_cleanup.assert_called_once()


# ---------------------------------------------------------------------------
# Telegram 事件訊息:哪些 log 會發到手機(telegram=True 的 INFO + WARNING 以上)
# ---------------------------------------------------------------------------
class TestTelegramEvents:
    def _run_one_cycle(self, dry_run):
        """跑一輪:進場成交 → 平倉成交 → 收到停止訊號 → 清理。回傳會發到 Telegram 的訊息。"""
        from loguru import logger

        from app.log.telegram_notifier import telegram_filter

        bot = make_bot(dry_run=dry_run, testnet=False)
        sent = []
        sink = logger.add(lambda m: sent.append(m.record), level="DEBUG", filter=telegram_filter)
        try:
            with patch.object(bot, "_get_last_price", return_value=85283.10), \
                 patch.object(bot, "_window_end", return_value=datetime.now(HKT) + timedelta(hours=1)), \
                 patch.object(bot, "_place_entry_order", return_value={"id": "e1"}), \
                 patch.object(bot, "_place_exit_order", return_value={"id": "x1"}), \
                 patch.object(bot, "_cleanup"):

                def fake_wait(order, window_end):
                    if order["id"] == "x1":
                        bot._stop_requested = True
                    return 0.001

                with patch.object(bot, "_wait_until_filled_or_stop", side_effect=fake_wait):
                    bot.run()
        finally:
            logger.remove(sink)
        return [r["message"] for r in sent]

    def test_live_run_sends_start_fills_and_end(self):
        msgs = self._run_one_cycle(dry_run=False)
        assert len(msgs) == 4, msgs
        start, bought, closed, end = msgs
        assert "啟動" in start and "實盤" in start and "85283.10" in start and "84643.48" in start
        assert "買入成交" in bought
        assert "平倉成交" in closed and "+0.64" in closed  # (85283.10 − 84643.48) × 0.001
        assert "結束" in end and "完成 1 輪" in end

    def test_dry_run_only_sends_start_and_end(self):
        msgs = self._run_one_cycle(dry_run=True)
        assert len(msgs) == 2, msgs
        assert "DRY RUN" in msgs[0] and "結束" in msgs[1]

    def test_mode_banners_are_not_sent_separately(self):
        from loguru import logger

        from app.log.telegram_notifier import telegram_filter

        sent = []
        sink = logger.add(lambda m: sent.append(m.record), level="DEBUG", filter=telegram_filter)
        try:
            make_bot(dry_run=True, testnet=False)
        finally:
            logger.remove(sink)
        assert sent == []  # DRY RUN / 正式環境 橫幅不另外發,已包含在啟動訊息裡

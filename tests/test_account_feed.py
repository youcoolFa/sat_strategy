"""
tests/test_account_feed.py

針對 app/account_feed.py 的 AccountFeed 做單元測試，不連真實 Redis：
用假的 xreadgroup/xgroup_create/xack 回應餵訊息進去，確認 AccountFeed 解析/
儲存/防呆邏輯正確。

執行方式:
    cd /Users/mac/sat_strategy
    /opt/anaconda3/bin/python3 -m pytest tests/test_account_feed.py -v
"""
from unittest.mock import MagicMock, patch

import redis

from app.account_feed import (
    AccountFeed,
    ORDER_STREAM,
    POSITION_STREAM,
    WALLET_STREAM,
)


def _is_pending_read(args) -> bool:
    """第三個位置參數是 streams dict；值是 "0" 代表在讀 pending（_drain_pending），
    值是 ">" 代表在讀全新訊息（正常迴圈）。兩種呼叫要分開餵不同的假回應。"""
    streams = args[2]
    return next(iter(streams.values())) == "0"


def make_feed_with_responses(responses, pending_responses=None, xgroup_create_side_effect=None):
    """
    responses: 一連串「正常新訊息（id=">"）」xreadgroup() 回傳值的清單，餵完就讓
    feed 停止、跳出迴圈。
    pending_responses: 一連串「補讀 pending（id="0"，_drain_pending 用）」的回傳值，
    預設不給就是空清單（模擬沒有任何 pending 訊息，drain 立刻結束）。
    """
    feed = AccountFeed(redis_url="redis://fake:6379/0")
    fake_client = MagicMock()

    if xgroup_create_side_effect is not None:
        fake_client.xgroup_create.side_effect = xgroup_create_side_effect

    pending_responses = list(pending_responses or [])
    call_count = {"pending": 0, "normal": 0}

    def fake_xreadgroup(*args, **kwargs):
        if _is_pending_read(args):
            idx = call_count["pending"]
            call_count["pending"] += 1
            if idx < len(pending_responses):
                return pending_responses[idx]
            return None  # 沒有更多 pending 訊息，_drain_pending 結束

        idx = call_count["normal"]
        call_count["normal"] += 1
        if idx < len(responses):
            return responses[idx]
        feed.stop()
        return None

    fake_client.xreadgroup.side_effect = fake_xreadgroup

    with patch("app.account_feed.redis.Redis.from_url", return_value=fake_client):
        feed._consume_loop()

    return feed, fake_client


class TestAccountFeed:
    def test_wallet_entry_updates_latest_wallet(self):
        responses = [
            [(WALLET_STREAM, [("1-0", {"data": '{"coin": "USDT", "equity": 100.5}'})])],
        ]
        feed, _ = make_feed_with_responses(responses)
        assert feed.get_latest_wallet("USDT") == {"coin": "USDT", "equity": 100.5}

    def test_position_entry_updates_latest_position(self):
        responses = [
            [(POSITION_STREAM, [("1-0", {"data": '{"settle_coin": "USDT", "symbol": "BTCUSDT", "size": 0.1}'})])],
        ]
        feed, _ = make_feed_with_responses(responses)
        assert feed.get_latest_position("USDT") == {
            "settle_coin": "USDT", "symbol": "BTCUSDT", "size": 0.1,
        }

    def test_order_entry_updates_latest_order(self):
        responses = [
            [(ORDER_STREAM, [("1-0", {"data": '{"symbol": "BTCUSDT", "order_id": "abc", "order_status": "New"}'})])],
        ]
        feed, _ = make_feed_with_responses(responses)
        assert feed.get_latest_order() == {
            "symbol": "BTCUSDT", "order_id": "abc", "order_status": "New",
        }

    def test_later_entry_overwrites_earlier_one_for_same_coin(self):
        responses = [
            [(WALLET_STREAM, [("1-0", {"data": '{"coin": "USDT", "equity": 100.0}'})])],
            [(WALLET_STREAM, [("2-0", {"data": '{"coin": "USDT", "equity": 200.0}'})])],
        ]
        feed, _ = make_feed_with_responses(responses)
        assert feed.get_latest_wallet("USDT")["equity"] == 200.0

    def test_malformed_entry_is_skipped_not_crash(self):
        responses = [
            [(WALLET_STREAM, [("1-0", {"data": "not valid json"})])],
            [(WALLET_STREAM, [("2-0", {"data": '{"coin": "USDT", "equity": 50.0}'})])],
        ]
        feed, _ = make_feed_with_responses(responses)
        assert feed.get_latest_wallet("USDT")["equity"] == 50.0

    def test_ack_called_for_every_processed_entry(self):
        responses = [
            [(WALLET_STREAM, [("1-0", {"data": '{"coin": "USDT", "equity": 100.5}'})])],
        ]
        feed, fake_client = make_feed_with_responses(responses)
        fake_client.xack.assert_called_once_with(WALLET_STREAM, "sat_strategy", "1-0")

    def test_no_wallet_before_any_message(self):
        feed = AccountFeed(redis_url="redis://fake:6379/0")
        assert feed.get_latest_wallet("USDT") is None
        assert feed.get_latest_position("USDT") is None
        assert feed.get_latest_order() is None

    def test_busygroup_error_on_group_create_is_ignored(self):
        error = redis.ResponseError("BUSYGROUP Consumer Group name already exists")
        responses = [
            [(WALLET_STREAM, [("1-0", {"data": '{"coin": "USDT", "equity": 1.0}'})])],
        ]
        feed, _ = make_feed_with_responses(responses, xgroup_create_side_effect=error)
        assert feed.get_latest_wallet("USDT")["equity"] == 1.0

    def test_non_busygroup_response_error_on_group_create_propagates(self):
        error = redis.ResponseError("WRONGTYPE something else entirely")
        feed = AccountFeed(redis_url="redis://fake:6379/0")
        fake_client = MagicMock()
        fake_client.xgroup_create.side_effect = error

        with patch("app.account_feed.redis.Redis.from_url", return_value=fake_client):
            try:
                feed._consume_loop()
                assert False, "expected ResponseError to propagate"
            except redis.ResponseError:
                pass

    def test_pending_entry_from_previous_crash_is_processed_before_new_messages(self):
        """
        模擬「上次 process 讀到訊息但還沒 ack 就當機」的情境：訊息卡在同一個
        consumer 名下的 pending list 裡。重啟後 _drain_pending() 必須先把它
        補讀出來，不能永遠漏掉。
        """
        pending_responses = [
            [(WALLET_STREAM, [("1-0", {"data": '{"coin": "USDT", "equity": 42.0}'})])],
        ]
        feed, fake_client = make_feed_with_responses(responses=[], pending_responses=pending_responses)

        assert feed.get_latest_wallet("USDT")["equity"] == 42.0
        fake_client.xack.assert_any_call(WALLET_STREAM, "sat_strategy", "1-0")

    def test_pending_entries_are_processed_before_normal_new_messages(self):
        pending_responses = [
            [(WALLET_STREAM, [("1-0", {"data": '{"coin": "USDT", "equity": 1.0}'})])],
        ]
        responses = [
            [(WALLET_STREAM, [("2-0", {"data": '{"coin": "USDT", "equity": 2.0}'})])],
        ]
        feed, _ = make_feed_with_responses(responses=responses, pending_responses=pending_responses)

        # 正常新訊息（id=2.0）比 pending（id=1.0）晚處理，最後應該以最新值為準。
        assert feed.get_latest_wallet("USDT")["equity"] == 2.0

    def test_start_without_redis_url_logs_warning_and_does_not_crash(self):
        with patch("app.account_feed.os.getenv", return_value=None):
            feed = AccountFeed(redis_url=None)
        feed.start()
        assert feed._thread is None

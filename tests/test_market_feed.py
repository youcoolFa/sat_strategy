"""
tests/test_market_feed.py

針對 app/market_feed.py 的 TickerFeed 做單元測試，不連真實 Redis：
用假的 pubsub/redis client 餵訊息進去，確認 TickerFeed 解析/儲存/防呆邏輯正確。

執行方式:
    cd /Users/mac/sat_strategy
    pytest -v
"""
from unittest.mock import MagicMock, patch

from app.market_feed import TickerFeed


class FakePubSub:
    """模擬 redis-py 的 pubsub()：subscribe() 什麼都不做，listen() 回傳預先準備好的訊息。"""

    def __init__(self, messages):
        self._messages = messages

    def subscribe(self, channel):
        pass

    def listen(self):
        yield from self._messages


def make_feed_with_messages(messages):
    feed = TickerFeed(redis_url="redis://fake:6379/0")
    fake_client = MagicMock()
    fake_client.pubsub.return_value = FakePubSub(messages)

    with patch("app.market_feed.redis.Redis.from_url", return_value=fake_client):
        feed._subscribe_loop()

    return feed


class TestTickerFeed:
    def test_get_last_price_returns_none_before_any_message(self):
        feed = TickerFeed(redis_url="redis://fake:6379/0")
        assert feed.get_last_price() is None

    def test_valid_ticker_message_updates_last_price(self):
        messages = [
            {"type": "subscribe", "data": 1},  # subscribe 確認訊息，應該被略過
            {"type": "message", "data": '{"symbol": "BTCUSDT", "last_price": 65000.5}'},
        ]
        feed = make_feed_with_messages(messages)
        assert feed.get_last_price() == 65000.5

    def test_later_message_overwrites_earlier_one(self):
        messages = [
            {"type": "message", "data": '{"last_price": 100.0}'},
            {"type": "message", "data": '{"last_price": 200.0}'},
        ]
        feed = make_feed_with_messages(messages)
        assert feed.get_last_price() == 200.0

    def test_malformed_message_is_skipped_not_crash(self):
        messages = [
            {"type": "message", "data": "not valid json"},
            {"type": "message", "data": '{"last_price": 999.0}'},
        ]
        feed = make_feed_with_messages(messages)
        assert feed.get_last_price() == 999.0

    def test_message_missing_last_price_key_is_skipped(self):
        messages = [
            {"type": "message", "data": '{"symbol": "BTCUSDT"}'},
        ]
        feed = make_feed_with_messages(messages)
        assert feed.get_last_price() is None

    def test_start_without_redis_url_logs_warning_and_does_not_crash(self):
        with patch("app.market_feed.os.getenv", return_value=None):
            feed = TickerFeed(redis_url=None)
        feed.start()
        assert feed._thread is None

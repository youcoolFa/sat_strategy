"""Telegram 發送器:背景 thread 發送(不卡住呼叫端)、同一位置的訊息有冷卻時間、
發送失敗只記本地 log(不會再觸發 Telegram)、訊息格式。全部用假的 HTTP session,不碰網路。"""

from datetime import datetime, timezone

import pytest
from loguru import logger

from app.log.telegram_notifier import TelegramNotifier, telegram_filter


class FakeResponse:
    def __init__(self, ok=True, status_code=200):
        self._ok = ok
        self.status_code = status_code

    def raise_for_status(self):
        if not self._ok:
            import requests

            raise requests.HTTPError(str(self.status_code), response=self)

    def json(self):
        return {"ok": self._ok}


class FakeSession:
    def __init__(self, fail_times=0, reject_html=False):
        self.sent = []
        self.modes = []  # 每則用的 parse_mode(None = 純文字)
        self.fail_times = fail_times
        self.reject_html = reject_html

    def post(self, url, data, timeout):
        if self.fail_times > 0:
            self.fail_times -= 1
            import requests

            raise requests.ConnectionError("network down")
        if self.reject_html and data.get("parse_mode") == "HTML":
            return FakeResponse(ok=False, status_code=400)  # Bad Request: can't parse entities
        self.sent.append(data["text"])
        self.modes.append(data.get("parse_mode"))
        return FakeResponse()

    def get(self, url, timeout):
        return FakeResponse()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(session=None, clock=None, **kw):
    n = TelegramNotifier(
        project="Bybit WebSocket", bot_token="123:abc", chat_id="42", session=session or FakeSession(),
        clock=clock or Clock(), retry_base_seconds=0, **kw,
    )
    return n


@pytest.fixture
def captured():
    """把 loguru 的 record 接到記憶體,測試 sink 收到的格式。"""
    sink_ids = []
    yield sink_ids
    for i in sink_ids:
        logger.remove(i)


def log_through(notifier, captured, fn):
    if not getattr(notifier, "_test_sink_added", False):  # 同一個 notifier 只接一次 sink
        captured.append(logger.add(notifier.sink, level="DEBUG", filter=telegram_filter))
        notifier._test_sink_added = True
    fn()
    assert notifier.flush(timeout=5)


class TestFilter:
    def _record(self, level, extra=None):
        from types import SimpleNamespace

        return {"level": SimpleNamespace(no=logger.level(level).no, name=level), "extra": extra or {}}

    def test_warning_and_above_are_sent(self):
        assert telegram_filter(self._record("WARNING"))
        assert telegram_filter(self._record("ERROR"))
        assert telegram_filter(self._record("CRITICAL"))

    def test_info_is_not_sent_unless_marked(self):
        assert not telegram_filter(self._record("INFO"))
        assert telegram_filter(self._record("INFO", {"telegram": True}))

    def test_warning_can_opt_out(self):
        assert not telegram_filter(self._record("WARNING", {"telegram": False}))


class TestSending:
    def test_disabled_without_token_or_chat_id(self):
        n = TelegramNotifier(project="x", bot_token="", chat_id="", session=FakeSession(), start_worker=False)
        assert n.enabled is False
        assert n.notify("hi") is False

    def test_notify_is_sent_in_background(self):
        session = FakeSession()
        n = make(session)
        assert n.notify("hello") is True
        assert n.flush(timeout=5)
        assert session.sent == ["hello"]

    def test_retries_then_succeeds(self):
        session = FakeSession(fail_times=2)
        n = make(session, max_retries=3)
        n.notify("again")
        assert n.flush(timeout=5)
        assert session.sent == ["again"]

    def test_gives_up_after_max_retries_without_raising(self):
        session = FakeSession(fail_times=10)
        n = make(session, max_retries=3)
        n.notify("lost")
        assert n.flush(timeout=5)
        assert session.sent == []
        assert n.dropped == 1

    def test_full_queue_drops_instead_of_blocking(self):
        n = make(start_worker=False, max_queue=2)
        assert n.notify("1") and n.notify("2")
        assert n.notify("3") is False
        assert n.dropped == 1

    def test_long_message_is_truncated(self):
        session = FakeSession()
        n = make(session)
        n.notify("x" * 10000)
        n.flush(timeout=5)
        assert len(session.sent[0]) <= 4000


class TestSinkFormat:
    def test_warning_message_has_level_project_location_and_text(self, captured):
        session = FakeSession()
        n = make(session)
        log_through(n, captured, lambda: logger.warning("WebSocket 斷線"))
        [text] = session.sent
        assert "🟡 #警告" in text and "Bybit WebSocket" in text
        assert "WebSocket 斷線" in text
        assert "test_telegram_notifier" in text  # 發生位置(模組:函式:行)

    def test_exception_type_and_message_are_included(self, captured):
        session = FakeSession()
        n = make(session)

        def boom():
            try:
                1 / 0
            except ZeroDivisionError:
                logger.exception("計算失敗")

        log_through(n, captured, boom)
        [text] = session.sent
        assert "🔴 #錯誤" in text and "ZeroDivisionError" in text

    def test_marked_info_event_is_sent_with_event_icon(self, captured):
        session = FakeSession()
        n = make(session)
        log_through(n, captured, lambda: logger.bind(telegram=True).info("已啟動"))
        [text] = session.sent
        assert "🔵 #系統" in text and "已啟動" in text  # 沒指定類別的事件 = 系統

    def test_plain_info_is_not_sent(self, captured):
        session = FakeSession()
        n = make(session)
        log_through(n, captured, lambda: logger.info("ticker 更新"))
        assert session.sent == []


class TestThrottle:
    def test_same_call_site_is_sent_once_per_window_then_reports_suppressed_count(self, captured):
        session = FakeSession()
        clock = Clock()
        n = make(session, clock=clock, throttle_seconds=300)

        def spam():
            for _ in range(5):
                logger.warning("Redis 連線問題,5 秒後重試")  # 同一行 = 同一個來源

        log_through(n, captured, spam)
        assert len(session.sent) == 1

        clock.t += 301
        log_through(n, captured, spam)
        assert len(session.sent) == 2
        assert "略過 4 則" in session.sent[1]

    def test_different_call_sites_are_not_throttled_together(self, captured):
        session = FakeSession()
        n = make(session, throttle_seconds=300)

        def two():
            logger.warning("A")
            logger.warning("B")

        log_through(n, captured, two)
        assert len(session.sent) == 2

    def test_marked_events_are_never_throttled(self, captured):
        session = FakeSession()
        n = make(session, throttle_seconds=300)

        def fills():
            for i in range(3):
                logger.bind(telegram=True).info(f"成交 {i}")

        log_through(n, captured, fills)
        assert len(session.sent) == 3


class TestNoFeedbackLoop:
    def test_send_failure_is_logged_locally_but_never_sent_to_telegram(self, captured):
        session = FakeSession(fail_times=100)
        n = make(session, max_retries=2)
        local = []
        captured.append(logger.add(lambda m: local.append(m.record), level="DEBUG"))
        log_through(n, captured, lambda: logger.error("原本的錯誤"))
        failure_logs = [r for r in local if "Telegram" in r["message"]]
        assert failure_logs, "發送失敗應該記在本地 log"
        assert all(r["extra"].get("telegram") is False for r in failure_logs)
        assert n.dropped == 1


class TestThrottleBackoff:
    def test_persistent_problem_doubles_the_window_up_to_the_cap(self, captured):
        """斷線一整個週末:同一位置一直出問題,冷卻時間 5 → 10 → 20 分鐘…(上限 max_throttle_seconds)。"""
        session = FakeSession()
        clock = Clock()
        n = make(session, clock=clock, throttle_seconds=300, max_throttle_seconds=1200)

        def tick():
            logger.warning("Redis 連線問題")

        log_through(n, captured, tick)  # t=0 發出(冷卻 300)
        clock.t += 100
        log_through(n, captured, tick)  # 冷卻中,略過
        clock.t += 201
        log_through(n, captured, tick)  # t=301 發出,期間有略過 → 冷卻加倍成 600
        clock.t += 400
        log_through(n, captured, tick)  # t=701 < 301+600,略過
        clock.t += 201
        log_through(n, captured, tick)  # t=902 發出 → 冷卻 1200(上限)
        clock.t += 1199
        log_through(n, captured, tick)  # 略過
        assert len(session.sent) == 3

    def test_quiet_period_resets_the_window(self, captured):
        session = FakeSession()
        clock = Clock()
        n = make(session, clock=clock, throttle_seconds=300)

        def tick():
            logger.warning("偶發問題")

        log_through(n, captured, tick)
        clock.t += 100
        log_through(n, captured, tick)  # 略過
        clock.t += 201
        log_through(n, captured, tick)  # 發出,冷卻 600
        clock.t += 5000  # 安靜很久
        log_through(n, captured, tick)  # 發出,期間沒略過 → 冷卻回到 300
        clock.t += 301
        log_through(n, captured, tick)
        assert len(session.sent) == 4


class TestCategoryStyles:
    """Telegram 文字不能上色:用「彩色圓點 + hashtag」區分類別。點 hashtag 可以篩出同類訊息。"""

    @pytest.mark.parametrize("category, expected", [
        ("system", "🔵 #系統"), ("fill", "🟢 #成交"), ("pnl", "🟣 #損益"),
        ("position", "🟠 #持倉"), ("equity", "🟤 #權益"), ("status", "⚪ #狀態"),
    ])
    def test_event_categories(self, captured, category, expected):
        session = FakeSession()
        n = make(session)
        log_through(n, captured, lambda: logger.bind(telegram=True, category=category).info("內容"))
        assert session.sent[0].startswith(f"<b>{expected}｜Bybit WebSocket</b>")

    def test_critical_has_its_own_style(self, captured):
        session = FakeSession()
        n = make(session)
        log_through(n, captured, lambda: logger.critical("資料庫掛了"))
        assert "🆘 #嚴重" in session.sent[0]

    def test_events_omit_the_code_location_but_problems_keep_it(self, captured):
        session = FakeSession()
        n = make(session)

        def both():
            logger.bind(telegram=True, category="fill").info("成交")
            logger.warning("出問題")

        log_through(n, captured, both)
        event, problem = session.sent
        assert "<i>" in event and ":both:" not in event
        assert ":both:" in problem

    def test_sent_as_html_with_message_text_escaped(self, captured):
        session = FakeSession()
        n = make(session)
        log_through(n, captured, lambda: logger.warning("price<0 & qty>1"))
        assert session.modes == ["HTML"]
        assert "price&lt;0 &amp; qty&gt;1" in session.sent[0]

    def test_falls_back_to_plain_text_when_telegram_rejects_the_html(self, captured):
        session = FakeSession(reject_html=True)
        n = make(session)
        log_through(n, captured, lambda: logger.warning("price<0"))
        assert session.modes == [None]
        assert "🟡 #警告" in session.sent[0] and "price<0" in session.sent[0] and "<b>" not in session.sent[0]
        assert n.dropped == 0

    def test_plain_notify_is_not_html(self):
        session = FakeSession()
        n = make(session)
        n.notify("a<b")
        n.flush(timeout=5)
        assert session.sent == ["a<b"] and session.modes == [None]

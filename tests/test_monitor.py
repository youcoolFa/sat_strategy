"""app/monitor.py 的 _run_bot:bot.run() 沒被內部處理到的例外記 CRITICAL(含完整堆疊)、回傳 False
(launchd 監管腳本靠非 0 exit code 重啟)。2026-10-11:交易所拒絕(ccxt.ExchangeError:餘額不足、
下單 / 平倉被拒)= 確定的成交失敗 → logger.bind(trade=True),送「交易快報」;其他錯誤 → 系統警報。"""

import ccxt
import pytest
from loguru import logger

from app import monitor


class FakeBot:
    def __init__(self, error=None):
        self.error = error

    def run(self):
        if self.error:
            raise self.error


@pytest.fixture
def records():
    got = []
    sink = logger.add(lambda m: got.append(m.record), level="CRITICAL")
    yield got
    logger.remove(sink)


def test_success_returns_true(records):
    assert monitor._run_bot(FakeBot()) is True
    assert records == []


@pytest.mark.parametrize("error", [ccxt.InsufficientFunds("not enough"), ccxt.InvalidOrder("bad qty"),
                                   ccxt.ExchangeError("rejected")])
def test_exchange_rejection_is_a_critical_trade_failure(records, error):
    assert monitor._run_bot(FakeBot(error)) is False
    [r] = records
    assert r["level"].name == "CRITICAL" and r["extra"].get("trade") is True
    assert r["exception"] is not None  # 完整堆疊


def test_other_crashes_are_system_problems(records):
    assert monitor._run_bot(FakeBot(RuntimeError("bug"))) is False
    [r] = records
    assert r["level"].name == "CRITICAL" and r["extra"].get("trade") is not True


def test_network_error_is_not_a_trade_failure(records):
    """網路錯誤照使用者決定算系統問題(bot 內部本來就會重試,真的拋出來代表程式要重啟)"""
    assert monitor._run_bot(FakeBot(ccxt.NetworkError("down"))) is False
    [r] = records
    assert r["extra"].get("trade") is not True

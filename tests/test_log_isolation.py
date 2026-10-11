"""測試不能寫進真的 logs/(2026-10-11 發現:app/bot.py 一 import 就 setup_logger(),測試的
「DRY RUN 模式」「測試網」等假紀錄跟週末真錢 BTC 的 logs/sat_strategy.log 混在一起)。
conftest 在任何測試模組 import 前就把 log 導到臨時資料夾。"""

import os

from app.log import logger_setup


def test_tests_never_log_into_the_real_project_logs():
    real = os.path.join(logger_setup._PROJECT_ROOT, "logs")
    assert os.path.realpath(logger_setup._LOG_DIR) != os.path.realpath(real)

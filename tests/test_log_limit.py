"""log 大小限制器:資料夾總大小超過上限就從最舊的檔開始刪,目前在寫/最近還在寫的檔不刪。"""

import os
import time

from app.log.log_limit import enforce_log_limit, max_total_mb_from_env, size_retention

MB = 1024 * 1024


def make_file(directory, name, mb, age_hours):
    path = directory / name
    path.write_bytes(b"x" * int(mb * MB))
    t = time.time() - age_hours * 3600
    os.utime(path, (t, t))
    return path


class TestEnforceLogLimit:
    def test_under_the_limit_nothing_is_deleted(self, tmp_path):
        make_file(tmp_path, "bot.1.log", 1, 48)
        make_file(tmp_path, "bot.2.log", 1, 24)
        assert enforce_log_limit(tmp_path, max_total_mb=5) == []
        assert len(list(tmp_path.iterdir())) == 2

    def test_deletes_oldest_first_until_under_the_limit(self, tmp_path):
        oldest = make_file(tmp_path, "bot.1.log", 2, 72)
        older = make_file(tmp_path, "bot.2.log", 2, 48)
        newer = make_file(tmp_path, "bot.3.log", 2, 24)
        removed = enforce_log_limit(tmp_path, max_total_mb=4.5)
        assert removed == [oldest]
        assert not oldest.exists() and older.exists() and newer.exists()

    def test_protected_and_recently_written_files_are_never_deleted(self, tmp_path):
        current = make_file(tmp_path, "bot.log", 3, 72)  # 正在寫的檔(就算最舊)
        active = make_file(tmp_path, "other.console.log", 3, 0.1)  # 6 分鐘前還有寫入
        old = make_file(tmp_path, "bot.1.log", 3, 48)
        removed = enforce_log_limit(tmp_path, max_total_mb=1, protect=[current])
        assert removed == [old]
        assert current.exists() and active.exists()

    def test_only_matching_files_are_touched(self, tmp_path):
        make_file(tmp_path, "bot.1.log", 3, 72)
        keep = make_file(tmp_path, "notes.txt", 3, 72)
        enforce_log_limit(tmp_path, max_total_mb=1, pattern="bot*.log*")
        assert keep.exists()

    def test_missing_directory_is_fine(self, tmp_path):
        assert enforce_log_limit(tmp_path / "nope", max_total_mb=1) == []


class TestLoguruRetention:
    def test_retention_callable_enforces_the_limit_and_keeps_the_active_file(self, tmp_path):
        active = make_file(tmp_path, "bot.log", 1, 0)
        old = make_file(tmp_path, "bot.2026-08-01.log", 3, 72)
        retention = size_retention(tmp_path, max_total_mb=2, pattern="bot*.log*", protect=[active])
        retention([str(old), str(active)])  # loguru 輪替時呼叫,參數是它找到的 log 檔清單
        assert not old.exists() and active.exists()


class TestLimitFromEnv:
    def test_default_and_override(self, monkeypatch):
        monkeypatch.delenv("LOG_MAX_TOTAL_MB", raising=False)
        assert max_total_mb_from_env(500) == 500
        monkeypatch.setenv("LOG_MAX_TOTAL_MB", "200")
        assert max_total_mb_from_env(500) == 200

    def test_bad_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("LOG_MAX_TOTAL_MB", "abc")
        assert max_total_mb_from_env(500) == 500
        monkeypatch.setenv("LOG_MAX_TOTAL_MB", "0")
        assert max_total_mb_from_env(500) == 500


class TestSatStrategyLoggerSetup:
    def test_trims_both_log_groups_but_never_launchd_logs(self, tmp_path, monkeypatch):
        from app.log import logger_setup

        logs = tmp_path / "logs"
        logs.mkdir()
        rotated = make_file(logs, "sat_strategy.2026-08-01_09-58-34.log", 2, 72)
        launchd = make_file(logs, "launchd_start.log", 2, 72)
        old_run = make_file(tmp_path, "sat_strategy_20260801_085408.log", 2, 72)
        current_run = make_file(tmp_path, "sat_strategy_20261010_040001.log", 0.1, 0)
        monkeypatch.setattr(logger_setup, "_PROJECT_ROOT", str(tmp_path))
        monkeypatch.setattr(logger_setup, "_LOG_DIR", str(logs))
        monkeypatch.setenv("LOG_MAX_TOTAL_MB", "1")

        logger_setup.setup_logger()

        assert not rotated.exists() and not old_run.exists()
        assert launchd.exists() and current_run.exists()


    def test_rotated_logs_are_compressed(self, tmp_path, monkeypatch):
        import gzip

        from loguru import logger

        from app.log import logger_setup

        logs = tmp_path / "logs"
        monkeypatch.setattr(logger_setup, "_PROJECT_ROOT", str(tmp_path))
        monkeypatch.setattr(logger_setup, "_LOG_DIR", str(logs))
        monkeypatch.setattr(logger_setup, "LOG_ROTATION", "2 KB")
        logger_setup.setup_logger()
        for i in range(100):
            logger.debug(f"第 {i} 行 " + "x" * 50)

        archives = sorted(logs.glob("sat_strategy.*.log.gz"))
        assert archives
        assert "第 0 行" in gzip.open(archives[0], "rt").read()
        assert (logs / "sat_strategy.log").exists()

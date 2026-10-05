"""
app/log/log_limit.py

log 大小限制器:一個資料夾裡的 log 檔總大小超過上限,就從最舊的檔開始刪,直到降回
上限以下。原本只靠 loguru 的 retention="14 days",但那只在「程式正在跑、檔案輪替」
時才會清;程式停掉後舊檔就一直留著(2026-10-05 清掉的 logs/ 有 1.6 GB)。

什麼時候檢查:logger_setup.setup_logger() 啟動時一次;之後每次檔案輪替(每 10 MB)
由 loguru 呼叫 size_retention() 回傳的函式一次。

永遠不刪:
    - protect 指定的檔(目前正在寫的 log 檔)
    - active_seconds 內還有寫入的檔(可能是另一個還在跑的程式正在寫,例如 nohup 的
      stdout 導向檔;刪掉正在寫的檔,空間不會釋放,內容卻會不見)

上限:環境變數 LOG_MAX_TOTAL_MB(.env),沒設或不合法就用呼叫端給的預設值。
sat_strategy、strategy_lab 用同一套。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Iterable, List, Union

PathLike = Union[str, Path]
MB = 1024 * 1024


def max_total_mb_from_env(default: float) -> float:
    raw = os.getenv("LOG_MAX_TOTAL_MB")
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


def enforce_log_limit(
    directory: PathLike,
    max_total_mb: float,
    pattern: str = "*.log*",
    protect: Iterable[PathLike] = (),
    active_seconds: float = 3600,
) -> List[Path]:
    """刪掉最舊的 log 檔直到總大小 <= max_total_mb。回傳被刪掉的檔案(最舊的在前)。"""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    protected = {Path(p).resolve() for p in protect}
    now = time.time()

    files = []
    for path in directory.glob(pattern):
        try:
            if path.is_file():
                stat = path.stat()
                files.append((stat.st_mtime, stat.st_size, path))
        except OSError:
            continue
    files.sort(key=lambda f: f[0])  # 最舊的在前

    total = sum(size for _, size, _ in files)
    limit = max_total_mb * MB
    removed: List[Path] = []
    for mtime, size, path in files:
        if total <= limit:
            break
        if path.resolve() in protected or now - mtime < active_seconds:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        removed.append(path)
    return removed


def size_retention(
    directory: PathLike, max_total_mb: float, pattern: str = "*.log*", protect: Iterable[PathLike] = ()
) -> Callable[[List[str]], None]:
    """給 loguru logger.add(retention=...) 用:每次檔案輪替後檢查一次總大小。
    loguru 傳進來的檔案清單不用,直接掃整個資料夾(包含別的程式寫的 log)。"""
    protect = list(protect)

    def retention(_files: List[str]) -> None:
        enforce_log_limit(directory, max_total_mb, pattern=pattern, protect=protect)

    return retention

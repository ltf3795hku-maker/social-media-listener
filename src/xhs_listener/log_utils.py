"""任务日志工具：参考旧项目 job_log.py，供未来前端实时展示。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional


# 前端读到该标记即可知道本次采集任务已经结束。
LOG_QUEUE_END = "__JOB_LOG_END__"


def emit_log(log_queue: Optional[Any], message: str) -> None:
    """向前端队列写入带时间戳日志；没有队列时静默跳过。"""

    if log_queue is None:
        return
    ts = datetime.now().strftime("%H:%M:%S")
    log_queue.put(f"[{ts}] {message}")


def finish_log_queue(log_queue: Optional[Any]) -> None:
    """通知前端日志流结束。"""

    if log_queue is None:
        return
    try:
        log_queue.put(LOG_QUEUE_END)
    except Exception:  # noqa: BLE001
        pass


def timestamped(message: str) -> str:
    """给落盘日志加时间戳，便于回看采集过程。"""

    ts = datetime.now().strftime("%H:%M:%S")
    return f"[{ts}] {message}"


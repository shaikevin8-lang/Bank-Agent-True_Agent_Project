"""定时转账后台执行器：后台线程周期扫描并执行到期的定时转账。

「到点主动执行」的落点：服务启动后由本模块起一个守护线程，按固定间隔调用
:func:`common.clock_settlement.run_due_now`，以默认演示租户的沙箱业务时间为
判定基准，把 ``scheduled_at <= business_now`` 的定时转账自动扣款、置为
``executed``、更新回执并向归属会话推送一条通知。

安全属性：

1. **守护线程 + 全兜底**：单次扫描任何异常都只记 warning，绝不退出线程；线程
   ``daemon=True``，进程退出时不阻塞。
2. **幂等**：结算本身按 ``status='scheduled'`` + 行锁 ``FOR UPDATE`` 判定，
   同一笔定时转账只会执行一次，重复扫描不会重复扣款。
3. **间隔可调**：默认 2 秒，便于演示；生产可调大。单实例进程内一把锁即可，
   无需分布式调度（演示环境）。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

#: 默认扫描间隔（秒）。
DEFAULT_INTERVAL_SECONDS = 2.0


class TransferScheduler:
    """后台守护线程：按固定间隔扫描并执行到期定时转账。"""

    def __init__(self, interval_seconds: float = DEFAULT_INTERVAL_SECONDS) -> None:
        self.interval_seconds = float(interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """启动后台线程（幂等）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="transfer-scheduler", daemon=True)
        self._thread.start()
        logger.info("定时转账后台执行器已启动（间隔 %.1fs）", self.interval_seconds)

    def stop(self) -> None:
        """停止后台线程（幂等）。"""
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=self.interval_seconds + 1)
        logger.info("定时转账后台执行器已停止")

    def tick(self) -> list[dict[str, Any]]:
        """手动触发一次扫描（测试 / 一次性补结算用）。"""
        from common import clock_settlement

        return clock_settlement.run_due_now()

    def _run(self) -> None:
        from common import clock_settlement

        while not self._stop.is_set():
            try:
                clock_settlement.run_due_now()
            except Exception as exc:  # noqa: BLE001 - 线程兜底，绝不退出
                logger.warning("定时转账后台扫描异常：%s", exc)
            self._stop.wait(self.interval_seconds)


_scheduler: TransferScheduler | None = None


def get_scheduler() -> TransferScheduler:
    """返回进程级调度器单例。"""
    global _scheduler
    if _scheduler is None:
        _scheduler = TransferScheduler()
    return _scheduler


def start_scheduler(interval_seconds: float = DEFAULT_INTERVAL_SECONDS) -> TransferScheduler:
    """启动进程级调度器（幂等），返回实例。"""
    scheduler = get_scheduler()
    scheduler.interval_seconds = float(interval_seconds)
    scheduler.start()
    return scheduler


def stop_scheduler() -> None:
    """停止进程级调度器（幂等）。"""
    get_scheduler().stop()


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "TransferScheduler",
    "get_scheduler",
    "start_scheduler",
    "stop_scheduler",
]

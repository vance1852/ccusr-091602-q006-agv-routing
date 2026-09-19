"""时钟抽象：测试用手动时钟，服务运行用系统时钟。"""

from __future__ import annotations

import time


class Clock:
    """时间源，单位秒。"""

    def now(self) -> float:
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> float:
        return time.time()


class ManualClock(Clock):
    """只能前进的手动时钟，用于确定性测试与事件回放。"""

    def __init__(self, start: float = 0.0):
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("时钟只能前进")
        self._now += seconds
        return self._now

    def set(self, at: float) -> None:
        if at < self._now:
            raise ValueError("时钟不能回拨")
        self._now = float(at)

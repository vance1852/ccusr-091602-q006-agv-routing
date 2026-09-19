"""时间源。生产用墙钟，测试与回放用可任意定位的仿真钟。"""

import time


class Clock:
    def now(self) -> float:
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> float:
        return time.time()


class SimClock(Clock):
    def __init__(self, start: float = 0.0):
        self.t = float(start)

    def now(self) -> float:
        return self.t

    def set(self, t: float) -> float:
        self.t = float(t)
        return self.t

    def advance(self, dt: float) -> float:
        self.t += float(dt)
        return self.t

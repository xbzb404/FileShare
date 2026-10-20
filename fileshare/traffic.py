# -*- coding: utf-8 -*-
"""连接监控：谁在下载、下的什么、当前占多少带宽。

每个正在传输的请求登记成一条 :class:`Transfer`，传输过程只做整数累加
（很轻），UI 每秒 ``sample()`` 一次算瞬时速度。这样：
  · 网络线程的热路径里没有锁竞争、没有字符串格式化；
  · 界面上看到的就是「此刻每个 IP 的速度」，跟任务管理器一个观感。
"""

import threading
import time


class Transfer(object):
    __slots__ = ("tid", "peer", "ip", "proto", "target", "direction",
                 "started", "bytes", "size", "last_bytes", "last_ts",
                 "speed", "ended", "user")

    def __init__(self, tid, peer, proto, target, size=0, direction="down",
                 user=""):
        self.tid = tid
        self.peer = peer or ""
        self.ip = (peer or "").rsplit(":", 1)[0] if peer else "?"
        self.proto = proto
        self.target = target or ""
        self.direction = direction          # down = 别人从这里下，up = 传进来
        self.started = time.time()
        self.bytes = 0
        self.size = int(size or 0)
        self.last_bytes = 0
        self.last_ts = self.started
        self.speed = 0.0
        self.ended = 0.0
        self.user = user

    @property
    def name(self):
        return (self.target or "").replace("\\", "/").rsplit("/", 1)[-1]


class TrafficMonitor(object):
    """活跃传输表。``rows()`` 给界面用，``summary()`` 给托盘/状态栏用。"""

    def __init__(self, keep=8.0, limit=400):
        self._lock = threading.Lock()
        self._items = []
        self._seq = 0
        self.keep = keep
        self.limit = limit

    # ------------------------------------------------------------ 登记
    def begin(self, peer, proto, target, size=0, direction="down", user=""):
        with self._lock:
            self._seq += 1
            tr = Transfer("c%d" % self._seq, peer, proto, target, size,
                          direction, user)
            self._items.append(tr)
            if len(self._items) > self.limit:
                # 只留活跃的，防止长期运行后列表无限膨胀
                self._items = [t for t in self._items if not t.ended][-self.limit // 2:]
            return tr

    def add(self, tr, n):
        if tr is None or n <= 0:
            return
        with self._lock:
            tr.bytes += n

    def end(self, tr):
        if tr is None:
            return
        tr.ended = time.time()

    # ------------------------------------------------------------ 采样
    def sample(self):
        """算一遍瞬时速度，返回按速度倒序的行（含刚结束几秒的）。"""
        now = time.time()
        with self._lock:
            alive = []
            for t in self._items:
                dt = now - t.last_ts
                if dt >= 0.2:
                    t.speed = max(0.0, (t.bytes - t.last_bytes) / dt)
                    t.last_bytes = t.bytes
                    t.last_ts = now
                elif t.ended:
                    t.speed = 0.0
                if t.ended and (now - t.ended) > self.keep:
                    continue
                alive.append(t)
            self._items = alive
            rows = [_row(t, now) for t in alive]
        rows.sort(key=lambda r: (-r["speed"], -r["bytes"]))
        return rows

    def rows(self):
        with self._lock:
            now = time.time()
            rows = [_row(t, now) for t in self._items]
        rows.sort(key=lambda r: (-r["speed"], -r["bytes"]))
        return rows

    def summary(self):
        """(活跃连接数, 不同 IP 数, 总速度)"""
        now = time.time()
        with self._lock:
            active = [t for t in self._items
                      if not t.ended or (now - t.ended) <= 2.0]
            total = sum(t.speed for t in active)
            ips = set(t.ip for t in active if not t.ended)
            n = sum(1 for t in active if not t.ended)
        return n, len(ips), total

    def clear(self):
        with self._lock:
            self._items = []


def _row(t, now):
    dur = (t.ended or now) - t.started
    pct = 0.0
    if t.size > 0:
        pct = min(1.0, t.bytes / float(t.size))
    return {
        "id": t.tid,
        "peer": t.peer,
        "ip": t.ip,
        "proto": t.proto,
        "name": t.name,
        "target": t.target,
        "direction": t.direction,
        "bytes": t.bytes,
        "size": t.size,
        "speed": t.speed,
        "duration": max(0.0, dur),
        "pct": pct,
        "ended": bool(t.ended),
        "user": t.user,
    }

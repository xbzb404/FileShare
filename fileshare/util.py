# -*- coding: utf-8 -*-
"""限速与统计：全局限速令牌桶 + 传输速率统计。"""

import os
import sys
import threading
import time


def write_crash_log(text):
    """把异常写进 exe 同目录的 FileShare_error.log。

    windowed 打包没有控制台，异常不落盘就什么线索都没有；
    写失败（只读目录等）也不能再抛——这是最后一道保险。
    """
    try:
        base = (os.path.dirname(sys.executable)
                if getattr(sys, "frozen", False)
                else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(base, "FileShare_error.log")
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(text)
    except Exception:                                       # noqa: BLE001
        pass


class RateLimiter(object):
    """令牌桶限速器（全局限速，多线程/协程共享同一个实例）。

    调用方拿数据后调 :meth:`consume`，把返回的秒数 sleep 掉即可 ——
    限速器自己不阻塞，这样同一套代码既能在协程里 ``await asyncio.sleep``，
    也能在线程里 ``time.sleep``。

    ``rate=0`` 表示不限速（此时 consume 恒返回 0，零开销）。
    桶容量取 1/4 秒的量：既允许一点点突发（避免小包被拉成一条一条），
    又不会因为桶太大而在限速下出现「先冲一段再卡住」。
    """

    __slots__ = ("_rate", "_cap", "_tokens", "_last", "_lock")

    def __init__(self, rate=0):
        self._rate = 0
        self._cap = 0.0
        self._tokens = 0.0
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self.set_rate(rate)

    @property
    def rate(self):
        return self._rate

    def set_rate(self, rate):
        """rate: 每秒字节数，0 = 不限速。"""
        rate = max(0, int(rate or 0))
        with self._lock:
            self._rate = rate
            self._cap = rate * 0.25 if rate else 0.0
            self._tokens = min(self._tokens, self._cap) if rate else 0.0
            self._last = time.monotonic()

    def consume(self, n):
        """预扣 n 字节，返回需要等待的秒数（0 表示不用等）。"""
        rate = self._rate
        if not rate or n <= 0:
            return 0.0
        now = time.monotonic()
        with self._lock:
            dt = now - self._last
            if dt > 0:
                self._tokens = min(self._cap, self._tokens + dt * rate)
                self._last = now
            self._tokens -= n
            if self._tokens >= 0:
                return 0.0
            return -self._tokens / rate

    def chunk_size(self, base):
        """限速时把读块缩小，让速率更平滑；不限速时用大块换吞吐。"""
        rate = self._rate
        if not rate:
            return base
        return max(16 * 1024, min(base, int(rate / 20) or base))


class Stats(object):
    """全局流量的原子计数 + 秒级速率采样。

    写侧（网络线程）只做整数累加，读侧（UI 线程）取快照 —— 不持锁遍历，
    UI 永远不会被网络线程挡住。
    """

    def __init__(self, buckets=120):
        self._lock = threading.Lock()
        self.sent = 0          # 服务端 → 客户端（上传/下载给别人）
        self.recv = 0          # 客户端 → 服务端（FTP/HTTP 上传进来）
        self.dl = 0            # 内置下载器收到的字节
        self._last = (time.monotonic(), self.sent, self.recv, self.dl)
        self._hist = []        # [(t, up_bps, down_bps, dl_bps)]
        self._buckets = buckets
        self.peak_up = 0.0
        self.peak_down = 0.0
        self.peak_dl = 0.0

    def add_sent(self, n):
        with self._lock:
            self.sent += n

    def add_recv(self, n):
        with self._lock:
            self.recv += n

    def add_dl(self, n):
        with self._lock:
            self.dl += n

    def sample(self):
        """算一次瞬时速率（每秒调一次），返回 (up, down, dl) B/s。"""
        now = time.monotonic()
        with self._lock:
            t0, s0, r0, d0 = self._last
            dt = max(1e-6, now - t0)
            up = (self.sent - s0) / dt
            down = (self.recv - r0) / dt
            dl = (self.dl - d0) / dt
            self._last = (now, self.sent, self.recv, self.dl)
            self._hist.append((now, up, down, dl))
            if len(self._hist) > self._buckets:
                del self._hist[:len(self._hist) - self._buckets]
            self.peak_up = max(self.peak_up, up)
            self.peak_down = max(self.peak_down, down)
            self.peak_dl = max(self.peak_dl, dl)
        return up, down, dl

    def totals(self):
        with self._lock:
            return {"sent": self.sent, "recv": self.recv, "dl": self.dl}

    def avg(self, n=30):
        """最近 n 秒的平均速率 + 峰值。

        每秒一条采样，所以「最近 n 条」≈「最近 n 秒」。用时间加权而不是
        简单算术平均：UI 卡顿/系统睡眠会让采样间隔大于 1 秒，简单平均
        会把那一段算重的。
        """
        with self._lock:
            hist = list(self._hist[-n:]) if n else list(self._hist)
            span_n = len(hist)
        if not hist:
            return {"up": 0.0, "down": 0.0, "dl": 0.0, "total": 0.0,
                    "peak": 0.0, "peak_total": 0.0, "span": 0.0, "n": 0}

        # 第 i 条采样代表「上一次采样 → 这一次采样」这段的速率，
        # 所以权重取前者到的间隔；首条没有前驱，按 1 秒算。
        w_up = w_down = w_dl = 0.0
        wsum = 0.0
        peak = 0.0
        peak_total = 0.0
        for i in range(len(hist)):
            if i == 0:
                dt = 1.0
            else:
                dt = hist[i][0] - hist[i - 1][0]
                if dt <= 0.0:
                    dt = 1.0
                dt = min(dt, 5.0)      # 睡眠/卡顿造成的超大间隔削平
            w_up += hist[i][1] * dt
            w_down += hist[i][2] * dt
            w_dl += hist[i][3] * dt
            wsum += dt
            peak = max(peak, hist[i][1], hist[i][2], hist[i][3])
            peak_total = max(peak_total, hist[i][1] + hist[i][2] + hist[i][3])
        if wsum <= 0:
            wsum = float(len(hist))
        up, down, dl = w_up / wsum, w_down / wsum, w_dl / wsum
        return {"up": up, "down": down, "dl": dl, "total": up + down + dl,
                "peak": peak, "peak_total": peak_total,
                "span": min(wsum, float(n)), "n": span_n}

    # 兼容：某些地方想同时拿两条曲线
    def history(self, n=60):
        with self._lock:
            return list(self._hist[-n:])

    def reset_peaks(self):
        with self._lock:
            self.peak_up = self.peak_down = self.peak_dl = 0.0


class EventLog(object):
    """环形事件日志（网络线程写，UI 线程读）。"""

    def __init__(self, cap=800):
        self._lock = threading.Lock()
        self._items = []
        self._cap = cap
        self._seq = 0

    def add(self, kind, text, peer=""):
        """kind: info / ok / warn / err / dl"""
        ts = time.strftime("%H:%M:%S")
        with self._lock:
            self._items.append((ts, kind, text, peer))
            if len(self._items) > self._cap:
                del self._items[:len(self._items) - self._cap]
            self._seq += 1

    @property
    def seq(self):
        with self._lock:
            return self._seq

    def tail(self, n=200):
        with self._lock:
            return list(self._items[-n:])

    def clear(self):
        with self._lock:
            self._items = []
            self._seq += 1


def human_bytes(n, unit=None):
    """1536 → '1.5 KB'；unit 给定时固定用该单位。"""
    n = float(n or 0)
    if unit:
        return "%.1f %s" % (n / _UNITS[unit], unit)
    for name in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or name == "TB":
            return ("%.0f %s" % (n, name)) if name == "B" else ("%.1f %s" % (n, name))
        n /= 1024.0
    return "%.1f TB" % n


def human_speed(n):
    return human_bytes(n) + "/s"


def human_link(bps):
    """比特/秒 → '100 Mbps' / '1 Gbps' 这种好读的写法（网卡链路速率）。"""
    try:
        bps = float(bps or 0)
    except (TypeError, ValueError):
        return "未知"
    if bps <= 0:
        return "0 bps"
    if bps >= 1e9:
        s = ("%.1f" % (bps / 1e9)).rstrip("0").rstrip(".")
        return "%s Gbps" % s
    if bps >= 1e6:
        s = ("%.1f" % (bps / 1e6)).rstrip("0").rstrip(".")
        return "%s Mbps" % s
    if bps >= 1e3:
        return "%d kbps" % int(round(bps / 1e3))
    return "%d bps" % int(bps)


_UNITS = {"B": 1.0, "KB": 1024.0, "MB": 1024.0 ** 2, "GB": 1024.0 ** 3}


def human_time(sec):
    sec = int(max(0, sec or 0))
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm%02ds" % (sec // 60, sec % 60)
    return "%dh%02dm" % (sec // 3600, (sec % 3600) // 60)

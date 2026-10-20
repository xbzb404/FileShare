# -*- coding: utf-8 -*-
"""吞吐基准：单线程 vs 多线程，实测 HTTP 服务端的传输速度。

    python tools/bench.py [文件大小MB]

会临时生成一个大文件、起本机 HTTP 服务、分别用 1 / 4 / 8 线程下载，
打印平均速度。回环地址下能测出服务端自身的上限（不受网卡限制）。
"""

import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fileshare.server.httpd import HttpFileServer            # noqa: E402
from fileshare.util import human_speed                       # noqa: E402


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_file(folder, size_mb):
    path = os.path.join(folder, "bench.bin")
    block = bytes(range(256)) * 4096                          # 1 MiB
    with open(path, "wb") as fp:
        for _ in range(size_mb):
            fp.write(block)
    return path


def run_threads(url, threads, total_size, stats):
    part = total_size // threads
    got = [0] * threads

    def worker(i):
        start = i * part
        end = total_size - 1 if i == threads - 1 else (i + 1) * part - 1
        req = urllib.request.Request(url, headers={
            "Range": "bytes=%d-%d" % (start, end)})
        n = 0
        with urllib.request.urlopen(req, timeout=60) as resp:
            while True:
                buf = resp.read(256 * 1024)
                if not buf:
                    break
                n += len(buf)
        got[i] = n

    t0 = time.time()
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    dt = time.time() - t0
    stats.append((threads, sum(got), dt))


def main():
    size_mb = int(sys.argv[1]) if len(sys.argv) > 1 else 256
    tmp = tempfile.mkdtemp(prefix="fs_bench_")
    try:
        print("生成测试文件 %d MB …" % size_mb)
        make_file(tmp, size_mb)
        total = os.path.getsize(os.path.join(tmp, "bench.bin"))
        port = free_port()
        srv = HttpFileServer(lambda: [{"name": "b", "path": tmp}], port=port)
        ok, msg = srv.start()
        if not ok:
            print("服务启动失败：", msg)
            return 1
        url = "http://127.0.0.1:%d/b/bench.bin" % port
        print("服务已起在 %s，文件 %d MB\n" % (url, total // 1048576))

        stats = []
        # 预热一次，排除首次读盘/建连的偏差
        run_threads(url, 1, total, [])
        for n in (1, 4, 8):
            run_threads(url, n, total, stats)

        print("%-8s %-14s %-8s %s" % ("线程", "传输量", "耗时", "平均速度"))
        print("-" * 46)
        best = 0
        for threads, got, dt in stats:
            speed = got / dt if dt else 0
            best = max(best, speed)
            print("%-8d %-14s %-8s %s" % (
                threads, "%.1f MB" % (got / 1048576.0), "%.2fs" % dt,
                human_speed(speed)))
        print("-" * 46)
        print("峰值吞吐：%s（%.0f Mbps）" % (best, best * 8 / 1e6))
        srv.stop()
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

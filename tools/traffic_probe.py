# -*- coding: utf-8 -*-
"""连接监控探针：模拟 3 台设备同时下载，验证「按 IP 统计」真的出数据。

Windows 上 127.0.0.0/8 整段可用，所以用不同的回环源地址冒充不同客户端，
服务端看到的 peer 就是不同 IP —— 不用第二台机器也能验证多 IP 展示。

    python tools/traffic_probe.py
"""

import http.client
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fileshare.server.httpd import HttpFileServer                # noqa: E402
from fileshare.traffic import TrafficMonitor                    # noqa: E402
from fileshare.util import RateLimiter                          # noqa: E402

SOURCES = ["127.0.0.2", "127.0.0.3", "127.0.0.4"]


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def pull(port, source, stop, got):
    while not stop.is_set():
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10,
                                              source_address=(source, 0))
            conn.request("GET", "/s/big.bin")
            resp = conn.getresponse()
            while not stop.is_set():
                buf = resp.read(65536)
                if not buf:
                    break
                got[0] += len(buf)
            conn.close()
        except Exception:                                  # noqa: BLE001
            time.sleep(0.2)


def main():
    tmp = tempfile.mkdtemp(prefix="fs_tp_")
    share = os.path.join(tmp, "s")
    os.makedirs(share)
    path = os.path.join(share, "big.bin")
    block = os.urandom(1024 * 1024)
    with open(path, "wb") as fp:
        for _ in range(48):
            fp.write(block)

    port = free_port()
    mon = TrafficMonitor()
    limiter = RateLimiter(1500 * 1024)          # 1.5 MB/s，好让传输持续住
    srv = HttpFileServer(lambda: [{"name": "s", "path": share}], port=port,
                         limiter=limiter, traffic=mon)
    ok, msg = srv.start()
    assert ok, msg
    print("服务已起：127.0.0.1:%d（限速 1.5 MB/s）" % port)

    stop = threading.Event()
    got = [[0] for _ in SOURCES]
    threads = [threading.Thread(target=pull, args=(port, ip, stop, g),
                                daemon=True)
               for ip, g in zip(SOURCES, got)]
    for t in threads:
        t.start()
    time.sleep(2.5)                              # 让速度稳定下来

    mon.sample()
    time.sleep(1.0)
    rows = mon.sample()
    print("采集到 %d 条连接：" % len(rows))
    for r in rows:
        print("  %-12s %-6s %-12s %8.2f MB/s" % (
            r["ip"], r["proto"], r["name"], r["speed"] / 1048576.0))

    # 渲染界面并截图
    from fileshare.config import Config
    from fileshare.ui.app import App, _by_ip
    cfg = Config(path=os.path.join(tmp, "cfg.json"))
    cfg.data["shares"] = [{"name": "s", "path": share, "enabled": True}]
    cfg.data["http"]["enabled"] = False
    cfg.data["ftp"]["enabled"] = False
    app = App(cfg)
    app.manager.traffic = mon
    app.manager.stats.add_sent(sum(g[0] for g in got))
    app._traffic_rows = rows
    app._traffic_agg = _by_ip(rows)
    app.show_page("traffic")
    for _ in range(20):
        app.root.update()
        time.sleep(0.05)

    from tools.gui_probe import find_window, grab
    wins = find_window("文件共享中心")
    if wins:
        out = os.path.join(ROOT, "screenshots", "traffic_live.png")
        print("截图 →", out if grab(wins[0][0], out) else "失败")

    agg = app._traffic_agg
    ips = sorted(a["ip"] for a in agg)
    ok = len(agg) == 3 and all(a["speed"] > 0 for a in agg)
    print("\n按 IP 汇总：%s" % [(a["ip"], round(a["speed"] / 1024), a["conns"])
                              for a in agg])
    stop.set()
    time.sleep(0.4)
    app._quit()
    srv.stop()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n%s" % ("TRAFFIC PROBE PASS" if ok else "TRAFFIC PROBE FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

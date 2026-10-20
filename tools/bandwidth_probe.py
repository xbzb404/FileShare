# -*- coding: utf-8 -*-
"""带宽探针：真跑 35 秒流量，验证「总带宽」和「近 30 秒平均」算得准不准。

断言三件事：
  1. 瞬时总带宽 ≈ 限速值（4 MB/s）；
  2. 近 30 秒加权平均 ≈ 同一量级（爬升段会拉低一点，允许 -25%）；
  3. 网卡链路速率、占用百分比都能正常显示。

    python tools/bandwidth_probe.py
"""

import http.client
import os
import shutil
import socket
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

LIMIT = 4096 * 1024                 # 4 MB/s，跑满 100Mbps 网卡的 1/3
SOURCES = ["127.0.0.2", "127.0.0.3", "127.0.0.4"]
SECONDS = 34


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def pull(port, source, stop):
    while not stop.is_set():
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10,
                                              source_address=(source, 0))
            conn.request("GET", "/s/big.bin")
            resp = conn.getresponse()
            while not stop.is_set():
                if not resp.read(65536):
                    break
            conn.close()
        except Exception:                                  # noqa: BLE001
            time.sleep(0.2)


def main():
    tmp = tempfile.mkdtemp(prefix="fs_bw_")
    share = os.path.join(tmp, "s")
    os.makedirs(share)
    block = os.urandom(1024 * 1024)
    with open(os.path.join(share, "big.bin"), "wb") as fp:
        for _ in range(160):                   # 160MB，够 34 秒 × 4MB/s
            fp.write(block)

    port = free_port()

    from fileshare.config import Config
    from fileshare.ui.app import App
    from fileshare.util import human_link
    cfg = Config(path=os.path.join(tmp, "cfg.json"))
    cfg.data["shares"] = [{"name": "s", "path": share, "enabled": True}]
    cfg.data["http"]["enabled"] = False
    cfg.data["ftp"]["enabled"] = False
    app = App(cfg)
    app._tick()                # 启动每秒采样链（正常由 run() 启动，探针手动启动）

    # 关键：服务端必须写进 App 自己的 Stats/Traffic 实例，否则界面读的是
    # 另一份计数器（永远 0）。
    srv = HttpFileServer(lambda: [{"name": "s", "path": share}], port=port,
                         limiter=RateLimiter(LIMIT),
                         stats=app.manager.stats, traffic=app.manager.traffic)
    ok, msg = srv.start()
    assert ok, msg
    print("服务已起 127.0.0.1:%d，限速 %.0f MB/s" % (port, LIMIT / 1048576.0))

    stop = threading.Event()

    # 故意灌一路「下载器」流量（软件作为客户端去下别人的东西），
    # 用来验证共享总带宽不会把它算进去。
    def fake_downloader():
        while not stop.is_set():
            app.manager.stats.add_dl(512 * 1024)      # ≈1 MB/s
            time.sleep(0.5)

    threading.Thread(target=fake_downloader, daemon=True).start()

    threads = [threading.Thread(target=pull, args=(port, ip, stop), daemon=True)
               for ip in SOURCES]
    for t in threads:
        t.start()

    t0 = time.time()
    samples = []
    while time.time() - t0 < SECONDS:
        app.root.update()
        time.sleep(0.05)
        if int(time.time() - t0) > len(samples):
            samples.append(app._last_speed)

    up, down, dl = app._last_speed
    total = up + down                       # 共享流量口径：不含下载器
    ag = app._avg30
    link = app._link_bps
    print("\n瞬时：发送给客户端 %.2f MB/s  接收上传 %.2f MB/s  → 共享总带宽 %.2f MB/s"
          % (up / 1048576.0, down / 1048576.0, total / 1048576.0))
    print("      （同期下载器 %.2f MB/s，按要求不计入共享总带宽）"
          % (dl / 1048576.0))
    print("近 30 秒平均：发送 %.2f  接收 %.2f  → 共享总带宽 %.2f MB/s（采样 %.0f 秒）"
          % (ag["up"] / 1048576.0, ag["down"] / 1048576.0,
             (ag["up"] + ag["down"]) / 1048576.0, ag["span"]))
    print("网卡链路 %s，当前占用 %.1f%%"
          % (human_link(link), (total * 8.0 / float(link) * 100) if link else 0))

    app.show_page("overview")
    for _ in range(24):
        app.root.update()
        time.sleep(0.05)
    from tools.gui_probe import find_window, grab
    wins = find_window("文件共享中心")
    if wins:
        out = os.path.join(ROOT, "screenshots", "bandwidth.png")
        print("截图 →", out if grab(wins[0][0], out) else "截图失败")

    expect = LIMIT
    ok_total = abs(total - expect) <= expect * 0.25
    ok_avg = (ag["up"] + ag["down"]) >= expect * 0.75
    ok_link = link > 0
    # 关键：下载器那 1 MB/s 不能混进共享总带宽
    ok_excl = dl > 0.5 * 1048576 and total < expect * 1.15
    print("\n判定：共享总带宽 %s · 30 秒平均 %s · 网卡占用 %s · 排除下载器 %s" % (
        "OK" if ok_total else "偏差过大",
        "OK" if ok_avg else "偏低",
        "OK" if ok_link else "未知",
        "OK" if ok_excl else "失败（下载器被算进去了）"))

    stop.set()
    time.sleep(0.4)
    app._quit()
    srv.stop()
    shutil.rmtree(tmp, ignore_errors=True)
    good = ok_total and ok_avg and ok_link and ok_excl
    print("\n%s" % ("BANDWIDTH PROBE PASS" if good else "BANDWIDTH PROBE FAIL"))
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())

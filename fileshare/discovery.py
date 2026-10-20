# -*- coding: utf-8 -*-
"""局域网互发现：同网络里跑本软件的主机互相找到对方的共享。

机制（纯标准库，无广播依赖，路由器禁广播也能用）：

- **服务端**：开了共享后，本机监听一个固定的冷门探测端口
  （默认 47610）。其他客户端连上来发一行 ``FSQ1`` 才回应一份 JSON
  （主机名 + HTTP/FTP/SMB 的开启状态与端口）——随便谁拿端口扫描器
  扫到这个端口，不发暗号只会被断开，拿不到共享信息。
- **客户端（扫描器）**：对所在 /24 网段的 1~254 并发发起探测，
  单个地址超时很短（0.15s），几十个并发线程一两秒扫完，全程后台
  线程跑，界面完全无感。
"""

import json
import os
import socket
import threading
import time

DISCOVERY_PORT = 47610            # 固定探测端口（冷门，避开 8080/2121/445 等）
MAGIC = b"FSQ1\n"                 # 客户端暗号
MAX_PAYLOAD = 8 * 1024            # 应答上限，防异常客户端撑爆内存


class DiscoveryServer(object):
    """应答本机共享状态的 TCP 小服务。

    ``provider`` 是无参函数，返回要广播的状态 dict（见 manager._discovery_info）。
    端口被占（同机开了第二个实例）时 start 返回 False，不影响其它功能。
    """

    def __init__(self, provider, port=DISCOVERY_PORT, host="0.0.0.0"):
        self.provider = provider
        self.port = int(port)
        self.host = host
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self.running = False

    def start(self):
        if self.running:
            return True, "已在运行"
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((self.host, self.port))
        except OSError as exc:
            try:
                s.close()
            except OSError:
                pass
            return False, "探测端口 %d 被占用（%s）" % (self.port, exc)
        s.listen(16)
        s.settimeout(0.5)
        self._sock = s
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="fs-discover-srv")
        self._thread.start()
        self.running = True
        return True, "探测服务已启动（端口 %d）" % self.port

    def stop(self):
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self.running = False
        return True, "已停止"

    # ---- 内部 ----
    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._answer, args=(conn, addr),
                             daemon=True).start()

    def _answer(self, conn, addr):
        try:
            conn.settimeout(2.0)
            data = conn.recv(64)
            if data.strip() != MAGIC.strip():
                return                      # 不是本软件的探测，直接断开
            try:
                info = self.provider() or {}
            except Exception:               # noqa: BLE001
                info = {}
            payload = json.dumps(info, ensure_ascii=False).encode("utf-8")
            conn.sendall(payload[:MAX_PAYLOAD])
        except (OSError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def query_host(ip, port=DISCOVERY_PORT, timeout=0.8):
    """向单个地址发探测暗号，返回解析后的状态 dict；不是本软件/超时返回 None。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect((ip, port))
        s.sendall(MAGIC)
        buf = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = s.recv(2048)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_PAYLOAD:
                break
            if b"}" in buf:              # JSON 行结束（应答只有一段 JSON）
                break
        if not buf:
            return None
        info = json.loads(buf.decode("utf-8", "replace"))
        if not isinstance(info, dict) or info.get("app") != "FileShare":
            return None
        info["ip"] = ip
        info["seen"] = time.time()
        return info
    except (OSError, ValueError):
        return None
    finally:
        try:
            s.close()
        except OSError:
            pass


def subnet_of(ip):
    """192.168.49.253 → ('192.168.49', 253)；解析不了返回 None。"""
    parts = (ip or "").split(".")
    if len(parts) != 4:
        return None
    try:
        ints = [int(p) for p in parts]
    except ValueError:
        return None
    if any(n < 0 or n > 255 for n in ints):
        return None
    return ".".join(str(n) for n in ints[:3]), ints[3]


def scan_subnet(base_ip, port=DISCOVERY_PORT, exclude_ip=None, timeout=0.35,
                workers=64, progress=None, targets=None):
    """并发扫 /24 网段里跑本软件的主机。

    返回按主机名排序的列表；``progress`` 可选回调（已完成数, 总数），
    用于界面显示扫描进度。``targets`` 可显式指定地址列表（测试用），
    不给就扫 ``base_ip`` 所在 /24 的 1~254。全程线程池，主界面无感。
    """
    if targets is None:
        subnet = subnet_of(base_ip)
        if subnet is None:
            return [], 0
        net, _me = subnet
        targets = ["%s.%d" % (net, i) for i in range(1, 255)]
        if exclude_ip:
            targets = [t for t in targets if t != exclude_ip]
    else:
        targets = list(targets)
        if exclude_ip:
            targets = [t for t in targets if t != exclude_ip]
    results = []
    lock = threading.Lock()
    done = {"n": 0}
    total = len(targets)

    def work(ip):
        info = query_host(ip, port=port, timeout=timeout)
        with lock:
            if info is not None:
                results.append(info)
            done["n"] += 1
            if progress is not None:
                try:
                    progress(done["n"], total)
                except Exception:        # noqa: BLE001
                    pass

    threads = []
    for ip in targets:
        # 分批起线程，避免一次开 254 个
        while len([t for t in threads if t.is_alive()]) >= workers:
            time.sleep(0.01)
        th = threading.Thread(target=work, args=(ip,), daemon=True)
        th.start()
        threads.append(th)
    for th in threads:
        th.join(timeout=timeout + 2.0)
    results.sort(key=lambda r: (r.get("host") or "", r["ip"]))
    return results, total

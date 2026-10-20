# -*- coding: utf-8 -*-
"""核心逻辑 + 两个服务端 + 下载器的端到端测试。

这些测试**真的起服务、真的传数据**（回环地址），不是打桩：
HTTP 服务端测 Range/206/416/目录列表/上传，FTP 测登录/列目录/下载/续传/上传，
下载器测分片、完整性、暂停续传。

跑法（managed Python 即可，不需要 tkinter）：
    python -m unittest discover -s tests -v
"""

import ftplib
import io
import os
import socket
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fileshare.config import Config                             # noqa: E402
from fileshare.download.engine import (Downloader, Segment,     # noqa: E402
                                      _plan_segments)
from fileshare.netutil import safe_join                         # noqa: E402
from fileshare.server.ftpd import FtpServer                     # noqa: E402
from fileshare.server.httpd import (HttpFileServer,             # noqa: E402
                                    _parse_range)
from fileshare.util import EventLog, RateLimiter, Stats, human_bytes  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_until(fn, timeout=20.0, interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(interval)
    return False


# ------------------------------------------------------------------ 单元

class TestUtil(unittest.TestCase):
    def test_rate_limiter_unlimited(self):
        lim = RateLimiter(0)
        self.assertEqual(lim.consume(10 * 1024 * 1024), 0.0)
        self.assertEqual(lim.chunk_size(1234), 1234)

    def test_rate_limiter_limits(self):
        lim = RateLimiter(100 * 1024)          # 100 KB/s
        # 桶容量是 1/4 秒 = 25KB，一次要 100KB 必然要等
        delay = lim.consume(100 * 1024)
        self.assertGreater(delay, 0.5)
        self.assertLess(delay, 1.5)
        # 限速时读块会被压小，让速率更平滑
        self.assertLess(lim.chunk_size(512 * 1024), 512 * 1024)

    def test_stats_rate(self):
        st = Stats()
        st.add_sent(1024 * 1024)
        time.sleep(0.05)
        up, _down, _dl = st.sample()
        self.assertGreater(up, 0)
        self.assertEqual(st.totals()["sent"], 1024 * 1024)

    def test_human_bytes(self):
        self.assertEqual(human_bytes(0), "0 B")
        self.assertEqual(human_bytes(1536), "1.5 KB")
        self.assertTrue(human_bytes(3 * 1024 ** 3).endswith("GB"))

    def test_stats_avg_empty(self):
        a = Stats().avg(30)
        self.assertEqual(a["total"], 0.0)
        self.assertEqual(a["n"], 0)

    def test_stats_avg_window(self):
        """avg(n) 只统计最近 n 条，且按 1 秒一条做时间加权。"""
        st = Stats()
        t0 = 1000.0
        # 前 10 条 9 MB/s（窗口外的老数据）+ 后 30 条 1 MB/s
        st._hist = [(t0 + i, 9 * 1024 * 1024.0, 0.0, 0.0) for i in range(10)]
        st._hist += [(t0 + 10 + i, 1024 * 1024.0, 0.0, 0.0)
                     for i in range(30)]
        a = st.avg(30)                      # 只看最后 30 条 → 1 MB/s
        self.assertEqual(a["n"], 30)
        self.assertAlmostEqual(a["up"], 1024 * 1024.0, delta=1024)
        self.assertAlmostEqual(a["peak"], 1024 * 1024.0, delta=1024)
        # 全 40 条：(10×9 + 30×1) / 40 = 3 MB/s
        b = st.avg(60)
        self.assertEqual(b["n"], 40)
        self.assertAlmostEqual(b["up"], 3 * 1024 * 1024.0, delta=2048)
        self.assertAlmostEqual(b["peak"], 9 * 1024 * 1024.0, delta=1024)

    def test_stats_avg_irregular_interval(self):
        """采样间隔不齐（UI 卡顿/睡眠）时按时间加权，不被稀疏采样带偏。"""
        st = Stats()
        # t=1 那条代表 0~1s 的 10 MB/s；t=11 那条代表 1~11s 的 0
        st._hist = [(0.0, 0.0, 0.0, 0.0),
                    (1.0, 10 * 1024 * 1024.0, 0.0, 0.0),
                    (11.0, 0.0, 0.0, 0.0)]
        a = st.avg(30)
        # 简单算术平均会得 3.33 MB/s；正确的加权结果 ≈ 10MB / 7s ≈ 1.43 MB/s
        self.assertGreater(a["up"], 1.0 * 1024 * 1024)
        self.assertLess(a["up"], 2.0 * 1024 * 1024)

    def test_link_label(self):
        from fileshare.util import human_link
        self.assertEqual(human_link(100000000), "100 Mbps")
        self.assertEqual(human_link(1000000000), "1 Gbps")
        self.assertEqual(human_link(2500000000), "2.5 Gbps")
        self.assertEqual(human_link(0), "0 bps")

    def test_link_speed_probe(self):
        """网卡速率探测：拿不到不算失败（虚拟机/无网卡），拿到必须合理。"""
        from fileshare.netutil import adapters, link_speed
        for a in adapters():
            self.assertGreaterEqual(a["speed"], 0)
            self.assertLess(a["speed"], 10 ** 12)
        self.assertGreaterEqual(link_speed(), 0)

    def test_safe_join_blocks_traversal(self):
        root = os.path.abspath(os.sep + "tmp" if os.name != "nt" else "C:\\tmp")
        self.assertIsNone(safe_join(root, "../windows/win.ini"))
        self.assertIsNone(safe_join(root, "a/../../b"))
        got = safe_join(root, "a/b.txt")
        self.assertTrue(got.endswith(os.path.join("a", "b.txt")))


class TestRangeParse(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(_parse_range("bytes=0-99", 1000), (0, 99))
        self.assertEqual(_parse_range("bytes=900-", 1000), (900, 999))
        self.assertEqual(_parse_range("bytes=-100", 1000), (900, 999))
        self.assertEqual(_parse_range("bytes=0-99999", 1000), (0, 999))

    def test_invalid(self):
        # 合法但不可满足 → None（416）
        self.assertIsNone(_parse_range("bytes=5000-6000", 1000))
        self.assertIsNone(_parse_range("bytes=5-2", 1000))
        # 语法畸形 → "ignore"（按 RFC 7233 当没发，回 200 全量）
        self.assertEqual(_parse_range("items=0-10", 1000), "ignore")
        self.assertEqual(_parse_range("bytes=abc", 1000), "ignore")
        self.assertEqual(_parse_range("bytes=-", 1000), "ignore")

    def test_segments(self):
        segs = _plan_segments(8 * 1024 * 1024, 4)
        self.assertEqual(len(segs), 4)
        self.assertEqual(segs[0].start, 0)
        self.assertEqual(segs[-1].end, 8 * 1024 * 1024 - 1)
        # 段之间必须严丝合缝、不重叠
        for a, b in zip(segs, segs[1:]):
            self.assertEqual(a.end + 1, b.start)
        # 小于最小分片时退化为单段
        self.assertEqual(len(_plan_segments(1000, 8)), 1)


# ------------------------------------------------------------------ HTTP

class TestHttpServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fs_http_")
        share = os.path.join(cls.tmp, "media")
        os.makedirs(os.path.join(share, "sub"))
        cls.data = bytes((i * 37) % 256 for i in range(1024 * 1024 + 123))
        cls.file = os.path.join(share, "big.bin")
        with open(cls.file, "wb") as fp:
            fp.write(cls.data)
        with open(os.path.join(share, "sub", "note.txt"), "w",
                  encoding="utf-8") as fp:
            fp.write("hello 共享")

        cls.port = free_port()
        cls.srv = HttpFileServer(
            lambda: [{"name": "media", "path": cls.tmp + os.sep + "media"}],
            port=cls.port, dirlist=True)
        ok, msg = cls.srv.start()
        assert ok, msg
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def _get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        return urllib.request.urlopen(req, timeout=10)

    def test_root_lists_shares(self):
        with self._get("/") as resp:
            body = resp.read().decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn("media", body)
        self.assertIn("文件共享", body)

    def test_dir_listing_beats_unicode(self):
        with self._get("/media/sub/") as resp:
            body = resp.read().decode("utf-8")
        self.assertIn("note.txt", body)

    def test_full_download(self):
        with self._get("/media/big.bin") as resp:
            data = resp.read()
            self.assertEqual(resp.status, 200)
            self.assertEqual(int(resp.headers["Content-Length"]), len(self.data))
            self.assertEqual(resp.headers["Accept-Ranges"], "bytes")
        self.assertEqual(data, self.data)

    def test_range_request(self):
        with self._get("/media/big.bin", {"Range": "bytes=1000-2023"}) as resp:
            part = resp.read()
            self.assertEqual(resp.status, 206)
            self.assertEqual(resp.headers["Content-Range"],
                             "bytes 1000-2023/%d" % len(self.data))
        self.assertEqual(part, self.data[1000:2024])

    def test_range_suffix(self):
        with self._get("/media/big.bin", {"Range": "bytes=-500"}) as resp:
            part = resp.read()
        self.assertEqual(part, self.data[-500:])

    def test_range_unsatisfiable(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/media/big.bin", {"Range": "bytes=99999999-"})
        self.assertEqual(ctx.exception.code, 416)

    def test_head(self):
        req = urllib.request.Request(self.base + "/media/big.bin", method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(int(resp.headers["Content-Length"]), len(self.data))
            self.assertEqual(resp.read(), b"")

    def test_404_and_traversal(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/media/nope.bin")
        self.assertEqual(ctx.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/media/../../windows/win.ini")
        self.assertIn(ctx.exception.code, (403, 404))

    def test_put_rejected_by_default(self):
        """上传开关默认关闭：PUT 必须 403，且不能落盘。"""
        payload = "不应被写入".encode("utf-8") * 100
        req = urllib.request.Request(self.base + "/media/up.bin", data=payload,
                                     method="PUT")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=15)
        self.assertEqual(ctx.exception.code, 403)
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, "media", "up.bin")))

    def test_keepalive_two_requests(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", "/media/sub/note.txt")
            r1 = conn.getresponse()
            r1.read()
            conn.request("GET", "/media/sub/note.txt")
            r2 = conn.getresponse()
            self.assertEqual(r2.read(), "hello 共享".encode("utf-8"))
        finally:
            conn.close()


class TestHttpUpload(unittest.TestCase):
    """上传开关打开后的正向用例 + 安全防护用例。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fs_up_")
        cls.share = os.path.join(cls.tmp, "media")
        os.makedirs(cls.share)
        cls.port = free_port()
        cls.srv = HttpFileServer(
            lambda: [{"name": "media", "path": cls.share}],
            port=cls.port, allow_upload=True, dirlist=True)
        ok, msg = cls.srv.start()
        assert ok, msg
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def _put(self, path, payload):
        req = urllib.request.Request(self.base + path, data=payload,
                                     method="PUT")
        return urllib.request.urlopen(req, timeout=15)

    def test_put_ok(self):
        payload = "上传测试".encode("utf-8") * 1000
        with self._put("/media/up.bin", payload) as resp:
            self.assertEqual(resp.status, 201)
        with open(os.path.join(self.share, "up.bin"), "rb") as fp:
            self.assertEqual(fp.read(), payload)
        os.remove(os.path.join(self.share, "up.bin"))

    def test_put_traversal_rejected(self):
        """.. 跳出目录、盘符、备用数据流全部拒绝，且不能落盘。"""
        bad = ["/media/../evil.txt", "/media/..%2Fevil.txt",
               "/media/sub/../../evil.txt", "/media/evil:ads.txt"]
        for path in bad:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._put(path, b"x")
            self.assertIn(ctx.exception.code, (400, 403, 404),
                          "%s -> %d" % (path, ctx.exception.code))
        names = os.listdir(self.share)
        self.assertEqual(names, [], "共享目录不该有落盘文件：%s" % names)

    def test_put_exec_hardened(self):
        """可执行文件落盘必须改名 .noexec。"""
        with self._put("/media/tool.exe", b"MZ fake exe") as resp:
            self.assertEqual(resp.status, 201)
        self.assertFalse(os.path.exists(os.path.join(self.share, "tool.exe")))
        hardened = os.path.join(self.share, "tool.exe.noexec")
        self.assertTrue(os.path.exists(hardened))
        with open(hardened, "rb") as fp:
            self.assertEqual(fp.read(), b"MZ fake exe")
        os.remove(hardened)

    def test_put_no_overwrite(self):
        """同名文件已存在 → 409，且原文件不被改动。"""
        p = os.path.join(self.share, "keep.txt")
        with open(p, "wb") as fp:
            fp.write(b"original")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._put("/media/keep.txt", b"overwrite!")
        self.assertEqual(ctx.exception.code, 409)
        with open(p, "rb") as fp:
            self.assertEqual(fp.read(), b"original")
        self.assertFalse(os.path.exists(p + ".fsupload"))
        os.remove(p)


# ------------------------------------------------------------------ FTP

class TestFtpServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fs_ftp_")
        cls.share = os.path.join(cls.tmp, "media")
        os.makedirs(os.path.join(cls.share, "sub"))
        cls.data = bytes((i * 11) % 256 for i in range(600 * 1024))
        cls.file = os.path.join(cls.share, "big.bin")
        with open(cls.file, "wb") as fp:
            fp.write(cls.data)
        cls.port = free_port()
        cls.srv = FtpServer(
            lambda: [{"name": "media", "path": cls.share}],
            port=cls.port, user="share", password="p@ss", allow_upload=True)
        ok, msg = cls.srv.start()
        assert ok, msg

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def _login(self):
        ftp = ftplib.FTP()
        ftp.encoding = "utf-8"
        ftp.connect("127.0.0.1", self.port, timeout=15)
        ftp.login("share", "p@ss")
        return ftp

    def test_login_bad_password(self):
        ftp = ftplib.FTP()
        ftp.connect("127.0.0.1", self.port, timeout=10)
        with self.assertRaises(ftplib.error_perm):
            ftp.login("share", "wrong")
        ftp.close()

    def test_list_root_and_cwd(self):
        ftp = self._login()
        try:
            names = ftp.nlst()
            self.assertIn("media", names)
            ftp.cwd("media")
            self.assertEqual(ftp.pwd(), "/media")
            self.assertIn("big.bin", ftp.nlst())
            ftp.cwd("sub")
            self.assertEqual(ftp.pwd(), "/media/sub")
            ftp.cwd("..")
            self.assertEqual(ftp.pwd(), "/media")
        finally:
            ftp.quit()

    def test_mlsd(self):
        ftp = self._login()
        try:
            ftp.cwd("media")
            rows = list(ftp.mlsd())
            names = [r[0] for r in rows]
            self.assertIn("big.bin", names)
            entry = dict(rows[0][1])
            self.assertIn("type", entry)
        finally:
            ftp.quit()

    def test_size_and_retr(self):
        ftp = self._login()
        try:
            ftp.cwd("media")
            self.assertEqual(ftp.size("big.bin"), len(self.data))
            buf = io.BytesIO()
            ftp.retrbinary("RETR big.bin", buf.write)
            self.assertEqual(buf.getvalue(), self.data)
        finally:
            ftp.quit()

    def test_rest_resume(self):
        """REST + RETR：从 100 KB 处续传，只应拿到剩余部分。"""
        offset = 100 * 1024
        ftp = self._login()
        try:
            ftp.cwd("media")
            buf = io.BytesIO()
            ftp.retrbinary("RETR big.bin", buf.write, rest=offset)
            self.assertEqual(buf.getvalue(), self.data[offset:])
        finally:
            ftp.quit()

    def test_stor_upload(self):
        payload = b"x" * 4096 + "中文内容".encode("utf-8")
        ftp = self._login()
        try:
            ftp.cwd("media")
            ftp.storbinary("STOR up.bin", io.BytesIO(payload))
        finally:
            ftp.quit()
        with open(os.path.join(self.share, "up.bin"), "rb") as fp:
            self.assertEqual(fp.read(), payload)
        os.remove(os.path.join(self.share, "up.bin"))

    def test_mkdir_rmdir_rename(self):
        ftp = self._login()
        try:
            ftp.cwd("media")
            ftp.mkd("newdir")
            self.assertIn("newdir", ftp.nlst())
            ftp.rename("newdir", "renamed")
            self.assertIn("renamed", ftp.nlst())
            ftp.rmd("renamed")
            self.assertNotIn("renamed", ftp.nlst())
        finally:
            ftp.quit()

    def test_rename_cannot_bypass_noexec(self):
        """RNTO 不能把文件改名成可执行名绕过 noexec，也不能造 ADS 名。"""
        ftp = self._login()
        try:
            ftp.cwd("media")
            ftp.storbinary("STOR plain.txt", io.BytesIO(b"hello"))
            # 改成 .exe → 必须被自动加固成 .noexec
            ftp.rename("plain.txt", "evil.exe")
            names = set(ftp.nlst())
            self.assertNotIn("evil.exe", names)
            self.assertIn("evil.exe.noexec", names)
            # 冒号（NTFS 备用数据流）名 → 直接拒绝
            with self.assertRaises(ftplib.error_perm):
                ftp.rename("plain.txt", "ads:hide")
        finally:
            ftp.quit()
        for junk in ("plain.txt", "evil.exe.noexec"):
            p = os.path.join(self.share, junk)
            if os.path.exists(p):
                os.remove(p)

    def test_traversal_blocked(self):
        ftp = self._login()
        try:
            with self.assertRaises(ftplib.error_perm):
                ftp.cwd("/media/../../../windows")
        finally:
            ftp.quit()


# ------------------------------------------------------------------ 下载器

class TestDownloader(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fs_dl_")
        share = os.path.join(cls.tmp, "share")
        os.makedirs(share)
        cls.data = bytes((i * 7) % 256 for i in range(6 * 1024 * 1024))
        with open(os.path.join(share, "movie.bin"), "wb") as fp:
            fp.write(cls.data)
        cls.port = free_port()
        cls.srv = HttpFileServer(lambda: [{"name": "s", "path": share}],
                                 port=cls.port)
        ok, msg = cls.srv.start()
        assert ok, msg
        cls.url = "http://127.0.0.1:%d/s/movie.bin" % cls.port
        cls.out = os.path.join(cls.tmp, "out")
        os.makedirs(cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def _make(self, threads=4):
        cfg = Config(path=os.path.join(self.out, "cfg.json"))
        cfg.set(self.out, "download", "dir")
        cfg.set(threads, "download", "threads")
        dl = Downloader(cfg, RateLimiter(0), Stats(), EventLog())
        return dl

    def test_multithread_download(self):
        dl = self._make(threads=4)
        task = dl.add(self.url, folder=self.out, threads=4)
        ok = wait_until(lambda: task.status in ("done", "error"), 60)
        self.assertTrue(ok, "下载超时：%s" % task.error)
        self.assertEqual(task.status, "done", task.error)
        self.assertEqual(task.size, len(self.data))
        with open(task.final_path, "rb") as fp:
            self.assertEqual(fp.read(), self.data)

    def test_duplicate_url_rejected(self):
        """同一个链接不允许重复下载（含结尾斜杠变体）。"""
        dl = self._make(threads=2)
        dl.add(self.url, folder=self.out, threads=2, autostart=False)
        with self.assertRaises(ValueError):
            dl.add(self.url, folder=self.out, threads=2, autostart=False)
        with self.assertRaises(ValueError):
            dl.add(self.url + "/", folder=self.out, threads=2,
                   autostart=False)
        self.assertEqual(len(dl.tasks), 1)

    def test_discovery_query_and_scan(self):
        """互发现：探测端口应答暗号；扫描器能找到网段内的本机服务。"""
        from fileshare.discovery import DiscoveryServer, query_host, \
            scan_subnet
        port = free_port()

        def provider():
            return {"app": "FileShare", "ver": 1, "host": "测试机",
                    "http": {"on": True, "port": 8080, "shares": ["media"]},
                    "ftp": {"on": False, "port": 2121},
                    "smb": {"on": False, "names": []}}

        srv = DiscoveryServer(provider, port=port, host="127.0.0.1")
        ok, msg = srv.start()
        assert ok, msg
        try:
            # 1) 单点探测
            info = query_host("127.0.0.1", port=port, timeout=1.5)
            self.assertIsNotNone(info, "探测无应答")
            self.assertEqual(info["app"], "FileShare")
            self.assertEqual(info["host"], "测试机")
            self.assertTrue(info["http"]["on"])
            self.assertEqual(info["http"]["shares"], ["media"])

            # 2) 错误暗号必须被无视：直接断开、不给任何数据
            s = socket.create_connection(("127.0.0.1", port), timeout=2)
            try:
                s.sendall(b"GET / HTTP/1.0\r\n\r\n")
                s.settimeout(1.0)
                data = s.recv(1024)
                self.assertEqual(data, b"", "错误暗号竟收到应答：%r" % data)
            finally:
                s.close()

            # 3) 扫描器：显式目标列表里能找到它
            found, _total = scan_subnet("127.0.0.1", port=port,
                                        targets=["127.0.0.1"], timeout=1.5)
            self.assertTrue(any(f["host"] == "测试机" for f in found),
                            "扫描器没找到主机：%s" % found)

            # 4) 找不到的地址安静返回空
            found2, _t2 = scan_subnet("127.0.0.1", port=port,
                                      targets=["127.0.0.40"], timeout=0.3)
            self.assertEqual(found2, [])
        finally:
            srv.stop()

    def test_pause_then_resume(self):
        """暂停后必须留下 .fspart + .fsmeta，恢复后文件完整。"""
        dl = self._make(threads=4)
        dl.limiter.set_rate(2 * 1024 * 1024)          # 限速，保证来得及暂停
        task = dl.add(self.url, folder=self.out, threads=4,
                      filename="resume.bin")
        self.assertTrue(wait_until(lambda: task.done > 512 * 1024, 20))
        dl.pause(task.id)
        self.assertTrue(wait_until(lambda: task.status == "paused", 10))
        part_done = task.done
        self.assertTrue(os.path.exists(task.part_path))
        self.assertTrue(os.path.exists(task.meta_path))
        dl.limiter.set_rate(0)
        dl.resume(task.id)
        self.assertTrue(wait_until(lambda: task.status in ("done", "error"), 60))
        self.assertEqual(task.status, "done", task.error)
        with open(task.final_path, "rb") as fp:
            self.assertEqual(fp.read(), self.data)
        self.assertGreaterEqual(task.done, part_done)

    def test_404_is_error(self):
        dl = self._make()
        task = dl.add("http://127.0.0.1:%d/s/none.bin" % self.port,
                      folder=self.out, filename="none.bin")
        self.assertTrue(wait_until(lambda: task.status == "error", 20))
        self.assertNotEqual(task.status, "done")

    def test_duplicate_done_raises_with_done_flag(self):
        """重复下载已完成的链接 → DuplicateError(done=True)，供 UI 问覆盖/另存。"""
        dl = self._make(threads=2)
        task = dl.add(self.url, folder=self.out, filename="dup.bin",
                      threads=2)
        self.assertTrue(wait_until(lambda: task.status == "done", 60),
                        task.error)
        self.assertTrue(os.path.isfile(task.final_path))
        with self.assertRaises(Downloader.DuplicateError) as ctx:
            dl.add(self.url, folder=self.out, threads=2)
        self.assertTrue(ctx.exception.done, "已完成重复必须带 done 标记")
        self.assertIsNotNone(ctx.exception.task)
        self.assertEqual(ctx.exception.task.id, task.id)

    def test_cancel_midway_cleans_cache(self):
        """取消（删除）一个下载中的任务：.fspart/.fsmeta 必须真的被删掉。"""
        dl = self._make(threads=2)
        dl.limiter.set_rate(512 * 1024)            # 限速，保证来得及取消
        task = dl.add(self.url, folder=self.out, filename="cancel.bin",
                      threads=2)
        self.assertTrue(wait_until(lambda: task.done > 256 * 1024, 20),
                        "还没开始下载就超时")
        part, meta = task.part_path, task.meta_path
        self.assertTrue(os.path.isfile(part), "下载中应有 .fspart")
        dl.pause(task.id)
        dl.remove(task.id, delete_file=True)
        dl.limiter.set_rate(0)
        self.assertFalse(os.path.exists(part), "取消后 .fspart 没删：%s" % part)
        self.assertFalse(os.path.exists(meta), "取消后 .fsmeta 没删：%s" % meta)
        self.assertIsNone(dl.get(task.id), "任务应已移除")

    def test_delete_done_removes_file(self):
        """删除已完成任务并选择删文件：最终文件必须消失。"""
        dl = self._make(threads=2)
        task = dl.add(self.url, folder=self.out, filename="delme.bin",
                      threads=2)
        self.assertTrue(wait_until(lambda: task.status == "done", 60),
                        task.error)
        path = task.final_path
        self.assertTrue(os.path.isfile(path))
        dl.remove(task.id, delete_file=True)
        self.assertFalse(os.path.exists(path), "最终文件没删：%s" % path)
        self.assertIsNone(dl.get(task.id))

    def test_pause_all_and_active_count(self):
        """批量暂停与活跃计数（顶栏按钮的依据）。"""
        dl = self._make(threads=2)
        dl.limiter.set_rate(256 * 1024)
        t1 = dl.add(self.url, folder=self.out, filename="p1.bin", threads=2)
        self.assertTrue(wait_until(lambda: t1.done > 64 * 1024, 20))
        self.assertGreaterEqual(dl.active_count(), 1)
        self.assertGreaterEqual(dl.pause_all(), 1)
        self.assertEqual(dl.active_count(), 0)
        dl.limiter.set_rate(0)
        dl.remove(t1.id, delete_file=True)

    def test_recover_orphan_cache(self):
        """启动扫描：.fsmeta + .fspart 能复原成暂停任务，可继续。"""
        import json
        rec = tempfile.mkdtemp(prefix="rec_", dir=self.tmp)
        dl = self._make(threads=2)
        dl.limiter.set_rate(256 * 1024)
        task = dl.add(self.url, folder=rec, filename="orphan.bin",
                      threads=2)
        self.assertTrue(wait_until(lambda: task.done > 512 * 1024, 20))
        dl.pause(task.id)
        dl.limiter.set_rate(0)
        part, meta = task.part_path, task.meta_path
        self.assertTrue(os.path.isfile(part) and os.path.isfile(meta))
        meta_blob = json.load(open(meta, encoding="utf-8"))
        # 模拟「任务记录丢了、缓存还在」
        dl.remove(task.id, delete_file=False)          # 保留缓存
        self.assertTrue(os.path.isfile(part), "remove 不该删缓存")

        dl2 = self._make(threads=2)
        recovered, orphans = dl2.recover_orphans(folders=[rec])
        self.assertEqual(recovered, 1, "缓存没能复原成任务")
        # 只关心本次测试目录；默认下载目录里可能有其它用例的残留
        rec_orphans = [p for p in orphans
                       if os.path.normcase(p).startswith(os.path.normcase(rec))]
        self.assertEqual(rec_orphans, [], "复原成功不该报无主：%s" % rec_orphans)
        t2 = dl2.tasks[0]
        self.assertEqual(t2.status, "paused")
        self.assertEqual(t2.url, meta_blob["url"])
        self.assertEqual(os.path.normcase(t2.part_path), os.path.normcase(part))
        self.assertGreater(t2.done, 0)
        # 恢复后能真的接着下完
        dl2.limiter.set_rate(0)
        dl2.resume(t2.id)
        self.assertTrue(wait_until(lambda: t2.status in ("done", "error"), 90),
                        "恢复任务没能继续")
        self.assertEqual(t2.status, "done", t2.error)
        with open(t2.final_path, "rb") as fp:
            self.assertEqual(fp.read(), self.data)

    def test_orphan_without_meta_reported(self):
        """只有 .fspart / 损坏的 .fsmeta → 无法复原，作为无主文件上报。"""
        rec = tempfile.mkdtemp(prefix="orph_", dir=self.tmp)
        dl = self._make(threads=2)
        lonely = os.path.join(rec, "lonely.bin.fspart")
        with open(lonely, "wb") as fp:
            fp.write(b"x" * 1024)
        broken = os.path.join(rec, "broken.bin.fsmeta")
        with open(broken, "w", encoding="utf-8") as fp:
            fp.write("{not json")
        recovered, orphans = dl.recover_orphans(folders=[rec])
        self.assertEqual(recovered, 0)
        names = set(os.path.basename(p) for p in orphans)
        self.assertIn("lonely.bin.fspart", names)
        self.assertIn("broken.bin.fsmeta", names)
        self.assertEqual(len(dl.tasks), 0)

    def test_delete_file_removes_all_artifacts(self):
        """删除任务并删文件：最终文件 + .fspart + .fsmeta 一个都不能剩。"""
        rec = tempfile.mkdtemp(prefix="del_", dir=self.tmp)
        dl = self._make(threads=2)
        dl.limiter.set_rate(256 * 1024)
        task = dl.add(self.url, folder=rec, filename="all.bin", threads=2)
        self.assertTrue(wait_until(lambda: task.done > 128 * 1024, 20))
        dl.pause(task.id)
        part, meta = task.part_path, task.meta_path
        # 伪造「已完成」状态，并放一个最终文件，模拟两种残留同时存在
        with open(task.final_path, "wb") as fp:
            fp.write(b"FINAL")
        task.status = "done"
        self.assertTrue(os.path.isfile(part) and os.path.isfile(meta))
        dl.limiter.set_rate(0)
        dl.remove(task.id, delete_file=True)
        self.assertFalse(os.path.exists(task.final_path), "最终文件没删")
        self.assertFalse(os.path.exists(part), ".fspart 没删")
        self.assertFalse(os.path.exists(meta), ".fsmeta 没删")


if __name__ == "__main__":
    unittest.main(verbosity=2)

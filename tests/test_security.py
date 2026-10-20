# -*- coding: utf-8 -*-
"""安全测试套件（与 test_core 的功能测试分开，按攻击面分类）。

覆盖面：
- A1 访问控制：路径穿越（明文 / URL 编码 / 双重编码 / 反斜杠）、越权读
- A2 认证：FTP 密码爆破面、匿名登录、写命令开关
- A3 注入 / 解析：畸形请求行、超长头、空字节、畸形 Range
- A4 文件上传：默认关闭、穿越、ADS、保留设备名、可执行加固、ACL、覆盖
- A5 存储型 XSS：上传的 HTML/SVG 强制下载 + nosniff
- A6 拒绝服务：超长头、垃圾请求后服务仍存活
"""

import ftplib
import io
import os
import socket
import tempfile
import unittest
import urllib.error
import urllib.request

from test_core import free_port

from fileshare.config import Config
from fileshare.server.ftpd import FtpServer
from fileshare.server.httpd import HttpFileServer
from fileshare.uploadguard import deny_execute, harden

SECRET = "TOP-SECRET-不可泄漏-7f3a9"


def _make_share(tmp, name="media"):
    share = os.path.join(tmp, name)
    os.makedirs(share, exist_ok=True)
    return share


def _http(tmp, upload=False, dirlist=True):
    share = _make_share(tmp)
    port = free_port()
    srv = HttpFileServer(lambda: [{"name": "media", "path": share}],
                         port=port, allow_upload=upload, dirlist=dirlist)
    ok, msg = srv.start()
    assert ok, msg
    return srv, share, "http://127.0.0.1:%d" % port


def _raw_send(port, payload, timeout=5.0):
    """发一段裸字节，返回 (服务是否还活着, 响应首行或 b'')。"""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(payload)
        try:
            data = s.recv(4096)
        except (socket.timeout, ConnectionError):
            data = b""
    finally:
        s.close()
    alive = _http_alive(port)
    return alive, data


def _http_alive(port):
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/favicon.ico" % port)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status in (200, 204)
    except urllib.error.HTTPError as exc:
        return exc.code in (200, 204)
    except Exception:                                    # noqa: BLE001
        return False


# ------------------------------------------------------------------ A1 穿越

class TestHttpTraversal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sec_http_")
        cls.srv, cls.share, cls.base = _http(cls.tmp)
        with open(os.path.join(cls.share, "ok.txt"), "wb") as fp:
            fp.write(b"shared")
        # 共享目录外的「机密文件」：任何穿越都不该读到它
        cls.secret = os.path.join(cls.tmp, "secret.txt")
        with open(cls.secret, "wb") as fp:
            fp.write(SECRET.encode("utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def _get(self, path):
        req = urllib.request.Request(self.base + path)
        return urllib.request.urlopen(req, timeout=10)

    TRAVERSALS = [
        "/media/../../secret.txt",
        "/media/..%2Fsecret.txt",
        "/media/%2e%2e%2fsecret.txt",
        "/media/%2e%2e/secret.txt",
        "/media/sub/../../secret.txt",
        "/media/..%5Csecret.txt",
        "/media/%5C..%5Csecret.txt",
        "/media/%252e%252e%252fsecret.txt",     # 双重编码
        "/media/....//secret.txt",
        "/media/..%c0%afsecret.txt",            # 过长 UTF-8 斜杠
        "/media/.%2e/secret.txt",
        "/..%2f..%2fsecret.txt",
    ]

    def test_traversal_variants_blocked(self):
        for path in self.TRAVERSALS:
            with self.assertRaises((urllib.error.HTTPError,
                                    urllib.error.URLError),
                                   msg="穿越未拦截：%s" % path):
                self._get(path)

    def test_secret_content_never_leaked(self):
        for path in self.TRAVERSALS:
            try:
                with self._get(path) as resp:
                    body = resp.read()
            except (urllib.error.HTTPError, urllib.error.URLError):
                continue
            self.assertNotIn(b"TOP-SECRET", body, "经 %s 泄漏" % path)

    def test_null_byte_path(self):
        with self.assertRaises((urllib.error.HTTPError, urllib.error.URLError,
                                ConnectionError)):
            self._get("/media/ok.txt%00.exe")

    def test_legit_download_still_works(self):
        with self._get("/media/ok.txt") as resp:
            self.assertEqual(resp.read(), b"shared")


# ------------------------------------------------------------------ A3 解析健壮性

class TestHttpRobustness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sec_rob_")
        cls.srv, cls.share, cls.base = _http(cls.tmp)
        cls.port = cls.srv.port
        with open(os.path.join(cls.share, "range.bin"), "wb") as fp:
            fp.write(b"0123456789" * 100)          # 1000 字节

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_unknown_method_405(self):
        req = urllib.request.Request(self.base + "/media/", data=b"",
                                     method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 405)

    def test_malformed_request_no_crash(self):
        alive, _ = _raw_send(self.port, b"GARBAGE / HTTP/1.1\r\n\r\n")
        self.assertTrue(alive, "垃圾请求把服务打挂了")

    def test_oversized_header_rejected(self):
        # 64 KB 头 > MAX_HEAD(32 KB)，必须被拒绝且服务存活
        junk = b"X-Junk: " + b"A" * 65536 + b"\r\n"
        payload = (b"GET /media/ HTTP/1.1\r\nHost: x\r\n" + junk +
                   b"Connection: close\r\n\r\n")
        alive, _ = _raw_send(self.port, payload)
        self.assertTrue(alive, "超长头把服务打挂了")

    def test_many_headers_no_crash(self):
        payload = b"GET /media/ HTTP/1.1\r\nHost: x\r\n" + \
            b"".join(b"X-H%d: v\r\n" % i for i in range(500)) + b"\r\n"
        alive, _ = _raw_send(self.port, payload)
        self.assertTrue(alive)

    def test_range_abuse(self):
        cases = [("bytes=99999999999999-", 416),          # 越界起点
                 ("bytes=abc", 200),                       # 畸形 → 忽略
                 ("bytes=5-2", 416),                       # 倒序
                 ("bytes=-", 200),                         # 空
                 ("bytes=0-99", 206)]                      # 正常分段
        for value, expect in cases:
            req = urllib.request.Request(
                self.base + "/media/range.bin", headers={"Range": value})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    code = resp.status
            except urllib.error.HTTPError as exc:
                code = exc.code
            self.assertEqual(code, expect, "Range=%r" % value)

    def test_nosniff_always_present(self):
        with urllib.request.urlopen(self.base + "/media/", timeout=10) as resp:
            self.assertEqual(resp.headers.get("X-Content-Type-Options"),
                             "nosniff")


# ------------------------------------------------------------------ A5 存储 XSS

class TestHttpStoredXss(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sec_xss_")
        cls.srv, cls.share, cls.base = _http(cls.tmp, upload=True)
        for name, body in (("page.html", b"<script>alert(1)</script>"),
                           ("pic.svg", b"<svg onload='alert(1)'/>"),
                           ("doc.xml", b"<?xml version='1.0'?><a/>"),
                           ("note.txt", b"just text")):
            with open(os.path.join(cls.share, name), "wb") as fp:
                fp.write(body)

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def _head(self, name):
        req = urllib.request.Request(self.base + "/media/" + name,
                                     method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.headers

    def test_dangerous_types_forced_download(self):
        for name in ("page.html", "pic.svg", "doc.xml"):
            h = self._head(name)
            self.assertTrue(h.get("Content-Disposition", "").startswith(
                "attachment"), "%s 应强制下载" % name)

    def test_safe_type_inline(self):
        h = self._head("note.txt")
        self.assertTrue(h.get("Content-Disposition", "").startswith("inline"))

    def test_nosniff_on_files(self):
        h = self._head("page.html")
        self.assertEqual(h.get("X-Content-Type-Options"), "nosniff")


# ------------------------------------------------------------------ A2/A4 FTP

class TestFtpSecurity(unittest.TestCase):
    """写开关关闭的实例：所有写命令必须 550。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sec_ftp_")
        cls.share = _make_share(cls.tmp)
        with open(os.path.join(cls.share, "f.bin"), "wb") as fp:
            fp.write(b"x" * 1024)
        cls.port = free_port()
        cls.srv = FtpServer(lambda: [{"name": "media", "path": cls.share}],
                            port=cls.port, user="share", password="p@ss",
                            allow_upload=False, anonymous=False)
        ok, msg = cls.srv.start()
        assert ok, msg

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def _login(self, user="share", password="p@ss"):
        ftp = ftplib.FTP()
        ftp.connect("127.0.0.1", self.port, timeout=10)
        ftp.login(user, password)
        return ftp

    def test_wrong_password_rejected(self):
        with self.assertRaises(ftplib.error_perm):
            self._login(password="wrong")

    def test_wrong_user_rejected(self):
        with self.assertRaises(ftplib.error_perm):
            self._login(user="admin")

    def test_anonymous_rejected(self):
        with self.assertRaises(ftplib.error_perm):
            self._login(user="anonymous", password="anon@x")

    def test_write_commands_all_denied(self):
        ftp = self._login()
        try:
            ftp.cwd("media")
            with self.assertRaises(ftplib.error_perm, msg="STOR 未拦"):
                ftp.storbinary("STOR new.bin", io.BytesIO(b"data"))
            with self.assertRaises(ftplib.error_perm, msg="DELE 未拦"):
                ftp.delete("f.bin")
            with self.assertRaises(ftplib.error_perm, msg="MKD 未拦"):
                ftp.mkd("d")
            with self.assertRaises(ftplib.error_perm, msg="RNFR 未拦"):
                ftp.rename("f.bin", "g.bin")
        finally:
            ftp.quit()
        # 一个字节都不能动
        self.assertTrue(os.path.exists(os.path.join(self.share, "f.bin")))
        self.assertEqual(os.listdir(self.share), ["f.bin"])

    def test_retr_traversal_denied(self):
        ftp = self._login()
        try:
            for target in ("../../secret.txt", "/media/../../secret.txt",
                           "a:ads", "CON"):
                buf = io.BytesIO()
                with self.assertRaises(ftplib.error_perm,
                                       msg="RETR %s 未拦" % target):
                    ftp.retrbinary("RETR " + target, buf.write)
        finally:
            ftp.quit()


class TestFtpUploadSecurity(unittest.TestCase):
    """写开关打开的实例：上传路径与改名防护。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sec_ftpu_")
        cls.share = _make_share(cls.tmp)
        cls.port = free_port()
        cls.srv = FtpServer(lambda: [{"name": "media", "path": cls.share}],
                            port=cls.port, user="share", password="p",
                            allow_upload=True, anonymous=False)
        ok, msg = cls.srv.start()
        assert ok, msg

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_stor_ads_reserved_denied(self):
        ftp = ftplib.FTP()
        ftp.connect("127.0.0.1", self.port, timeout=10)
        ftp.login("share", "p")
        try:
            ftp.cwd("media")
            for name in ("evil:ads.txt", "CON", "x/../../y"):
                with self.assertRaises(ftplib.error_perm,
                                       msg="STOR %s 未拦" % name):
                    ftp.storbinary("STOR " + name, io.BytesIO(b"data"))
            # 尾随点：Windows 语义等价于去掉点的名字 —— 落盘成
            # a..exe（.exe 被加固成 .noexec），绝不能以原名存在
            ftp.storbinary("STOR a..exe.", io.BytesIO(b"data"))
        finally:
            ftp.quit()
        names = set(os.listdir(self.share))
        self.assertEqual([n for n in names if n not in ("media",)],
                         ["a..exe.noexec"],
                         "尾随点绕过或可执行文件未加固：%s" % names)


# ------------------------------------------------------------------ A4 ACL

class TestExecHardening(unittest.TestCase):
    def test_harden_renames_and_denies(self):
        tmp = tempfile.mkdtemp(prefix="sec_acl_")
        p = os.path.join(tmp, "tool.exe")
        with open(p, "wb") as fp:
            fp.write(b"MZ")
        final = harden(p)
        self.assertEqual(final, p + ".noexec")
        self.assertTrue(os.path.exists(final))
        self.assertFalse(os.path.exists(p))
        if os.name == "nt":
            self.assertTrue(deny_execute(final), "icacls 拒绝执行 ACE 失败")

    def test_non_exec_untouched(self):
        tmp = tempfile.mkdtemp(prefix="sec_acl_")
        p = os.path.join(tmp, "note.txt")
        with open(p, "wb") as fp:
            fp.write(b"hi")
        self.assertEqual(harden(p), p)


# ------------------------------------------------------------------ 配置安全

class TestConfigDefaults(unittest.TestCase):
    def test_fresh_config_upload_off(self):
        tmp = tempfile.mkdtemp(prefix="sec_cfg_")
        cfg = Config(path=os.path.join(tmp, "c.json"))
        self.assertFalse(cfg.get("http", "upload", default=True))
        self.assertFalse(cfg.get("ftp", "upload", default=True))

    def test_legacy_config_migrates_to_readonly(self):
        """老配置里写着上传开启 → 升级后强制回只读。"""
        import json
        tmp = tempfile.mkdtemp(prefix="sec_cfg_")
        path = os.path.join(tmp, "c.json")
        with open(path, "w", encoding="utf-8") as fp:
            json.dump({"schema": 1,
                       "http": {"upload": True},
                       "ftp": {"upload": True}}, fp)
        cfg = Config(path=path)
        self.assertFalse(cfg.data["http"]["upload"])
        self.assertFalse(cfg.data["ftp"]["upload"])


class TestBindSelection(unittest.TestCase):
    """监听网卡选择：默认全部，指定 IP 时真的只绑那个地址。"""

    def test_default_bind_all(self):
        tmp = tempfile.mkdtemp(prefix="sec_bind_")
        cfg = Config(path=os.path.join(tmp, "c.json"))
        self.assertEqual(cfg.get("listen", "ip"), "0.0.0.0")

    def test_listen_choices_has_all_entry(self):
        from fileshare.netutil import listen_choices
        choices = listen_choices()
        self.assertTrue(choices)
        self.assertEqual(choices[0][0], "0.0.0.0")
        for ip, label in choices[1:]:
            self.assertNotIn("\n", label)

    def test_bind_specific_ip(self):
        """绑定 127.0.0.1：本机回环能访问，服务正常起。"""
        import urllib.request
        tmp = tempfile.mkdtemp(prefix="sec_bind_")
        share = _make_share(tmp)
        with open(os.path.join(share, "x.txt"), "wb") as fp:
            fp.write(b"bound")
        port = free_port()
        srv = HttpFileServer(lambda: [{"name": "media", "path": share}],
                             port=port, host="127.0.0.1")
        ok, msg = srv.start()
        assert ok, msg
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/media/x.txt" % port, timeout=10) as r:
                self.assertEqual(r.read(), b"bound")
        finally:
            srv.stop()

# -*- coding: utf-8 -*-
"""服务总控：把 HTTP / FTP / SMB 三个协议、全局限速、统计串起来。

界面只跟这一层打交道：``start("http")`` / ``stop("http")`` / ``status()``。
每个协议都用独立的类，启停都在后台线程里做（界面不卡）。
"""

import os
import threading
import time

from ..config import Config
from ..netutil import is_admin, port_free, primary_ip
from ..traffic import TrafficMonitor
from ..util import EventLog, RateLimiter, Stats
from . import smb
from .ftpd import FtpServer
from .httpd import HttpFileServer

KB = 1024
PROTOCOLS = ("http", "ftp", "smb")


class ShareManager(object):
    def __init__(self, cfg=None, log=None):
        self.cfg = cfg or Config()
        self.log = log or EventLog()
        self.stats = Stats()
        self.traffic = TrafficMonitor()
        self.up_limiter = RateLimiter(0)      # 服务端对外发送（别人下载）
        self.dl_limiter = RateLimiter(0)      # 内置下载器
        self.smb = smb.SmbManager(self.log, cfg=cfg)
        self.http = None
        self.ftp = None
        self._lock = threading.RLock()
        self._busy = set()
        self.started_at = time.time()
        self.last_error = {}
        self.apply_limits()
        # 局域网互发现：开了共享就监听固定探测端口，让别的主机找到这里
        from ..discovery import DiscoveryServer
        self._discovery = DiscoveryServer(
            self._discovery_info,
            port=int(cfg.get("listen", "discovery_port") or 47610),
            host=self.bind_ip())
        self._update_discovery()

    # ---- 互发现 ----
    def _discovery_info(self):
        """广播给局域网其它客户端的本机共享状态。"""
        import socket
        names = [s.get("name") for s in self.shares()]
        hp = int(self.cfg.get("http", "port") or 8080)
        fp = int(self.cfg.get("ftp", "port") or 2121)
        return {
            "app": "FileShare",
            "ver": 1,
            "host": socket.gethostname(),
            "http": {"on": self.is_running("http"), "port": hp,
                     "shares": names if self.is_running("http") else []},
            "ftp": {"on": self.is_running("ftp"), "port": fp},
            "smb": {"on": bool(self.smb.managed),
                    "names": sorted(self.smb.managed)},
        }

    def _update_discovery(self):
        """有任一共享在跑就开探测端口，全关了就关掉；换监听网卡时重绑。"""
        any_on = (self.is_running("http") or self.is_running("ftp")
                  or bool(self.smb.managed))
        want_host = self.bind_ip()
        if self._discovery.running and self._discovery.host != want_host:
            self._discovery.stop()               # 换网卡 → 重新绑定
        if any_on and not self._discovery.running:
            self._discovery.host = want_host
            ok, msg = self._discovery.start()
            self.log.add("info", "局域网互发现：" + msg, "网络")
        elif not any_on and self._discovery.running:
            self._discovery.stop()
            self.log.add("info", "共享已全部关闭，互发现探测停止", "网络")

    # ------------------------------------------------------------ 共享目录
    def shares(self):
        """当前生效的共享（启用 + 目录存在）。名字只用一层，不含斜杠。"""
        out = []
        used = set()
        for s in self.cfg.get("shares") or []:
            if not s.get("enabled", True):
                continue
            path = s.get("path") or ""
            if not os.path.isdir(path):
                continue
            name = (s.get("name") or os.path.basename(path.rstrip("\\/")) or "share")
            name = name.replace("/", "_").replace("\\", "_").strip()
            base, i = name, 2
            while name.lower() in used:
                name = "%s%d" % (base, i)
                i += 1
            used.add(name.lower())
            out.append({"name": name, "path": os.path.abspath(path)})
        return out

    def all_shares(self):
        """配置里的全部条目（含未启用的），给界面用。"""
        return list(self.cfg.get("shares") or [])

    # ------------------------------------------------------------ 限速
    def apply_limits(self):
        up = int(self.cfg.get("limit", "up_kbps") or 0) * KB
        down = int(self.cfg.get("limit", "down_kbps") or 0) * KB
        self.up_limiter.set_rate(up)
        self.dl_limiter.set_rate(down)
        return up, down

    # ------------------------------------------------------------ HTTP
    def bind_ip(self):
        """当前配置的监听地址（0.0.0.0 = 全部网卡）。"""
        ip = self.cfg.get("listen", "ip") or "0.0.0.0"
        return ip.strip() if ip.strip() else "0.0.0.0"

    def start_http(self):
        with self._lock:
            if self.http is not None and self.http.running:
                return True, "HTTP 已在运行"
            port = int(self.cfg.get("http", "port") or 8080)
            host = self.bind_ip()
            if not port_free(port, host=host):
                msg = "端口 %d 在 %s 上被占用，换一个端口或网卡试试" % (
                    port, "全部网卡" if host == "0.0.0.0" else host)
                self.last_error["http"] = msg
                self.log.add("err", "HTTP 启动失败：" + msg, "HTTP")
                return False, msg
            srv = HttpFileServer(
                self.shares, port=port, limiter=self.up_limiter,
                stats=self.stats, log=self.log,
                allow_upload=bool(self.cfg.get("http", "upload", default=False)),
                dirlist=bool(self.cfg.get("http", "dirlist", default=True)),
                traffic=self.traffic, host=host)
            ok, msg = srv.start()
            if ok:
                self.http = srv
                self.last_error.pop("http", None)
                self.log.add("ok", "HTTP 服务已启动 · %s:%d" % (
                    "全部网卡" if host == "0.0.0.0" else host, port), "HTTP")
            else:
                self.last_error["http"] = msg
                self.log.add("err", "HTTP 启动失败：" + msg, "HTTP")
            return ok, msg

    def stop_http(self):
        with self._lock:
            if self.http is None:
                return True, "未运行"
            self.http.stop()
            self.http = None
            self.log.add("info", "HTTP 服务已停止", "HTTP")
            return True, "已停止"

    # ------------------------------------------------------------ FTP
    def start_ftp(self):
        with self._lock:
            if self.ftp is not None and self.ftp.running:
                return True, "FTP 已在运行"
            port = int(self.cfg.get("ftp", "port") or 2121)
            host = self.bind_ip()
            if not port_free(port, host=host):
                msg = "端口 %d 在 %s 上被占用，换一个端口或网卡试试" % (
                    port, "全部网卡" if host == "0.0.0.0" else host)
                self.last_error["ftp"] = msg
                self.log.add("err", "FTP 启动失败：" + msg, "FTP")
                return False, msg
            anon = bool(self.cfg.get("ftp", "anonymous"))
            srv = FtpServer(
                self.shares, port=port,
                user=self.cfg.get("ftp", "user") or "share",
                password=self.cfg.get("ftp", "password") or "",
                anonymous=anon,
                # 匿名会话固定只读：谁都能进的情况下再允许上传，
                # 等于把共享目录开放给整个局域网任意写入
                allow_upload=(bool(self.cfg.get("ftp", "upload", default=False))
                              and not anon),
                limiter=self.up_limiter, stats=self.stats, log=self.log,
                traffic=self.traffic, host=host)
            ok, msg = srv.start()
            if ok:
                self.ftp = srv
                self.last_error.pop("ftp", None)
                self.log.add("ok", "FTP 服务已启动 · %s:%d" % (
                    "全部网卡" if host == "0.0.0.0" else host, port), "FTP")
            else:
                self.last_error["ftp"] = msg
                self.log.add("err", "FTP 启动失败：" + msg, "FTP")
            return ok, msg

    def stop_ftp(self):
        with self._lock:
            if self.ftp is None:
                return True, "未运行"
            self.ftp.stop()
            self.ftp = None
            self.log.add("info", "FTP 服务已停止", "FTP")
            return True, "已停止"

    # ------------------------------------------------------------ SMB
    def _smb_cred(self):
        """账号模式的用户名/密码（界面可改）。"""
        user = (self.cfg.get("smb", "user") or "FileShare").strip() or "FileShare"
        pwd = self.cfg.get("smb", "password") or ""
        if not pwd:
            # 空密码在「账号模式」下必然连不上（系统禁止空密码网络登录），
            # 用与建账户时一致的默认值，避免「账户密码」和界面显示对不上
            pwd = smb.SHARE_PW
        return user, pwd

    def sync_smb(self):
        ok, msg = self.smb.available()
        if not ok:
            self.last_error["smb"] = msg
            return False, msg

        readonly = bool(self.cfg.get("smb", "readonly", default=True))
        user, _pwd = self._smb_cred()

        # 迁移：老版本有「免密」模式，会把目录授予 Everyone。现在只认指定
        # 账户，所以开共享时先把那些 Everyone 显式 ACE 收回来（继承来的不动）。
        try:
            dropped, dfail = self.smb.drop_legacy_everyone(
                [s["path"] for s in self.shares()])
            if dropped:
                self.log.add("info", "已收回 %d 处旧的 Everyone 目录授权"
                             % len(dropped), "SMB")
            for f in dfail:
                self.log.add("warn", "收回 Everyone 授权失败：%s" % f, "SMB")
        except Exception as exc:                            # noqa: BLE001
            self.log.add("warn", "收回 Everyone 授权异常：%s" % exc, "SMB")

        done, errors = self.smb.sync(self.shares(), readonly=readonly,
                                     account=user)

        # 建好共享只是第一步。Win10/11 默认还堵着三道门：没有可用的登录
        # 账户、防火墙只对「专用网络」放行 445、目录 NTFS 权限不含访问者。
        # 不把这三件事办掉，共享建得再对，局域网其它机器也是「无法访问」。
        # 所以启用 SMB 时顺手做一遍（幂等，重复调用没有副作用）。
        try:
            fixed, failed = self.smb_repair()
            for f in failed:
                self.log.add("warn", "SMB 环境修复未完成：%s" % f, "SMB")
        except Exception as exc:                            # noqa: BLE001
            self.log.add("warn", "SMB 环境修复异常：%s" % exc, "SMB")

        if errors:
            self.last_error["smb"] = "；".join(errors[:2])
            return False, self.last_error["smb"]
        self.last_error.pop("smb", None)
        if done:
            self.log.add("ok", "SMB 共享已同步（%d 个）" % len(done), "SMB")
            return True, "已同步 %d 个共享" % len(done)
        # 一个都没动 = 本来就是对的状态。这时候绝对不能去「删了重建」：
        # 客户端只要还开着共享里的文件，重建就会触发「是否强制关闭文件」
        # 的确认，无头进程答不上来就直接失败（用户看到的那句黄字）。
        return True, "共享已是最新，无需改动"

    def smb_diagnose(self):
        """SMB 能否被局域网其它机器访问的体检结果。"""
        try:
            user, pwd = self._smb_cred()
            return self.smb.diagnose(self.shares(), user=user, password=pwd)
        except Exception as exc:                            # noqa: BLE001
            self.log.add("err", "SMB 体检失败：%s" % exc, "SMB")
            return []

    def smb_repair(self):
        """一键修复：建账户并加固 + 放行 445 + 补目录权限。"""
        readonly = bool(self.cfg.get("smb", "readonly", default=True))
        user, pwd = self._smb_cred()
        return self.smb.repair(self.shares(), readonly=readonly,
                               user=user, password=pwd)

    def stop_smb(self):
        """取消共享：删共享 + 把加过的目录权限还原。"""
        # 我们授过的身份：指定账户 + 老版本可能授过的 Everyone（迁移遗留）
        principals = [self._smb_cred()[0]]
        removed, failed = self.smb.remove_all(
            restore=True, principals=principals,
            paths=[s["path"] for s in self.shares()])
        if failed:
            self.log.add("warn", "SMB 权限还原部分失败：%s" % "；".join(failed[:2]),
                         "SMB")
        self.log.add("info", "SMB 共享已移除%s" % (
            "，目录权限已还原（%d 项）" % len(removed) if removed else ""), "SMB")
        return True, "已移除"

    # ------------------------------------------------------------ 统一入口
    def start(self, proto):
        fn = {"http": self.start_http, "ftp": self.start_ftp,
              "smb": self.sync_smb}.get(proto)
        if fn is None:
            return False, "未知协议"
        ok, msg = fn()
        if ok:
            self.cfg.set(True, proto, "enabled")
            self.cfg.save()
        self._update_discovery()
        return ok, msg

    def stop(self, proto):
        fn = {"http": self.stop_http, "ftp": self.stop_ftp,
              "smb": self.stop_smb}.get(proto)
        if fn is None:
            return False, "未知协议"
        ok, msg = fn()
        if proto != "smb" or ok:
            self.cfg.set(False, proto, "enabled")
            self.cfg.save()
        self._update_discovery()
        return ok, msg

    def toggle(self, proto, on=None):
        if on is None:
            on = not self.is_running(proto)
        return self.start(proto) if on else self.stop(proto)

    def is_running(self, proto):
        if proto == "http":
            return bool(self.http and self.http.running)
        if proto == "ftp":
            return bool(self.ftp and self.ftp.running)
        if proto == "smb":
            return bool(self.smb.managed)
        return False

    def stop_all(self):
        for proto in ("http", "ftp"):
            try:
                self.stop(proto)
            except Exception:                              # noqa: BLE001
                pass
        try:
            # 退出也要还原权限：共享都没了还留着读写授权等于开后门
            principals = [self._smb_cred()[0]]
            self.smb.remove_all(
                restore=True, principals=principals,
                paths=[s["path"] for s in self.shares()])
        except Exception:                                  # noqa: BLE001
            pass
        try:
            self._discovery.stop()
        except Exception:                                  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 状态
    def status(self):
        bind = self.bind_ip()
        ip = bind if bind != "0.0.0.0" else primary_ip()
        st = {}
        http_on = self.is_running("http")
        ftp_on = self.is_running("ftp")
        smb_on = self.is_running("smb")
        hp = int(self.cfg.get("http", "port") or 8080)
        fp = int(self.cfg.get("ftp", "port") or 2121)
        st["http"] = {
            "on": http_on, "port": hp,
            "url": "http://%s:%d/" % (ip, hp),
            "conns": (self.http.active_conns if self.http else 0),
            "reqs": (self.http.total_requests if self.http else 0),
            "error": self.last_error.get("http", ""),
        }
        st["ftp"] = {
            "on": ftp_on, "port": fp,
            "url": "ftp://%s:%d/" % (ip, fp),
            "conns": (self.ftp.active_conns if self.ftp else 0),
            "error": self.last_error.get("ftp", ""),
        }
        st["smb"] = {
            "on": smb_on, "names": sorted(self.smb.managed),
            "url": "\\\\%s\\%s" % (ip, sorted(self.smb.managed)[0])
                   if smb_on else "\\\\%s" % ip,
            "error": self.last_error.get("smb", ""),
            "account": self.smb.account_state()[0],
            "hardened": self.smb.check_hardening(self._smb_cred()[0])[0],
            "user": self._smb_cred()[0],
            "password": self._smb_cred()[1],
            # 该账户的权限范围：界面要直说「只限这几个目录」，
            # 而不是让人自己去翻 ACL
            "dirs": [os.path.basename((s.get("path") or "").rstrip("\\/"))
                     or (s.get("path") or "")
                     for s in self.shares() if (s.get("path") or "")],
            "issues": [],
        }
        st["ip"] = ip
        st["bind_ip"] = bind
        st["admin"] = is_admin()
        st["shares"] = len(self.shares())
        st["uptime"] = time.time() - self.started_at
        return st

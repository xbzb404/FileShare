# -*- coding: utf-8 -*-
"""自研 FTP 服务器（纯标准库，一线程一连接）。

为什么自己写而不是塞一个 pyftpdlib：
  · 项目要打包成**单个 exe**，少一个依赖就少一份体积和启动开销；
  · 全局限速要和 HTTP 共用同一个令牌桶 —— 第三方库的限速是按连接算的，
    多连接时会突破总上限；
  · 只暴露「共享目录」这一件事，不需要虚拟用户、权限组那套。

协议实现范围（足够 Windows 资源管理器 / FileZilla / WinSCP / curl 正常使用）：
  USER PASS SYST FEAT OPTS NOOP TYPE PWD CWD CDUP SIZE MDTM REST PASV EPSV
  PORT LIST NLST MLSD RETR STOR APPE DELE MKD RMD RNFR RNTO ABOR QUIT
``REST`` + ``RETR``/``STOR`` 即断点续传（下行续传、上行续传都支持）。
"""

import os
import socket
import threading
import time

from ..netutil import safe_join
from ..uploadguard import harden, safe_target
from ..util import human_bytes

O_BINARY = getattr(os, "O_BINARY", 0)

BLOCK = 256 * 1024              # 数据传输块大小
CTRL_TIMEOUT = 600.0            # 控制连接空闲超时
DATA_WAIT = 25.0                # 等客户端连上数据端口的最长时间


class FtpServer(object):
    def __init__(self, shares_provider, port=2121, user="share", password="fileshare",
                 anonymous=False, allow_upload=True, limiter=None, stats=None,
                 log=None, host="0.0.0.0", traffic=None):
        self.shares_provider = shares_provider
        self.traffic = traffic
        self.port = int(port)
        self.host = host
        self.user = user or "share"
        self.password = password or ""
        self.anonymous = bool(anonymous)
        self.allow_upload = bool(allow_upload)
        self.limiter = limiter
        self.stats = stats
        self.log = log

        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._ok = False
        self._msg = ""
        self._sessions = set()
        self._sessions_lock = threading.Lock()
        self.active_conns = 0

    # ------------------------------------------------------------ 生命周期
    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive() and self._ok)

    @property
    def message(self):
        return self._msg

    def start(self):
        if self.running:
            return True, "FTP 服务已在运行"
        self._stop.clear()
        self._ready.clear()
        self._ok = False
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.host, self.port))
            s.listen(64)
            s.settimeout(1.0)
        except OSError as exc:
            self._msg = "端口 %d 无法监听：%s" % (self.port, exc)
            self._ok = False
            return False, self._msg
        self._sock = s
        self._ok = True
        self._msg = "OK"
        self._thread = threading.Thread(target=self._accept_loop, name="ftp-accept",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(2.0)
        return True, "FTP 服务已启动，端口 %d" % self.port

    def stop(self):
        self._stop.set()
        try:
            if self._sock is not None:
                self._sock.close()
        except OSError:
            pass
        self._sock = None
        with self._sessions_lock:
            for sess in list(self._sessions):
                try:
                    sess.close()
                except Exception:                          # noqa: BLE001
                    pass
        t = self._thread
        if t is not None and t.is_alive():
            t.join(3.0)
        self._thread = None
        self._ok = False

    def _accept_loop(self):
        self._ready.set()
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.settimeout(CTRL_TIMEOUT)
            except OSError:
                pass
            sess = FtpSession(self, conn, addr)
            with self._sessions_lock:
                self._sessions.add(sess)
            sess.start()

    def _drop(self, sess):
        with self._sessions_lock:
            self._sessions.discard(sess)

    def _log(self, kind, text, peer=""):
        if self.log is not None:
            self.log.add(kind, text, peer or "FTP")

    # ---- 供会话使用 ----
    def shares(self):
        try:
            return [s for s in (self.shares_provider() or []) if s.get("path")]
        except Exception:                                  # noqa: BLE001
            return []


class FtpSession(threading.Thread):
    def __init__(self, server, conn, addr):
        threading.Thread.__init__(self, daemon=True, name="ftp-sess")
        self.srv = server
        self.conn = conn
        self.addr = addr
        self.peer = "%s:%s" % (addr[0], addr[1]) if addr else ""
        self.rf = None
        self.cwd = "/"
        self.authenticated = False
        self.user = ""
        self.rest = 0
        self.binary = True
        self.pasv = None
        self.port_addr = None
        self._closed = False
        self._rnfr = None

    # ------------------------------------------------------------ 主循环
    def run(self):
        self.srv.active_conns += 1
        try:
            self.rf = self.conn.makefile("rb")
            self._send("220 文件共享中心 FTP 服务 就绪")
            while not self.srv._stop.is_set() and not self._closed:
                line = self.rf.readline(4096)
                if not line:
                    break
                try:
                    text = line.decode("utf-8", "replace").strip("\r\n")
                except Exception:                          # noqa: BLE001
                    break
                if not text:
                    continue
                cmd, _, arg = text.partition(" ")
                cmd = cmd.upper()
                arg = arg.strip()
                handler = getattr(self, "_c_" + cmd.lower(), None)
                if handler is None:
                    self._send("502 命令未实现：%s" % cmd)
                    continue
                try:
                    handler(arg)
                except (ConnectionError, OSError):
                    break
                except Exception as exc:                   # noqa: BLE001
                    self._send("451 命令执行失败：%s" % exc)
        except (ConnectionError, OSError, socket.timeout):
            pass
        finally:
            self.close()
            self.srv.active_conns -= 1
            self.srv._drop(self)

    def close(self):
        self._closed = True
        for attr in ("pasv", "conn", "rf"):
            obj = getattr(self, attr, None)
            try:
                if obj is not None:
                    obj.close()
            except Exception:                              # noqa: BLE001
                pass
        self.pasv = None
        self.conn = None

    # ------------------------------------------------------------ 工具
    def _send(self, text):
        if not self.conn:
            return
        try:
            self.conn.sendall((text + "\r\n").encode("utf-8"))
        except OSError:
            self._closed = True

    def _need_auth(self):
        if self.authenticated:
            return True
        self._send("530 请先登录")
        return False

    def _need_write(self):
        if not self.srv.allow_upload:
            self._send("550 服务器已禁止写入")
            return False
        return True

    def _vpath(self, arg):
        """FTP 参数 → 规范化虚拟路径（绝对路径）。"""
        if not arg:
            return self.cwd
        if arg.startswith("/"):
            raw = arg
        else:
            raw = self.cwd.rstrip("/") + "/" + arg
        parts = []
        for p in raw.replace("\\", "/").split("/"):
            if p in ("", "."):
                continue
            if p == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(p)
        return "/" + "/".join(parts)

    def _real(self, vpath):
        """虚拟路径 → (真实路径, share) ；越界/不存在返回 (None, None)。"""
        parts = [p for p in vpath.split("/") if p]
        if not parts:
            return None, None
        share = None
        for s in self.srv.shares():
            if s.get("name") == parts[0]:
                share = s
                break
        if share is None:
            return None, None
        real = safe_join(share["path"], "/".join(parts[1:]))
        return real, share

    def _open_data(self):
        """建立数据连接（PASV / EPSV 被动，或 PORT 主动）。"""
        sock = None
        if self.pasv is not None:
            try:
                self.pasv.settimeout(DATA_WAIT)
                sock, _ = self.pasv.accept()
                sock.settimeout(None)
            except OSError:
                sock = None
            finally:
                try:
                    self.pasv.close()
                except OSError:
                    pass
                self.pasv = None
        elif self.port_addr is not None:
            try:
                sock = socket.create_connection(self.port_addr, timeout=DATA_WAIT)
                sock.settimeout(None)
            except OSError:
                sock = None
            finally:
                self.port_addr = None
        if sock is None:
            self._send("425 无法建立数据连接")
            return None
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        return sock

    def _pump_out(self, sock, fp, offset=0, tr=None):
        """读文件 → 发到数据连接，带全局限速。返回发出的字节数。"""
        sent = 0
        limiter = self.srv.limiter
        stats = self.srv.stats
        try:
            fp.seek(offset)
            base = limiter.chunk_size(BLOCK) if limiter else BLOCK
            while True:
                buf = fp.read(base)
                if not buf:
                    break
                sock.sendall(buf)
                sent += len(buf)
                if stats is not None:
                    stats.add_sent(len(buf))
                if tr is not None:
                    tr.bytes += len(buf)
                if limiter is not None:
                    delay = limiter.consume(len(buf))
                    if delay > 0:
                        time.sleep(delay)
        except (ConnectionError, OSError):
            pass
        return sent

    def _pump_in(self, sock, fp, offset=0, tr=None):
        """从数据连接收数据 → 写文件，带全局限速。返回收到的字节数。"""
        got = 0
        limiter = self.srv.limiter
        stats = self.srv.stats
        try:
            fp.seek(offset)
            base = limiter.chunk_size(BLOCK) if limiter else BLOCK
            while True:
                buf = sock.recv(base)
                if not buf:
                    break
                fp.write(buf)
                got += len(buf)
                if stats is not None:
                    stats.add_recv(len(buf))
                if tr is not None:
                    tr.bytes += len(buf)
                if limiter is not None:
                    delay = limiter.consume(len(buf))
                    if delay > 0:
                        time.sleep(delay)
        except (ConnectionError, OSError):
            pass
        return got

    # ------------------------------------------------------------ 基础命令
    def _c_user(self, arg):
        self.user = arg
        if self.srv.anonymous and arg.lower() == "anonymous":
            self.authenticated = True
            self._send("230 匿名登录成功")
        elif arg == self.srv.user:
            self._send("331 需要密码")
        elif self.srv.anonymous:
            self.authenticated = True
            self._send("230 登录成功（匿名模式）")
        else:
            self._send("331 需要密码")

    def _c_pass(self, arg):
        if self.authenticated:
            self._send("230 已登录")
            return
        if self.user == self.srv.user and arg == self.srv.password:
            self.authenticated = True
            self._send("230 登录成功")
            self.srv._log("ok", "FTP 登录：%s" % self.user, self.peer)
        elif self.srv.anonymous:
            self.authenticated = True
            self._send("230 登录成功（匿名模式）")
        else:
            self._send("530 用户名或密码错误")

    def _c_quit(self, arg):
        self._send("221 再见")
        self._closed = True

    def _c_noop(self, arg):
        self._send("200 OK")

    def _c_syst(self, arg):
        self._send("215 UNIX Type: L8")

    def _c_feat(self, arg):
        feats = ["UTF8", "SIZE", "MDTM", "REST STREAM",
                 "MLST type*;size*;modify*;", "TVFS", "EPSV", "PASV"]
        self._send("211-Features:")
        for f in feats:
            self._send(" " + f)
        self._send("211 End")

    def _c_opts(self, arg):
        self._send("200 已接受 UTF8 模式")

    def _c_type(self, arg):
        self.binary = not arg.upper().startswith("A")
        self._send("200 类型已设置为 %s" % ("I" if self.binary else "A"))

    def _c_mode(self, arg):
        self._send("200 只支持流模式")

    def _c_stru(self, arg):
        self._send("200 只支持文件结构")

    def _c_pwd(self, arg):
        self._send('257 "%s" 是当前目录' % self.cwd)

    _c_xpwd = _c_pwd

    def _c_cwd(self, arg):
        if not self._need_auth():
            return
        vpath = self._vpath(arg)
        real, share = self._real(vpath)
        if real is not None and os.path.isdir(real):
            self.cwd = vpath
            self._send('250 目录已切换到 "%s"' % self.cwd)
        elif vpath in ("/", ""):
            self.cwd = "/"
            self._send('250 目录已切换到 "/"')
        else:
            self._send("550 目录不存在")

    _c_xcwd = _c_cwd

    def _c_cdup(self, arg):
        self._c_cwd("..")

    _c_xcup = _c_cdup

    def _c_size(self, arg):
        if not self._need_auth():
            return
        real, _s = self._real(self._vpath(arg))
        if real and os.path.isfile(real):
            self._send("213 %d" % os.path.getsize(real))
        else:
            self._send("550 文件不存在")

    def _c_mdtm(self, arg):
        if not self._need_auth():
            return
        real, _s = self._real(self._vpath(arg))
        if real and os.path.exists(real):
            ts = time.gmtime(os.path.getmtime(real))
            self._send("213 " + time.strftime("%Y%m%d%H%M%S", ts))
        else:
            self._send("550 文件不存在")

    def _c_rest(self, arg):
        try:
            self.rest = max(0, int(arg))
            self._send("350 续传位置已设为 %d" % self.rest)
        except ValueError:
            self._send("501 参数错误")

    def _c_abor(self, arg):
        self.rest = 0
        self._send("226 已中止")

    # ------------------------------------------------------------ 数据通道
    def _c_pasv(self, arg):
        if not self._need_auth():
            return
        self._close_pasv()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", 0))
            s.listen(4)
            s.settimeout(DATA_WAIT)
        except OSError as exc:
            self._send("425 无法进入被动模式：%s" % exc)
            return
        self.pasv = s
        port = s.getsockname()[1]
        try:
            # 用控制连接的本地地址：客户端从哪条路由进来就回哪条路由的地址。
            # 本机测试时它会是 127.0.0.1，正好也是客户端能连上的地址；
            # 早先这里会强行换成 hostname 解析出的局域网 IP，反而在只有
            # 回环连接的场景下把被动模式弄坏。
            ip = self.conn.getsockname()[0]
        except OSError:
            ip = "127.0.0.1"
        h = ip.split(".")
        self._send("227 进入被动模式 (%s,%s,%s,%s,%d,%d)" % (
            h[0], h[1], h[2], h[3], port >> 8, port & 0xFF))

    def _c_epsv(self, arg):
        if not self._need_auth():
            return
        self._close_pasv()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", 0))
            s.listen(4)
            s.settimeout(DATA_WAIT)
        except OSError as exc:
            self._send("425 无法进入被动模式：%s" % exc)
            return
        self.pasv = s
        self._send("229 进入扩展被动模式 (|||%d|)" % s.getsockname()[1])

    def _c_port(self, arg):
        if not self._need_auth():
            return
        try:
            nums = [int(x) for x in arg.split(",")]
            ip = ".".join(str(n) for n in nums[:4])
            port = nums[4] * 256 + nums[5]
            self.port_addr = (ip, port)
            self._send("200 主动模式已接受")
        except (ValueError, IndexError):
            self._send("501 参数错误")

    def _close_pasv(self):
        if self.pasv is not None:
            try:
                self.pasv.close()
            except OSError:
                pass
            self.pasv = None
        self.port_addr = None

    # ------------------------------------------------------------ 列表
    def _listing_lines(self, real_dir, vpath, style):
        try:
            entries = list(os.scandir(real_dir))
        except OSError:
            return None
        lines = []
        for e in entries:
            try:
                st = e.stat()
                isdir = e.is_dir()
            except OSError:
                continue
            if style == "mlsd":
                kind = "dir" if isdir else "file"
                lines.append("type=%s;%smodify=%s; %s" % (
                    kind,
                    "" if isdir else "size=%d;" % st.st_size,
                    time.strftime("%Y%m%d%H%M%S", time.gmtime(st.st_mtime)),
                    e.name))
            else:
                perm = "drwxr-xr-x" if isdir else "-rw-r--r--"
                mt = time.localtime(st.st_mtime)
                recent = (time.time() - st.st_mtime) < 180 * 86400
                date = time.strftime("%b %d %H:%M" if recent else "%b %d  %Y", mt)
                lines.append("%s 1 ftp ftp %12d %s %s" % (
                    perm, 0 if isdir else st.st_size, date, e.name))
        lines.sort()
        return lines

    def _send_listing(self, vpath, style):
        if not self._need_auth():
            return
        real, share = self._real(vpath)
        if vpath == "/":
            if style == "nlst":
                lines = [s.get("name") or "" for s in self.srv.shares()]
            else:
                lines = []
                stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
                pretty = time.strftime("%b %d %H:%M")
                for s in self.srv.shares():
                    name = s.get("name") or ""
                    lines.append(
                        ("type=dir;modify=%s; %s" % (stamp, name))
                        if style == "mlsd" else
                        "drwxr-xr-x 1 ftp ftp %12d %s %s" % (0, pretty, name))
        else:
            if real is None or not os.path.isdir(real):
                self._send("550 目录不存在")
                return
            if style == "nlst":
                # NLST 只回名字，不带长格式 —— 客户端靠它做补全/批量下载
                try:
                    lines = sorted(e.name for e in os.scandir(real))
                except OSError:
                    self._send("550 无法读取目录")
                    return
            else:
                lines = self._listing_lines(real, vpath, style)
                if lines is None:
                    self._send("550 无法读取目录")
                    return
        sock = self._open_data()
        if sock is None:
            return
        self._send("150 开始传输目录列表")
        try:
            payload = ("\r\n".join(lines) + ("\r\n" if lines else "")).encode(
                "utf-8", "replace")
            sock.sendall(payload)
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        except OSError:
            self._send("426 连接中断")
            return
        finally:
            try:
                sock.close()
            except OSError:
                pass
        self._send("226 目录列表传输完成")

    def _c_list(self, arg):
        target = arg
        # 客户端可能发 "LIST -a" / "LIST -l /path"
        if target.startswith("-"):
            parts = target.split(" ", 1)
            target = parts[1] if len(parts) > 1 else ""
        self._send_listing(self._vpath(target), "list")

    def _c_nlst(self, arg):
        target = arg
        if target.startswith("-"):
            parts = target.split(" ", 1)
            target = parts[1] if len(parts) > 1 else ""
        self._send_listing(self._vpath(target), "nlst")

    def _c_mlsd(self, arg):
        self._send_listing(self._vpath(arg), "mlsd")

    def _c_mlst(self, arg):
        if not self._need_auth():
            return
        vpath = self._vpath(arg)
        real, _s = self._real(vpath)
        if real and os.path.exists(real):
            st = os.stat(real)
            kind = "dir" if os.path.isdir(real) else "file"
            self._send("250-正在列出 %s" % vpath)
            self._send(" type=%s;%smodify=%s; %s" % (
                kind, "" if kind == "dir" else "size=%d;" % st.st_size,
                time.strftime("%Y%m%d%H%M%S", time.gmtime(st.st_mtime)),
                os.path.basename(real)))
            self._send("250 结束")
        else:
            self._send("550 路径不存在")

    # ------------------------------------------------------------ 传输
    def _c_retr(self, arg):
        if not self._need_auth():
            return
        vpath = self._vpath(arg)
        real, _s = self._real(vpath)
        if real is None or not os.path.isfile(real):
            self._send("550 文件不存在")
            return
        offset = self.rest
        self.rest = 0
        size = os.path.getsize(real)
        if offset > size:
            offset = 0
        sock = self._open_data()
        if sock is None:
            return
        self._send("150 正在传输 %s（%s）" % (
            os.path.basename(real), human_bytes(size - offset)))
        sent = 0
        tr = None
        if self.srv.traffic is not None:
            tr = self.srv.traffic.begin(self.peer, "FTP", real, size - offset,
                                        "down", user=self.user)
        try:
            with open(real, "rb") as fp:
                sent = self._pump_out(sock, fp, offset, tr)
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        finally:
            if tr is not None:
                self.srv.traffic.end(tr)
            try:
                sock.close()
            except OSError:
                pass
        self._send("226 传输完成（%s）" % human_bytes(sent))
        self.srv._log("ok" if offset + sent >= size else "warn",
                      "FTP 下载 %s（%s%s）" % (
                          vpath, human_bytes(sent),
                          "" if offset + sent >= size else "，中断"),
                      self.peer)

    def _real_upload(self, vpath):
        """上传专用解析：比 :meth:`_real` 更严。

        ``_real`` 只做 ``safe_join``；上传还要逐段消毒（ADS 冒号、保留设备名、
        尾随点空格）并对父目录做 realpath 比对，所以单独走一条路。
        """
        parts = [p for p in vpath.split("/") if p]
        if not parts:
            return None, None, "路径为空"
        share = None
        for s in self.srv.shares():
            if s.get("name") == parts[0]:
                share = s
                break
        if share is None:
            return None, None, "共享不存在"
        real, err = safe_target(share["path"], "/".join(parts[1:]))
        if real is None:
            return None, None, err
        return real, share, None

    def _c_stor(self, arg, append=False):
        if not self._need_auth() or not self._need_write():
            return
        vpath = self._vpath(arg)
        real, _share, err = self._real_upload(vpath)
        if real is None:
            self.srv._log("warn", "拒绝 FTP 上传 %s：%s" % (vpath, err),
                          self.peer)
            self._send("550 路径非法：%s" % err)
            return
        parent = os.path.dirname(real)
        if not os.path.isdir(parent):
            self._send("550 上级目录不存在")
            return
        offset = 0 if append else self.rest
        self.rest = 0
        exists = os.path.exists(real)
        if exists and not offset:
            # 不覆盖已有文件；REST 续传（offset>0）例外，那是把中断的上传接完
            self._send("553 同名文件已存在，请换个文件名")
            return
        mode = "r+b" if (offset and exists) else "wb"
        sock = self._open_data()
        if sock is None:
            return
        self._send("150 准备接收 %s" % os.path.basename(real))
        got = 0
        tr = None
        if self.srv.traffic is not None:
            tr = self.srv.traffic.begin(self.peer, "FTP", real, 0, "up",
                                        user=self.user)
        try:
            with open(real, mode) as fp:
                if offset and mode == "r+b":
                    fp.truncate(offset)
                got = self._pump_in(sock, fp, offset if mode == "r+b" else 0, tr)
        except OSError as exc:
            self._send("550 写入失败：%s" % exc)
            return
        finally:
            if tr is not None:
                self.srv.traffic.end(tr)
            try:
                sock.close()
            except OSError:
                pass
        # 落盘后加固：可执行扩展名改名 + 拒绝执行 ACL
        final = real
        try:
            final = harden(real)
        except Exception:                                  # noqa: BLE001
            pass
        if final != real:
            self.srv._log("warn",
                          "FTP 上传 %s 是可执行文件，已改名为 %s 并禁止执行"
                          % (vpath, os.path.basename(final)), self.peer)
        self._send("226 接收完成（%s）" % human_bytes(got))
        self.srv._log("ok", "FTP 上传 %s（%s）" % (vpath, human_bytes(got)),
                      self.peer)

    def _c_appe(self, arg):
        self._c_stor(arg, append=True)

    # ------------------------------------------------------------ 文件管理
    def _c_dele(self, arg):
        if not self._need_auth() or not self._need_write():
            return
        real, _s = self._real(self._vpath(arg))
        if real and os.path.isfile(real):
            try:
                os.remove(real)
                self._send("250 已删除")
                self.srv._log("warn", "FTP 删除 %s" % arg, self.peer)
            except OSError as exc:
                self._send("550 删除失败：%s" % exc)
        else:
            self._send("550 文件不存在")

    def _c_mkd(self, arg):
        if not self._need_auth() or not self._need_write():
            return
        vpath = self._vpath(arg)
        real, _s = self._real(vpath)
        if real is None:
            self._send("550 路径非法")
            return
        try:
            os.makedirs(real, exist_ok=True)
            self._send('257 "%s" 已创建' % vpath)
        except OSError as exc:
            self._send("550 创建失败：%s" % exc)

    _c_xmkd = _c_mkd

    def _c_rmd(self, arg):
        if not self._need_auth() or not self._need_write():
            return
        real, _s = self._real(self._vpath(arg))
        if real and os.path.isdir(real):
            try:
                os.rmdir(real)
                self._send("250 已删除目录")
            except OSError as exc:
                self._send("550 删除失败：%s" % exc)
        else:
            self._send("550 目录不存在")

    _c_xrmd = _c_rmd

    def _c_rnfr(self, arg):
        if not self._need_auth() or not self._need_write():
            return
        real, _s = self._real(self._vpath(arg))
        if real and os.path.exists(real):
            self._rnfr = real
            self._send("350 请提供新名称")
        else:
            self._send("550 路径不存在")

    def _c_rnto(self, arg):
        if not self._need_auth() or not self._need_write():
            return
        if not self._rnfr:
            self._send("503 请先发 RNFR")
            return
        # 目标名与上传同一标准消毒：不能借「改名」造出冒号（ADS）、
        # 保留设备名，也不能把文件改回可执行扩展名绕过 noexec。
        real, _share, err = self._real_upload(self._vpath(arg))
        if real is None:
            self._send("550 %s" % (err or "路径非法"))
            return
        try:
            os.replace(self._rnfr, real)
            final = harden(real)
            if final != real:
                self._send("250 重命名成功（可执行文件已禁止执行）")
            else:
                self._send("250 重命名成功")
        except OSError as exc:
            self._send("550 重命名失败：%s" % exc)
        finally:
            self._rnfr = None

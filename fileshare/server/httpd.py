# -*- coding: utf-8 -*-
"""自研 asyncio HTTP 文件服务器（下载 + 可选上传，上传默认关闭）。

为什么不用标准库 ``http.server``：它是「一线程一连接」的阻塞模型，
每个连接一次 ``send`` 都是一次系统调用加 GIL 切换，几十个并发下载就把
CPU 吃满、把界面拖卡。这里用单线程事件循环 + 线程池读文件：

  · 事件循环只管 socket（Windows 上是 IOCP/Proactor，吞吐高、连接数无压力）；
  · 文件读放在线程池里 ``os.pread``，绝不阻塞循环；
  · 一次搬 512 KB（限速时自动缩小），配合较大的 socket 写缓冲，
    局域网千兆能稳定跑满；
  · ``drain()`` 提供天然背压 —— 慢客户端只会拖慢自己，不会把内存打爆。

协议侧支持 Range（单段），所以多线程下载、断点续传、暂停恢复都能用。
"""

import asyncio
import concurrent.futures
import datetime
import html
import mimetypes
import os
import shutil
import socket
import threading
import time
import urllib.parse

from ..netutil import safe_join
from ..uploadguard import harden, safe_target
from ..util import human_bytes, human_speed

try:                                                     # Windows 有 pread 就用它
    _pread = os.pread
except AttributeError:                                   # pragma: no cover
    def _pread(fd, n, off):
        os.lseek(fd, off, os.SEEK_SET)
        return os.read(fd, n)

O_BINARY = getattr(os, "O_BINARY", 0)

BASE_CHUNK = 512 * 1024          # 不限速时一次搬 512 KB
MAX_HEAD = 32 * 1024             # 请求头上限
KEEPALIVE_TIMEOUT = 45.0         # 复用连接的两次请求之间允许空闲多久
MAX_CONNS = 192                  # 同时处理的连接上限

MIME_OVERRIDE = {
    ".mkv": "video/x-matroska", ".mp4": "video/mp4", ".flv": "video/x-flv",
    ".ts": "video/mp2t", ".apk": "application/vnd.android.package-archive",
    ".7z": "application/x-7z-compressed", ".rar": "application/vnd.rar",
    ".iso": "application/x-iso9660-image", ".md": "text/markdown; charset=utf-8",
}

STATUS_TEXT = {
    200: "OK", 204: "No Content", 206: "Partial Content", 301: "Moved Permanently",
    304: "Not Modified", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 408: "Request Timeout", 411: "Length Required",
    413: "Payload Too Large", 416: "Range Not Satisfiable",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error", 501: "Not Implemented",
}

HTTP_DATE = "%a, %d %b %Y %H:%M:%S GMT"


def http_date(ts):
    return time.strftime(HTTP_DATE, time.gmtime(ts))


def guess_type(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in MIME_OVERRIDE:
        return MIME_OVERRIDE[ext]
    mime, _enc = mimetypes.guess_type(path)
    if mime:
        if mime.startswith("text/"):
            return mime + "; charset=utf-8"
        return mime
    return "application/octet-stream"


class Request(object):
    __slots__ = ("method", "target", "path", "query", "version", "headers",
                 "rest", "peer")

    def __init__(self, method, target, version, headers, rest, peer):
        self.method = method
        self.version = version
        self.headers = headers
        self.rest = rest
        self.peer = peer
        self.target = target
        path, _, query = target.partition("?")
        self.path = urllib.parse.unquote(path)
        self.query = urllib.parse.parse_qs(query)


class HttpFileServer(object):
    """对外提供 HTTP 下载服务。``shares`` = [{"name":..., "path":...}]"""

    def __init__(self, shares_provider, port=8080, limiter=None, stats=None,
                 log=None, allow_upload=False, dirlist=True, host="0.0.0.0",
                 traffic=None):
        self.shares_provider = shares_provider      # callable -> list[dict]
        self.traffic = traffic                      # TrafficMonitor（可空）
        self.port = int(port)
        self.host = host
        self.limiter = limiter
        self.stats = stats
        self.log = log
        self.allow_upload = bool(allow_upload)
        self.dirlist = dirlist

        self._thread = None
        self._loop = None
        self._server = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._ok = False
        self._msg = ""
        self._pool = None
        self._sem = None
        self.active_conns = 0
        self.total_requests = 0

    # ------------------------------------------------------------ 生命周期
    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive() and self._ok)

    @property
    def message(self):
        return self._msg

    def start(self):
        if self.running:
            return True, "HTTP 服务已在运行"
        self._stop.clear()
        self._ready.clear()
        self._ok = False
        self._thread = threading.Thread(target=self._run, name="http-server",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(6.0):
            return False, "HTTP 服务启动超时"
        return self._ok, self._msg

    def stop(self):
        self._stop.set()
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        t = self._thread
        if t is not None and t.is_alive():
            t.join(4.0)
        self._thread = None
        self._ok = False
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None

    def _run(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=24, thread_name_prefix="httpio")
        try:
            srv = loop.run_until_complete(asyncio.start_server(
                self._handle_client, self.host, self.port, backlog=512,
                reuse_address=True))
        except OSError as exc:
            self._msg = "端口 %d 无法监听：%s" % (self.port, _err_text(exc))
            self._ok = False
            self._ready.set()
            loop.close()
            return
        self._server = srv
        self._sem = asyncio.Semaphore(MAX_CONNS)
        self._ok = True
        self._msg = "OK"
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                srv.close()
                loop.run_until_complete(srv.wait_closed())
            except Exception:                              # noqa: BLE001
                pass
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:                              # noqa: BLE001
                pass
            loop.close()
            self._loop = None

    # ------------------------------------------------------------ 连接处理
    async def _handle_client(self, reader, writer):
        sock = writer.get_extra_info("socket")
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        try:
            writer.transport.set_write_buffer_limits(high=BASE_CHUNK // 2,
                                                     low=BASE_CHUNK // 8)
        except Exception:                                  # noqa: BLE001
            pass
        peer = ""
        try:
            pai = writer.get_extra_info("peername")
            if pai:
                peer = "%s:%s" % (pai[0], pai[1])
        except Exception:                                  # noqa: BLE001
            pass
        self.active_conns += 1
        try:
            while not self._stop.is_set():
                req = await self._read_request(reader, peer)
                if req is None:
                    break
                self.total_requests += 1
                keep = await self._dispatch(req, reader, writer)
                if not keep:
                    break
        except (ConnectionError, asyncio.IncompleteReadError,
                asyncio.TimeoutError, OSError):
            pass
        except Exception as exc:                           # noqa: BLE001
            self._log("err", "HTTP 处理异常：%s" % exc, peer)
        finally:
            self.active_conns -= 1
            try:
                writer.close()
            except Exception:                              # noqa: BLE001
                pass

    async def _read_request(self, reader, peer):
        buf = b""
        deadline = KEEPALIVE_TIMEOUT
        while b"\r\n\r\n" not in buf:
            try:
                chunk = await asyncio.wait_for(reader.read(8192), deadline)
            except asyncio.TimeoutError:
                return None
            if not chunk:
                return None
            buf += chunk
            if len(buf) > MAX_HEAD:
                return None
            deadline = 10.0
        head, _, rest = buf.partition(b"\r\n\r\n")
        try:
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ")
            if len(parts) < 2:
                return None
            method, target = parts[0].upper(), parts[1]
            version = parts[2] if len(parts) > 2 else "HTTP/1.1"
        except (UnicodeDecodeError, IndexError):
            return None
        headers = {}
        for ln in lines[1:]:
            k, sep, v = ln.partition(":")
            if sep:
                headers[k.strip().lower()] = v.strip()
        if headers.get("expect", "").lower() == "100-continue":
            # 客户端在等 100，不然不会发 body（curl 大文件上传时常见）
            pass
        return Request(method, target, version, headers, rest, peer)

    async def _dispatch(self, req, reader, writer):
        h = req.headers
        keep = h.get("connection", "").lower() != "close"
        if req.version == "HTTP/1.0" and "keep-alive" not in h.get("connection", "").lower():
            keep = False
        try:
            if req.path == "/favicon.ico":
                return await self._send_empty(writer, 204, keep)
            if req.method in ("GET", "HEAD"):
                return await self._do_get(req, writer, keep)
            if req.method == "PUT":
                return await self._do_put(req, reader, writer, keep)
            if req.method == "OPTIONS":
                allow = "GET, HEAD, %s, OPTIONS" % (
                    "PUT" if self.allow_upload else "PUT（未开启）")
                return await self._send_simple(
                    writer, 200, b"", keep,
                    extra={"Allow": allow, "Accept-Ranges": "bytes"})
            # POST / DELETE 等一律拒绝：上传只有 PUT 一个入口。
            return await self._send_error(
                writer, 405, keep, "只支持 GET / HEAD / PUT")
        except (ConnectionError, asyncio.CancelledError):
            raise
        except Exception as exc:                           # noqa: BLE001
            self._log("err", "HTTP 请求失败 %s：%s" % (req.path, exc), req.peer)
            try:
                await self._send_error(writer, 500, False, "服务器内部错误")
            except Exception:                              # noqa: BLE001
                pass
            return False

    # ------------------------------------------------------------ GET
    def _shares(self):
        try:
            return [s for s in (self.shares_provider() or []) if s.get("path")]
        except Exception:                                  # noqa: BLE001
            return []

    def _find_share(self, name):
        for s in self._shares():
            if s.get("name") == name:
                return s
        return None

    async def _do_get(self, req, writer, keep):
        shares = self._shares()
        rel = req.path.lstrip("/")
        if not shares:
            # 没有共享不是错误，给个能看懂的页面（404 会让用户以为服务挂了）
            return await self._send_html(writer, req, self._page_empty(), keep)
        if not rel:
            body = self._page_root(shares)
            return await self._send_html(writer, req, body, keep)
        first, _, rest = rel.partition("/")
        share = self._find_share(first)
        if share is None:
            return await self._send_error(writer, 404, keep, "共享不存在：%s" % first)
        target = safe_join(share["path"], rest)
        if target is None:
            return await self._send_error(writer, 403, keep, "非法路径")
        if os.path.isdir(target):
            if not self.dirlist:
                return await self._send_error(writer, 403, keep, "目录浏览已关闭")
            body = self._page_dir(share, rest, target)
            return await self._send_html(writer, req, body, keep)
        if not os.path.isfile(target):
            return await self._send_error(writer, 404, keep, "文件不存在")
        return await self._send_file(req, writer, target, keep)

    async def _send_file(self, req, writer, path, keep):
        try:
            st = os.stat(path)
        except OSError:
            return await self._send_error(writer, 404, keep, "文件打不开")
        size = st.st_size
        etag = '"%x-%x"' % (int(st.st_mtime), size)
        last_mod = http_date(st.st_mtime)

        if req.headers.get("if-none-match") == etag:
            return await self._send_simple(writer, 304, b"", keep,
                                           extra={"ETag": etag,
                                                  "Last-Modified": last_mod})

        start, end, partial = 0, size - 1, False
        rng = req.headers.get("range")
        if rng:
            parsed = _parse_range(rng, size)
            if parsed is None:                     # 合法但不可满足 → 416
                return await self._send_simple(
                    writer, 416, b"", keep,
                    extra={"Content-Range": "bytes */%d" % size})
            if parsed == "ignore":                 # 畸形 → 当没发，回全量
                parsed = None
            else:
                start, end = parsed
                partial = True
        length = max(0, end - start + 1)

        # 存储型 XSS 防护：上传进来的 HTML/SVG 等能在共享页同源执行的类型，
        # 一律强制「另存为」而不是内联渲染（nosniff 已在 _build_head 全局加）。
        ext = os.path.splitext(path)[1].lower()
        inline_ok = ext not in (".html", ".htm", ".xhtml", ".xht", ".svg",
                                ".xml", ".swf")
        disposition = ("inline" if inline_ok else "attachment")

        headers = {
            "Content-Type": guess_type(path),
            "Content-Length": str(length),
            "Accept-Ranges": "bytes",
            "ETag": etag,
            "Last-Modified": last_mod,
            "Cache-Control": "public, max-age=0",
            "Content-Disposition": "%s; filename*=UTF-8''%s" % (
                disposition, urllib.parse.quote(os.path.basename(path))),
        }
        if partial:
            headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
        status = 206 if partial else 200
        writer.write(_build_head(status, headers, keep))
        if req.method == "HEAD" or length == 0:
            await writer.drain()
            self._log("info", "HEAD %s (%s)" % (req.path, human_bytes(length)),
                      req.peer)
            return keep

        loop = asyncio.get_event_loop()
        fd = None
        sent = 0
        tr = None
        if self.traffic is not None:
            tr = self.traffic.begin(req.peer, "HTTP", path, size, "down")
        try:
            fd = os.open(path, os.O_RDONLY | O_BINARY)
            pos, left = start, length
            base = self.limiter.chunk_size(BASE_CHUNK) if self.limiter else BASE_CHUNK
            while left > 0 and not self._stop.is_set():
                n = min(base, left)
                buf = await loop.run_in_executor(self._pool, _pread, fd, n, pos)
                if not buf:
                    break
                writer.write(buf)
                await writer.drain()
                pos += len(buf)
                left -= len(buf)
                sent += len(buf)
                if self.stats is not None:
                    self.stats.add_sent(len(buf))
                if tr is not None:
                    tr.bytes += len(buf)                # 单协程内累加，无需加锁
                if self.limiter is not None:
                    delay = self.limiter.consume(len(buf))
                    if delay > 0:
                        await asyncio.sleep(delay)
        except (ConnectionError, asyncio.CancelledError):
            return False
        except OSError as exc:
            self._log("err", "读文件失败 %s：%s" % (path, exc), req.peer)
            return False
        finally:
            if self.traffic is not None and tr is not None:
                self.traffic.end(tr)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._log("ok" if sent >= length else "warn",
                  "下载 %s（%s%s）" % (req.path, human_bytes(sent),
                                     "，中断" if sent < length else ""),
                  req.peer)
        return keep

    # ------------------------------------------------------------ PUT 上传
    async def _do_put(self, req, reader, writer, keep):
        """网页上传。默认关闭（403）；开关打开后也要过全套路径防护。"""
        if not self.allow_upload:
            return await self._send_error(
                writer, 403, keep, "上传功能未开启（到服务设置里手动打开）")
        shares = self._shares()
        if not shares:
            return await self._send_error(writer, 404, keep,
                                          "还没有共享任何目录")
        rel = req.path.lstrip("/")
        first, _, rest = rel.partition("/")
        share = self._find_share(first)
        if share is None or not rest:
            return await self._send_error(writer, 403, keep, "上传路径非法")
        # 安全防护：拒绝跳出目录 / 盘符 / 备用数据流 / 保留名，可执行名落盘加固
        target, err = safe_target(share["path"], rest)
        if target is None:
            return await self._send_error(writer, 403, keep, err or "上传路径非法")
        parent = os.path.dirname(target)
        if not os.path.isdir(parent):
            return await self._send_error(writer, 404, keep, "目标目录不存在")
        if os.path.exists(target):
            return await self._send_error(writer, 409, keep,
                                          "同名文件已存在，拒绝覆盖")

        try:
            length = int(req.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return await self._send_error(writer, 400, keep, "缺少请求体")
        # 防写满磁盘：声明的大小超过剩余空间就直接拒绝（507）
        try:
            if length > shutil.disk_usage(parent).free:
                return await self._send_error(writer, 507, keep,
                                              "磁盘剩余空间不足")
        except OSError:
            pass

        base = self.limiter.chunk_size(256 * 1024) if self.limiter else 256 * 1024
        written = 0
        tmp = target + ".fsupload"
        tr = (self.traffic.begin(req.peer, "HTTP", target, length, "up")
              if self.traffic is not None else None)
        f = await asyncio.get_event_loop().run_in_executor(
            self._pool, lambda: open(tmp, "wb"))
        try:
            buf = req.rest
            while written < length:
                if not buf:
                    try:
                        buf = await asyncio.wait_for(
                            reader.read(min(base, 262144)), 120.0)
                    except asyncio.TimeoutError:
                        raise ConnectionError("上传超时")
                    if not buf:
                        raise ConnectionError("上传中断")
                need = length - written
                data = buf[:need]
                buf = buf[len(data):]
                await asyncio.get_event_loop().run_in_executor(
                    self._pool, f.write, data)
                written += len(data)
                if self.stats is not None:
                    self.stats.add_recv(len(data))
                if tr is not None:
                    self.traffic.add(tr, len(data))
                if self.limiter is not None:
                    delay = self.limiter.consume(len(data))
                    if delay > 0:
                        await asyncio.sleep(delay)
            await asyncio.get_event_loop().run_in_executor(self._pool, f.close)
        except Exception as exc:                           # noqa: BLE001
            try:
                await asyncio.get_event_loop().run_in_executor(self._pool, f.close)
            except Exception:                              # noqa: BLE001
                pass
            try:
                os.remove(tmp)
            except OSError:
                pass
            if tr is not None:
                self.traffic.end(tr)
            self._log("err", "上传失败 %s：%s" % (req.path, exc), req.peer)
            return await self._send_error(writer, 400, False, "上传失败")

        # 先落盘到最终名字，再加固（改名 .noexec + 拒绝执行 ACL）。
        # 顺序不能反：harden 操作的是 target 这个路径，必须先有文件才改得动。
        os.replace(tmp, target)
        final = harden(target)
        if tr is not None:
            tr.target = final
            tr.size = written
            self.traffic.end(tr)
        if final != target:
            self._log("warn", "上传 %s 是可执行文件，已改名并禁止执行"
                      % os.path.basename(target), req.peer)
        self._log("ok", "上传 %s（%s）" % (req.path, human_bytes(written)),
                  req.peer)
        return await self._send_simple(
            writer, 201, b"Created", keep,
            extra={"Content-Type": "text/plain; charset=utf-8"})

    # ------------------------------------------------------------ 响应
    async def _send_html(self, writer, req, body, keep):
        raw = body.encode("utf-8")
        if req.method == "HEAD":
            writer.write(_build_head(200, {
                "Content-Type": "text/html; charset=utf-8",
                "Content-Length": str(len(raw))}, keep))
            await writer.drain()
            return keep
        writer.write(_build_head(200, {
            "Content-Type": "text/html; charset=utf-8",
            "Content-Length": str(len(raw)),
            "Cache-Control": "no-cache"}, keep))
        writer.write(raw)
        await writer.drain()
        return keep

    async def _send_simple(self, writer, status, body, keep, extra=None):
        headers = {"Content-Type": "text/plain; charset=utf-8",
                   "Content-Length": str(len(body))}
        headers.update(extra or {})
        writer.write(_build_head(status, headers, keep))
        if body:
            writer.write(body)
        await writer.drain()
        return keep

    async def _send_empty(self, writer, status, keep):
        return await self._send_simple(writer, status, b"", keep)

    async def _send_error(self, writer, code, keep, detail=""):
        body = ("<html><head><meta charset='utf-8'><title>%d</title></head>"
                "<body style='font-family:system-ui,Segoe UI;padding:40px'>"
                "<h2>%d %s</h2><p>%s</p></body></html>") % (
                    code, code, STATUS_TEXT.get(code, ""), html.escape(detail))
        raw = body.encode("utf-8")
        writer.write(_build_head(code, {
            "Content-Type": "text/html; charset=utf-8",
            "Content-Length": str(len(raw))}, keep))
        writer.write(raw)
        await writer.drain()
        self._log("warn", "HTTP %d %s" % (code, detail or ""), "")
        return keep

    def _log(self, kind, text, peer):
        if self.log is not None:
            self.log.add(kind, text, peer or "HTTP")

    # ------------------------------------------------------------ 页面
    def _page_empty(self):
        body = ("<h1>文件共享</h1>"
                "<p class='sub'>服务已经跑起来了，但还没有共享任何目录</p>"
                "<div class='list'><div style='padding:22px 18px;color:#5d5d5d'>"
                "到运行在本机的「文件共享中心」程序里，"
                "在「共享目录」页添加一个文件夹，刷新本页即可看到。</div></div>")
        return _page_style("文件共享").replace("__BODY__", body)

    def _page_root(self, shares):
        base = _page_style("文件共享")
        rows = []
        for s in shares:
            name = html.escape(s.get("name") or "")
            try:
                items = len(os.listdir(s["path"]))
                meta = "%d 个项目" % items
            except OSError:
                meta = "无法读取"
            rows.append(
                "<a class='row' href='/%s/'>"
                "<span class='ico'>%s</span>"
                "<span class='name'>%s</span>"
                "<span class='meta'>%s</span></a>" % (
                    urllib.parse.quote(name), _ICON_FOLDER, name,
                    html.escape(meta)))
        idx = base.replace("__TITLE__", "文件共享").replace(
            "__BODY__",
            "<h1>文件共享</h1><p class='sub'>共 %d 个共享目录</p>%s" % (
                len(shares), "".join(rows)))
        return idx

    def _page_dir(self, share, rel, path):
        rel = rel.strip("/")
        try:
            entries = list(os.scandir(path))
        except OSError:
            return _page_style("无法读取").replace(
                "__TITLE__", "无法读取").replace(
                "__BODY__", "<h1>无法读取目录</h1>")
        data = []
        for e in entries:
            try:
                st = e.stat()
                isdir = e.is_dir()
            except OSError:
                continue
            data.append((0 if isdir else 1, e.name.lower(), e.name, isdir,
                         st.st_size, st.st_mtime))
        data.sort()

        crumbs = ["<a href='/'>文件共享</a>", "<a href='/%s/'>%s</a>" % (
            urllib.parse.quote(share["name"]), html.escape(share["name"]))]
        acc = "/" + urllib.parse.quote(share["name"])
        for part in [p for p in rel.split("/") if p]:
            acc += "/" + urllib.parse.quote(part)
            crumbs.append("<a href='%s/'>%s</a>" % (acc, html.escape(part)))

        rows = []
        if rel:
            parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
            href = "/%s/%s" % (urllib.parse.quote(share["name"]),
                               (urllib.parse.quote(parent) + "/") if parent else "")
            rows.append("<a class='row' href='%s'>"
                        "<span class='ico'>%s</span><span class='name'>..</span>"
                        "<span class='meta'>上级目录</span></a>"
                        % (href, _ICON_UP))
        for _o, _ln, name, isdir, size, mtime in data:
            href = "/%s/%s%s" % (urllib.parse.quote(share["name"]),
                                 urllib.parse.quote(rel + "/" if rel else ""),
                                 urllib.parse.quote(name))
            if isdir:
                href += "/"
            rows.append(
                "<a class='row' href='%s'>"
                "<span class='ico'>%s</span>"
                "<span class='name'>%s</span>"
                "<span class='meta'>%s</span>"
                "<span class='size'>%s</span>"
                "<span class='time'>%s</span></a>" % (
                    href, _ICON_FOLDER if isdir else _ICON_FILE,
                    html.escape(name), "" if isdir else "",
                    "" if isdir else human_bytes(size),
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))))

        upload_ui = ""
        if self.allow_upload:
            upload_ui = _UPLOAD_BAR.replace("__REL__", acc + "/")

        body = ("%s<h1>%s</h1><p class='sub'>%d 个项目 · %s</p>%s"
                "<div class='list'>%s</div>") % (
                    " &nbsp;/&nbsp; ".join(crumbs),
                    html.escape(share["name"] + ("/" + rel if rel else "")),
                    len(data), human_bytes(sum(d[4] for d in data if not d[3])),
                    upload_ui, "".join(rows))
        return _page_style(share["name"]).replace(
            "__TITLE__", html.escape(share["name"])).replace("__BODY__", body)


# ---------------------------------------------------------------- 工具函数

def _err_text(exc):
    return str(exc) or exc.__class__.__name__


def _build_head(status, headers, keep):
    lines = ["HTTP/1.1 %d %s" % (status, STATUS_TEXT.get(status, "OK")),
             "Server: FileShare/1.0",
             "Date: " + http_date(time.time()),
             "Connection: " + ("keep-alive" if keep else "close"),
             # 防 MIME 嗅探：浏览器不得把响应「猜」成脚本执行
             "X-Content-Type-Options: nosniff"]
    for k, v in headers.items():
        lines.append("%s: %s" % (k, v))
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace")


def _parse_range(spec, size):
    """只支持单段 Range。

    返回 ``(start, end)``；语法畸形返回 ``"ignore"``（按 RFC 7233 当没发，
    回 200 全量）；语法合法但不可满足返回 ``None``（回 416）。
    """
    spec = (spec or "").strip()
    if not spec.lower().startswith("bytes="):
        return "ignore"
    spec = spec[6:].split(",")[0].strip()
    if "-" not in spec:
        return "ignore"
    a, _, b = spec.partition("-")
    try:
        if not a:                                  # bytes=-500 → 末 500 字节
            n = int(b)
            if n <= 0:
                return "ignore"                    # 后缀长度非法 → 忽略
            start = max(0, size - n)
            return start, size - 1
        start = int(a)
        end = int(b) if b else size - 1
    except ValueError:
        return "ignore"
    if start < 0 or start >= size:
        return None
    end = min(end, size - 1)
    if end < start:
        return None
    return start, end


_ICON_FOLDER = ("<svg viewBox='0 0 24 24' width='20' height='20' fill='none' "
                "stroke='#e3a008' stroke-width='1.8'><path d='M3 7a2 2 0 0 1 "
                "2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z'/>"
                "</svg>")
_ICON_FILE = ("<svg viewBox='0 0 24 24' width='20' height='20' fill='none' "
              "stroke='#8a8a8a' stroke-width='1.8'><path d='M14 3H7a2 2 0 0 0"
              "-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z'/><path d='M14 3v5h5'/>"
              "</svg>")
_ICON_UP = ("<svg viewBox='0 0 24 24' width='20' height='20' fill='none' "
            "stroke='#8a8a8a' stroke-width='1.8'><path d='M12 19V5'/>"
            "<path d='M5 12l7-7 7 7'/></svg>")

_UPLOAD_BAR = """<div class="upload" id="up">
  <input type="file" id="f" multiple>
  <button onclick="doUp()">上传到当前目录</button>
  <span id="msg"></span>
</div>
<script>
async function doUp(){
  const fs = document.getElementById('f').files;
  const msg = document.getElementById('msg');
  for (let i=0;i<fs.length;i++){
    msg.textContent = '正在上传 ' + fs[i].name + ' …';
    const r = await fetch('__REL__' + encodeURIComponent(fs[i].name),
                          {method:'PUT', body:fs[i]});
    if(!r.ok){ msg.textContent = '上传失败：' + fs[i].name; return; }
  }
  msg.textContent = '上传完成，正在刷新…';
  location.reload();
}
</script>"""

_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ · 文件共享</title>
<style>
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{margin:0;padding:32px 20px 60px;font:15px/1.6 "Microsoft YaHei UI",
 "Segoe UI",system-ui,sans-serif;background:#f3f3f3;color:#1b1b1b}
.wrap{max-width:940px;margin:0 auto}
h1{font-size:26px;margin:14px 0 2px;font-weight:600}
.sub{margin:0 0 18px;color:#5d5d5d;font-size:13px}
a{color:inherit;text-decoration:none}
.crumbs{font-size:13px;color:#5d5d5d}
.crumbs a:hover{color:#0067c0;text-decoration:underline}
.list{background:#fff;border:1px solid #e3e3e3;border-radius:10px;overflow:hidden}
.row{display:flex;align-items:center;gap:12px;padding:11px 16px;
 border-bottom:1px solid #f0f0f0;transition:background .12s}
.row:last-child{border-bottom:none}
.row:hover{background:#f6f9fd}
.ico{display:flex;width:20px;justify-content:center;flex:0 0 20px}
.name{flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.size{flex:0 0 90px;text-align:right;color:#5d5d5d;font-size:13px;
 font-variant-numeric:tabular-nums}
.time{flex:0 0 128px;text-align:right;color:#8a8a8a;font-size:12.5px;
 font-variant-numeric:tabular-nums}
.meta{color:#8a8a8a;font-size:13px}
.upload{display:flex;gap:10px;align-items:center;margin:0 0 16px;flex-wrap:wrap}
.upload button{padding:7px 14px;border:1px solid #d6d6d6;background:#fff;
 border-radius:6px;cursor:pointer;font:inherit;font-size:13px}
.upload button:hover{background:#f2f7fd;border-color:#0067c0;color:#0067c0}
#msg{color:#5d5d5d;font-size:13px}
footer{margin-top:26px;text-align:center;color:#8a8a8a;font-size:12px}
@media (prefers-color-scheme:dark){
 body{background:#202020;color:#fff}
 .sub,.size{color:#c8c8c8} .time,.meta{color:#9a9a9a}
 .list{background:#2b2b2b;border-color:#3a3a3a}
 .row{border-bottom-color:#333} .row:hover{background:#333}
 .upload button{background:#2b2b2b;border-color:#4a4a4a;color:#eee}
 .crumbs a:hover{color:#4cc2ff}
}
</style></head><body><div class="wrap">
__BODY__
<footer>文件共享中心 · by zhb</footer>
</div></body></html>"""


def _page_style(title):
    return _PAGE.replace("__TITLE__", title)

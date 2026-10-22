# -*- coding: utf-8 -*-
"""内置多线程下载器：分片并发 + 断点续传 + 断线重连 + 全局限速。

工作方式（和 IDM / aria2 一个思路）：

1. **探测**：HEAD（不支持就退化为 ``GET + Range: 0-0``）拿到文件大小、
   是否支持 Range、ETag；
2. **分片**：把 ``[0, size)`` 切成 N 段（默认 8），每段一个线程、一条独立
   连接，各发各的 ``Range: bytes=start-end``；
3. **落盘**：所有线程用 ``os.pwrite`` 直接写到同一个 ``.fspart`` 文件的
   对应偏移 —— 不共享文件指针，也不需要把整块数据留在内存里；
4. **断点续传**：每段的已下载字节记进 ``.fsmeta``（JSON）。暂停、崩溃、
   关程序之后再启动，按记录的偏移继续发 ``Range``；
5. **断线重连**：任何一段断了，从该段的当前位置重连续传（指数退避），
   不从头再来；
6. **限速**：所有线程共用一个令牌桶，所以设的是「总速度」而不是「每线程」。

下载完成后 ``.fspart`` 原子改名为目标文件、删掉 ``.fsmeta``。
"""

import http.client
import json
import os
import ssl
import threading
import time
import urllib.parse

from ..netutil import safe_filename, unique_path
from ..util import human_bytes

BLOCK = 128 * 1024
MIN_SEG = 1 * 1024 * 1024          # 每段至少 1 MB
MAX_RETRY = 60                     # 单段最多重连次数
CONNECT_TIMEOUT = 20
READ_TIMEOUT = 30

try:
    _pwrite = os.pwrite
except AttributeError:                                  # pragma: no cover
    _pwrite = None
    _pwrite_lock = threading.Lock()


def _write_at(fd, data, offset):
    if _pwrite is not None:
        return _pwrite(fd, data, offset)
    with _pwrite_lock:                                  # 老 Python 兜底
        os.lseek(fd, offset, os.SEEK_SET)
        return os.write(fd, data)


def _split(url):
    u = urllib.parse.urlsplit(url.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("只支持 http / https 链接")
    return u


def _url_of(u):
    return urllib.parse.urlunsplit(u)


def _path_of(u):
    p = u.path or "/"
    if u.query:
        p += "?" + u.query
    return p


def _connect(u, timeout=CONNECT_TIMEOUT):
    if u.scheme == "https":
        ctx = ssl.create_default_context()
        return http.client.HTTPSConnection(u.hostname, u.port or 443,
                                           timeout=timeout, context=ctx)
    return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)


def open_stream(u, method="GET", headers=None, timeout=CONNECT_TIMEOUT,
                follow=5):
    """发一个请求并跟随重定向，返回 (conn, resp, final_url)。"""
    headers = dict(headers or {})
    headers.setdefault("User-Agent", "FileShare/1.0")
    headers.setdefault("Accept", "*/*")
    # 必须禁用压缩：否则 Content-Length 是压缩后的长度，分片偏移全错
    headers.setdefault("Accept-Encoding", "identity")
    cur = u
    for _ in range(follow + 1):
        conn = _connect(cur, timeout)
        try:
            conn.request(method, _path_of(cur), headers=headers)
            resp = conn.getresponse()
        except Exception:
            conn.close()
            raise
        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.getheader("Location")
            conn.close()
            if not loc:
                raise IOError("服务器返回 %d 但没有 Location" % resp.status)
            cur = _split(urllib.parse.urljoin(_url_of(cur), loc))
            continue
        return conn, resp, cur
    raise IOError("重定向次数过多")


class Segment(object):
    __slots__ = ("start", "end", "done")

    def __init__(self, start, end, done=0):
        self.start = int(start)
        self.end = int(end)
        self.done = int(done)

    @property
    def length(self):
        """end < 0 表示「大小未知」，一直读到 EOF。"""
        if self.end < 0:
            return -1
        return self.end - self.start + 1

    @property
    def remaining(self):
        return -1 if self.end < 0 else max(0, self.length - self.done)

    def to_list(self):
        return [self.start, self.end, self.done]


class Task(object):
    """一个下载任务。字段都能直接从 UI 线程读（只读）。"""

    _seq = 0
    _seq_lock = threading.Lock()

    def __init__(self, url, folder, threads=8, filename=None):
        with Task._seq_lock:
            Task._seq += 1
            self.id = "t%d_%d" % (int(time.time()), Task._seq)
        self.url = url
        self.folder = folder
        self.threads = max(1, min(32, int(threads or 8)))
        self.filename = filename or ""
        self.status = "pending"        # pending/connecting/running/paused/done/error
        self.size = 0
        self.done = 0
        self.speed = 0.0
        self.error = ""
        self.etag = ""
        self.ranged = True
        self.unknown_size = False
        self.segs = []
        self.final_path = ""
        self.part_path = ""
        self.meta_path = ""
        self.created = time.time()
        self.finished = 0.0
        self.retries = 0
        self._stop = threading.Event()
        self._ctrl = None
        self._workers = []
        self.lock = threading.RLock()

    # ---- 展示用 ----
    @property
    def name(self):
        return self.filename or os.path.basename(self.url.split("?")[0]) or "download"

    @property
    def progress(self):
        if self.size <= 0:
            return 0.0
        return min(1.0, self.done / float(self.size))

    @property
    def eta(self):
        if self.speed <= 1 or self.size <= 0:
            return -1
        return max(0, (self.size - self.done) / self.speed)

    def to_meta(self):
        return {
            "url": self.url, "size": self.size, "etag": self.etag,
            "ranged": self.ranged, "unknown": self.unknown_size,
            "final": self.final_path, "part": self.part_path,
            "name": self.filename,
            "segs": [s.to_list() for s in self.segs],
        }

    def load_meta(self, meta):
        self.size = int(meta.get("size") or 0)
        self.etag = meta.get("etag") or ""
        self.ranged = bool(meta.get("ranged", True))
        self.unknown_size = bool(meta.get("unknown", False))
        self.final_path = meta.get("final") or ""
        self.part_path = meta.get("part") or ""
        self.filename = meta.get("name") or self.filename
        segs = []
        for row in meta.get("segs") or []:
            try:
                segs.append(Segment(row[0], row[1], row[2]))
            except (TypeError, IndexError, ValueError):
                pass
        self.segs = segs
        self.done = sum(s.done for s in segs)


class Downloader(object):
    def __init__(self, cfg, limiter, stats, log, on_change=None):
        self.cfg = cfg
        self.limiter = limiter
        self.stats = stats
        self.log = log
        self.on_change = on_change
        self._tasks = []
        self._lock = threading.RLock()
        self._monitor = None
        self._stop_all = threading.Event()
        self._last_done = {}
        self._store = os.path.join(
            os.path.dirname(cfg.path) if cfg.path else os.path.expanduser("~"),
            "downloads.json")

    # ------------------------------------------------------------ 任务管理
    @property
    def tasks(self):
        with self._lock:
            return list(self._tasks)

    def get(self, tid):
        with self._lock:
            for t in self._tasks:
                if t.id == tid:
                    return t
        return None

    def find_by_url(self, url):
        """按链接找已有任务（忽略结尾斜杠差异）；没有返回 None。"""
        key = (url or "").strip().rstrip("/")
        if not key:
            return None
        for t in self.tasks:
            if (t.url or "").strip().rstrip("/") == key:
                return t
        return None

    class DuplicateError(ValueError):
        """重复链接。``done=True`` 表示文件已经下完过，UI 要给覆盖/另存选择。"""

        def __init__(self, message, done=False, task=None):
            super().__init__(message)
            self.done = done
            self.task = task

    def add(self, url, folder=None, threads=None, filename=None,
            autostart=True, allow_duplicate=False):
        u = _split(url)                                  # 先校验协议
        dup = None if allow_duplicate else self.find_by_url(url)
        if dup is not None:
            # 同一个链接不允许悄悄重复下载。已完成的由 UI 问「覆盖 / 另存」；
            # 未完成的直接拒绝——任务列表里已经有它了。
            status_cn = {"pending": "等待中", "connecting": "连接中",
                         "running": "下载中", "paused": "已暂停",
                         "done": "已完成", "error": "失败"
                         }.get(dup.status, dup.status)
            if dup.status == "done" and os.path.isfile(dup.final_path or ""):
                raise self.DuplicateError(
                    "该文件已经下载完成：%s" % dup.final_path,
                    done=True, task=dup)
            raise self.DuplicateError(
                "该链接已在任务列表中（%s），不要重复下载" % status_cn)
        folder = folder or self.cfg.get("download", "dir") or os.path.expanduser("~")
        os.makedirs(folder, exist_ok=True)
        threads = threads or int(self.cfg.get("download", "threads") or 8)
        task = Task(_url_of(u), folder, threads, filename)
        if not task.filename:
            guess = urllib.parse.unquote(u.path.rsplit("/", 1)[-1])
            task.filename = safe_filename(guess, "download.bin")
        with self._lock:
            self._tasks.append(task)
        self.log.add("dl", "新建下载任务：%s" % task.name, "下载")
        self._save_store()
        if autostart:
            self.start(task.id)
        self._notify()
        return task

    def start(self, tid):
        task = self.get(tid)
        if task is None:
            return False
        if task.status == "running":
            return True
        task._stop.clear()
        task.error = ""
        task.status = "connecting"
        th = threading.Thread(target=self._run_task, args=(task,),
                              name="dl-" + task.id, daemon=True)
        task._ctrl = th
        th.start()
        self._ensure_monitor()
        self._notify()
        return True

    def pause(self, tid):
        task = self.get(tid)
        if task is None:
            return False
        task._stop.set()
        for w in list(task._workers):
            pass
        if task.status in ("running", "connecting", "pending"):
            task.status = "paused"
        self._save_meta(task)
        self.log.add("info", "已暂停：%s" % task.name, "下载")
        self._notify()
        return True

    def resume(self, tid):
        task = self.get(tid)
        if task is None:
            return False
        if task.status == "done":
            return False
        return self.start(tid)

    def pause_all(self):
        """暂停所有进行中的任务；返回暂停了几个。"""
        n = 0
        for t in self.tasks:
            if t.status in ("running", "connecting", "pending"):
                self.pause(t.id)
                n += 1
        return n

    def active_count(self):
        return sum(1 for t in self.tasks
                   if t.status in ("running", "connecting", "pending"))

    def remove(self, tid, delete_file=False):
        task = self.get(tid)
        if task is None:
            return False
        task._stop.set()
        # 必须等工作线程真的退出再删文件：Windows 上「文件被占用」会让
        # os.remove 直接失败，之前 0.05 秒的等待根本不够，缓存就留下来了。
        for w in list(getattr(task, "_workers", []) or []):
            try:
                w.join(timeout=3.0)
            except RuntimeError:
                pass
        if delete_file:
            # 未完成任务 → 清 .fspart/.fsmeta 缓存；
            # 已完成任务 → 连最终文件一起删。失败重试几次（杀毒/索引
            # 可能短暂占用），真删不掉就把原因记到日志里，不静默吞掉。
            paths = [task.part_path, task.meta_path]
            if task.status == "done" and task.final_path:
                paths.append(task.final_path)
            for p in paths:
                if not p:
                    continue
                for _try in range(6):
                    if not os.path.isfile(p):
                        break
                    try:
                        os.remove(p)
                    except OSError:
                        time.sleep(0.2)
                if os.path.isfile(p):
                    self.log.add("warn", "删除文件失败（可能被占用）：%s" % p,
                                 "下载")
        with self._lock:
            if task in self._tasks:
                self._tasks.remove(task)
        self.log.add("info", "移除任务：%s" % task.name, "下载")
        self._save_store()
        self._notify()
        return True

    def stop_all(self):
        """优雅停掉全部下载：停线程、落盘断点，不等也绝不拖残留。

        ``_stop_all`` 一置，监视循环退出；每个任务的 ``_stop`` 一置，
        分段线程写完当前块就收工。join 设了 2 秒上限——真有网络卡住的
        连接也不陪葬（线程都是 daemon，进程照样能退）。
        """
        self._stop_all.set()
        for t in self.tasks:
            if t.status in ("running", "connecting"):
                t._stop.set()
                t.status = "paused"
        for t in self.tasks:
            self._save_meta(t)
        self._save_store()
        deadline = time.time() + 2.0
        for t in self.tasks:
            for w in list(getattr(t, "_workers", []) or []):
                w.join(max(0.0, deadline - time.time()))
        mon, self._monitor = self._monitor, None
        if mon is not None and mon.is_alive():
            mon.join(0.3)

    # ------------------------------------------------------------ 主流程
    def _run_task(self, task):
        try:
            if not task.segs or task.done == 0 or not os.path.isfile(task.part_path or ""):
                self._prepare(task)
            task.status = "running"
            self._notify()
            self._spawn_workers(task)

            while any(w.is_alive() for w in task._workers):
                time.sleep(0.4)
                if self._stop_all.is_set():
                    task._stop.set()
                self._save_meta(task)

            if task._stop.is_set():
                if task.status != "error":
                    task.status = "paused"
                self._save_meta(task)
                self._notify()
                return

            if self._all_done(task):
                self._finalize(task)
            else:
                task.status = "error"
                task.error = task.error or "连接中断，未完成"
                self._save_meta(task)
            self._notify()
        except Exception as exc:                           # noqa: BLE001
            task.status = "error"
            task.error = str(exc)[:200]
            self.log.add("err", "下载失败 %s：%s" % (task.name, exc), "下载")
            self._notify()

    def _prepare(self, task):
        task.status = "connecting"
        self.log.add("dl", "正在连接：%s" % task.url, "下载")
        info = self._probe(task)
        size = info["size"]
        task.size = size
        task.ranged = info["ranged"]
        task.etag = info["etag"] or ""
        task.unknown_size = size <= 0
        if not task.filename:
            task.filename = info.get("filename") or "download.bin"

        base_target = os.path.join(task.folder, safe_filename(task.filename))
        # 有 .fsmeta 说明是同一次下载的续传，沿用原文件名；否则遇到同名文件
        # 就加 (1)(2) 后缀，绝不覆盖用户已有的文件
        if os.path.isfile(base_target + ".fsmeta") or not os.path.exists(base_target):
            final_path = base_target
        else:
            final_path = unique_path(base_target)
        task.final_path = final_path
        task.part_path = final_path + ".fspart"
        task.meta_path = final_path + ".fsmeta"

        # 有旧进度就接着来（同一个文件、同一个大小、同一个 ETag）
        if os.path.isfile(task.meta_path) and os.path.isfile(task.part_path):
            try:
                with open(task.meta_path, "r", encoding="utf-8") as fp:
                    meta = json.load(fp)
                same = (int(meta.get("size") or 0) == size and
                        (not meta.get("etag") or not task.etag or
                         meta.get("etag") == task.etag))
                if same:
                    task.load_meta(meta)
                    task.part_path = meta.get("part") or task.part_path
                    task.final_path = meta.get("final") or task.final_path
                    self.log.add("ok", "发现未完成的下载，从 %s 继续" %
                                 human_bytes(task.done), "下载")
                    if task.done > 0:
                        self._notify()
                        return
            except (OSError, ValueError):
                pass

        # 新建或重来
        if os.path.isfile(task.part_path):
            try:
                os.remove(task.part_path)
            except OSError:
                pass
        fd = os.open(task.part_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
        try:
            if size > 0:
                os.ftruncate(fd, size)
        finally:
            os.close(fd)
        task.segs = _plan_segments(size, task.threads if task.ranged else 1)
        task.done = 0
        self._save_meta(task)

    def _probe(self, task):
        u = _split(task.url)
        size, ranged, etag, cd = 0, False, "", ""
        try:
            conn, resp, _u = open_stream(u, "HEAD")
            try:
                if resp.status >= 400:
                    raise IOError("HTTP %d" % resp.status)
                size = int(resp.getheader("Content-Length") or 0)
                ranged = "bytes" in (resp.getheader("Accept-Ranges") or "").lower()
                etag = resp.getheader("ETag") or ""
                cd = resp.getheader("Content-Disposition") or ""
            finally:
                conn.close()
        except Exception:                                  # noqa: BLE001
            size, ranged = 0, False
        if size <= 0 or not ranged:
            # 用 1 字节的 Range 请求再确认一次（有些服务器不认 HEAD）
            try:
                conn, resp, _u = open_stream(u, "GET",
                                             {"Range": "bytes=0-0"})
                try:
                    if resp.status == 206:
                        cr = resp.getheader("Content-Range") or ""
                        if "/" in cr:
                            try:
                                size = int(cr.rsplit("/", 1)[1])
                            except ValueError:
                                size = size or 0
                        ranged = True
                        etag = etag or (resp.getheader("ETag") or "")
                    elif resp.status == 200:
                        size = size or int(resp.getheader("Content-Length") or 0)
                        ranged = False
                    else:
                        raise IOError("HTTP %d" % resp.status)
                    cd = cd or (resp.getheader("Content-Disposition") or "")
                finally:
                    conn.close()
            except Exception as exc:                       # noqa: BLE001
                raise IOError("无法连接：%s" % exc)
        return {"size": max(0, size), "ranged": ranged, "etag": etag,
                "filename": _filename_from(cd) or ""}

    # ------------------------------------------------------------ 分段线程
    def _spawn_workers(self, task):
        task._workers = []
        task._fd_closed = 0
        fd = os.open(task.part_path, os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            for idx in range(len(task.segs)):
                th = threading.Thread(target=self._worker,
                                      args=(task, idx, fd),
                                      name="seg%d" % idx, daemon=True)
                task._workers.append(th)
                th.start()
        except Exception:
            os.close(fd)
            raise
        # fd 的生命周期交给「最后一个退出的线程」关闭
        task._fd = fd

    def _worker(self, task, idx, fd):
        seg = task.segs[idx]
        backoff = 0.5
        tries = 0
        try:
            while not task._stop.is_set():
                if seg.end >= 0 and seg.done >= seg.length:
                    break
                pos = seg.start + seg.done
                end = seg.end if seg.end >= 0 else ""
                try:
                    conn, resp, _u = open_stream(
                        _split(task.url), "GET",
                        {"Range": "bytes=%s-%s" % (pos, end)},
                        timeout=READ_TIMEOUT)
                    try:
                        if resp.status == 416:
                            seg.done = max(0, seg.length)     # 已经下完了
                            break
                        if resp.status not in (200, 206):
                            raise IOError("HTTP %d" % resp.status)
                        if resp.status == 200 and idx > 0:
                            raise IOError("服务器忽略了 Range（不支持分片）")
                        base = self.limiter.chunk_size(BLOCK) if self.limiter else BLOCK
                        while not task._stop.is_set():
                            buf = resp.read(base)
                            if not buf:
                                break
                            _write_at(fd, buf, pos)
                            pos += len(buf)
                            seg.done += len(buf)
                            with task.lock:
                                task.done += len(buf)
                            if self.stats is not None:
                                self.stats.add_dl(len(buf))
                            if self.limiter is not None:
                                delay = self.limiter.consume(len(buf))
                                if delay > 0:
                                    time.sleep(delay)
                            if seg.end >= 0 and seg.done >= seg.length:
                                break
                    finally:
                        try:
                            conn.close()
                        except Exception:                  # noqa: BLE001
                            pass
                    if seg.end < 0:
                        # 大小未知：读到 EOF 就是全部内容
                        seg.end = seg.start + seg.done - 1
                        with task.lock:
                            task.size = task.done
                        break
                    tries = 0
                    backoff = 0.5
                except Exception as exc:                   # noqa: BLE001
                    if task._stop.is_set():
                        break
                    tries += 1
                    task.retries += 1
                    task.error = str(exc)[:120]
                    if tries > MAX_RETRY:
                        raise IOError("多次重连失败：%s" % exc)
                    time.sleep(min(8.0, backoff))
                    backoff = min(8.0, backoff * 1.7)
                    # 关键：不清零 seg.done —— 下一轮从当前位置续传
            if seg.end >= 0 and seg.done > seg.length:
                seg.done = seg.length
        finally:
            # 最后一个退出的线程负责关 fd
            with task.lock:
                task._fd_closed = getattr(task, "_fd_closed", 0) + 1
                if task._fd_closed >= len(task.segs):
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def _all_done(self, task):
        if not task.segs:
            return False
        for s in task.segs:
            if s.end < 0:
                continue
            if s.done < s.length:
                return False
        return True

    def _finalize(self, task):
        task.done = sum(s.done for s in task.segs)
        try:
            if task.size > 0:
                fd = os.open(task.part_path, os.O_RDWR | getattr(os, "O_BINARY", 0))
                try:
                    os.ftruncate(fd, task.size)
                finally:
                    os.close(fd)
            final = task.final_path
            if os.path.exists(final) and os.path.abspath(final) != os.path.abspath(task.part_path):
                try:
                    os.remove(final)
                except OSError:
                    pass
            os.replace(task.part_path, final)
            try:
                if os.path.isfile(task.meta_path):
                    os.remove(task.meta_path)
            except OSError:
                pass
            task.status = "done"
            task.finished = time.time()
            task.speed = 0.0
            self.log.add("ok", "下载完成：%s（%s）" % (
                task.name, human_bytes(task.size or task.done)), "下载")
        except OSError as exc:
            task.status = "error"
            task.error = "收尾失败：%s" % exc
            self.log.add("err", task.error, "下载")
        self._save_store()

    # ------------------------------------------------------------ 元数据
    def _save_meta(self, task):
        if not task.meta_path or task.status == "done":
            return
        try:
            with open(task.meta_path, "w", encoding="utf-8") as fp:
                json.dump(task.to_meta(), fp, ensure_ascii=False)
        except OSError:
            pass

    def _save_store(self):
        rows = []
        for t in self.tasks:
            rows.append({
                "id": t.id, "url": t.url, "folder": t.folder,
                "threads": t.threads, "filename": t.filename,
                "status": "paused" if t.status in ("running", "connecting") else t.status,
                "size": t.size, "done": t.done, "final": t.final_path,
                "part": t.part_path, "meta": t.meta_path,
                "error": t.error,
            })
        try:
            with open(self._store, "w", encoding="utf-8") as fp:
                json.dump(rows, fp, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def recover_orphans(self, folders=None):
        """扫描下载目录里没有对应任务的缓存文件。

        ``.fsmeta`` 里存了 url / 大小 / ETag / 分段进度，**能从缓存完整复原
        任务**（复原成「已暂停」，点继续就能接着下）。能复原的直接复原；
        复原不了的（缺 url、缺 .fspart、JSON 损坏）作为无主文件返回给 UI，
        由用户决定是否清理。

        返回 ``(恢复的任务数, 无主文件路径列表)``。
        """
        dirs = []
        for d in (folders or []):
            if d and os.path.isdir(d) and d not in dirs:
                dirs.append(d)
        default = self.cfg.get("download", "dir") or ""
        if default and os.path.isdir(default) and default not in dirs:
            dirs.insert(0, default)
        # 已有任务的目录也扫一遍（历史任务可能存到别的盘）
        for t in self.tasks:
            for d in (t.folder, os.path.dirname(t.final_path or "")):
                if d and os.path.isdir(d) and d not in dirs:
                    dirs.append(d)

        known_meta = set(os.path.normcase(t.meta_path or "")
                         for t in self.tasks)
        known_part = set(os.path.normcase(t.part_path or "")
                         for t in self.tasks)
        recovered = 0
        orphans = []
        for folder in dirs:
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for name in names:
                low = name.lower()
                if not (low.endswith(".fsmeta") or low.endswith(".fspart")):
                    continue
                path = os.path.join(folder, name)
                if os.path.normcase(path) in known_meta \
                        or os.path.normcase(path) in known_part:
                    continue          # 属于现有任务，交给正常续传逻辑
                if low.endswith(".fsmeta"):
                    ok, task = self._recover_from_meta(path)
                    if ok:
                        recovered += 1
                        known_meta.add(os.path.normcase(task.meta_path))
                        known_part.add(os.path.normcase(task.part_path))
                        continue
                    orphans.append(path)
                else:
                    # 只有 .fspart 没有 .fsmeta → 无从知道 URL，无法复原。
                    # 但如果同名 .fsmeta 存在（上面恢复成功），这里会被跳过。
                    stem = path[:-len(".fspart")]
                    if os.path.isfile(stem + ".fsmeta"):
                        continue
                    orphans.append(path)
        if recovered:
            self._save_store()
            self.log.add("ok", "启动扫描：从缓存恢复了 %d 个下载任务"
                         % recovered, "下载")
        if orphans:
            self.log.add("warn", "启动扫描：发现 %d 个无主缓存文件"
                         % len(orphans), "下载")
        return recovered, orphans

    def _recover_from_meta(self, meta_path):
        """尝试用 .fsmeta（+ 同名 .fspart）复原一个任务。返回 (成功?, task)。"""
        try:
            with open(meta_path, "r", encoding="utf-8") as fp:
                meta = json.load(fp)
        except (OSError, ValueError):
            return False, None
        url = (meta.get("url") or "").strip()
        if not url:
            return False, None
        part = meta.get("part") or (meta_path[:-len(".fsmeta")] + ".fspart")
        if not os.path.isfile(part):
            return False, None         # 数据都没了，元数据没用
        final = meta.get("final") or meta_path[:-len(".fsmeta")]
        if self.find_by_url(url) is not None:
            return False, None         # 已有同链接任务，不重复恢复
        folder = os.path.dirname(final) or os.path.dirname(meta_path)
        task = Task(url, folder, int(meta.get("threads") or 8),
                    meta.get("name") or os.path.basename(final))
        task.load_meta(meta)
        task.final_path = final
        task.part_path = part
        task.meta_path = meta_path
        task.status = "paused"
        with self._lock:
            self._tasks.append(task)
        self.log.add("ok", "从缓存恢复任务：%s（已完成 %s）"
                     % (task.name, human_bytes(task.done)), "下载")
        return True, task

    def load_store(self):
        try:
            with open(self._store, "r", encoding="utf-8") as fp:
                rows = json.load(fp)
        except (OSError, ValueError):
            return 0
        n = 0
        with self._lock:
            for r in rows or []:
                try:
                    t = Task(r.get("url") or "", r.get("folder") or "",
                             int(r.get("threads") or 8), r.get("filename") or "")
                    t.id = r.get("id") or t.id
                    t.size = int(r.get("size") or 0)
                    t.done = int(r.get("done") or 0)
                    t.final_path = r.get("final") or ""
                    t.part_path = r.get("part") or ""
                    t.meta_path = r.get("meta") or ""
                    t.error = r.get("error") or ""
                    t.status = "paused" if r.get("status") != "done" else "done"
                    if not t.url:
                        continue
                    self._tasks.append(t)
                    n += 1
                except (ValueError, TypeError):
                    continue
        return n

    # ------------------------------------------------------------ 监视
    def _ensure_monitor(self):
        if self._monitor is not None and self._monitor.is_alive():
            return
        self._monitor = threading.Thread(target=self._monitor_loop,
                                         name="dl-monitor", daemon=True)
        self._monitor.start()

    def _monitor_loop(self):
        while not self._stop_all.is_set():
            time.sleep(1.0)
            for t in self.tasks:
                if t.status == "running":
                    done = t.done
                    prev = self._last_done.get(t.id, done)
                    t.speed = max(0.0, done - prev)
                    self._last_done[t.id] = done
                else:
                    t.speed = 0.0
                    self._last_done.pop(t.id, None)
            self._notify()

    def _notify(self):
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:                              # noqa: BLE001
                pass


def _plan_segments(size, threads):
    if size <= 0:
        return [Segment(0, -1, 0)]
    n = max(1, min(int(threads or 1), int(size // MIN_SEG) or 1))
    base = size // n
    segs = []
    for i in range(n):
        start = i * base
        end = size - 1 if i == n - 1 else (i + 1) * base - 1
        segs.append(Segment(start, end, 0))
    return segs


def _filename_from(content_disposition):
    if not content_disposition:
        return ""
    parts = content_disposition.split(";")
    for p in parts[1:]:
        k, _, v = p.strip().partition("=")
        if k.lower() == "filename*":
            v = v.strip().strip('"')
            if "''" in v:
                v = v.split("''", 1)[1]
            try:
                return urllib.parse.unquote(v)
            except Exception:                              # noqa: BLE001
                return v
    for p in parts[1:]:
        k, _, v = p.strip().partition("=")
        if k.lower() == "filename":
            v = v.strip().strip('"')
            if v:
                try:
                    return v.encode("latin-1").decode("utf-8")
                except (UnicodeDecodeError, UnicodeEncodeError):
                    return v
    return ""

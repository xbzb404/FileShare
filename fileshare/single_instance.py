# -*- coding: utf-8 -*-
"""单实例守卫  ·  by zhb

为什么需要：用户反复双击 exe 时，如果没有单实例，每点一次就拉起一个
完整进程（各自监听端口、各自写配置、各自托盘图标），既浪费又会互相抢
端口。要求是「已经在跑就直接把已有窗口提到前台，不再起新进程」。

三层实现，从可靠到兜底：
1. **命名互斥体**（CreateMutexW，``Local\\`` 命名空间）—— 精确判定
   「已经有一个在跑」。进程一死（哪怕崩溃）由系统自动释放，不会留下
   stale lock；不需要管理员权限。
2. **回环 TCP 通道**（127.0.0.1:ACT_PORT）—— 第二个实例发一句
   ``FSACT1\\n``，第一个实例在自己的 Tk 线程里执行「显示窗口」，
   能跑应用自己的恢复逻辑（withdraw → deiconify + 置前台）。
3. **FindWindow 兜底** —— TCP 不可用（端口被无关程序占了）时按窗口
   标题直接找句柄，ShowWindow + SetForegroundWindow。

握手带 magic 串：端口万一被别的程序占用，不会误判成「已在运行」。
"""

import ctypes
import socket
import sys
import threading

ACT_PORT = 47611                    # 激活通道端口（冷门；discovery 用 47610）
MUTEX_NAME = "Local\\FileShare_SingleInstance_v1"
MAGIC = b"FSACT1\n"
REPLY = b"FSOK1\n"


def _win_user32():
    return ctypes.windll.user32


def raise_by_title(title):
    """按窗口标题把已有窗口提到前台（FindWindow 兜底路径）。

    返回 True 表示找到了窗口并尝试了置前台。
    """
    if sys.platform != "win32" or not title:
        return False
    try:
        user32 = _win_user32()
        user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        user32.FindWindowW.restype = ctypes.c_void_p
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            return False
        SW_RESTORE = 9
        user32.ShowWindow(ctypes.c_void_p(hwnd), SW_RESTORE)
        # SetForegroundWindow 有「只有前台进程能改前台」的限制：
        # 先挂到当前前台线程的输入队列，设完再摘下来。
        user32.GetWindowThreadProcessId.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        pid = ctypes.c_ulong(0)
        tid_cur = user32.GetWindowThreadProcessId(
            user32.GetForegroundWindow(), ctypes.byref(pid))
        tid_me = ctypes.windll.kernel32.GetCurrentThreadId()
        attached = False
        if tid_cur and tid_cur != tid_me:
            attached = bool(user32.AttachThreadInput(tid_me, tid_cur, True))
        user32.BringWindowToTop(ctypes.c_void_p(hwnd))
        ok = bool(user32.SetForegroundWindow(ctypes.c_void_p(hwnd)))
        if attached:
            user32.AttachThreadInput(tid_me, tid_cur, False)
        if not ok:
            # 最后的兜底：未公开 API，但 Windows 上一直有效
            user32.SwitchToThisWindow(ctypes.c_void_p(hwnd), True)
        return True
    except Exception:                                       # noqa: BLE001
        return False


class SingleInstance(object):
    """单实例守卫。

    用法::

        si = SingleInstance(title=window_title())
        if not si.acquire():
            return                      # 已有实例，已尝试激活，直接退
        ...
        si.activator = lambda: root.after(0, app.show_window)
        app.run()
        si.release()
    """

    def __init__(self, title, port=ACT_PORT, log=None, mutex_name=MUTEX_NAME):
        self.title = title or ""
        self.port = int(port)
        self.log = log
        self.mutex_name = mutex_name or MUTEX_NAME
        self._activator = None          # 第一个实例设置：Tk 线程里显示窗口
        self._pending = False           # 窗口建好前来过叫门 → 补一次
        self._mutex = None
        self._srv = None
        self._thread = None
        self._stop = threading.Event()
        self._lock_fallback_sock = None

    @property
    def activator(self):
        return self._activator

    @activator.setter
    def activator(self, fn):
        self._activator = fn
        # 窗口还没建好时第二个实例来叫过门：补一次，别把用户双击吞了
        if fn is not None and self._pending:
            self._pending = False
            try:
                fn()
            except Exception:                           # noqa: BLE001
                pass

    # ------------------------------------------------------------ 检测
    def _lock(self):
        """抢锁。True=本进程第一个；False=已有实例。"""
        if sys.platform == "win32":
            return self._lock_mutex()
        return self._lock_port()

    def _lock_mutex(self):
        kernel32 = ctypes.windll.kernel32
        # 注意：不要用 ctypes.get_last_error()——在部分环境里它恒返回 0
        # （use_last_error 标志未生效），必须直接调 GetLastError()。
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.GetLastError.restype = ctypes.c_ulong
        handle = kernel32.CreateMutexW(None, False, self.mutex_name)
        if not handle:
            # 创建失败（极端情况）：别因为守卫故障挡住用户启动
            self._warn("单实例互斥体创建失败，放行启动")
            return True
        if kernel32.GetLastError() == 183:         # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return False
        self._mutex = handle
        return True

    def _lock_port(self):
        """非 Windows 兜底：占住回环端口当锁。"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", self.port))
            s.listen(1)
        except OSError:
            s.close()
            return False
        self._lock_fallback_sock = s
        return True

    # ------------------------------------------------------------ 激活
    def _activate_existing(self):
        """（第二个实例）把已经在跑的实例窗口提到前台。"""
        if self._activate_by_tcp():
            return
        raise_by_title(self.title)

    def _activate_by_tcp(self):
        try:
            s = socket.create_connection(("127.0.0.1", self.port), timeout=0.6)
        except OSError:
            return False
        try:
            s.settimeout(0.8)
            s.sendall(MAGIC)
            reply = s.recv(32)
        except OSError:
            return False
        finally:
            try:
                s.close()
            except OSError:
                pass
        return reply.startswith(REPLY)

    # ------------------------------------------------------------ 服务端
    def _start_server(self):
        """（第一个实例）开激活通道；端口被占就算了，FindWindow 兜底。"""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv.bind(("127.0.0.1", self.port))
            srv.listen(4)
        except OSError:
            srv.close()
            self._warn("激活通道端口 %d 被占用，激活改走窗口标题兜底"
                       % self.port)
            return
        srv.settimeout(0.5)
        self._srv = srv
        self._thread = threading.Thread(target=self._serve,
                                        name="si-activate", daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self._srv.accept()
            except (socket.timeout, OSError):
                continue
            try:
                conn.settimeout(1.0)
                data = conn.recv(32)
                if data.startswith(MAGIC):
                    try:
                        conn.sendall(REPLY)
                    except OSError:
                        pass
                    act = self._activator
                    if act is not None:
                        try:
                            act()
                        except Exception:               # noqa: BLE001
                            pass
                    else:
                        # 窗口还没建好：先记着，activator 设上时补一次
                        self._pending = True
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    # ------------------------------------------------------------ 对外
    def acquire(self):
        """True=本进程是第一个（通道已开）；False=已有实例（已尝试激活）。"""
        if not self._lock():
            self._activate_existing()
            return False
        self._start_server()
        return True

    def release(self):
        """优雅退出时调用：关通道、还锁。进程死亡时 OS 也会兜底释放。"""
        self._stop.set()
        srv, self._srv = self._srv, None
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass
        th, self._thread = self._thread, None
        if th is not None and th.is_alive():
            th.join(1.0)
        if self._mutex is not None:
            try:
                ctypes.windll.kernel32.CloseHandle(
                    ctypes.c_void_p(self._mutex))
            except Exception:                               # noqa: BLE001
                pass
            self._mutex = None
        if self._lock_fallback_sock is not None:
            try:
                self._lock_fallback_sock.close()
            except OSError:
                pass
            self._lock_fallback_sock = None

    def _warn(self, text):
        if self.log is not None:
            try:
                self.log.add("warn", text, "系统")
            except Exception:                               # noqa: BLE001
                pass

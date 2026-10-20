#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""win_tray.py —— Windows 任务栏通知区域（系统托盘）图标 ·  by xbzb

用 ctypes 直接调 Shell_NotifyIcon，纯标准库实现，不依赖 pystray / pywin32，
方便 PyInstaller 打包成单文件 exe。

用法（在 Tk 主线程里创建，并在主线程定期 poll）：

    tray = win_tray.TrayIcon(
        tooltip="FileShare · 就绪",
        menu=[("打开主界面", open_win),
              ("-", None),                      # 分隔线
              ("一键全同步", do_sync),
              ("退出", quit_app)],
        on_click=open_win,                      # 单击图标
        on_double=open_win,                     # 双击图标
        icon_path=r"icon.ico")              # 不给就从 exe / 系统取
    tray.show()

    def pump():                                 # ★ 必须：在 UI 线程定期调用
        tray.poll()
        root.after(100, pump)
    pump()

    tray.set_tooltip("FileShare · 传输中 42%")
    tray.notify("同步完成", "新增 12，跳过 3，失败 0")
    tray.hide()          # 退出前务必调用，否则图标会残留

────────────── 为什么回调要绕一圈 poll() 才执行（踩过的大坑）──────────────
窗口过程（WNDPROC）是 ctypes 回调，它被调用时，Tk 的 mainloop 已经释放了
GIL。此时如果在这个回调里**回调 Tcl/Tk**（例如 root.after、控件操作），
CPython 的线程状态会错乱并直接 abort：

    Fatal Python error: PyEval_RestoreThread: the function must be called
    with the GIL held, ... (the current Python thread state is NULL)

打包成 exe 后表现为 ucrtbase.dll 里 0xc0000409（fastfail）闪退，事件查看器
里能看到 BEX64 崩溃记录。
所以本模块的规矩是：
  · WNDPROC 里**只做 Win32 调用 + 置标志位 / 入队**，绝不碰 Tcl、绝不碰
    tkinter 变量，也不弹 TrackPopupMenu（避免在回调里起嵌套消息循环）；
  · 所有交给上层的事件都排进内部队列，由 UI 线程调用 poll() 取走执行。
只要遵守这条，托盘就和普通 Tk 代码一样安全。

其它实现要点：
  · Shell_NotifyIcon 需要真实 HWND：这里注册自定义窗口类并建一个「无样式」
    的隐藏窗口（不可见、不上任务栏、不进 Alt-Tab）。
  · 窗口建在 Tk 主线程 —— Tk 的事件循环就是本线程的 GetMessage /
    DispatchMessage，所以托盘消息会自动派发到 WNDPROC，无需另起消息循环。
  · 监听 "TaskbarCreated" 消息：Explorer 崩溃重启后自动把图标加回去。
  · 任何一步失败都只是没有托盘图标，绝不影响主程序（全部 try 兜底）。
"""

import os
import sys
import ctypes
import queue
import ctypes.wintypes as wt

__all__ = ["TrayIcon", "available"]

IS_WINDOWS = sys.platform.startswith("win")

# --------------------------------------------------------------- Win32 常量
WM_APP = 0x8000
WM_TRAY = WM_APP + 1            # 托盘回调消息（自定义）
WM_NULL = 0x0000
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO, NIIF_WARNING, NIIF_NOSOUND = 0x01, 0x02, 0x10

IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE = 0x0010, 0x0040
IDI_APPLICATION = 32516

TPM_LEFTALIGN, TPM_RIGHTBUTTON = 0x0000, 0x0002
TPM_RETURNCMD, TPM_NONOTIFY = 0x0100, 0x0080
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800

LRESULT = getattr(wt, "LRESULT", ctypes.c_ssize_t)
HCURSOR = getattr(wt, "HCURSOR", ctypes.c_void_p)
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    """Shell_NotifyIcon 用的 NOTIFYICONDATA（Unicode，V3 版结构）。"""

    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", wt.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wt.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", HCURSOR),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


# --------------------------------------------------------------- API 绑定
_user32 = _shell32 = _kernel32 = None
if IS_WINDOWS:
    try:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        _user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        _user32.RegisterClassW.restype = wt.ATOM
        _user32.CreateWindowExW.argtypes = [
            wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p]
        _user32.CreateWindowExW.restype = wt.HWND
        _user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        _user32.DefWindowProcW.restype = LRESULT
        _user32.DestroyWindow.argtypes = [wt.HWND]
        _user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT,
                                       ctypes.c_int, ctypes.c_int, wt.UINT]
        _user32.LoadImageW.restype = wt.HANDLE
        _user32.LoadIconW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
        _user32.LoadIconW.restype = wt.HICON
        _user32.CreatePopupMenu.restype = wt.HMENU
        _user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t,
                                        wt.LPCWSTR]
        _user32.AppendMenuW.restype = wt.BOOL
        _user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, wt.HWND,
                                           ctypes.c_void_p]
        _user32.TrackPopupMenu.restype = ctypes.c_int
        _user32.DestroyMenu.argtypes = [wt.HMENU]
        _user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
        _user32.SetForegroundWindow.argtypes = [wt.HWND]
        _user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        _user32.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
        _user32.RegisterWindowMessageW.restype = wt.UINT

        _shell32.Shell_NotifyIconW.argtypes = [wt.DWORD,
                                               ctypes.POINTER(NOTIFYICONDATAW)]
        _shell32.Shell_NotifyIconW.restype = wt.BOOL
        _shell32.ExtractIconExW.argtypes = [wt.LPCWSTR, ctypes.c_int,
                                            ctypes.POINTER(wt.HICON),
                                            ctypes.POINTER(wt.HICON), wt.UINT]
        _shell32.ExtractIconExW.restype = wt.UINT

        _kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
        _kernel32.GetModuleHandleW.restype = wt.HMODULE
    except Exception:                      # pragma: no cover
        _user32 = _shell32 = _kernel32 = None


def available():
    """当前环境能不能用托盘图标（非 Windows / 系统库缺失时为 False）。"""
    return bool(_user32 and _shell32 and _kernel32)


def _base_dir():
    """资源目录：打包后是解包目录，否则是脚本所在目录。"""
    if hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _load_icon(path=None):
    """取图标句柄：优先给定文件/同目录 ico → 再从 exe 里提取 → 系统默认图标。"""
    cands = [path, os.path.join(_base_dir(), "icon.ico")]
    for p in cands:
        if p and os.path.isfile(p):
            h = _user32.LoadImageW(None, p, IMAGE_ICON, 0, 0,
                                   LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h:
                return h
    if getattr(sys, "frozen", False):
        large, small = wt.HICON(), wt.HICON()
        if _shell32.ExtractIconExW(sys.executable, 0, ctypes.byref(large),
                                   ctypes.byref(small), 1):
            return large.value or small.value
    return _user32.LoadIconW(None, IDI_APPLICATION)


class TrayIcon:
    """任务栏通知区域图标（右键菜单 / 单击 / 双击 / 气泡提示）。

    事件不直接在窗口过程里执行，而是排进内部队列，由 UI 线程调用
    poll() 取走执行 —— 原因见模块开头「为什么回调要绕一圈 poll()」。
    """

    _class_registered = False
    _class_name = "FileShareTrayWndClass"

    def __init__(self, tooltip="FileShare 托盘", menu=None, on_click=None,
                 on_double=None, icon_path=None, uID=1):
        self.tooltip = str(tooltip or "")[:120]
        self.menu = list(menu or [])
        self.on_click = on_click
        self.on_double = on_double
        self.icon_path = icon_path
        self.uID = uID

        self.hwnd = None
        self.hicon = None
        self._nid = None
        self._wndproc = None      # 必须留引用，否则回调被 GC 掉会崩
        self._visible = False
        self._taskbar_created = 0

        # 事件出口：窗口过程只往这里塞，UI 线程用 poll() 取
        self._outbox = queue.Queue()
        self._want_popup = False  # 右键菜单请求（只置位，不在回调里弹）
        self._popping = False     # 菜单正在弹出：防止重入弹第二个

    # ---------------- 生命周期 ----------------
    def show(self):
        """创建隐藏窗口并挂上托盘图标；成功返回 True。"""
        if self._visible or not available():
            return False
        if not self._create_window():
            return False
        self.hicon = _load_icon(self.icon_path)
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = self.uID
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.hicon
        nid.szTip = self.tooltip
        if not _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            self._destroy_window()
            return False
        self._nid = nid
        self._visible = True
        return True

    def hide(self):
        """移除托盘图标（退出前务必调用，否则任务栏会残留幽灵图标）。"""
        if self._nid is not None:
            try:
                _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            except Exception:
                pass
        self._nid = None
        self._visible = False
        self._destroy_window()

    @property
    def visible(self):
        return self._visible

    def _create_window(self):
        if self.hwnd:
            return True
        try:
            hinst = _kernel32.GetModuleHandleW(None)
            self._wndproc = WNDPROC(self._on_message)
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = self._class_name
            if not TrayIcon._class_registered:
                _user32.RegisterClassW(ctypes.byref(wc))
                TrayIcon._class_registered = True
            # 不带任何 WS_ 样式 → 不可见、不上任务栏、不进 Alt-Tab，但能收消息
            self.hwnd = _user32.CreateWindowExW(
                0, self._class_name, "FileShare 托盘", 0, 0, 0, 0, 0,
                None, None, hinst, None)
            if not self.hwnd:
                return False
            self._taskbar_created = _user32.RegisterWindowMessageW("TaskbarCreated")
            return True
        except Exception:
            self.hwnd = None
            return False

    def _destroy_window(self):
        try:
            if self.hwnd:
                _user32.DestroyWindow(self.hwnd)
        except Exception:
            pass
        self.hwnd = None

    # ---------------- 外观 / 提示（UI 线程调用） ----------------
    def set_tooltip(self, text):
        """更新鼠标悬停提示（进度、状态都可以放这儿）。"""
        text = str(text or "")[:120]
        if text == self.tooltip and self._visible:
            return
        self.tooltip = text
        if not (self._visible and self._nid is not None):
            return
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = self.uID
        nid.uFlags = NIF_TIP
        nid.szTip = text
        try:
            _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:
            pass

    def notify(self, title, message, warning=False):
        """弹一个气泡通知（系统托盘提示）。"""
        if not (self._visible and self._nid is not None):
            return False
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = self.uID
        nid.uFlags = NIF_INFO
        nid.dwInfoFlags = (NIIF_WARNING if warning else NIIF_INFO) | NIIF_NOSOUND
        nid.szInfoTitle = str(title or "")[:62]
        nid.szInfo = str(message or "")[:250]
        try:
            return bool(_shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)))
        except Exception:
            return False

    # ---------------- 事件泵（UI 线程调用） ----------------
    def poll(self, limit=16):
        """★ 必须在 UI 线程（Tk 的 after 回调等）里定期调用。

        这里才会真正执行菜单回调 / 单击 / 双击，以及弹出右键菜单。
        返回本次处理的事件数。
        """
        done = 0
        # 1) 右键菜单（要求时且没在弹，就弹；弹的整个过程都在 UI 线程里）
        if self._want_popup and not self._popping:
            self._want_popup = False
            self._popping = True
            try:
                self._popup()
            except Exception:
                pass
            finally:
                self._popping = False
            done += 1
        # 2) 队列里的回调
        for _ in range(max(0, int(limit))):
            try:
                fn = self._outbox.get_nowait()
            except queue.Empty:
                break
            done += 1
            try:
                fn()
            except Exception:
                pass
        return done

    def _post(self, fn):
        """把回调排进队列（窗口过程里唯一允许的「出口」动作）。"""
        if fn is None:
            return
        try:
            self._outbox.put(fn)
        except Exception:
            pass

    # ---------------- 消息（窗口过程：只准 Win32 + 置位，不准碰 Tcl） ----------------
    def _on_message(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                ev = lparam & 0xFFFF
                if ev in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._want_popup = True       # 只置位，交给 poll() 弹
                elif ev == WM_LBUTTONDBLCLK:
                    self._post(self.on_double or self.on_click)
                elif ev == WM_LBUTTONUP:
                    self._post(self.on_click)
                return 0
            if self._taskbar_created and msg == self._taskbar_created:
                # 资源管理器重启过：把图标重新挂回去
                if self._visible and self._nid is not None:
                    _shell32.Shell_NotifyIconW(NIM_ADD,
                                               ctypes.byref(self._nid))
                return 0
        except Exception:
            pass
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _popup(self):
        """在鼠标处弹出右键菜单（UI 线程执行），选中项排进队列。"""
        if not self.hwnd:
            return
        hmenu = _user32.CreatePopupMenu()
        if not hmenu:
            return
        handlers = {}
        for i, item in enumerate(self.menu):
            label, cb = item if isinstance(item, (tuple, list)) else (item, None)
            if not cb or str(label).strip() in ("", "-", "--"):
                _user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                continue
            mid = 1000 + i
            handlers[mid] = cb
            _user32.AppendMenuW(hmenu, MF_STRING, mid, str(label))
        pt = wt.POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        # 先置前，否则点菜单外面菜单不收起（Windows 经典行为）
        _user32.SetForegroundWindow(self.hwnd)
        cmd = _user32.TrackPopupMenu(
            hmenu, TPM_LEFTALIGN | TPM_RIGHTBUTTON | TPM_RETURNCMD
            | TPM_NONOTIFY, pt.x, pt.y, 0, self.hwnd, None)
        _user32.DestroyMenu(hmenu)
        _user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        self._post(handlers.get(cmd))


# --------------------------------------------------------------- 自检
def _selftest():
    """命令行自检：挂上图标 3 秒（含提示 / 气泡），再移除。"""
    import time
    if not available():
        print("当前环境不支持托盘图标")
        return 1
    calls = []
    tray = TrayIcon(
        tooltip="FileShare · 自检中",
        menu=[("打开主界面", lambda: calls.append("open")),
              ("-", None),
              ("一键全同步", lambda: calls.append("sync")),
              ("退出", lambda: calls.append("quit"))],
        on_click=lambda: calls.append("click"),
        icon_path=os.path.join(_base_dir(), "icon.ico"))
    ok = tray.show()
    print("托盘图标挂载:", "成功" if ok else "失败")
    if not ok:
        return 1
    print("  hwnd =", tray.hwnd, "| 提示 =", tray.tooltip)
    tray.set_tooltip("FileShare · 传输中 42%")
    print("  改提示 =", tray.tooltip)
    print("  气泡通知:", "已弹出" if tray.notify("同步完成", "新增 12，跳过 3，失败 0")
                          else "失败")
    user32 = ctypes.windll.user32
    msg = wt.MSG()
    end = time.time() + 3.0
    while time.time() < end:                      # 简易消息泵，好让系统来取图标
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        tray.poll()                               # ★ UI 线程取事件
        time.sleep(0.02)
    tray.hide()
    print("托盘图标已移除 | 事件记录 =", calls)
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())

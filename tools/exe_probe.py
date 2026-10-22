# -*- coding: utf-8 -*-
"""打包版 GUI 检查：窗口是否正常出现、图标是否进了 exe、缩托盘是否生效。

    python tools/exe_probe.py [dist/FileShare.exe]

onefile 的 exe 是「引导器 → 子进程」，窗口属于子进程，所以一律按窗口标题
反查，不能拿 Popen 的 pid 去 EnumWindows。
"""

import ctypes
import os
import socket
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
SW_MINIMIZE = 6
SW_RESTORE = 9


def enum_top():
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _l):
        n = user32.GetWindowTextLengthW(hwnd)
        if not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append((hwnd, buf.value, bool(user32.IsWindowVisible(hwnd)), pid.value))
        return True

    user32.EnumWindows(cb, 0)
    return out


def image_name(pid):
    """取进程镜像文件名（小写）；失败返回 None。"""
    h = kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return None
        return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(h))


def find_main(exe_path=None):
    """Tk 会额外建一个同标题的隐藏「icon window」，必须挑可见的那个。

    ``exe_path`` 给了就按进程镜像名过滤：否则可能命中**用户自己开着的
    同名窗口**或并行探针拉起的实例（实测踩过——量到别人 pid 上，
    对方一退出 OpenProcess 就失败，CPU/内存全读成 None）。
    """
    fallback = None
    want = os.path.basename(exe_path).lower() if exe_path else None
    for hwnd, title, vis, pid in enum_top():
        if "文件共享中心" not in title:
            continue
        if want and image_name(pid) != want:
            continue
        if vis:
            return hwnd, title, vis, pid
        if fallback is None:
            fallback = (hwnd, title, vis, pid)
    return fallback or (None, None, None, None)


def grab(hwnd, path):
    from PIL import Image
    gdi32 = ctypes.windll.gdi32
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)
    user32.PrintWindow(hwnd, mdc, 2)

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    bi = BIH()
    bi.biSize = ctypes.sizeof(BIH)
    bi.biWidth = w
    bi.biHeight = -h
    bi.biPlanes = 1
    bi.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert(
        "RGB").save(path)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(hwnd, hdc)
    return True


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        ROOT, "dist", "FileShare.exe")
    if not os.path.isfile(exe):
        print("找不到", exe)
        return 2
    tmp = tempfile.mkdtemp(prefix="fs_exe_")
    env = dict(os.environ)
    env["APPDATA"] = tmp
    env["LOCALAPPDATA"] = tmp
    # 单实例隔离：别被用户自己开着的实例挡掉，也别去激活用户的窗口
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    act_port = s.getsockname()[1]
    s.close()
    env["FILESHARE_MUTEX"] = "Local\\FileShare_ExeProbe_%d" % os.getpid()
    env["FILESHARE_ACT_PORT"] = str(act_port)
    print("启动", exe)
    proc = subprocess.Popen([exe], env=env, cwd=ROOT)
    ok = True
    hwnd = None
    try:
        end = time.time() + 30
        while time.time() < end:
            hwnd, title, vis, _pid = find_main(exe)
            if hwnd and vis:
                break
            time.sleep(0.4)
        if not hwnd:
            print("!! 30 秒内没有出现主窗口")
            return 1
        print("主窗口：%r  visible=%s  hwnd=%s" % (title, vis, hwnd))
        time.sleep(2.0)
        out = os.path.join(ROOT, "screenshots", "exe_main.png")
        print("截图 →", out if grab(hwnd, out) else "失败")

        # 最小化 → 应该被收进托盘（IsWindowVisible 变 0）
        user32.ShowWindow(hwnd, SW_MINIMIZE)
        time.sleep(1.5)
        vis2 = bool(user32.IsWindowVisible(hwnd))
        print("最小化后 IsWindowVisible =", vis2, "（期望 False，说明已收进托盘）")
        if vis2:
            ok = False

        # 从托盘唤回：给窗口发 SW_RESTORE 不行（已隐藏），直接看进程还在
        if proc.poll() is not None:
            print("!! 进程已退出，pid 状态 =", proc.returncode)
            ok = False
        else:
            print("进程仍在运行 pid=%s（托盘图标应可见）" % proc.pid)
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n%s" % ("EXE PROBE PASS" if ok else "EXE PROBE FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

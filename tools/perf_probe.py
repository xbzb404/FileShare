# -*- coding: utf-8 -*-
"""性能实测：空闲与实时刷新时占多少 CPU / 内存。

    python tools/perf_probe.py [dist/FileShare.exe]

界面每 2 秒重绘一次概览（实时曲线），这是最吃 CPU 的常态；这里量一下
平均 CPU 占用，避免「看着好看但一直烧 CPU」。
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
sys.path.insert(0, ROOT)

k32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def ft_seconds(ft):
    return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7


def cpu_seconds(pid):
    h = k32.OpenProcess(0x1000, False, pid)      # QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                   ctypes.byref(k), ctypes.byref(u)):
            return None
        return ft_seconds(k) + ft_seconds(u)
    finally:
        k32.CloseHandle(h)


def mem_mb(pid):
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return None
    try:
        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(pmc)
        if not psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            return None
        return pmc.WorkingSetSize / 1048576.0
    finally:
        k32.CloseHandle(h)


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        ROOT, "dist", "FileShare.exe")
    tmp = tempfile.mkdtemp(prefix="fs_perf_")
    env = dict(os.environ)
    env["APPDATA"] = tmp
    env["LOCALAPPDATA"] = tmp
    # 单实例隔离：别被用户自己开着的实例挡掉
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    act_port = s.getsockname()[1]
    s.close()
    env["FILESHARE_MUTEX"] = "Local\\FileShare_Perf_%d" % os.getpid()
    env["FILESHARE_ACT_PORT"] = str(act_port)
    t0 = time.time()
    proc = subprocess.Popen([exe], env=env, cwd=ROOT)

    from tools.exe_probe import find_main
    hwnd = None
    end = time.time() + 30
    while time.time() < end:
        hwnd, title, vis, pid = find_main(exe)
        if hwnd and vis:
            break
        time.sleep(0.3)
    if not hwnd:
        print("窗口没出来")
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
        return 1
    startup = time.time() - t0
    print("启动到窗口可见：%.2f 秒" % startup)

    time.sleep(4.0)                                   # 让自动启动的服务就绪
    c1 = cpu_seconds(pid)
    m1 = mem_mb(pid)
    print("内存：%.1f MB（工作集）" % (m1 or 0))
    print("采样 20 秒空闲（概览页每 2 秒重绘一次）…")
    t = time.time()
    time.sleep(20)
    c2 = cpu_seconds(pid)
    m2 = mem_mb(pid)
    dt = time.time() - t
    cpu = (c2 - c1) / dt * 100.0 if (c1 is not None and c2 is not None) else -1
    print("平均 CPU 占用：%.1f%%（单核百分比）" % cpu)
    print("20 秒后内存：%.1f MB" % (m2 or 0))

    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                   capture_output=True)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    ok = 0 <= cpu < 6.0 and (m2 or 999) < 160
    print("\n%s" % ("PERF OK（空闲 CPU < 6%，内存 < 160 MB）" if ok else "PERF 偏高"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

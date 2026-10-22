# -*- coding: utf-8 -*-
"""单实例 + 优雅退出专项探针  ·  by zhb

背景（用户原话）：「应用启动时应当检查是否已经开启应用，开启应用就应该
直接把已经启动的应用界面打开，而不是重复创建一个新程序进程打开……并且
退出时也应当优雅退出，避免出现残留。」

两段测试：
  第一段（进程内）：互斥体互斥、TCP 激活通道端到端、释放后可重新抢占、
            端口被无关程序占用时不误判、按标题置前台、叫门待机补齐。
  第二段（真实子进程）：隔离配置（APPDATA/FILESHARE_MUTEX/FILESHARE_ACT_PORT）
            连开两个 main.py —— 第二个必须秒退、不建窗口；第一个被最小化
            后要被第二个「叫」回前台；WM_CLOSE 后第一个必须自己退干净；
            进程死后互斥体必须还回去（下一个实例能正常抢到）。

    python tools/single_instance_probe.py
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

OUT = os.path.join(ROOT, "screenshots", "_sitest")
os.makedirs(OUT, exist_ok=True)

TITLE_HINT = "文件共享中心"
FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))
    if not cond:
        FAILS.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ------------------------------------------------------------ Win32 小工具
def find_window_by_pid(pid, title_hint=TITLE_HINT):
    """按进程 PID 找它的主窗口（躲开用户自己开着的同名窗口）。"""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if title_hint not in buf.value:
            return True
        p = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid:
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def is_minimized(hwnd):
    import ctypes
    return bool(ctypes.windll.user32.IsIconic(hwnd))


def minimize(hwnd):
    import ctypes
    ctypes.windll.user32.ShowWindow(hwnd, 6)        # SW_MINIMIZE


def wm_close(hwnd):
    import ctypes
    ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE


def process_start(pid):
    """进程创建时间（epoch 秒）；失败返回 None。"""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return None
    try:
        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                   ctypes.byref(k), ctypes.byref(u)):
            return None
        return (((c.dwHighDateTime << 32) | c.dwLowDateTime) / 1e7
                - 11644473600.0)
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


def find_our_window(t0, want_pid=None, want_image=None, exclude=None):
    """找「本次探针拉起的」主窗口。

    源码版：窗口属于 Popen 的 pid，直接按 pid 找；
    打包版（onefile）：窗口属于引导器拉起的子进程，按「镜像名 +
    创建时间晚于 t0」认领——避免命中用户自己开着的同名窗口。
    ``exclude``：已知属于第一个实例的 hwnd，认领时跳过它。
    """
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    cands = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if TITLE_HINT not in buf.value:
            return True
        p = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        cands.append((hwnd, p.value))
        return True

    user32.EnumWindows(cb, 0)
    for hwnd, pid in cands:
        if exclude is not None and hwnd == exclude:
            continue
        if want_pid is not None:
            if pid == want_pid:
                return hwnd
            continue
        st = process_start(pid)
        if st is not None and st >= t0 - 2.0:
            if want_image:
                from tools.exe_probe import image_name
                if image_name(pid) == want_image:
                    return hwnd
            else:
                return hwnd
    return None


# ------------------------------------------------------------ 第一段：进程内
def part_inprocess():
    print("== 第一段：进程内 ==")
    from fileshare.single_instance import SingleInstance, raise_by_title

    mutex = "Local\\FileShare_Probe_A_%d" % os.getpid()
    port = free_port()
    fired = threading.Event()

    si1 = SingleInstance(title="探针窗口A", port=port, mutex_name=mutex)
    check("1. 首个实例抢到锁", si1.acquire())

    si1.activator = lambda: fired.set()
    si2 = SingleInstance(title="探针窗口A", port=port, mutex_name=mutex)
    check("2. 第二个实例抢不到锁", si2.acquire() is False)
    check("3. TCP 激活通道端到端（叫门到达）", fired.wait(3.0))

    # 叫门待机补齐：activator 还没设上时来叫门，设上后要补一次
    si1.activator = None
    fired.clear()
    si2.release()
    si3 = SingleInstance(title="探针窗口A", port=port, mutex_name=mutex)
    si3.acquire()                       # 又叫了一次门（activator=None → 待机）
    time.sleep(0.3)
    pending_ok = True
    si1.activator = lambda: fired.set()
    check("4. 窗口未就绪时的叫门被补齐", fired.wait(1.0))
    si3.release()

    # 释放后可重新抢占
    si1.release()
    si4 = SingleInstance(title="探针窗口A", port=port, mutex_name=mutex)
    check("5. 释放后锁可重新抢占", si4.acquire())
    si4.release()

    # 端口被无关程序占用：不误判「已在运行」，激活通道自动让位
    thief = socket.socket()
    thief.bind(("127.0.0.1", port))
    thief.listen(1)
    si5 = SingleInstance(title="探针窗口A", port=port, mutex_name=mutex)
    check("6. 端口被占仍能启动（互斥体判定不受影响）", si5.acquire())
    si6 = SingleInstance(title="探针窗口A", port=port, mutex_name=mutex)
    check("7. 端口被占时第二个实例仍被拒（不误放行）",
          si6.acquire() is False)
    si5.release()
    thief.close()

    # raise_by_title：真实窗口 + withdraw 状态也能找到并置前台
    import tkinter as tk
    root = tk.Tk()
    root.title("探针窗口B-唯一标题-%d" % os.getpid())
    root.geometry("300x200")
    root.update()
    root.withdraw()
    root.update()
    ok = raise_by_title("探针窗口B-唯一标题-%d" % os.getpid())
    check("8. 按标题置前台（withdraw 窗口也可找）", ok)
    root.destroy()

    # 无关标题不能误命中
    check("9. 无关标题不误命中",
          raise_by_title("绝对不存在的窗口标题-xyz") is False)


# ------------------------------------------------------------ 第二段：子进程
def part_subprocess(exe=None):
    print("== 第二段：真实子进程 ==")
    from fileshare import window_title

    tmp = tempfile.mkdtemp(prefix="fs_si_", dir=OUT)
    appdata = os.path.join(tmp, "AppData")
    os.makedirs(appdata, exist_ok=True)
    dldir = os.path.join(tmp, "downloads")
    os.makedirs(dldir, exist_ok=True)
    # 预置隔离配置：服务全关、下载目录指到临时目录，绝不碰用户真实配置
    cfg_dir = os.path.join(appdata, "FileShare")
    os.makedirs(cfg_dir, exist_ok=True)
    with open(os.path.join(cfg_dir, "config.json"), "w", encoding="utf-8") as fp:
        json.dump({
            "schema": 3, "shares": [],
            "listen": {"ip": "0.0.0.0", "discovery_port": 47610},
            "http": {"enabled": False, "port": 8080},
            "ftp": {"enabled": False, "port": 2121},
            "smb": {"enabled": False},
            "limit": {"up_kbps": 0, "down_kbps": 0},
            "download": {"dir": dldir, "threads": 4},
            "ui": {"close_action": "", "theme": "light"},
        }, fp)

    mutex = "Local\\FileShare_Probe_B_%d" % os.getpid()
    port = free_port()
    env = dict(os.environ)
    env["APPDATA"] = appdata
    env["FILESHARE_MUTEX"] = mutex
    env["FILESHARE_ACT_PORT"] = str(port)

    if exe:
        cmd = [exe, "--no-tray"]
        what = "打包版 %s" % exe
    else:
        cmd = [sys.executable, os.path.join(ROOT, "main.py"), "--no-tray"]
        what = "源码版 main.py"

    def launch():
        return subprocess.Popen(
            cmd, cwd=ROOT, env=env,
            stdout=open(os.path.join(tmp, "out.log"), "a"),
            stderr=subprocess.STDOUT)

    try:
        t_start = time.time()
        p1 = launch()
        hwnd = None
        for _ in range(150):                       # 最多等 15 秒出窗口
            if p1.poll() is not None:
                break
            hwnd = (find_window_by_pid(p1.pid) if not exe
                    else find_our_window(t_start, want_image="fileshare.exe"))
            if hwnd:
                break
            time.sleep(0.1)
        check("10. 第一个实例正常起窗口（%s）" % what,
              hwnd is not None and p1.poll() is None,
              "pid=%d hwnd=%s" % (p1.pid, hex(hwnd or 0)))

        if hwnd:
            minimize(hwnd)
            time.sleep(0.3)
            was_min = is_minimized(hwnd)

            t0 = time.time()
            p2 = launch()
            try:
                rc2 = p2.wait(timeout=15)
            except subprocess.TimeoutExpired:
                rc2 = None
                p2.kill()
            dt = time.time() - t0
            check("11. 第二个实例秒退（不建新进程窗口）",
                  rc2 == 0 and dt < 10,
                  "exit=%s 耗时=%.2fs" % (rc2, dt))
            # 第二个实例绝不能留下自己的窗口（onefile 是子进程，按启动时间认领）
            check("12. 第二个实例没有留下窗口",
                  find_our_window(t0, exclude=hwnd) is None)

            restored = False
            for _ in range(50):                    # 最多等 5 秒被叫回前台
                if not is_minimized(hwnd):
                    restored = True
                    break
                time.sleep(0.1)
            check("13. 第一个实例被第二个「叫」回前台",
                  restored and was_min,
                  "曾被最小化=%s 现已恢复=%s" % (was_min, restored))

            # 优雅退出：WM_CLOSE（无托盘 → 直接干净退出）
            wm_close(hwnd)
            try:
                rc1 = p1.wait(timeout=15)
            except subprocess.TimeoutExpired:
                rc1 = None
                p1.kill()
            check("14. 优雅退出（WM_CLOSE 后自己退干净）",
                  rc1 == 0, "exit=%s" % rc1)

        # 进程死后互斥体必须还回去（无残留）
        time.sleep(0.5)
        from fileshare.single_instance import SingleInstance
        si = SingleInstance(title=window_title(), port=port,
                            mutex_name=mutex)
        check("15. 进程退出后锁被释放（无 stale lock）", si.acquire())
        si.release()
    finally:
        for p in (p1, p2):
            try:
                if p.poll() is None:
                    p.kill()
            except Exception:                                   # noqa: BLE001
                pass
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:                                       # noqa: BLE001
            pass


def main():
    exe = None
    if "--exe" in sys.argv:
        idx = sys.argv.index("--exe")
        exe = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else os.path.join(
            ROOT, "dist", "FileShare.exe")
        if not os.path.isfile(exe):
            print("找不到", exe)
            return 2
    part_inprocess()
    part_subprocess(exe)
    print("\n%s" % ("SINGLE-INSTANCE PROBE PASS" if not FAILS
                    else "SINGLE-INSTANCE PROBE FAIL: %s" % FAILS))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())

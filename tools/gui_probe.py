# -*- coding: utf-8 -*-
"""GUI 探针：真实创建窗口 → 逐页渲染 → 统计绘制元素 → 截图。

    python tools/gui_probe.py [输出目录]

必须在**系统 Python**（带 tkinter）下运行：
    C:/Users/<你>/AppData/Local/Microsoft/WindowsApps/python.exe

判定标准：每页 ``items > 20`` 且 ``PROBE PASS``。日志页用的是 Text 控件，
单独检查其行数。
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "screenshots")


def find_window(title_part):
    import ctypes
    from ctypes import wintypes
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _l):
        n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
            if title_part in buf.value and ctypes.windll.user32.IsWindowVisible(hwnd):
                found.append((hwnd, buf.value))
        return True

    ctypes.windll.user32.EnumWindows(cb, 0)
    return found


def grab(hwnd, path):
    """PrintWindow 截图（窗口被遮挡也能拿到内容）。"""
    import ctypes
    from ctypes import wintypes
    from PIL import Image

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return False
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)
    user32.PrintWindow(hwnd, mdc, 2)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth = w
    bi.biHeight = -h
    bi.biPlanes = 1
    bi.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
    img.convert("RGB").save(path)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(hwnd, hdc)
    return True


def main():
    os.makedirs(OUT, exist_ok=True)
    from fileshare.config import Config
    from fileshare.ui.app import App, NAV
    import tkinter as tk

    # 用临时配置，绝不动用户真实设置
    tmpcfg = os.path.join(OUT, "_probe_config.json")
    for p in (tmpcfg, tmpcfg + ".tmp",
              # 下载器的任务库会落在配置同目录，不清掉会把真实任务
              # （含本机路径）拍进截图
              os.path.join(OUT, "downloads.json")):
        if os.path.exists(p):
            os.remove(p)
    cfg = Config(path=tmpcfg)
    cfg.data["shares"] = [
        {"name": "media", "path": ROOT, "enabled": True},
        {"name": "docs", "path": os.path.expanduser("~"), "enabled": True},
    ]
    cfg.data["http"]["enabled"] = False
    cfg.data["ftp"]["enabled"] = False

    app = App(cfg)
    root = app.root
    root.update()

    fails = []
    for key, _icon, title in NAV:
        app.show_page(key)
        root.update()
        root.update_idletasks()
        if key == "logs":
            app.log.add("info", "探针：这是一行测试日志", "PROBE")
            app.log.add("ok", "探针：服务启动成功", "HTTP")
            app._sync_log(force=True)
            root.update()
            lines = int(app.logtext.index("end-1c").split(".")[0])
            print("  %-9s log_lines=%d" % (key, lines))
            if lines < 2:
                fails.append(key)
        else:
            items = len(app.scroll.canvas.find_all())
            print("  %-9s items=%d" % (key, items))
            if key == "peers":
                # 空状态页画布项很少，改判关键文本
                texts = set()
                for it in app.scroll.canvas.find_withtag("all"):
                    try:
                        texts.add(app.scroll.canvas.itemcget(it, "text"))
                    except Exception:
                        pass
                if not ({"共享网络", "重新扫描"} & texts
                        and ({"没有发现其他主机", "正在扫描局域网…"} & texts)):
                    fails.append(key)
            elif items < 20:
                fails.append(key)
        time.sleep(0.35)
        root.update()
        wins = find_window("文件共享中心")
        if wins:
            path = os.path.join(OUT, "%s.png" % key)
            if grab(wins[0][0], path):
                print("          截图 %s" % os.path.basename(path))

    root.destroy()
    print("\n%s" % ("PROBE PASS" if not fails else "PROBE FAIL: %s" % fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())

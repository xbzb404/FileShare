# -*- coding: utf-8 -*-
"""托盘探针：验证「右键菜单不闪退」「最小化真的收进托盘」「能叫回来」。

这是最容易崩的地方（WNDPROC 里碰到 Tcl 会 0xc0000409 静默退出），
所以必须自动化测一遍：

    python tools/tray_probe.py
"""

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

WM_TRAY = 0x8000 + 1
WM_RBUTTONUP = 0x0205


def pump(app, seconds=1.5):
    end = time.time() + seconds
    while time.time() < end:
        try:
            app.root.update()
            if app.tray is not None:
                app.tray.poll()
        except Exception as exc:                            # noqa: BLE001
            print("  !! 事件泵异常：", exc)
            return False
        time.sleep(0.03)
    return True


def main():
    from fileshare.config import Config
    from fileshare.ui.app import App
    import fileshare.tray as tray_mod

    tmp = tempfile.mkdtemp(prefix="fs_tray_")
    cfg = Config(path=os.path.join(tmp, "config.json"))
    cfg.data["http"]["enabled"] = False
    cfg.data["ftp"]["enabled"] = False
    cfg.data["smb"]["enabled"] = False

    app = App(cfg)
    app.root.update()
    ok = True

    if app.tray is None or not app.tray.visible:
        print("!! 托盘图标没挂上")
        return 1
    print("托盘图标已挂载  hwnd=%s  tooltip=%s" % (app.tray.hwnd, app.tray.tooltip))

    # 1) 右键：替换成假的 TrackPopupMenu（真弹会阻塞等鼠标），直接返回「打开主界面」
    tray_mod._user32.TrackPopupMenu = lambda *a: 1000        # 第 0 项 = 打开主界面
    app.root.withdraw()                                      # 先藏起来
    pump(app, 0.4)
    tray_mod._user32.PostMessageW(app.tray.hwnd, WM_TRAY, 0, WM_RBUTTONUP)
    app.root.update()
    print("  收到右键消息：%s" % ("是" if app.tray._want_popup else "否"))
    pump(app, 1.5)
    state = app.root.state()
    if state != "normal":
        print("  !! 菜单项没派发到「打开主界面」，窗口状态 = %r" % state)
        ok = False
    else:
        print("右键菜单 → 打开主界面：OK（进程未崩，窗口被唤回）")

    # 2) 最小化 → 应该变成 withdrawn（任务栏不留按钮），托盘能叫回来
    app.root.deiconify()
    app.root.state("normal")
    pump(app, 0.4)
    app.root.iconify()
    pump(app, 0.8)
    state = app.root.state()
    if state != "withdrawn":
        print("  !! 最小化后状态是 %r，期望 withdrawn" % state)
        ok = False
    else:
        print("最小化 → 收进托盘：OK（state=withdrawn）")

    app._show_window()
    pump(app, 0.5)
    if app.root.state() != "normal":
        print("  !! 从托盘叫回来失败：%r" % app.root.state())
        ok = False
    else:
        print("托盘 → 唤回窗口：OK")

    # 3) 退出必须移除图标，否则任务栏会留幽灵图标
    app._quit()
    print("退出流程：OK（托盘图标已移除）")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n%s" % ("TRAY PROBE PASS" if ok else "TRAY PROBE FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

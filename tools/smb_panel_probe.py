# -*- coding: utf-8 -*-
"""临时探针：验证 SMB 面板在两种模式下渲染正常（含凭据行、复制按钮）。"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fileshare.config import Config          # noqa: E402
from fileshare.ui.app import App             # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  [OK]   " if ok else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not ok:
        FAILS.append(name)


def build(mode):
    tmp = tempfile.mkdtemp(prefix="fs_panel_")
    cfg = Config(path=os.path.join(tmp, "config.json"))
    cfg.data["http"]["enabled"] = False
    cfg.data["ftp"]["enabled"] = False
    cfg.data["smb"]["enabled"] = False
    cfg.data["smb"]["mode"] = mode
    cfg.data["smb"]["password"] = "fileshare"
    cfg.data["shares"] = [{"name": "Fshare", "path": tmp, "enabled": True}]
    cfg.data["download"]["dir"] = tmp
    app = App(cfg)
    root = app.root
    root.geometry("1180x900")
    root.update()

    # 让面板以为共享已开（非管理员环境建不了真共享，这里只验渲染）
    real_status = app.manager.status

    def patched():
        st = real_status()
        st["admin"] = True
        st["smb"]["on"] = True
        st["smb"]["url"] = "\\\\192.168.49.253\\Fshare"
        st["smb"]["user"] = "FileShare"
        st["smb"]["password"] = "fileshare"
        return st

    app.manager.status = patched
    app.show_page("servers")
    for _ in range(30):
        app._poll()
        root.update()
        time.sleep(0.05)
    app.scroll.canvas.yview_moveto(1.0)
    app.scroll.refresh()
    root.update()
    return app, root


def texts(app):
    c = app.scroll.canvas
    return [c.itemcget(i, "text") for i in c.find_all() if c.type(i) == "text"]


def run(mode):
    print("== 模式 %s ==" % mode)
    app, root = build(mode)
    try:
        c = app.scroll.canvas
        ts = texts(app)
        joined = " | ".join(ts)
        if mode == "account":
            check("凭据行可见", any("用户名  FileShare" in t for t in ts),
                  "找到：%s" % [t for t in ts if "用户名" in t][:1])
            check("提示弹凭据框", any("输入网络凭据" in t for t in ts))
            check("提示带共享名", any("只输" in t and "枚举" in t for t in ts))
            check("有复制按钮", any("复制凭据" in t for t in ts))
        else:
            check("免密被拦时给换模式按钮",
                  any("改用账号模式" in t for t in ts))
            check("免密文案已改（不推荐）",
                  any("不推荐" in t for t in ts))
        # 几何：所有文字不越出画布宽度
        w = c.winfo_width()
        over = []
        for i in c.find_all():
            if c.type(i) != "text":
                continue
            bb = c.bbox(i)
            if bb and bb[2] > w + 2:
                over.append((c.itemcget(i, "text")[:20], bb[2], w))
        check("文字不越界", not over, str(over[:2]))
        print("  画布元素 %d 个，region=%s viewport=%d"
              % (len(c.find_all()), c.cget("scrollregion"), c.winfo_height()))
    finally:
        app._quit()


if __name__ == "__main__":
    run("account")
    run("everyone")
    print("\n%s" % ("PANEL PROBE PASS" if not FAILS else "PANEL PROBE FAIL: %s" % FAILS))
    sys.exit(0 if not FAILS else 1)

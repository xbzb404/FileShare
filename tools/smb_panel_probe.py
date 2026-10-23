# -*- coding: utf-8 -*-
"""SMB 面板探针：只保留「指定账户」后，面板该显示什么、不该显示什么。

跑法：``python tools/smb_panel_probe.py``
"""
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
    print(("  [OK]   " if ok else "  [FAIL] ") + name
          + (("  " + extra) if extra else ""))
    if not ok:
        FAILS.append(name)


def build(hardened=True):
    tmp = tempfile.mkdtemp(prefix="fs_panel_")
    cfg = Config(path=os.path.join(tmp, "config.json"))
    cfg.data["http"]["enabled"] = False
    cfg.data["ftp"]["enabled"] = False
    cfg.data["smb"]["enabled"] = False
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
        st["smb"]["hardened"] = hardened
        st["smb"]["dirs"] = ["Fshare"]
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


def run():
    print("== 指定账户模式 ==")
    app, root = build(hardened=True)
    try:
        c = app.scroll.canvas
        ts = texts(app)
        check("凭据行可见", any("用户名  FileShare" in t for t in ts),
              "找到：%s" % [t for t in ts if "用户名" in t][:1])
        check("提示弹凭据框", any("输入网络凭据" in t for t in ts))
        check("提示带共享名", any("只输" in t and "枚举" in t for t in ts))
        check("有复制按钮", any("复制凭据" in t for t in ts))
        check("已加固时说明禁止哪些登录",
              any("禁止系统登录" in t and "计划任务" in t for t in ts))
        check("已加固时说明权限范围",
              any("权限仅限指定目录" in t and "Fshare" in t for t in ts))
        check("不再有免密开关", not any("免密只读" in t for t in ts))
        check("不再有换模式按钮", not any("改用账号模式" in t for t in ts))
        check("写明只能用这个账户", any("只能用这个账户访问" in t for t in ts))

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

    print("\n== 未加固时 ==")
    app2, _root2 = build(hardened=False)
    try:
        ts2 = texts(app2)
        check("未加固时给提示", any("还能用于本地或远程桌面登录" in t
                                   for t in ts2))
    finally:
        app2._quit()

    print("\n%s" % ("PANEL PROBE PASS" if not FAILS
                    else "PANEL PROBE FAIL: %s" % FAILS))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(run())

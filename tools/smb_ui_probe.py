# -*- coding: utf-8 -*-
"""临时：验证 SMB 新控件的真实行为（模式切换、密码落盘、面板内容）。

注意：设置页本来就有端口、FTP 账号等输入框，所以「输入框个数」要用
两种模式之差来比较，不能直接断言等于 2。
"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fileshare.config import Config
from fileshare.ui.app import App
from fileshare.widgets import LineEntry

FAIL = []


def check(name, ok, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  " + detail) if detail else ""))
    if not ok:
        FAIL.append(name)


def build(mode):
    tmp = tempfile.mkdtemp(prefix="fs_beh_%s_" % mode)
    cfg = Config(path=os.path.join(tmp, "config.json"))
    cfg.data["http"]["enabled"] = False
    cfg.data["ftp"]["enabled"] = False
    cfg.data["smb"]["enabled"] = False
    cfg.data["smb"]["mode"] = mode
    cfg.data["smb"]["user"] = "FileShare"
    cfg.data["smb"]["password"] = "FileShare@123"
    cfg.data["shares"] = [{"name": "demo", "path": tmp, "enabled": True}]
    cfg.data["download"]["dir"] = tmp
    app = App(cfg)
    app.root.update()
    app.show_page("servers")
    for _ in range(40):
        app._poll()
        app.root.update()
        time.sleep(0.04)
    n_entry = len([w for w in app._embedded if isinstance(w, LineEntry)])
    texts = " ".join(
        app.scroll.canvas.itemcget(it, "text")
        for it in app.scroll.canvas.find_all()
        if app.scroll.canvas.type(it) == "text")
    return app, cfg, n_entry, texts


print("== 账号模式 ==")
app_a, cfg_a, n_acc, text_a = build("account")
print("  输入框数量:", n_acc)
check("面板不再有免密开关", "免密只读" not in text_a)
check("面板有 SMB 密码复杂度提示", "密码需满足系统复杂度" in text_a)

app_a._set_smb_cred("password", "MyNewPass@2026")
check("自定义密码已落盘", cfg_a.get("smb", "password") == "MyNewPass@2026",
      cfg_a.get("smb", "password"))
app_a._set_smb_cred("user", "MyShare")
check("自定义用户名已落盘", cfg_a.get("smb", "user") == "MyShare")
app_a._set_smb_cred("user", "bad name")
check("带空格用户名被拒绝", cfg_a.get("smb", "user") == "MyShare")
app_a._quit()

print("\n== 只保留指定账户 ==")
check("面板写明只能用这个账户", "只能用这个账户访问" in text_a)
check("用户名改动已生效", (cfg_a.get("smb", "user") or "") == "MyShare")

print("\n%s" % ("SMB 控件行为 PASS" if not FAIL else "FAIL: %s" % FAIL))
sys.exit(0 if not FAIL else 1)

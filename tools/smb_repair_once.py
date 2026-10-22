# -*- coding: utf-8 -*-
"""以管理员身份跑一次 SMB 体检/修复，验证局域网访问能不能通。

用法（需管理员）：
    python tools/smb_repair_once.py E:\\Fshare                # 免密模式体检+修复
    python tools/smb_repair_once.py E:\\Fshare --account      # 账号模式
    python tools/smb_repair_once.py E:\\Fshare --revoke       # 取消共享并还原权限
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fileshare.server.smb import SmbManager


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    path = args[0] if args else ""
    user = os.environ.get("FILESHARE_SMB_USER", "FileShare")
    password = os.environ.get("FILESHARE_SMB_PASS", "fileshare")

    m = SmbManager()
    shares = [{"name": os.path.basename(path.rstrip("\\/")) or "share",
               "path": path}] if path else []

    if "--revoke" in flags:
        print("== 取消共享并还原权限 ==")
        done, failed = m.remove_all(restore=True, principals=[user],
                                    paths=[path] if path else [])
        print("  还原:", done or "（没有需要还原的授权）")
        for f in failed:
            print("  ✗", f)
        return 0 if not failed else 1

    print("管理员:", m.available())
    print("账户:", user)

    print("\n== 修复前体检 ==")
    for i in m.diagnose(shares, user=user):
        print("  [%s] %s" % (i["level"], i["title"]))

    print("\n== 执行修复 ==")
    done, failed = m.repair(shares, readonly=True,
                            user=user, password=password)
    for d in done:
        print("  ✓", d)
    for f in failed:
        print("  ✗", f)

    print("\n== 修复后体检 ==")
    issues = m.diagnose(shares, user=user)
    if not issues:
        print("  全部正常")
    for i in issues:
        print("  [%s] %s" % (i["level"], i["title"]))
    print("\n账户:", m.account_state())
    return 0 if not [i for i in issues if i["level"] == "error"] else 1


if __name__ == "__main__":
    sys.exit(main())

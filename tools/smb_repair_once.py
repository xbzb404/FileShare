# -*- coding: utf-8 -*-
"""以管理员身份跑 SMB 修复，验证修复真的能打通局域网访问。

用法（需管理员）：
    python tools/smb_repair_once.py E:\\Fshare
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fileshare.server.smb import SmbManager


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else ""
    m = SmbManager()
    print("管理员:", m.available())

    shares = [{"name": os.path.basename(path.rstrip("\\/")) or "share",
               "path": path}] if path else []

    print("\n== 修复前体检 ==")
    for i in m.diagnose(shares):
        print("  [%s] %s" % (i["level"], i["title"]))

    print("\n== 执行修复 ==")
    done, failed = m.repair(shares, readonly=True)
    for d in done:
        print("  ✓", d)
    for f in failed:
        print("  ✗", f)

    print("\n== 修复后体检 ==")
    issues = m.diagnose(shares)
    if not issues:
        print("  全部正常")
    for i in issues:
        print("  [%s] %s" % (i["level"], i["title"]))
    print("\n账户:", m.account_state())
    return 0 if not [i for i in issues if i["level"] == "error"] else 1


if __name__ == "__main__":
    sys.exit(main())

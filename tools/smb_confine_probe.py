# -*- coding: utf-8 -*-
"""真机验证「共享账户只认指定目录、目录换了权限跟着换」。

不打桩：真的调 ``icacls`` 写真实 NTFS ACL，再用 ``icacls`` 读回来核对。
用 ``Everyone`` 当授权对象，免去创建本地账户（临时目录的属主本来就有
改 DAC 的权限，不需要管理员）。

跑法：
    python tools/smb_confine_probe.py
"""

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fileshare.server.smb import SmbManager, run_hidden   # noqa: E402

FAILS = []
WHO = "Everyone"


def check(name, ok, extra=""):
    print(("  [OK]   " if ok else "  [FAIL] ") + name
          + (("  " + extra) if extra else ""))
    if not ok:
        FAILS.append(name)


def icacls(path):
    rc, out = run_hidden(["icacls", path], timeout=20)
    return out


def main():
    m = SmbManager()
    a = tempfile.mkdtemp(prefix="fs_conf_a_")
    b = tempfile.mkdtemp(prefix="fs_conf_b_")
    try:
        print("== 1. 授权到目录 A ==")
        ok, why = m.grant_ntfs(a, readonly=True, account=WHO, force=True)
        check("写显式 ACE 成功", ok, str(why))
        check("读回的级别是只读", m._explicit_rights(a, WHO) == "(OI)(CI)(RX)",
              str(m._explicit_rights(a, WHO)))
        check("has_explicit_grant 认得出来", m.has_explicit_grant(a, WHO))
        print("  icacls A:\n%s" % "\n".join(
            "    " + ln for ln in icacls(a).splitlines()[:6]))

        print("\n== 2. 目录切到 B：A 的授权必须收回 ==")
        done, failed = m.sync_ntfs([{"path": b}], readonly=True, account=WHO)
        check("同步没有失败项", not failed, str(failed))
        check("A 上的显式 ACE 已撤", m._explicit_rights(a, WHO) is None,
              str(m._explicit_rights(a, WHO)))
        check("A 上 has_explicit_grant 变 False", not m.has_explicit_grant(a, WHO))
        check("B 上已授权", m._explicit_rights(b, WHO) == "(OI)(CI)(RX)",
              str(m._explicit_rights(b, WHO)))
        check("记录里只剩 B",
              [os.path.normpath(x[0]) for x in m.granted]
              == [os.path.normpath(b)], str(m.granted))
        print("  动作：%s" % done)

        print("\n== 3. 只读 → 可写（必须重设，不是叠加）==")
        done, failed = m.sync_ntfs([{"path": b}], readonly=False, account=WHO)
        check("同步没有失败项", not failed, str(failed))
        check("B 的级别升到 (M)", m._explicit_rights(b, WHO) == "(OI)(CI)(M)",
              str(m._explicit_rights(b, WHO)))
        check("动作里有重授权", any("授权" in d for d in done), str(done))

        print("\n== 4. 重复同步不该再改 ACL ==")
        before = icacls(b)
        done, failed = m.sync_ntfs([{"path": b}], readonly=False, account=WHO)
        check("无失败", not failed, str(failed))
        check("无动作", done == [], str(done))
        check("ACL 未变", icacls(b) == before)
        print("  icacls B:\n%s" % "\n".join(
            "    " + ln for ln in before.splitlines()[:6]))

        print("\n== 5. 取消共享：显式授权全部还原 ==")
        done, failed = m.revoke_all(principals=[WHO], paths=[b])
        check("撤销无失败", not failed, str(failed))
        check("B 上已无显式 ACE", m._explicit_rights(b, WHO) is None,
              str(m._explicit_rights(b, WHO)))
        check("记录清空", m.granted == [], str(m.granted))
        print("  动作：%s" % done)
    finally:
        try:
            m.revoke_ntfs(a, WHO)
            m.revoke_ntfs(b, WHO)
        except Exception:                                   # noqa: BLE001
            pass
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)

    print("\n%s" % ("CONFINE PROBE PASS" if not FAILS
                    else "CONFINE PROBE FAIL: %s" % FAILS))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())

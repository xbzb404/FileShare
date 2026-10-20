# -*- coding: utf-8 -*-
"""Windows 原生共享（SMB）管理。

不引第三方 SMB 服务端，直接用系统自带的「共享文件夹」能力（``net share``）：
Windows 自己的 SMB 服务是内核态实现，吞吐和兼容性比任何 Python 实现都好，
我们要做的只是把共享建好、删掉、查状态。

需要管理员权限（``net share`` 会返回系统错误 5）。
"""

import os
import sys

from ..netutil import is_admin, run_hidden


class SmbError(Exception):
    pass


class SmbManager(object):
    def __init__(self, log=None):
        self.log = log
        self.managed = set()          # 本程序创建的共享名，退出时只删这些

    # ------------------------------------------------------------ 环境
    def available(self):
        if not sys.platform.startswith("win"):
            return False, "仅支持 Windows"
        if not is_admin():
            return False, "需要管理员权限（点右上角「以管理员身份重启」）"
        return True, "OK"

    def _run(self, args):
        rc, out = run_hidden(args, timeout=20)
        return rc, out

    # ------------------------------------------------------------ 查询
    def exists(self, name):
        rc, out = self._run(["net", "share", name])
        if rc == 0:
            return True
        # 中文系统：错误 2310 / 2118 都表示没有这个共享
        return False

    def list_all(self):
        """本机所有共享名 → 路径。解析 ``net share`` 的输出。"""
        rc, out = self._run(["net", "share"])
        result = {}
        if rc != 0:
            return result
        for line in out.splitlines():
            line = line.rstrip()
            if not line or line.startswith("-") or line.startswith("共享名") \
                    or line.startswith("Share name") or line.startswith("命令"):
                continue
            # 共享名后的第一个含盘符或反斜杠的字段就是本地路径
            parts = line.split()
            if not parts:
                continue
            name = parts[0]
            for p in parts[1:]:
                if len(p) > 2 and p[1] == ":" or p.startswith("\\\\"):
                    result[name] = p
                    break
            else:
                result.setdefault(name, "")
        return result

    # ------------------------------------------------------------ 增删
    def add(self, name, path, readonly=True, remark="文件共享中心"):
        if not os.path.isdir(path):
            raise SmbError("目录不存在：%s" % path)
        perm = "READ" if readonly else "FULL"
        # 先删同名的（可能是上次没退干净，或用户改了路径/权限）
        if self.exists(name):
            self.remove(name)
        rc, out = self._run([
            "net", "share", "%s=%s" % (name, path),
            "/grant:Everyone,%s" % perm,
            "/remark:%s" % remark,
            "/cache:none",
        ])
        if rc != 0:
            raise SmbError(_clean(out) or "创建共享失败（返回码 %d）" % rc)
        self.managed.add(name)

    def remove(self, name):
        rc, out = self._run(["net", "share", name, "/delete"])
        if rc != 0:
            rc2, out2 = run_hidden(
                ["cmd", "/c", "echo Y| net share %s /delete" % name], timeout=20)
            if rc2 != 0:
                raise SmbError(_clean(out2 or out) or "删除共享失败")
        self.managed.discard(name)

    # ------------------------------------------------------------ 同步
    def sync(self, shares, readonly=True):
        """让系统共享与配置一致：多删少补。返回 (ok_names, errors)。"""
        ok, msg = self.available()
        if not ok:
            return [], [("%s" % msg)]
        want = {}
        for s in shares:
            name = (s.get("name") or "").strip()
            if name and os.path.isdir(s.get("path") or ""):
                want[name] = s["path"]
        errors = []
        done = []
        # 删掉不再需要的（只删本程序建过的）
        for name in list(self.managed):
            if name not in want:
                try:
                    self.remove(name)
                    done.append(("remove", name))
                except SmbError as exc:
                    errors.append("%s：%s" % (name, exc))
        for name, path in want.items():
            try:
                self.add(name, path, readonly=readonly)
                done.append(("add", name))
            except SmbError as exc:
                errors.append("%s：%s" % (name, exc))
        if self.log is not None:
            for kind, name in done:
                self.log.add("ok", "SMB 共享%s：%s" % (
                    "已创建" if kind == "add" else "已移除", name), "SMB")
            for err in errors:
                self.log.add("err", "SMB 失败：%s" % err, "SMB")
        return done, errors

    def remove_all(self):
        """退出时清掉本程序建的共享。"""
        for name in list(self.managed):
            try:
                self.remove(name)
            except SmbError:
                pass


def _clean(text):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.startswith("命令成功完成")]
    return " ".join(lines)[:300]

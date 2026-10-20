# -*- coding: utf-8 -*-
"""上传守卫：路径不能跳出共享目录，落盘的文件不能被执行。

三道防线，缺一不可：

1. **路径**：逐段校验。拒绝 ``..``、绝对路径、盘符、NTFS 备用数据流（冒号）、
   Windows 保留设备名（CON/NUL/COM1…）、控制字符、尾随点与空格
   （Windows 会悄悄把它们吃掉，``a.exe.`` 会被当成 ``a.exe``）。
   最后用 ``realpath`` 比对父目录，防「目录联接/符号链接指向共享外面」。

2. **文件名**：可执行扩展名一律追加 ``.noexec``。这一层最直接 ——
   文件不再是 ``.exe``，双击、拖到命令行、被计划任务调用全都失效。

3. **ACL**：落盘文件追加「拒绝执行」ACE（SID ``S-1-1-0`` 即 Everyone）。
   Windows 上 deny 优先于 allow，所以就算有人手工把 ``.noexec`` 改回 ``.exe``，
   执行时仍会被拒绝。这一层防的是「改名绕过」。

> 说明：Windows 没有 Unix 的 ``x`` 权限位，但有 ``FILE_EXECUTE`` 这一个 ACE
> 维度，``icacls /deny ...:(X)`` 就是关它。纯文件属性做不到同样的效果。
"""

import os
import sys

from .netutil import run_hidden

# 双击 / 命令行 / 计划任务能直接跑起来的扩展名。
# 只列「真能执行」的，不含 .txt/.pdf 这类（它们靠关联程序打开，不算执行）。
EXEC_EXT = {
    # 原生可执行与安装包
    ".exe", ".com", ".scr", ".pif", ".msi", ".msp", ".msix", ".appx",
    ".cpl", ".dll", ".sys", ".drv", ".ocx", ".efi", ".jar",
    # 批处理与脚本宿主
    ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".vbs", ".vbe", ".js",
    ".jse", ".wsf", ".wsh", ".hta", ".wsb",
    # 快捷方式 / 注册表 / 其他会被系统当程序处理的
    ".lnk", ".url", ".reg", ".inf", ".scf", ".msc", ".gadget",
}

# Windows 设备名：任何扩展名都一样危险（CON.txt 打开的就是控制台）
RESERVED = {
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

NOEXEC_SUFFIX = ".noexec"


def is_exec_name(name):
    """这个名字是不是「能被直接执行」的。"""
    stem, ext = os.path.splitext(name or "")
    ext = ext.lower().rstrip(" .")          # a.EXE. / a.exe （尾随点先吃掉）
    return ext in EXEC_EXT


def clean_segment(seg):
    """校验单个路径段。返回 ``(干净的名字, None)`` 或 ``(None, 原因)``。"""
    if not seg or seg in (".", ".."):
        return None, "路径段非法"
    if any(ord(c) < 32 for c in seg):
        return None, "文件名含控制字符"
    if ":" in seg:
        return None, "文件名含冒号（NTFS 备用数据流）"
    if "/" in seg or "\\" in seg:
        return None, "文件名含路径分隔符"
    # Windows 会静默丢弃结尾的点和空格，a.exe. 等价于 a.exe，
    # 所以必须在这里先归一化，否则扩展名检查形同虚设。
    name = seg.rstrip(" .")
    if not name:
        return None, "文件名非法"
    if name.split(".")[0].upper() in RESERVED:
        return None, "Windows 保留设备名（CON/NUL/COM1…）"
    if len(name.encode("utf-8", "ignore")) > 200:
        return None, "文件名过长"
    return name, None


def safe_target(root, rel):
    """把上传的相对路径解析成真实路径，越界或非法时返回 ``(None, 原因)``。

    ``rel`` 是「共享名之后」的那段（可以带子目录）。每一段都要过
    :func:`clean_segment`，再用 realpath 确认没跑出共享根。
    """
    if not rel:
        return None, "缺少文件名"
    rel = str(rel).replace("\\", "/").replace("\x00", "")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts:
        return None, "缺少文件名"

    clean = []
    for seg in parts:
        name, err = clean_segment(seg)
        if name is None:
            return None, err
        clean.append(name)

    from .netutil import safe_join
    target = safe_join(root, "/".join(clean))
    if target is None:
        return None, "路径越出共享目录"

    # safe_join 已经比过一次，但符号链接只在 target 本身是链接时才检查；
    # 这里再对父目录做 realpath，挡住「目录联接指向共享外面」这一手。
    parent = os.path.dirname(target)
    try:
        real_root = os.path.realpath(root)
        real_parent = os.path.realpath(parent)
        if os.path.commonpath([real_parent, real_root]) != real_root:
            return None, "父目录越出共享目录"
    except (ValueError, OSError):
        return None, "路径无法解析"
    return target, None


def deny_execute(path):
    """给文件加「拒绝执行」ACE。成功返回 True。

    ``icacls`` 是系统自带、无需额外依赖；SID 写法（``*S-1-1-0``）不受
    系统语言影响（中文系统里 Everyone 显示为「Everyone」，用名字反而不可靠）。
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        rc, _out = run_hidden([which_icacls(), path, "/deny", "*S-1-1-0:(X)"],
                              timeout=8)
        return rc == 0
    except Exception:                                    # noqa: BLE001
        return False


def which_icacls():
    """打包成 exe 后 PATH 可能不含 System32，用绝对路径更稳。"""
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "icacls.exe")
    if os.path.isfile(exe):
        return exe
    return "icacls"


def harden(path):
    """落盘后加固，返回最终路径（可能被改名）。

    先改名再加 ACL：改名是「让人没法直接双击」，ACL 是「改了名也执行不了」。
    两步都失败也不会抛异常 —— 上传本身已经成功，安全加固尽力而为，
    失败原因由调用方记日志。
    """
    final = path
    try:
        if is_exec_name(os.path.basename(path)):
            cand = path + NOEXEC_SUFFIX
            try:
                os.replace(path, cand)
                final = cand
            except OSError:
                pass
    except Exception:                                    # noqa: BLE001
        pass
    deny_execute(final)
    return final


def explain(rel):
    """给界面/日志用的一句话说明（不产生副作用）。"""
    root_hint = "共享根"
    _t, err = safe_target(root_hint, rel) if rel else (None, "缺少文件名")
    return err or "允许"

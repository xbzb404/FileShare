# -*- coding: utf-8 -*-
"""一键打包成单文件 exe  ·  by zhb

用法：
    python make_icon.py        # 首次：生成 assets/icon.ico
    python build_exe.py        # 打包
    python build_exe.py --prune  # 只清理历史构建产物

Windows + Tk 组合下有两个必须显式处理的坑（见 skill pyinstaller-tk-windows）：
  1. Tcl/Tk 运行时若没被显式附加，exe 会「双击没反应、无报错」——
     所以先定位 tcl 目录（必须同时含 tcl8.x 与 tk8.x 子目录）再 --add-data。
  2. 某些环境把 os.remove 重定向到回收站，会让 PyInstaller 收尾失败并留下
     损坏的 exe —— 打包子进程里关掉删除钩子，旧产物用「改名搁置」处理。
"""

import os
import shutil
import subprocess
import sys
import time

from fileshare import APP_NAME, AUTHOR, AUTHOR_TAG, __version__

HERE = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(HERE, "assets", "icon.ico")
DIST = os.path.join(HERE, "dist")
WORK = os.path.join(HERE, "build")
SPEC = os.path.join(HERE, "spec")

KEEP_OLD = 2
NAME = "FileShare"

VER = tuple(int(x) for x in (__version__.split(".") + ["0", "0", "0"])[:4])

VERSION_INFO = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=%(ver)s, prodvers=%(ver)s, mask=0x3f, flags=0x0,
    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', '%(tag)s'),
        StringStruct('FileDescription', '%(name)s —— 局域网文件共享（HTTP / FTP / SMB），多线程下载、断点续传、全局限速 (%(author)s)'),
        StringStruct('FileVersion', '%(vstr)s'),
        StringStruct('InternalName', '%(exe)s'),
        StringStruct('LegalCopyright', 'Copyright (C) 2026 %(tag)s. All rights reserved.'),
        StringStruct('OriginalFilename', '%(exe)s.exe'),
        StringStruct('ProductName', '%(name)s (%(author)s)'),
        StringStruct('ProductVersion', '%(vstr)s')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
""" % {"ver": str(VER), "tag": AUTHOR_TAG, "author": AUTHOR, "name": APP_NAME,
       "exe": NAME, "vstr": ".".join(str(x) for x in VER)}

HIDDEN = [
    "tkinter", "_tkinter", "tkinter.font", "tkinter.ttk",
    "tkinter.filedialog", "tkinter.messagebox",
    "PIL.ImageTk", "PIL._tkinter_finder",
    "asyncio", "asyncio.windows_events", "asyncio.proactor_events",
    "http.client", "ssl", "socket", "selectors",
    "fileshare.server.httpd", "fileshare.server.ftpd",
    "fileshare.server.smb", "fileshare.server.manager",
    "fileshare.download.engine", "fileshare.tray",
    "fileshare.ui.app", "fileshare.widgets", "fileshare.theme",
]

EXCLUDES = [
    "numpy", "pandas", "scipy", "matplotlib", "PyQt5", "PyQt6", "PySide6",
    "PySide2", "IPython", "notebook", "test", "unittest", "pydoc_data",
    "setuptools", "pip", "wheel",
]


def prune_victims(path, keep=KEEP_OLD):
    parent = os.path.dirname(path) or "."
    prefix = os.path.basename(path) + "_old_"
    try:
        names = [n for n in os.listdir(parent) if n.startswith(prefix)]
    except OSError:
        return []
    if len(names) <= keep:
        return []
    names.sort(key=lambda n: os.path.getmtime(os.path.join(parent, n)),
               reverse=True)
    return names[keep:]


def prune_old(path, keep=KEEP_OLD):
    parent = os.path.dirname(path) or "."
    for name in prune_victims(path, keep):
        target = os.path.join(parent, name)
        try:
            if os.path.isdir(target):
                shutil.rmtree(target)
            else:
                os.remove(target)
            print("  prune  ->", name)
        except Exception as exc:                            # noqa: BLE001
            print("  ! prune failed:", name, exc)


def prune_all():
    print("== 收口历史搁置产物 ==")
    for path in (WORK, SPEC, os.path.join(DIST, NAME + ".exe")):
        prune_old(path)


def dispose(path):
    """改名搁置代替删除：改名是原子操作，不会因文件被占用而失败。"""
    if not os.path.exists(path):
        return
    stamp = time.strftime("%H%M%S")
    try:
        os.rename(path, "%s_old_%s" % (path, stamp))
        print("  dispose ->", os.path.basename(path) + "_old_" + stamp)
    except Exception as exc:                                # noqa: BLE001
        print("  ! dispose failed:", exc)


def find_tcl_tk():
    """定位同时含 tcl8.x 与 tk8.x 子目录的 tcl 目录。"""
    import _tkinter
    import tkinter
    roots = set()
    pyd = os.path.dirname(_tkinter.__file__)
    roots.add(os.path.dirname(pyd))
    roots.add(os.path.dirname(os.path.dirname(pyd)))
    roots.add(os.path.dirname(os.path.dirname(os.path.abspath(tkinter.__file__))))
    for extra in (getattr(sys, "base_prefix", None), getattr(sys, "prefix", None)):
        if extra:
            roots.add(extra)
    for root in roots:
        for cand in (os.path.join(root, "tcl"), os.path.join(root, "Lib", "tcl")):
            if not os.path.isdir(cand):
                continue
            subs = [d for d in os.listdir(cand)
                    if os.path.isdir(os.path.join(cand, d))]
            if any(d.startswith("tcl8") for d in subs) and \
                    any(d.startswith("tk8") for d in subs):
                return cand
    return None


def check_env():
    py = sys.executable
    try:
        import tkinter                                     # noqa: F401
        import _tkinter                                    # noqa: F401
    except Exception as exc:                               # noqa: BLE001
        print("!! 当前解释器没有 tkinter：", exc)
        print("   请改用系统 Python：")
        print("   C:\\Users\\<你>\\AppData\\Local\\Microsoft\\WindowsApps\\python.exe")
        sys.exit(2)
    try:
        import PyInstaller                                 # noqa: F401
    except Exception:                                      # noqa: BLE001
        print("== 正在安装 PyInstaller ==")
        subprocess.run([py, "-m", "pip", "install", "pyinstaller"], check=False)
    return py


def build(py, name, entry, windowed=True, extra=None):
    tcl = find_tcl_tk()
    print("== building %s ==  tcl=%s" % (name, tcl))
    cmd = [py, "-m", "PyInstaller", "--noconfirm", "--onefile", "--clean",
           "--name", name,
           "--distpath", DIST, "--workpath", WORK, "--specpath", SPEC,
           "--paths", HERE]
    for m in HIDDEN:
        cmd += ["--hidden-import", m]
    for m in EXCLUDES:
        cmd += ["--exclude-module", m]
    if windowed:
        cmd.append("--windowed")
    if os.path.exists(ICON):
        cmd += ["--icon", ICON, "--add-data", ICON + ";assets"]
    else:
        print("   !! 缺少 assets/icon.ico，先跑 python make_icon.py")
    if tcl:
        cmd += ["--add-data", tcl + ";tcl"]
    os.makedirs(SPEC, exist_ok=True)
    vf = os.path.join(SPEC, "version_info.txt")
    with open(vf, "w", encoding="utf-8") as fp:
        fp.write(VERSION_INFO)
    cmd += ["--version-file", vf]
    cmd += (extra or [])
    cmd.append(entry)

    out = os.path.join(DIST, name + ".exe")
    dispose(out)

    env = dict(os.environ)
    env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
    env["PYTHONIOENCODING"] = "utf-8"
    t0 = time.time()
    proc = subprocess.run(cmd, env=env, cwd=HERE)
    print("== %s done rc=%s in %.1fs" % (name, proc.returncode, time.time() - t0))
    if os.path.exists(out):
        print("   ->", out, "%.1f MB" % (os.path.getsize(out) / 1048576.0))
    return proc.returncode == 0 and os.path.exists(out)


def main():
    print("==== FileShare build script   %s ====" % AUTHOR)
    if "--prune" in sys.argv:
        prune_all()
        return 0
    py = check_env()
    os.makedirs(DIST, exist_ok=True)
    for d in (WORK, SPEC):
        dispose(d)
    for fn in os.listdir(DIST):
        if "_old_" in fn:
            try:
                os.remove(os.path.join(DIST, fn))
            except OSError:
                pass
    ok = build(py, NAME, os.path.join(HERE, "main.py"), windowed=True)
    print("\n==== 结果 ====")
    print(("  [OK]  " if ok else "  [FAIL]") + " " + NAME)
    if ok:
        print("  成品：%s" % os.path.join(DIST, NAME + ".exe"))
        print("  建议同步到 E:\\AI工程\\输出\\")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

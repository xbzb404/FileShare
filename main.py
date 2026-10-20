# -*- coding: utf-8 -*-
"""
文件共享中心 —— 程序入口  ·  by zhb

用法：
    python main.py              直接运行源码版
    python main.py --hidden     启动后直接缩到托盘（开机自启用）
    python main.py --no-tray    不创建托盘图标（排障用）
    双击 FileShare.exe          运行打包版

三种协议：HTTP（自研 asyncio，支持多线程/断点续传）、FTP（自研，支持
REST 续传）、SMB（系统原生共享，需要管理员权限，界面里可一键提权）。

作者：zhb
"""

import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _setup_paths():
    base = (os.path.dirname(sys.executable)
            if getattr(sys, "frozen", False) else APP_DIR)
    if base not in sys.path:
        sys.path.insert(0, base)


def _write_crash(exc_text):
    """打包成 --windowed 后没有控制台，异常必须自己落盘。"""
    try:
        base = (os.path.dirname(sys.executable)
                if getattr(sys, "frozen", False) else APP_DIR)
        path = os.path.join(base, "FileShare_error.log")
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(exc_text)
    except Exception:                                       # noqa: BLE001
        pass


def main():
    _setup_paths()

    try:
        import multiprocessing
        multiprocessing.freeze_support()
    except Exception:                                       # noqa: BLE001
        pass

    from fileshare import AUTHOR, APP_NAME, __version__
    from fileshare import theme as T

    if "--version" in sys.argv:
        print("%s v%s · %s" % (APP_NAME, __version__, AUTHOR))
        return

    T.init_process_dpi()

    from fileshare.config import Config
    from fileshare.ui.app import App

    cfg = Config()
    app = App(cfg, start_hidden="--hidden" in sys.argv)
    if "--no-tray" in sys.argv and app.tray is not None:
        try:
            app.tray.hide()
        except Exception:                                   # noqa: BLE001
            pass
        app.tray = None
    app.run()


def _run():
    try:
        main()
    except Exception:
        import traceback
        _write_crash(traceback.format_exc())
        raise


if __name__ == "__main__":
    _run()

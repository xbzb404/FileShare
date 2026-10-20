# -*- coding: utf-8 -*-
"""资源路径：源码运行与 PyInstaller 单文件运行都能找到 assets/。"""

import os
import sys


def base_dir():
    if hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def asset_path(name):
    """打包后资源在 ``_MEIPASS/assets``，源码时在项目根的 ``assets``。"""
    for cand in (os.path.join(base_dir(), "assets", name),
                 os.path.join(base_dir(), name)):
        if os.path.isfile(cand):
            return cand
    return os.path.join(base_dir(), "assets", name)


def data_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "FileShare")
    os.makedirs(d, exist_ok=True)
    return d

# -*- coding: utf-8 -*-
"""文件共享中心 —— 局域网文件共享（HTTP / FTP / SMB）·  by zhb

一个窗口管三件事：
  · 把本机目录对外共享（HTTP 高速下载 / FTP 上传下载 / Windows 共享）
  · 全局限速，想拉满就拉满，想省带宽就设个上限
  · 内置多线程下载器，断点续传

作者：zhb
"""

AUTHOR = "by zhb"
AUTHOR_TAG = "zhb"
APP_NAME = "文件共享中心"
APP_SUB = "局域网文件共享 · HTTP / FTP / SMB"
__version__ = "1.1.0"


def window_title():
    """主窗口标题。单实例守卫要靠它按标题找窗口，必须只有这一处拼装。"""
    return "%s v%s · %s" % (APP_NAME, __version__, AUTHOR)

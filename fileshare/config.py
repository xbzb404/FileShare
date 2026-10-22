# -*- coding: utf-8 -*-
"""配置读写：``%APPDATA%\\FileShare\\config.json``。

只做一件事：把用户设置和共享目录存下来，下次启动照旧。
读的时候用「默认值 + 深合并」，所以升级加了新键也不会丢旧配置。
"""

import copy
import json
import os
import threading


APP_DIR_NAME = "FileShare"

# 配置结构版本。1 = 早期（FTP 默认允许上传）；2 = 默认只读，上传必须手动开；
# 3 = Web 上传同样默认关闭，HTTP/FTP 的上传都要在「服务设置」手动打开。
SCHEMA = 3


def app_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_DIR_NAME)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = os.path.expanduser("~")
    return d


def config_path():
    return os.path.join(app_dir(), "config.json")


def default_download_dir():
    for cand in (os.path.join(os.path.expanduser("~"), "Downloads"),
                 os.path.expanduser("~")):
        if os.path.isdir(cand):
            return cand
    return os.path.expanduser("~")


DEFAULTS = {
    "schema": SCHEMA,
    "shares": [],
    "listen": {
        "ip": "0.0.0.0",     # 监听哪块网卡；0.0.0.0 = 全部
        "discovery_port": 47610,   # 局域网互发现探测端口（冷门固定端口）
    },
    "http": {
        "enabled": True,
        "port": 8080,
        "upload": False,     # 允许网页上传（PUT）；默认关 = Web 只读
        "dirlist": True,     # 目录列表
    },
    "ftp": {
        "enabled": True,
        "port": 2121,
        "user": "share",
        "password": "fileshare",
        "upload": False,     # 允许 STOR/DELE/MKD 等写操作；默认关 = 只读
        "anonymous": False,
    },
    "smb": {
        "enabled": False,
        "readonly": True,
        # everyone = 免密只读（Everyone 权限，任何设备直接进）
        # account  = 用专用本地账户，需输入用户名密码
        "mode": "everyone",
        "user": "FileShare",
        "password": "",
        "granted": [],       # 本程序加过的目录授权，取消共享时按它还原
    },
    "limit": {
        "up_kbps": 0,        # 服务端向外发（别人下载）总限速，0 = 不限
        "down_kbps": 0,      # 内置下载器总限速，0 = 不限
    },
    "download": {
        "dir": "",
        "threads": 8,
    },
    "ui": {
        "close_action": "tray",
        "theme": "light",
    },
}


def _deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config(object):
    """线程安全的配置对象：``cfg.data`` 是普通 dict。"""

    def __init__(self, path=None):
        self.path = path or config_path()
        self._lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULTS)
        self._dirty = False
        self.load()
        if self._dirty:
            # 迁移过一次就落盘，避免每次启动都重算
            self.save()
            self._dirty = False

    # ---- 读写 ----
    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                raw = json.load(fp)
        except (OSError, ValueError):
            raw = {}
        with self._lock:
            self.data = _deep_merge(DEFAULTS, raw)
            # v1 → v2：早期版本 FTP 默认允许上传，升级后一律回到「只读」，
            # 想上传由用户在「服务设置」里自己打开 —— 安全默认优先，
            # 不因为旧配置里写着 true 就继续放开写入。
            try:
                old = int(raw.get("schema") or 1)
            except (TypeError, ValueError):
                old = 1
            if old < SCHEMA:
                # v2/v3：HTTP、FTP 的上传一律回到「关闭」，想让谁上传
                # 由用户在「服务设置」里手动打开 —— 安全默认优先，
                # 不因为旧配置里写着 true 就继续放开写入。
                self.data["http"]["upload"] = False
                self.data["ftp"]["upload"] = False
                self.data["schema"] = SCHEMA
                self._dirty = True
            if not self.data["download"].get("dir"):
                self.data["download"]["dir"] = default_download_dir()
            self.data["shares"] = [s for s in self.data.get("shares", [])
                                   if isinstance(s, dict) and s.get("path")]
        return self.data

    def save(self):
        with self._lock:
            data = copy.deepcopy(self.data)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False

    # ---- 便捷访问 ----
    def get(self, *keys, **kw):
        d = self.data
        for k in keys:
            if not isinstance(d, dict) or k not in d:
                return kw.get("default")
            d = d[k]
        return d

    def set(self, value, *keys):
        with self._lock:
            d = self.data
            for k in keys[:-1]:
                d = d.setdefault(k, {})
            d[keys[-1]] = value

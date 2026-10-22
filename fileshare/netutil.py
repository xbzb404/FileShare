# -*- coding: utf-8 -*-
"""网络小工具：本机地址、端口占用检测、路径安全解析。"""

import os
import socket
import subprocess
import sys
import threading


CREATE_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0


def run_hidden(args, timeout=15, encoding=None):
    """跑一个不弹黑窗的子进程，返回 (rc, stdout+stderr)。

    ``stdin`` 必须给 DEVNULL：默认是「继承父进程」。打包成 ``--windowed``
    后本进程没有控制台，继承下来的是个无效句柄，被调命令（``net share`` /
    ``icacls`` 之类）一遇到交互确认就读不到输入，直接吐一句
    「没有提供有效的响应」然后失败——界面上还会把这句话原样显示出来。
    """
    kw = {"stdin": subprocess.DEVNULL}
    if sys.platform.startswith("win"):
        kw["creationflags"] = CREATE_NO_WINDOW
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kw["startupinfo"] = si
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout, **kw)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    enc = encoding or ("gbk" if sys.platform.startswith("win") else "utf-8")
    out = (p.stdout or b"").decode(enc, "ignore")
    err = (p.stderr or b"").decode(enc, "ignore")
    return p.returncode, (out + err).strip()


def primary_ip():
    """默认出口网卡的 IPv4（不真的发包）。拿不到就退回 127.0.0.1。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip if ip and not ip.startswith("0.") else "127.0.0.1"


def local_ips():
    """本机所有 IPv4（第一个是默认出口地址，之后是其它网卡）。"""
    ips = []
    try:
        first = primary_ip()
        if first:
            ips.append(first)
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips or ["127.0.0.1"]


# ---------------------------------------------------------------- 网卡链路速率
# 只为了算「占网卡百分之几」，所以必须便宜：用 GetAdaptersAddresses 直接
# 拿 ReceiveLinkSpeed，不拉 PowerShell（那要 300~800ms，还会弹一次窗口）。
# 结构体布局按 Win7+ 的 IP_ADAPTER_ADDRESSES_LH 声明，ctypes 自己算对齐。

_LINK_CACHE = {}
_LINK_LOCK = threading.Lock()


def _adapter_defs():
    """延迟定义 ctypes 结构（非 Windows 平台不去碰 ctypes.wintypes）。"""
    import ctypes
    from ctypes import wintypes

    class SOCKET_ADDRESS(ctypes.Structure):
        _fields_ = [("lpSockaddr", ctypes.c_void_p),
                    ("iSockaddrLength", ctypes.c_int)]

    class UNICAST(ctypes.Structure):
        pass

    UNICAST._fields_ = [
        ("Length", wintypes.ULONG),
        ("Flags", wintypes.DWORD),
        ("Next", ctypes.POINTER(UNICAST)),
        ("Address", SOCKET_ADDRESS),
    ]

    class ADAPTER(ctypes.Structure):
        pass

    ADAPTER._fields_ = [
        ("Length", wintypes.ULONG),
        ("IfIndex", wintypes.DWORD),
        ("Next", ctypes.POINTER(ADAPTER)),
        ("AdapterName", ctypes.c_char_p),
        ("FirstUnicastAddress", ctypes.POINTER(UNICAST)),
        ("FirstAnycastAddress", ctypes.c_void_p),
        ("FirstMulticastAddress", ctypes.c_void_p),
        ("FirstDnsServerAddress", ctypes.c_void_p),
        ("DnsSuffix", ctypes.c_wchar_p),
        ("Description", ctypes.c_wchar_p),
        ("FriendlyName", ctypes.c_wchar_p),
        ("PhysicalAddress", ctypes.c_ubyte * 8),
        ("PhysicalAddressLength", wintypes.ULONG),
        ("Flags", wintypes.ULONG),
        ("Mtu", wintypes.ULONG),
        ("IfType", wintypes.DWORD),
        ("OperStatus", ctypes.c_int),
        ("Ipv6IfIndex", wintypes.DWORD),
        ("ZoneIndices", wintypes.DWORD * 16),
        ("FirstPrefix", ctypes.c_void_p),
        ("TransmitLinkSpeed", ctypes.c_ulonglong),
        ("ReceiveLinkSpeed", ctypes.c_ulonglong),
    ]
    return ctypes, wintypes, ADAPTER, UNICAST


def adapters():
    """枚举网卡 → ``[{ip, name, speed, up}]``（speed 单位 bps，0 = 未知）。"""
    if not sys.platform.startswith("win"):
        return []
    try:
        ctypes, wintypes, ADAPTER, _U = _adapter_defs()
        iphlpapi = ctypes.WinDLL("iphlpapi.dll")
        fn = iphlpapi.GetAdaptersAddresses
        # argtypes 必须给：x64 下不声明会把 64 位指针截成 32 位
        fn.restype = wintypes.ULONG
        fn.argtypes = [wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p,
                       ctypes.POINTER(ADAPTER), ctypes.POINTER(wintypes.ULONG)]
        flags = 0x02 | 0x04 | 0x08          # SKIP_ANYCAST/MULTICAST/DNS
        size = wintypes.ULONG(16 * 1024)
        buf = None
        for _ in range(4):
            buf = ctypes.create_string_buffer(size.value)
            rc = fn(socket.AF_INET, flags, None,
                    ctypes.cast(buf, ctypes.POINTER(ADAPTER)),
                    ctypes.byref(size))
            if rc == 0:
                break
            if rc != 111:                   # ERROR_BUFFER_OVERFLOW
                return []
        else:
            return []

        out = []
        node = ctypes.cast(buf, ctypes.POINTER(ADAPTER))
        while node:
            a = node.contents
            ip = ""
            u = a.FirstUnicastAddress
            if u:
                sa = u.contents.Address
                if sa.lpSockaddr:
                    raw = ctypes.string_at(sa.lpSockaddr, 8)
                    if int.from_bytes(raw[0:2], "little") == socket.AF_INET:
                        ip = socket.inet_ntoa(raw[4:8])
            spd = int(a.ReceiveLinkSpeed or 0)
            if spd <= 0 or spd >= 0xFFFFFFFFFFFFFFF:      # 0xFF.. = 驱动没报
                spd = 0
            out.append({
                "ip": ip,
                "name": a.FriendlyName or "",
                "speed": spd,
                "up": int(a.OperStatus) == 1,
                "iftype": int(a.IfType),
            })
            node = a.Next
        return out
    except Exception:                                    # noqa: BLE001
        return []


def link_speed(ip=None, refresh=False):
    """承载 ``ip`` 的那块网卡的链路速率（bps）；拿不到返回 0。

    结果按 IP 缓存（网卡速率不会变，除非插拔网线）；``refresh=True`` 重查。
    """
    ip = ip or primary_ip()
    with _LINK_LOCK:
        if not refresh and ip in _LINK_CACHE:
            return _LINK_CACHE[ip]
    speed = 0
    found = adapters()
    for a in found:
        if a["ip"] == ip:
            speed = a["speed"]
            break
    if not speed:
        # 没精确匹配（多网卡 / 虚拟网卡）就取速率最高的在用网卡，
        # 总比显示「未知」好；至少能算个保守的占用率。
        cands = [a["speed"] for a in found if a["up"] and a["speed"] > 0]
        if cands:
            speed = max(cands)
    with _LINK_LOCK:
        _LINK_CACHE[ip] = speed
    return speed


def primary_adapter(refresh=False):
    """承载默认出口的那块网卡 → ``{ip, name, speed, up}``；找不到返回 None。"""
    ip = primary_ip()
    for a in adapters():
        if a["ip"] == ip:
            return a
    return None


def adapter_by_ip(ip):
    """按 IP 找网卡信息；找不到返回 None。"""
    if not ip or ip == "0.0.0.0":
        return None
    for a in adapters():
        if a["ip"] == ip:
            return a
    return None


def listen_choices():
    """可选的监听地址：``[(ip, 显示名), ...]``，第一项是全部网卡。"""
    from .util import human_link
    out = [("0.0.0.0", "全部网卡（0.0.0.0）")]
    seen = set()
    for a in adapters():
        ip = a.get("ip") or ""
        if not a.get("up") or not ip or ip.startswith("169.254.") \
                or ip in seen:
            continue
        seen.add(ip)
        name = a.get("name") or "网卡"
        speed = human_link(a.get("speed") or 0)
        label = "%s · %s · %s" % (name, ip, speed)
        out.append((ip, label))
    return out


def port_free(port, host="0.0.0.0"):
    """端口能不能绑上（True = 可用）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


def safe_join(root, rel):
    """把 URL 里的相对路径安全地拼到 root 下。

    返回绝对路径；越出 root（``..``、绝对路径、盘符、符号链接）时返回 None。
    """
    if rel is None:
        return None
    rel = str(rel).replace("\\", "/").lstrip("/")
    if not rel:
        return root
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        return None
    target = os.path.normpath(os.path.join(root, rel))
    root_n = os.path.normpath(root)
    try:
        if os.path.commonpath([os.path.abspath(root_n),
                               os.path.abspath(target)]) != os.path.abspath(root_n):
            return None
    except (ValueError, OSError):
        return None
    # 符号链接/目录联接也可能指到外面，realpath 再比一次
    try:
        if os.path.islink(target):
            real = os.path.realpath(target)
            if os.path.commonpath([os.path.realpath(root_n), real]) != \
                    os.path.realpath(root_n):
                return None
    except (ValueError, OSError):
        pass
    return target


def is_admin():
    if not sys.platform.startswith("win"):
        return os.geteuid() == 0
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:                                    # noqa: BLE001
        return False


def safe_filename(name, fallback="download.bin"):
    """去掉 Windows 非法字符的文件名。"""
    name = (name or "").strip().replace("\\", "/").split("/")[-1]
    bad = '<>:"/\\|?*'
    name = "".join(c for c in name if c not in bad and ord(c) >= 32).strip(" .")
    if not name or name.upper() in ("CON", "PRN", "AUX", "NUL"):
        return fallback
    return name[:180]


def unique_path(path):
    """目标已存在时自动加 (1)(2)…，不覆盖已有文件。"""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    for i in range(1, 1000):
        cand = "%s (%d)%s" % (base, i, ext)
        if not os.path.exists(cand):
            return cand
    return "%s (%d)%s" % (base, 999, ext)

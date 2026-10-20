# -*- coding: utf-8 -*-
"""
Win11 视觉层：颜色令牌、字体、DPI 缩放、抗锯齿圆角绘制。

与 BSODAnalyzer 同一套设计语言：
 * 颜色取自 Windows 11 的 Fluent 调色板（浅色 #F3F3F3 底 + 白卡片；
   深色 #202020 底 + #2B2B2B 卡片；强调色 #0067C0 / #4CC2FF）。
 * Tk 原生画圆角会有明显锯齿，这里统一用 Pillow 4 倍超采样后 LANCZOS 缩回，
   渲染成 PhotoImage 贴到 Canvas 上；文字仍交给 Tk 绘制（自带抗锯齿）。
 * 所有尺寸都过一遍 px()，使界面在高 DPI 屏上等比放大。
"""

import ctypes
import tkinter as tk
import tkinter.font as tkfont

try:
    from PIL import Image, ImageDraw, ImageTk
    HAS_PIL = True
except Exception:                                    # noqa: BLE001
    HAS_PIL = False

_SS = 4                                              # 超采样倍数
_image_cache = {}


# ------------------------------------------------------------------ DPI

def init_process_dpi():
    """让进程按系统 DPI 渲染（否则高 DPI 下整体被位图拉伸而发虚）。"""
    ok = False
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        ok = True
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
            ok = True
        except Exception:
            ok = False
    return ok


def system_dpi():
    try:
        d = int(ctypes.windll.user32.GetDpiForSystem())
        if d > 0:
            return d
    except Exception:
        pass
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        d = int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88))   # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        if d > 0:
            return d
    except Exception:
        pass
    return 96


SCALE = 1.0


def init_scaling():
    """初始化全局缩放系数，返回 (scale, dpi)。"""
    global SCALE
    init_process_dpi()
    dpi = system_dpi()
    SCALE = max(1.0, dpi / 96.0)
    return SCALE, dpi


def px(n):
    """逻辑像素 → 物理像素。"""
    return int(round(n * SCALE))


# ------------------------------------------------------------------ 颜色

LIGHT = {
    "name": "light",
    "bg": "#F3F3F3",
    "bg_alt": "#FAFAFA",
    "card": "#FFFFFF",
    "card_hover": "#FBFBFB",
    "border": "#E3E3E3",
    "border_strong": "#D6D6D6",
    "tx": "#1B1B1B",
    "tx2": "#5D5D5D",
    "tx3": "#8A8A8A",
    "accent": "#0067C0",
    "accent_hover": "#1A78CB",
    "accent_press": "#005A9E",
    "accent_text": "#FFFFFF",
    "hover": "#EDEDED",
    "press": "#E1E1E1",
    "nav_sel": "#E6E6E6",
    "nav_sel_bar": "#0067C0",
    "status_ok": "#0F7B0F",
    "status_warn": "#9D5D00",
    "status_bad": "#C42B1C",
    "chip_bg": "#EEEEEE",
    "track": "#E0E0E0",
    "track_fill": "#0067C0",
    "shadow": "#DCDCDC",
}

DARK = {
    "name": "dark",
    "bg": "#202020",
    "bg_alt": "#1A1A1A",
    "card": "#2B2B2B",
    "card_hover": "#323232",
    "border": "#3A3A3A",
    "border_strong": "#4A4A4A",
    "tx": "#FFFFFF",
    "tx2": "#C8C8C8",
    "tx3": "#9A9A9A",
    "accent": "#4CC2FF",
    "accent_hover": "#5FCBFF",
    "accent_press": "#3AA9E0",
    "accent_text": "#00304D",
    "hover": "#2E2E2E",
    "press": "#383838",
    "nav_sel": "#333333",
    "nav_sel_bar": "#4CC2FF",
    "status_ok": "#6CCB5F",
    "status_warn": "#FCE100",
    "status_bad": "#FF99A4",
    "chip_bg": "#383838",
    "track": "#3A3A3A",
    "track_fill": "#4CC2FF",
    "shadow": "#1A1A1A",
}

# 风险/状态配色（浅色/深色各一套，保证对比度）
SEV_COLORS = {
    "light": {
        "critical": ("#FDE7E9", "#B31B1B"),
        "high": ("#FDF3E5", "#8A5200"),
        "medium": ("#FFF8DE", "#7A6A00"),
        "low": ("#EEF2F7", "#44506A"),
        "info": ("#E8F2FB", "#005A9E"),
    },
    "dark": {
        "critical": ("#4A2024", "#FFC9CE"),
        "high": ("#3E2F17", "#FFD79A"),
        "medium": ("#3B3616", "#F7E48A"),
        "low": ("#2A3140", "#BFCCE0"),
        "info": ("#0F3247", "#8FD3FF"),
    },
}


def sev_colors(theme, sev):
    return SEV_COLORS.get(theme["name"], SEV_COLORS["light"]).get(
        sev, SEV_COLORS["light"]["info"])


# ------------------------------------------------------------------ 字体

_FAMILY_CANDIDATES = [
    "Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI",
    "Microsoft YaHei", "Tahoma",
]
_TITLE_CANDIDATES = [
    "Segoe UI Variable Display", "Segoe UI Semibold", "Segoe UI",
    "Microsoft YaHei UI",
]
_ICON_CANDIDATES = ["Segoe Fluent Icons", "Segoe MDL2 Assets"]
_MONO_CANDIDATES = ["Cascadia Mono", "Consolas", "Courier New"]


class Fonts(object):
    """按当前 DPI 生成全套字体对象。"""

    def __init__(self, root):
        fams = set()
        try:
            fams = set(tkfont.families(root))
        except Exception:
            pass

        def pick(cands, fallback="TkDefaultFont"):
            for c in cands:
                if c in fams:
                    return c
            return fallback

        self.ui_family = pick(_FAMILY_CANDIDATES)
        self.title_family = pick(_TITLE_CANDIDATES, self.ui_family)
        # ⚠️ 粗体必须用**中文字体族**，不能沿用 Segoe UI。
        # 实测（Win11 + Segoe UI Variable Text）：粗体 9~11px 渲染「删」时，
        # Windows 选到的粗体回退字体缺这个字形，直接画成实心方块（tofu）；
        # 同一字号常规体正常，13px 以上粗体也正常 —— 极难察觉。
        # Microsoft YaHei UI 的粗体是真实字重，字形完整。
        self.bold_family = pick(["Microsoft YaHei UI", "Microsoft YaHei",
                                 "Segoe UI"], self.ui_family)
        icon = pick(_ICON_CANDIDATES, "")
        self.icon_family = icon
        self.mono_family = pick(_MONO_CANDIDATES, "Courier")
        self.has_icons = bool(icon)

        # 字号按 DPI 放大（Tk 的 point 字号会再乘一次 tk scaling，
        # 因此这里用负值像素单位，由我们完全控制实际大小）
        def F(size, family=None, bold=False):
            if family is None:
                family = self.bold_family if bold else self.ui_family
            return tkfont.Font(
                root=root, family=family,
                size=-px(size), weight="bold" if bold else "normal",
            )

        self.h1 = F(21, bold=True)                   # 页面大标题
        self.h2 = F(15, bold=True)                   # 卡片组标题
        self.h3 = F(13, bold=True)                   # 卡片标题
        self.body = F(11)
        self.body_b = F(11, bold=True)
        self.small = F(10)
        self.small_b = F(10, bold=True)
        self.tiny = F(9)
        self.tiny_b = F(9, bold=True)
        self.nav = F(11)
        self.btn = F(11)
        self.btn_b = F(11, bold=True)
        self.mono = F(10, self.mono_family)
        self.icon = F(13, icon) if icon else F(13)
        self.icon_big = F(19, icon) if icon else F(19)
        self.huge = F(30, bold=True)


# 图标码位（Segoe Fluent Icons / MDL2 Assets 通用）
ICONS = {
    "overview": "\uE80F",
    "app": "\uEA3A",
    "script": "\uF5E2",
    "task": "\uE823",
    "log": "\uE81C",
    "risk": "\uE7BA",
    "system": "\uE946",
    "refresh": "\uE72C",
    "save": "\uEDE1",
    "theme": "\uE706",
    "warn": "\uE7BA",
    "error": "\uEA39",
    "ok": "\uE930",
    "info": "\uE946",
    "search": "\uE721",
    "chip": "\uE950",
    "trash": "\uE74D",
    "clock": "\uE823",
    "folder": "\uE8B7",
    "export": "\uEDE1",
    "power": "\uE7E8",
    "cmd": "\uE756",
    "manage": "\uE713",
    "shield": "\uEA18",
    "copy": "\uE8C8",
    "undo": "\uE7A7",
    # 文件共享中心用到的
    "server": "\uE774",
    "download": "\uE896",
    "upload": "\uE898",
    "play": "\uE768",
    "pause": "\uE769",
    "stop": "\uE71A",
    "add": "\uE710",
    "open": "\uE8E5",
    "link": "\uE71B",
    "speed": "\uE9D9",
    "settings": "\uE713",
    "close": "\uE711",
    "check": "\uE73E",
    "cancel": "\uE711",
}


def icon(name, fallback=""):
    return ICONS.get(name, fallback)


# ------------------------------------------------------------------ 圆角绘制

def make_round_image(w, h, radius, fill=None, outline=None, width=0):
    """生成抗锯齿圆角矩形图片（带缓存）。"""
    if w <= 0 or h <= 0:
        return None
    key = (w, h, radius, fill, outline, width)
    if key in _image_cache:
        return _image_cache[key]
    if not HAS_PIL:
        _image_cache[key] = None
        return None
    try:
        im = Image.new("RGBA", (w * _SS, h * _SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        r = max(0, min(radius, min(w, h) // 2))
        d.rounded_rectangle(
            [0, 0, w * _SS - 1, h * _SS - 1], radius=r * _SS,
            fill=fill, outline=outline,
            width=max(0, width) * _SS if outline and width else 0)
        im = im.resize((w, h), Image.LANCZOS)
        _image_cache[key] = im
        return im
    except Exception:
        _image_cache[key] = None
        return None


def round_photo(w, h, radius, fill=None, outline=None, width=0):
    """圆角图片 → Tk PhotoImage（不可用时返回 None，调用方走降级绘制）。"""
    im = make_round_image(w, h, radius, fill, outline, width)
    if im is None:
        return None
    try:
        return ImageTk.PhotoImage(im)
    except Exception:
        return None


def draw_round_rect(canvas, x1, y1, x2, y2, radius=8, fill=None, outline=None,
                    width=1, tags=None, photo_holder=None):
    """在 Canvas 上画圆角矩形（优先抗锯齿图片，缺失时退化为多边形）。

    photo_holder: 传入一个 list，用于保持 PhotoImage 引用（否则会被回收）。
    """
    w, h = int(x2 - x1), int(y2 - y1)
    photo = round_photo(w, h, radius, fill, outline, width)
    items = []
    if photo is not None:
        if photo_holder is not None:
            photo_holder.append(photo)
        items.append(canvas.create_image(x1, y1, image=photo, anchor="nw",
                                         tags=tags))
    else:
        r = min(radius, w // 2, h // 2)
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        items.append(canvas.create_polygon(
            pts, smooth=True, fill=fill or "", outline=outline or "",
            width=width, tags=tags))
    return items

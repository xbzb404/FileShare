# -*- coding: utf-8 -*-
"""主窗口：Win11 Fluent 风格（Canvas 自绘），五个页面。

不卡顿的三条做法：
1. **网络与磁盘全部在工作线程**，UI 线程只做「读数 → 画」；
2. **只重绘当前可见页面**，切页才画，后台页面一律跳过；
3. 每秒刷新一次的不是整棵树，而是内容 Canvas —— 一页只有几十个 item，
   重绘耗时在 5 ms 量级；滚动位置在重绘前后保存/恢复。
"""

import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import tkinter.ttk as ttk
from tkinter import filedialog

from .. import AUTHOR, APP_NAME, APP_SUB, __version__, window_title
from .. import theme as T
from ..config import Config
from ..download.engine import Downloader
from ..discovery import scan_subnet
from ..netutil import (adapter_by_ip, is_admin, link_speed, listen_choices,
                       primary_ip, safe_filename)
from ..server.manager import ShareManager
from ..tray import TrayIcon, available as tray_available
from ..util import (EventLog, human_bytes, human_link, human_speed,
                    human_time, write_crash_log)
from ..widgets import (LineEntry, NavItem, Painter, PillButton, ScrollArea,
                      Slider, Sparkline, Switch, action_at)

NAV = [
    ("overview", "overview", "概览"),
    ("traffic", "speed", "连接监控"),
    ("shares", "folder", "共享目录"),
    ("peers", "search", "共享网络"),
    ("servers", "server", "服务设置"),
    ("download", "download", "下载"),
    ("logs", "log", "运行日志"),
]

TAB_TITLE = dict((k, t) for k, _i, t in NAV)

SPEED_PRESETS = [
    (0, "不限速"), (1024, "1 MB/s"), (5120, "5 MB/s"),
    (10240, "10 MB/s"), (51200, "50 MB/s"), (102400, "100 MB/s"),
]


class App(object):
    def __init__(self, cfg=None, start_hidden=False):
        self.cfg = cfg or Config()
        name = self.cfg.get("ui", "theme") or "light"
        self.theme = dict(T.DARK if name == "dark" else T.LIGHT)
        self.log = EventLog()
        self.manager = ShareManager(self.cfg, self.log)
        self.downloader = Downloader(self.cfg, self.manager.dl_limiter,
                                     self.manager.stats, self.log,
                                     on_change=self._on_task_change)
        self.downloader.load_store()

        self.page = "overview"
        self._embedded = []
        self._closing = False
        self._min_guard = False
        self._ask_win = None
        self._busy = set()
        self._q = queue.Queue()
        self._poll_job = None
        self._tick_job = None
        self._last_speed = (0.0, 0.0, 0.0)
        self._link_bps = 0                  # 网卡链路速率（bps），后台探测
        self._link_info = None              # 监听网卡 {ip, name, speed, up}
        self._peers = {}                    # ip → 主机共享信息（含 seen 时间）
        self._peer_scan = {"running": False, "done": 0, "total": 0,
                           "last": 0.0, "error": ""}
        self._avg30 = {"up": 0.0, "down": 0.0, "dl": 0.0, "total": 0.0,
                       "peak": 0.0, "peak_total": 0.0, "span": 0.0, "n": 0}
        self._traffic_rows = []
        self._traffic_agg = []
        self._log_seq = -1
        self._dl_version = 0
        self._dl_seen = -1
        self._smb_issues = None             # SMB 体检结果缓存（None = 未查）
        self._smb_busy = False
        self._smb_fixing = False
        self._smb_last_repair = None        # (done, failed)，失败原因留在面板上
        self._dirty = True
        self._notice = ""
        self._notice_until = 0

        T.init_scaling()
        self.root = tk.Tk()
        self.root.title(window_title())
        self.root.configure(bg=self.theme["bg"])
        self.fonts = T.Fonts(self.root)
        self._set_window_geometry()
        self._set_icon()
        self._build()
        self._bind_events()

        self.tray = None
        self.single_instance = None       # main.py 里装配；退出时要还锁
        if tray_available():
            self._make_tray()
        if start_hidden and self.tray is not None:
            self._hide_to_tray()
        # 网卡链路速率放到后台探（ctypes 调用 ~1ms，但万一磁盘/驱动慢也别卡启动）
        threading.Thread(target=self._probe_link, daemon=True).start()
        self.root.after(120, self._autostart)

    def _probe_link(self):
        """查监听网卡的名字与协商速率（几毫秒，但也不值得卡启动）。"""
        try:
            bind = self.cfg.get("listen", "ip") or "0.0.0.0"
            info = adapter_by_ip(bind) if bind != "0.0.0.0" \
                else adapter_by_ip(primary_ip())
            if info is None and bind == "0.0.0.0":
                # 多网卡没精确匹配时 link_speed 会兜底取最快的，info 同样给个说法
                from ..netutil import primary_adapter
                info = primary_adapter()
            from ..netutil import link_speed
            bps = link_speed(bind if bind != "0.0.0.0" else None, refresh=True)
        except Exception:                                  # noqa: BLE001
            info, bps = None, 0
        self._link_info = info
        self._link_bps = bps

    # ------------------------------------------------------------ 窗口
    def _set_window_geometry(self):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = min(T.px(1180), int(sw * 0.86))
        h = min(T.px(760), int(sh * 0.82))
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.root.minsize(T.px(940), T.px(620))

    def _set_icon(self):
        try:
            from ..resources import asset_path
            ico = asset_path("icon.ico")
            if os.path.isfile(ico):
                self.root.iconbitmap(default=ico)
        except Exception:                                  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 骨架
    def _build(self):
        t = self.theme
        self.root_bg = t["bg"]

        # ---- 顶栏 ----
        top = tk.Frame(self.root, bg=t["bg"])
        top.pack(side="top", fill="x", padx=T.px(18), pady=(T.px(14), 0))
        self.top = top

        left = tk.Frame(top, bg=t["bg"])
        left.pack(side="left")
        tk.Label(left, text=APP_NAME, font=self.fonts.h1, bg=t["bg"],
                 fg=t["tx"]).pack(anchor="w")
        tk.Label(left, text=APP_SUB, font=self.fonts.small, bg=t["bg"],
                 fg=t["tx2"]).pack(anchor="w")

        right = tk.Frame(top, bg=t["bg"])
        right.pack(side="right")
        # 主操作按钮的固定槽位：非下载页放「全部启动/全部停止」服务控制，
        # 下载页换成「全部暂停/全部继续」。两个按钮同一位置交替显示。
        self.btn_slot = tk.Frame(right, bg=t["bg"])
        self.btn_slot.pack(side="right", padx=(T.px(8), 0))
        self.btn_all = PillButton(self.btn_slot, self.fonts, t, "全部启动",
                                  command=self._toggle_services,
                                  style="primary", icon="play", height=32)
        self.btn_dl = PillButton(self.btn_slot, self.fonts, t, "全部暂停",
                                 command=self._toggle_all,
                                 style="primary", icon="play", height=32)
        self.btn_all.pack(side="right")
        self.btn_theme = PillButton(right, self.fonts, t, "深色",
                                    command=self._toggle_theme,
                                    style="secondary", icon="theme", height=32)
        self.btn_theme.pack(side="right", padx=(T.px(8), 0))
        self.btn_tray = PillButton(right, self.fonts, t, "缩到托盘",
                                   command=self._hide_to_tray,
                                   style="secondary", icon="close", height=32)
        self.btn_tray.pack(side="right", padx=(T.px(8), 0))

        # ---- 分隔线 ----
        tk.Frame(self.root, bg=t["border"], height=1).pack(
            side="top", fill="x", padx=T.px(18), pady=(T.px(12), 0))

        # ---- 状态栏 ----
        bar = tk.Frame(self.root, bg=t["bg"], height=T.px(34))
        bar.pack(side="bottom", fill="x", padx=T.px(18))
        self.bar = bar
        self.lbl_status = tk.Label(bar, text="就绪", font=self.fonts.small,
                                   bg=t["bg"], fg=t["tx2"], anchor="w")
        self.lbl_status.pack(side="left", pady=T.px(7))
        self.lbl_speed = tk.Label(bar, text="", font=self.fonts.small,
                                  bg=t["bg"], fg=t["tx2"], anchor="e")
        self.lbl_speed.pack(side="right", pady=T.px(7))
        tk.Label(bar, text="%s · v%s" % (AUTHOR, __version__),
                 font=self.fonts.tiny, bg=t["bg"], fg=t["tx3"],
                 anchor="e").pack(side="right", padx=T.px(12), pady=T.px(7))

        # ---- 主体 ----
        body = tk.Frame(self.root, bg=t["bg"])
        body.pack(side="top", fill="both", expand=True, padx=T.px(18),
                  pady=(T.px(10), T.px(4)))
        self.body = body

        side = tk.Frame(body, bg=t["bg"], width=T.px(178))
        side.pack(side="left", fill="y", padx=(0, T.px(12)))
        side.pack_propagate(False)
        self.nav_items = {}
        for key, icon, text in NAV:
            item = NavItem(side, self.fonts, t, text, icon,
                           command=lambda k=key: self.show_page(k),
                           width=178, height=40)
            item.pack(fill="x", pady=T.px(1))
            self.nav_items[key] = item
        self.nav_items["overview"].set_selected(True)

        # 侧边栏底部：管理员状态
        self.lbl_admin = tk.Label(
            side, text="", font=self.fonts.tiny, bg=t["bg"],
            fg=t["tx3"], anchor="w", justify="left", wraplength=T.px(168))
        self.lbl_admin.pack(side="bottom", anchor="w", pady=T.px(10))
        if not is_admin():
            btn = PillButton(side, self.fonts, t, "以管理员重启",
                             command=self._restart_as_admin, style="subtle",
                             icon="shield", height=28)
            btn.pack(side="bottom", anchor="w", pady=T.px(4))

        content = tk.Frame(body, bg=t["bg"])
        content.pack(side="left", fill="both", expand=True)
        self.content = content

        self.scroll = ScrollArea(content, t)
        self.scroll.pack(fill="both", expand=True)
        c = self.scroll.canvas
        c.tag_bind("scanact", "<Button-1>", self._on_action)
        c.bind("<Configure>", lambda e: self._on_canvas_resize())

        self.logview = self._build_logview(content)

        self.render_page()
        self._refresh_admin_label()
        self._sync_header_buttons()

    def _build_logview(self, parent):
        t = self.theme
        frame = tk.Frame(parent, bg=t["bg"])
        bar = tk.Frame(frame, bg=t["bg"])
        bar.pack(side="top", fill="x", pady=(0, T.px(8)))
        PillButton(bar, self.fonts, t, "清空", command=self._clear_log,
                   style="secondary", height=28).pack(side="left")
        PillButton(bar, self.fonts, t, "复制全部", command=self._copy_log,
                   style="secondary", height=28).pack(side="left",
                                                     padx=(T.px(8), 0))
        self.lbl_logcount = tk.Label(bar, text="", font=self.fonts.tiny,
                                     bg=t["bg"], fg=t["tx3"])
        self.lbl_logcount.pack(side="right")

        wrap = tk.Frame(frame, bg=t["border"], padx=1, pady=1)
        wrap.pack(fill="both", expand=True)
        self.logtext = tk.Text(
            wrap, bg=t["card"], fg=t["tx"], relief="flat", bd=0,
            font=self.fonts.small, wrap="none", padx=T.px(12), pady=T.px(10),
            insertbackground=t["tx"], selectbackground=t["accent"],
            selectforeground=t["accent_text"], spacing1=1, spacing3=3)
        vsb = tk.Scrollbar(wrap, command=self.logtext.yview, width=T.px(12))
        self.logtext.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.logtext.pack(side="left", fill="both", expand=True)
        self.logtext.tag_configure("info", foreground=t["tx2"])
        self.logtext.tag_configure("ok", foreground=t["status_ok"])
        self.logtext.tag_configure("warn", foreground=t["status_warn"])
        self.logtext.tag_configure("err", foreground=t["status_bad"])
        self.logtext.tag_configure("dl", foreground=t["accent"])
        self.logtext.configure(state="disabled")
        return frame

    def _bind_events(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Unmap>", self._on_minimize)
        self.root.bind("<Map>", lambda e: None)
        for seq in ("<Control-q>",):
            self.root.bind(seq, lambda e: self._quit())
        # Tk 回调里漏网的异常：默认处理器往 stderr 写 traceback，
        # windowed 打包没有 stderr → OSError [Errno 22] 直接把进程带走
        # （踩过：一次 _sync_log 的 TclError 让整个程序当场消失）。
        # 改成写日志 + 落盘，界面保住。
        self.root.report_callback_exception = self._report_callback_exception

    def _report_callback_exception(self, exc, val, tb):
        import traceback as _tb
        text = "".join(_tb.format_exception(exc, val, tb)).strip()
        try:
            self.log.add("err", "界面回调异常：%s" % (val or exc), "系统")
        except Exception:                                   # noqa: BLE001
            pass
        write_crash_log(text)

    # ------------------------------------------------------------ 页面
    def show_page(self, key):
        if key == self.page:
            return
        first_visit = (key == "peers" and self.page != "peers")
        self.page = key
        for k, item in self.nav_items.items():
            item.set_selected(k == key)
        self._sync_header_buttons()
        # 切页一律回顶：不把上一页的滚动比例带到新页
        self.render_page(keep_scroll=False)
        if first_visit:
            # 切回「共享网络」页就主动扫一次（后台线程，界面无感）
            self._start_peer_scan(manual=False)

    def render_page(self, keep_scroll=True):
        if self.page == "logs":
            self.scroll.pack_forget()
            self.logview.pack(fill="both", expand=True)
            self._sync_log(force=True)
            return
        self.logview.pack_forget()
        if not self.scroll.winfo_ismapped():
            self.scroll.pack(fill="both", expand=True)

        # 重绘会销毁所有输入控件——先把焦点输入框的内容和焦点记下来，
        # 画完再还回去。没有这一步，下载页每秒随进度重绘时，
        # 正在输入的链接会整个消失（用户报告过的失焦问题）。
        snap = self._snapshot_entry_focus()

        c = self.scroll.canvas
        # keep_scroll=False（切页）时回顶：滚动比例是「当前页内容」的比例，
        # 直接带给下一页会把短页也挪到半山腰，标题被拉得看不见
        # （用户报告的滚轮 bug 的一半根源）。
        pos = c.yview()[0] if keep_scroll else 0.0
        for w in self._embedded:
            try:
                w.destroy()
            except Exception:                              # noqa: BLE001
                pass
        self._embedded = []
        c.delete("all")
        c._act_map = {}
        c._act_seq = 0
        c._img_refs = []

        width = c.winfo_width() or T.px(880)
        p = Painter(c, self.fonts, self.theme, width)
        fn = getattr(self, "_render_" + self.page, None)
        if fn:
            try:
                fn(p)
            except Exception as exc:                        # noqa: BLE001
                import traceback
                traceback.print_exc()
                p.body("页面渲染出错：%s" % exc, color=self.theme["status_bad"])
        p.gap(24)
        self.scroll.set_scrollregion(p.y)
        self.scroll.restore(pos)
        self._restore_entry_focus(snap)

    # ---- 输入焦点保护（配合 render_page 的销毁重建） ----
    def _snapshot_entry_focus(self):
        try:
            w = self.root.focus_get()
        except Exception:                                  # noqa: BLE001
            return None
        if w is None or w not in self._embedded:
            return None
        try:
            value = w.get()
        except Exception:                                  # noqa: BLE001
            return None
        if isinstance(w, ttk.Combobox):
            return None                    # 下拉框没有自由输入，不用回填
        return {"idx": self._embedded.index(w),
                "cls": type(w).__name__, "value": value}

    def _restore_entry_focus(self, snap):
        if not snap or self._closing:
            return
        i = snap["idx"]
        if i >= len(self._embedded):
            return
        w = self._embedded[i]
        if type(w).__name__ != snap["cls"]:
            return
        try:
            if isinstance(w, tk.Entry):
                if w.get() != snap["value"]:
                    w.delete(0, "end")
                    w.insert(0, snap["value"])
            w.focus_set()
            try:
                w.icursor("end")
            except Exception:                              # noqa: BLE001
                pass
        except Exception:                                  # noqa: BLE001
            pass

    def _on_canvas_resize(self):
        if self._closing:
            return
        w = self.scroll.canvas.winfo_width()
        if w < 200:
            return
        if getattr(self, "_last_w", 0) != w:
            self._last_w = w
            self.render_page()

    def _on_action(self, event):
        c = self.scroll.canvas
        try:
            cb = action_at(c, c.canvasx(event.x), c.canvasy(event.y))
        except Exception:                                  # noqa: BLE001
            cb = None
        if cb:
            try:
                cb()
            except Exception as exc:                        # noqa: BLE001
                self._toast("操作失败：%s" % exc)

    # ------------------------------------------------------------ 概览页
    def _render_overview(self, p):
        t = self.theme
        st = self.manager.status()
        up, down, dl = self._last_speed
        p.heading("概览", "本机共享出去的目录，以及此刻的传输情况")
        p.gap(2)

        # 三个协议并排一行（首屏就能看全）
        self._proto_tiles(p, [
            ("http", "HTTP 下载", "浏览器 / 多线程下载", st["http"]),
            ("ftp", "FTP 服务", "资源管理器 / FileZilla", st["ftp"]),
            ("smb", "Windows 共享", "\\\\%s\\共享名" % st["ip"], st["smb"]),
        ])

        # 带宽（总带宽 / 近 30 秒平均 / 网卡占用 / 曲线）
        p.section("带宽使用", "近 30 秒平均 · 曲线为最近 90 秒")
        p.card(lambda: self._speed_block(p, up, down, dl), pad=16)

        # 共享摘要
        p.section("共享目录", "%d 个" % st["shares"])
        shares = self.manager.shares()
        if not shares:
            p.card(lambda: (
                p.gap(4),
                p.body("还没有共享任何目录。", color=t["tx"]),
                p.body("到「共享目录」页添加一个文件夹，启动服务后局域网就能访问。",
                       color=t["tx2"], font=self.fonts.small),
                p.gap(4),
                p.actions([("添加共享目录", self._add_share, "primary"),
                           ("去设置页", lambda: self.show_page("servers"),
                            "secondary")]),
            ))
        else:
            for s in shares[:4]:
                self._share_row(p, s, compact=True)
            if len(shares) > 4:
                p.body("另有 %d 个共享目录，到「共享目录」页查看。" % (len(shares) - 4),
                       color=t["tx3"], font=self.fonts.small)

        # 连接提示
        p.section("怎么访问")
        p.card(lambda: self._access_block(p, st))

    def _proto_tiles(self, p, items):
        """三张协议磁贴横向并排（手绘，因为 Painter 的 card() 只能纵向堆）。"""
        t = self.theme
        f = self.fonts
        gap = T.px(10)
        n = len(items)
        tw = (p.w - gap * (n - 1)) / float(n)
        th = T.px(152)
        y0 = p.y
        pad = T.px(15)
        btn_h = T.px(28)

        for i, (proto, title, desc, info) in enumerate(items):
            x = p.x + i * (tw + gap)
            T.draw_round_rect(p.c, x, y0, x + tw, y0 + th, radius=T.px(8),
                              fill=t["card"], outline=t["border"], width=1,
                              photo_holder=p._holder())
            on = bool(info.get("on"))
            color = t["status_ok"] if on else t["tx3"]
            cy = y0 + pad

            # 状态点 + 名称
            line_h = f.h3.metrics("linespace")
            p.c.create_oval(x + pad, cy + line_h * 0.40,
                            x + pad + T.px(7), cy + line_h * 0.40 + T.px(7),
                            fill=color, outline="")
            p.c.create_text(x + pad + T.px(13), cy, text=title, anchor="nw",
                            font=f.h3, fill=t["tx"])
            cy += line_h + T.px(2)
            p.c.create_text(x + pad, cy, text=desc, anchor="nw",
                            font=f.tiny, fill=t["tx3"])
            cy += f.tiny.metrics("linespace") + T.px(6)

            # 地址 / 状态
            iw = tw - 2 * pad
            if on:
                addr = info.get("url") or ""
                p.c.create_text(x + pad, cy, text=addr, anchor="nw",
                                font=f.tiny, fill=t["accent"], width=iw)
                cy += f.tiny.metrics("linespace") + T.px(2)
                bits = []
                if proto != "smb":
                    bits.append("%d 连接" % (info.get("conns") or 0))
                if proto == "http":
                    bits.append("%d 次请求" % (info.get("reqs") or 0))
                bits.append("端口 %s" % info.get("port", "-"))
                p.c.create_text(x + pad, cy, text=" · ".join(bits), anchor="nw",
                                font=f.tiny, fill=t["tx2"], width=iw)
            else:
                text = (info.get("error") or "未启动")[:40]
                p.c.create_text(x + pad, cy, text=text, anchor="nw",
                                font=f.tiny,
                                fill=t["status_warn"] if info.get("error")
                                else t["tx3"], width=iw)
            # 按钮贴底
            by = y0 + th - pad - btn_h
            bw = (iw - T.px(6)) / 2.0
            p.button(x + pad, by, bw, btn_h, "停止" if on else "启动",
                     lambda pr=proto: self._toggle_proto(pr),
                     "secondary" if on else "primary")
            p.button(x + pad + bw + T.px(6), by, bw, btn_h, "复制地址",
                     lambda u=(info.get("url") or ""): self._copy(u), "subtle")

        p.y = y0 + th + T.px(12)

    def _speed_block(self, p, up, down, dl):
        t = self.theme
        f = self.fonts
        hist = self.manager.stats.history(90)
        ups = [h[1] for h in hist]
        downs = [h[2] for h in hist]
        ag = self._avg30
        # 只统计「本软件共享出去的流量」：服务端发送 + 客户端上传进来。
        # 不含系统里其它程序的流量（根本没采，读的是自己的计数器），
        # 也不含内置下载器（那是本软件当客户端去下别人的东西）。
        total_now = up + down
        avg_total = ag["up"] + ag["down"]
        link = self._link_bps
        # 注意单位：total 是「字节/秒」，link 是「比特/秒」，要 ×8 才能比
        frac = min(1.0, (total_now * 8.0 / float(link))) if link else 0.0

        # ---- 两个主数字：此刻共享带宽 / 近 30 秒平均 ----
        big_h = f.huge.metrics("linespace")
        lab_h = f.small.metrics("linespace")
        sub_h = f.tiny.metrics("linespace")
        gapx = T.px(18)
        col_w = (p.w - gapx) / 2.0
        span = ag.get("span") or 0.0
        avg_label = ("近 30 秒平均" if span >= 28
                     else "平均（已采样 %.0f 秒）" % max(1.0, span))
        cols = [
            ("共享总带宽（此刻）", human_speed(total_now), t["accent"],
             "发送给客户端 %s   接收上传 %s" % (human_speed(up),
                                          human_speed(down))),
            (avg_label, human_speed(avg_total), t["status_ok"],
             "发送 %s   接收 %s" % (human_speed(ag["up"]),
                                  human_speed(ag["down"]))),
        ]
        for i, (label, value, color, sub) in enumerate(cols):
            x = p.x + i * (col_w + gapx)
            p.c.create_text(x, p.y, text=value, anchor="nw", font=f.huge,
                            fill=color)
            p.c.create_text(x, p.y + big_h + T.px(2), text=label, anchor="nw",
                            font=f.small, fill=t["tx2"])
            p.c.create_text(x, p.y + big_h + lab_h + T.px(4), text=sub,
                            anchor="nw", font=f.tiny, fill=t["tx3"],
                            width=col_w)
        p.y += big_h + lab_h + sub_h + T.px(11)

        # ---- 网卡占用：共享总带宽 ÷ 链路速率 ----
        if link:
            over = frac >= 0.85
            p.progress_track(frac, height=6,
                             fill=t["status_bad"] if over else t["accent"])
            p.body("%s · 当前占用 %.1f%%%s" % (
                self._nic_label(), frac * 100,
                "（已接近链路上限）" if over else ""),
                color=t["status_bad"] if over else t["tx3"], font=f.tiny)
        else:
            p.body("没读到网卡协商速率，占用比例暂不可用（不影响流量统计）",
                   color=t["tx3"], font=f.tiny)
        p.gap(5)

        # ---- 曲线：只画共享流量（上行 / 下行）----
        # 下载器曲线已移到「下载」页——这里只看共享这条腿，口径更纯粹
        spark = Sparkline(p.c, t, height=104, bg=t["card"])
        spark.set_series([(ups, t["accent"], "上行"),
                          (downs, t["status_ok"], "下行")])
        self._embed(p, spark, T.px(104))

        # 图例（颜色跟曲线一一对应，否则两条线分不清）
        ly = p.y
        lx = p.x
        for name, color in (("发送给客户端", t["accent"]),
                            ("接收上传", t["status_ok"])):
            p.c.create_oval(lx, ly + T.px(3), lx + T.px(7), ly + T.px(10),
                            fill=color, outline="")
            _it, bb = p._text(lx + T.px(11), ly, name, f.tiny, t["tx3"])
            lx = bb[2] + T.px(16)
        p.y = ly + f.tiny.metrics("linespace") + T.px(4)

        n_dev = len([a for a in self._traffic_agg if a["conns"] > 0])
        tot = self.manager.stats.totals()
        peak_share = max([0.0] + [u + d for u, d in zip(ups, downs)])
        p.body("只统计本软件共享出去的流量（下载器的速度在「下载」页看）",
               color=t["tx3"], font=f.tiny)
        p.body("30 秒峰值 %s · 累计发送 %s · 累计接收 %s · %d 台设备在下载" % (
            human_speed(peak_share), human_bytes(tot["sent"]),
            human_bytes(tot["recv"]), n_dev),
            color=t["tx3"], font=f.tiny)

    def _share_row(self, p, s, compact=False):
        t = self.theme
        name = s.get("name") or os.path.basename(s.get("path", ""))
        path = s.get("path") or ""
        enabled = s.get("enabled", True)
        exists = os.path.isdir(path)

        def draw():
            p.gap(2)
            _it, bb = p._text(p.x, p.y, name, self.fonts.h3, t["tx"])
            p.y = bb[3] + T.px(1)
            p.mono_text(path, color=t["tx2"], width=p.w - T.px(10))
            chips = []
            if not enabled:
                chips.append(("已停用", t["tx3"], t["chip_bg"]))
            elif not exists:
                chips.append(("目录不存在", t["status_bad"], "#FDE7E9"))
            else:
                chips.append(("共享中", t["status_ok"], "#E6F4E6"))
            chips.append((_dir_size_text(path), t["tx2"], t["chip_bg"]))
            p.chip_row(chips)
            if compact:
                p.actions([("打开", lambda: self._open_path(path), "secondary"),
                           ("停用" if enabled else "启用",
                            lambda: self._toggle_share(s), "subtle")])
            else:
                p.actions([
                    ("打开目录", lambda: self._open_path(path), "secondary"),
                    ("启用" if not enabled else "停用",
                     lambda: self._toggle_share(s), "secondary"),
                    ("移除", lambda: self._remove_share(s), "danger"),
                ])
        p.card(draw, pad=15, gap=10)

    def _access_block(self, p, st):
        t = self.theme
        rows = []
        if st["http"]["on"]:
            rows.append(("浏览器（HTTP）", st["http"]["url"]))
            rows.append(("多线程下载", "在「下载」页粘贴上面的地址即可"))
        if st["ftp"]["on"]:
            rows.append(("资源管理器（FTP）", st["ftp"]["url"]))
            rows.append(("账号密码", "%s / %s" % (
                self.cfg.get("ftp", "user"),
                self.cfg.get("ftp", "password"))))
        if st["smb"]["on"]:
            rows.append(("Windows 共享", st["smb"]["url"]))
        if not rows:
            p.body("服务还没启动 —— 点右上角「全部启动」。", color=t["tx2"])
            return
        for label, value in rows:
            p.kv(label, value, kw=T.px(120))
        p.gap(2)
        p.body("同一局域网内的设备都能访问；跨网段需要在路由器上放行对应端口。",
               color=t["tx3"], font=self.fonts.tiny)

    # ------------------------------------------------------------ 连接监控页
    def _render_traffic(self, p):
        t = self.theme
        rows = self._traffic_rows
        agg = self._traffic_agg
        active = [r for r in rows if not r["ended"]]
        total = sum(a["speed"] for a in agg)
        ag = self._avg30
        link = self._link_bps
        p.heading("连接监控", "谁在从这里下载、每个 IP 此刻占多少带宽")
        p.gap(2)

        p.metric_tiles([
            ("活跃连接", str(len(active))),
            ("下载设备", str(len([a for a in agg if a["conns"] > 0]))),
            ("当前总带宽", human_speed(total)),
            ("近 30 秒平均", human_speed(ag["up"] + ag["down"])),
        ], cols=4)

        # 总带宽占用条：一眼看出还有多少余量
        if link:
            frac = min(1.0, total * 8.0 / float(link))
            over = frac >= 0.85
            p.progress_track(frac, height=6,
                             fill=t["status_bad"] if over else t["accent"])
            p.body("总带宽 %s / 网卡链路 %s · 占用 %.1f%%" % (
                human_speed(total), human_link(link), frac * 100),
                color=t["status_bad"] if over else t["tx3"],
                font=self.fonts.tiny)
        else:
            p.body("总带宽 %s（没读到网卡链路速率，占用比例不可用）" % (
                human_speed(total)), color=t["tx3"], font=self.fonts.tiny)

        p.actions([("刷新", self.render_page, "secondary"),
                   ("清空记录", self._clear_traffic, "subtle")])

        if not agg:
            p.empty_state(
                "现在没有人在下载",
                "有设备访问 HTTP / FTP 共享时，这里会按 IP 显示它的实时速度、"
                "正在传的文件和累计流量。",
                ["同一 IP 的多线程下载会合并成一行，速度是各线程之和",
                 "刚结束的连接会保留几秒，方便核对结果"])
            return

        p.section("按 IP 统计", "%d 台设备 · 合计 %s" % (len(agg), human_speed(total)))
        for a in agg[:12]:
            self._ip_card(p, a, total)
        if len(agg) > 12:
            p.body("另有 %d 台设备，看下面的明细表。" % (len(agg) - 12),
                   color=t["tx3"], font=self.fonts.small)

        p.section("连接明细", "%d 条" % len(rows))
        data = []
        for r in rows[:40]:
            data.append([
                r["ip"],
                ("下载" if r["direction"] == "down" else "上传") + " · " + r["proto"],
                (r["name"] or "-")[:32],
                "已结束" if r["ended"] else human_speed(r["speed"]),
                human_bytes(r["bytes"]),
                human_time(r["duration"]),
            ])
        p.card(lambda: p.table(
            ["IP", "方向 / 协议", "文件", "速度", "已传输", "时长"], data,
            widths=[1.4, 1.5, 2.6, 1.1, 1.0, 0.8]), pad=14)

    def _ip_card(self, p, a, total):
        t = self.theme
        live = a["conns"] > 0

        def draw():
            p.gap(2)
            line_h = self.fonts.h3.metrics("linespace")
            p.c.create_oval(p.x, p.y + line_h * 0.40, p.x + T.px(7),
                            p.y + line_h * 0.40 + T.px(7),
                            fill=t["status_ok"] if live else t["tx3"],
                            outline="")
            p.c.create_text(p.x + T.px(13), p.y, text=a["ip"], anchor="nw",
                            font=self.fonts.h3, fill=t["tx"])
            p.c.create_text(p.x + p.w, p.y,
                            text=human_speed(a["speed"]), anchor="ne",
                            font=self.fonts.h3,
                            fill=t["accent"] if live else t["tx3"])
            p.y += line_h + T.px(3)

            frac = (a["speed"] / total) if total > 0 else 0.0
            p.progress_track(frac, height=5)
            files = ", ".join(list(dict.fromkeys(a["files"]))[:3])
            bits = ["%d 个连接" % a["conns"], "累计 %s" % human_bytes(a["bytes"]),
                    "占总带宽 %.0f%%" % (frac * 100)]
            if files:
                bits.append(files[:56])
            p.body(" · ".join(bits), color=t["tx2"], font=self.fonts.tiny)
            p.gap(2)
        p.card(draw, pad=14, gap=8)

    def _clear_traffic(self):
        self.manager.traffic.clear()
        self._traffic_rows = []
        self._traffic_agg = []
        self._toast("已清空连接记录")
        self.render_page()

    # ------------------------------------------------------------ 共享目录页
    def _render_shares(self, p):
        t = self.theme
        shares = self.manager.all_shares()
        p.heading("共享目录", "每个目录会同时成为 HTTP / FTP 的一个子目录和 Windows 共享")
        p.gap(2)
        p.actions([("添加目录", self._add_share, "primary"),
                   ("全部启动", self._toggle_all, "secondary"),
                   ("刷新", self.render_page, "subtle")])
        p.section("已添加", "%d 个" % len(shares))
        if not shares:
            p.empty_state(
                "还没有共享目录",
                "选一个文件夹（比如电影、软件、照片），启动服务后，"
                "同局域网的手机、电视、另一台电脑就能直接访问。",
                ["HTTP 用浏览器打开 http://本机IP:8080",
                 "FTP 用资源管理器打开 ftp://本机IP:2121",
                 "SMB 用 \\\\本机IP\\共享名"])
            return
        for s in shares:
            self._share_row(p, s)

        p.section("说明")
        p.card(lambda: (
            p.body("· 目录名就是访问路径：共享目录 media → http://IP:8080/media/",
                   color=t["tx2"], font=self.fonts.small),
            p.body("· 停用不会删除目录，只是不再对外提供",
                   color=t["tx2"], font=self.fonts.small),
            p.body("· 同一个目录可以被多个协议同时使用，互不影响",
                   color=t["tx2"], font=self.fonts.small),
        ))

    # ------------------------------------------------------------ 服务设置页
    def _render_servers(self, p):
        t = self.theme
        st = self.manager.status()
        p.heading("服务设置", "端口、账号、权限与全局限速")
        p.gap(2)

        # HTTP
        p.section("HTTP 下载服务", "已启动" if st["http"]["on"] else "未启动")
        self._proto_settings(p, "http", st)

        # FTP
        p.section("FTP 服务", "已启动" if st["ftp"]["on"] else "未启动")
        self._proto_settings(p, "ftp", st)

        # SMB
        p.section("Windows 共享（SMB）", "需要管理员权限")
        self._smb_settings(p, st)

        # 监听网卡
        p.section("监听网卡", "HTTP 与 FTP 绑定在哪块网卡上（SMB 不适用）")
        p.card(lambda: self._bind_block(p), pad=16)

        # 限速
        p.section("带宽限制", "0 = 不限速，吃满能跑到的带宽")
        p.card(lambda: self._limit_block(p), pad=16)

        # 上传与安全
        p.section("上传与安全", "默认只读")
        p.card(lambda: self._security_block(p), pad=16)

    def _security_block(self, p):
        t = self.theme
        f = self.fonts
        up_ftp = bool(self.cfg.get("ftp", "upload"))

        p.chip_row([
            ("当前：%s" % ("只读（仅下载）" if not up_ftp else "允许 FTP 上传"),
             t["status_ok"] if not up_ftp else t["status_warn"],
             "#E6F4E6" if not up_ftp else "#FFF4E5"),
        ])
        p.gap(2)
        rows = [
            ("上传默认关", "HTTP 的 PUT 与 FTP 的 STOR/DELE/MKD 都出厂关闭，"
                        "要你在上面手动打开；关掉服务再启动也不会自己变回开启。"),
            ("不能跳出目录", "所有上传路径逐段校验：拒绝 .. 、绝对路径、盘符、"
                          "NTFS 备用数据流（冒号）、保留设备名（CON/NUL/COM1）"
                          "和尾随点空格，再用 realpath 确认没跑出共享目录。"),
            ("上传的文件不能执行", "可执行扩展名（exe/bat/cmd/ps1/vbs/msi/lnk…）"
                             "落盘后自动改名成 xxx.noexec，并追加「拒绝执行」"
                             "ACL —— 就算手动改回 .exe 也执行不了。"),
            ("不覆盖已有文件", "目标同名时直接拒绝（HTTP 409 / FTP 553），"
                         "只有 REST 续传例外。"),
            ("SMB 不归它管", "Windows 共享由系统内核传输，本程序既统计不到它的"
                          "流量，也控制不了上传 —— 只读请用 SMB 自己的共享权限。"),
        ]
        for title, desc in rows:
            _it, bb = p._text(p.x, p.y, "· " + title, f.small_b, t["tx"])
            p.y = bb[3] + T.px(1)
            p.body(desc, color=t["tx3"], font=f.tiny, indent=T.px(10))
            p.y += T.px(3)

    def _proto_settings(self, p, proto, st):
        t = self.theme
        cfg = self.cfg.data[proto]
        info = st[proto]

        def draw():
            p.gap(2)
            # 开关行
            row_y = p.y
            sw = Switch(p.c, self.fonts, t, value=info["on"],
                        command=lambda v, pr=proto: self._toggle_proto(pr, v))
            p.c.create_window(p.x, row_y, window=sw, anchor="nw")
            self._embedded.append(sw)
            p.c.create_text(p.x + T.px(56), row_y + T.px(2),
                            text="随程序启动" if info["on"] else "已停止",
                            anchor="nw", font=self.fonts.body, fill=t["tx"])
            p.y = row_y + T.px(34)

            # 端口
            p.c.create_text(p.x, p.y + T.px(5), text="监听端口", anchor="nw",
                            font=self.fonts.small, fill=t["tx3"])
            ent = LineEntry(p.c, self.fonts, t, str(cfg.get("port")), width=8,
                            command=lambda v, pr=proto: self._set_port(pr, v))
            p.c.create_window(p.x + T.px(76), p.y, window=ent, anchor="nw",
                              height=T.px(26))
            self._embedded.append(ent)
            if info["on"]:
                p.c.create_text(p.x + T.px(186), p.y + T.px(5),
                                text="%s" % (info.get("url") or ""),
                                anchor="nw", font=self.fonts.tiny,
                                fill=t["accent"])
            p.y += T.px(34)

            if proto == "ftp":
                p.c.create_text(p.x, p.y + T.px(5), text="用户名", anchor="nw",
                                font=self.fonts.small, fill=t["tx3"])
                eu = LineEntry(p.c, self.fonts, t, cfg.get("user"), width=12,
                               command=lambda v: self._set_text("ftp", "user", v))
                p.c.create_window(p.x + T.px(76), p.y, window=eu, anchor="nw",
                                  height=T.px(26))
                self._embedded.append(eu)
                p.c.create_text(p.x + T.px(200), p.y + T.px(5), text="密码",
                                anchor="nw", font=self.fonts.small,
                                fill=t["tx3"])
                ep = LineEntry(p.c, self.fonts, t, cfg.get("password"), width=12,
                               command=lambda v: self._set_text("ftp", "password", v))
                p.c.create_window(p.x + T.px(240), p.y, window=ep, anchor="nw",
                                  height=T.px(26))
                self._embedded.append(ep)
                p.y += T.px(34)

                sw2 = Switch(p.c, self.fonts, t, value=bool(cfg.get("upload")),
                             command=lambda v: self._set_bool("ftp", "upload", v))
                p.c.create_window(p.x, p.y, window=sw2, anchor="nw")
                self._embedded.append(sw2)
                p.c.create_text(p.x + T.px(56), p.y + T.px(3),
                                text="允许上传（默认关闭 · 关闭时只能下载）",
                                anchor="nw", font=self.fonts.body, fill=t["tx"])
                p.y += T.px(30)
                p.body("关闭后 STOR / DELE / MKD 等写命令一律返回 550，"
                       "FTP 只剩下载能力。", color=t["tx3"], font=self.fonts.tiny)
                p.y += T.px(4)

            if proto == "http":
                sw3 = Switch(p.c, self.fonts, t, value=bool(cfg.get("upload")),
                             command=lambda v: self._set_bool("http", "upload", v,
                                                              restart=True))
                p.c.create_window(p.x, p.y, window=sw3, anchor="nw")
                self._embedded.append(sw3)
                p.c.create_text(p.x + T.px(56), p.y + T.px(3),
                                text="允许网页上传（默认关闭 · 关闭时只能下载）",
                                anchor="nw", font=self.fonts.body, fill=t["tx"])
                p.y += T.px(30)
                p.body("打开后网页出现上传按钮；上传路径全程校验，"
                       "可执行文件落盘自动改名并禁止执行。改完需重启服务生效。",
                       color=t["tx3"], font=self.fonts.tiny)
                p.y += T.px(4)
                sw4 = Switch(p.c, self.fonts, t,
                             value=bool(cfg.get("dirlist", True)),
                             command=lambda v: self._set_bool("http", "dirlist", v,
                                                              restart=True))
                p.c.create_window(p.x, p.y, window=sw4, anchor="nw")
                self._embedded.append(sw4)
                p.c.create_text(p.x + T.px(56), p.y + T.px(3),
                                text="显示目录列表（关掉后只能按完整链接下载）",
                                anchor="nw", font=self.fonts.body, fill=t["tx"])
                p.y += T.px(34)

            if info.get("error"):
                p.body(info["error"], color=t["status_warn"],
                       font=self.fonts.tiny)
        p.card(draw, pad=16)

    def _smb_settings(self, p, st):
        t = self.theme
        f = self.fonts
        admin = st["admin"]
        mode = st["smb"].get("mode") or "everyone"
        everyone = mode == "everyone"

        def draw():
            p.gap(2)
            sw = Switch(p.c, f, t, value=st["smb"]["on"],
                        command=lambda v: self._toggle_proto("smb", v))
            p.c.create_window(p.x, p.y, window=sw, anchor="nw")
            self._embedded.append(sw)
            p.c.create_text(p.x + T.px(56), p.y + T.px(3),
                            text="创建同名 Windows 共享（\\\\本机IP\\共享名）",
                            anchor="nw", font=f.body, fill=t["tx"])
            p.y += T.px(30)

            sw2 = Switch(p.c, f, t,
                         value=bool(self.cfg.get("smb", "readonly", default=True)),
                         command=lambda v: self._set_bool("smb", "readonly", v,
                                                          restart=True))
            p.c.create_window(p.x, p.y, window=sw2, anchor="nw")
            self._embedded.append(sw2)
            p.c.create_text(p.x + T.px(56), p.y + T.px(3),
                            text="只读（关掉后局域网可写入）", anchor="nw",
                            font=f.body, fill=t["tx"])
            p.y += T.px(30)

            # 免密只读开关：打开 = Everyone 权限，任何设备直接进
            sw3 = Switch(p.c, f, t, value=everyone,
                         command=self._set_smb_mode)
            p.c.create_window(p.x, p.y, window=sw3, anchor="nw")
            self._embedded.append(sw3)
            p.c.create_text(p.x + T.px(56), p.y + T.px(3),
                            text="免密只读（Everyone 权限，其它设备直接进，不用账号）",
                            anchor="nw", font=f.body, fill=t["tx"])
            p.y += T.px(32)

            # 账号模式：用户名 / 密码可自定义
            if not everyone:
                p.c.create_text(p.x, p.y + T.px(6), text="用户名", anchor="nw",
                                font=f.small, fill=t["tx3"])
                eu = LineEntry(p.c, f, t, st["smb"].get("user") or "FileShare",
                               width=13,
                               command=lambda v: self._set_smb_cred("user", v))
                p.c.create_window(p.x + T.px(76), p.y, window=eu, anchor="nw",
                                  height=T.px(26))
                self._embedded.append(eu)
                p.c.create_text(p.x + T.px(210), p.y + T.px(6), text="密码",
                                anchor="nw", font=f.small, fill=t["tx3"])
                ep = LineEntry(p.c, f, t, st["smb"].get("password") or "",
                               width=15,
                               command=lambda v: self._set_smb_cred("password", v))
                p.c.create_window(p.x + T.px(250), p.y, window=ep, anchor="nw",
                                  height=T.px(26))
                self._embedded.append(ep)
                p.y += T.px(26)
                p.body("改完点下方「修复局域网访问」生效。密码需满足系统复杂度"
                       "（至少 8 位，含大小写/数字/符号更稳）。",
                       color=t["tx3"], font=f.tiny)
                p.y += T.px(20)

            if not admin:
                p.body("当前不是管理员：SMB 共享需要管理员权限，"
                       "点右上角「以管理员身份重启」。", color=t["status_warn"],
                       font=f.small)
            elif st["smb"]["on"]:
                p.mono_text(st["smb"]["url"], color=t["accent"])
                if everyone:
                    p.body("其它设备直接打开上面的地址即可，不用输入账号密码。",
                           color=t["tx2"], font=f.small)
                else:
                    p.body("其它机器访问时输入用户名 %s、密码 %s"
                           % (st["smb"].get("user") or "FileShare",
                              st["smb"].get("password") or ""),
                           color=t["tx2"], font=f.small)
            if st["smb"].get("error"):
                p.body(st["smb"]["error"], color=t["status_warn"], font=f.tiny)

            # ---- 局域网访问体检 ----
            self._smb_check()
            issues = self._smb_issues
            if issues is None:
                p.gap(6)
                p.body("正在检查局域网访问条件…", color=t["tx3"], font=f.tiny)
            else:
                errs = [i for i in issues if i["level"] == "error"]
                real = [i for i in issues if i.get("fixable")]
                if not errs:
                    p.gap(6)
                    p.body("✓ 局域网访问条件正常：其它机器可以直接打开上面的地址。",
                           color=t["status_ok"], font=f.small)
                else:
                    p.gap(8)
                    p.body("发现 %d 个会导致完全连不上的问题" % len(errs),
                           color=t["status_warn"], font=f.small)
                for it in (errs if errs else []):
                    p.c.create_text(p.x, p.y, text="· " + it["title"],
                                    anchor="nw", font=f.small,
                                    fill=t["status_bad"],
                                    width=p.w - T.px(80))
                    p.y += T.px(20)
                    if it.get("detail"):
                        p.c.create_text(p.x + T.px(12), p.y, text=it["detail"],
                                        anchor="nw", font=f.tiny, fill=t["tx3"],
                                        width=p.w - T.px(96))
                        p.y += T.px(18) * max(1, 1 + len(it["detail"]) // 46)

                # 上次修复失败的原因要留在界面上，不能只闪一下 toast
                last = getattr(self, "_smb_last_repair", None)
                if last:
                    done, failed = last
                    p.gap(4)
                    if failed:
                        p.body("上次修复 %d 项成功，%d 项失败：" % (
                            len(done), len(failed)), color=t["status_bad"],
                            font=f.tiny)
                        for msg in failed[:4]:
                            p.c.create_text(p.x + T.px(12), p.y, text="· " + msg,
                                            anchor="nw", font=f.tiny,
                                            fill=t["tx3"],
                                            width=p.w - T.px(96))
                            p.y += T.px(18) * max(1, 1 + len(msg) // 40)
                    else:
                        p.body("上次修复：%d 项全部成功" % len(done),
                               color=t["status_ok"], font=f.tiny)

                if issues and (real or last):
                    bh = T.px(32)
                    p.button(p.x, p.y + T.px(4), T.px(160), bh,
                             "修复局域网访问", self._smb_repair, "primary",
                             font=f.small)
                    p.y += bh + T.px(12)
                    hint = ("修复会：开启免密访问（Guest）、放行 TCP 445、"
                            "给共享目录授 Everyone 读权限。"
                            if everyone else
                            "修复会：创建/更新共享账户 %s、放行 TCP 445、"
                            "给共享目录授该账户读权限。"
                            % (st["smb"].get("user") or "FileShare"))
                    p.c.create_text(
                        p.x, p.y,
                        text=hint + "取消共享时会自动把这些权限还原。",
                        anchor="nw", font=f.tiny, fill=t["tx3"],
                        width=p.w - T.px(20))
                    p.y += T.px(16) * 2
            p.gap(4)
            p.body("Windows 自带的 SMB 是内核态实现，局域网大文件传输比 HTTP 还快，"
                   "适合当网盘用。", color=t["tx3"], font=f.tiny)
        p.card(draw, pad=16)

    def _set_smb_mode(self, everyone):
        """切换免密 / 账号模式。已在共享中时立刻按新模式重做一遍。"""
        self.cfg.set("everyone" if everyone else "account", "smb", "mode")
        self.cfg.save()
        self._smb_issues = None
        self._smb_last_repair = None
        self._toast("已切换为%s模式" % ("免密只读" if everyone else "账号密码"))
        if self.manager.is_running("smb"):
            # 已在共享中：按新模式重做授权（_smb_repair 自己会置忙标志）
            self._smb_repair()
        else:
            self.render_page()

    def _set_smb_cred(self, key, value):
        """改共享账户用户名/密码（落盘，重启后仍生效）。"""
        value = (value or "").strip()
        if key == "user" and (not value or " " in value):
            return                      # 用户名不能空、不能带空格
        self.cfg.set(value, "smb", key)
        self.cfg.save()
        self._smb_issues = None

    def _nic_label(self):
        """一句话说清当前用的哪块网卡、协商速率多少。"""
        info = self._link_info or {}
        name = info.get("name") or "未知网卡"
        speed = human_link(self._link_bps) if self._link_bps else "协商速率未知"
        bind = self.cfg.get("listen", "ip") or "0.0.0.0"
        if bind != "0.0.0.0":
            return "监听网卡「%s」· IP %s · 协商速率 %s" % (name, bind, speed)
        ip = info.get("ip") or primary_ip()
        return "网卡「%s」（全部网卡监听，出口 IP %s）· 协商速率 %s" % (
            name, ip, speed)

    def _bind_block(self, p):
        """监听网卡选择：下拉列出全部网卡（带名字 / IP / 协商速率）。"""
        t = self.theme
        cur = self.cfg.get("listen", "ip") or "0.0.0.0"
        choices = listen_choices()
        if all(ip != cur for ip, _lb in choices):
            # 配置里的 IP 现在枚举不到（拔了网线/换网），仍要显示出来
            choices.append((cur, "%s（当前不可用）" % cur))
        ips = [ip for ip, _lb in choices]
        labels = [lb for _ip, lb in choices]

        p.gap(2)
        p.body("服务监听地址", color=t["tx"], font=self.fonts.body_b)
        box = ttk.Combobox(self.root, state="readonly", values=labels,
                           font=self.fonts.small)
        box.current(ips.index(cur))
        box.configure(width=48)
        # 禁用滚轮切换：监听网卡是重操作（要重启服务），滚轮误滚一下
        # 服务就全重启了，必须用鼠标点开下拉再选。
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            box.bind(seq, lambda _e: "break")
        self._embed(p, box, T.px(30))

        info = self._link_info
        nic_name = (info or {}).get("name") or "未知"
        nic_speed = human_link(self._link_bps) if self._link_bps else "未知"
        if cur != "0.0.0.0":
            hint = "当前绑定「%s」· 协商速率 %s。换网卡后服务会自动重启。" % (
                cur, nic_speed)
        else:
            hint = ("当前监听全部网卡，局域网内任何地址都能访问"
                    "（出口走「%s」，协商速率 %s）。" % (nic_name, nic_speed))
        p.gap(6)
        p.body(hint, color=t["tx3"], font=self.fonts.tiny)
        p.body("只监听单块网卡 = 其它网卡上的设备连不进来，可当一道访问控制。",
               color=t["tx3"], font=self.fonts.tiny)

        def on_change(_e=None):
            ip = ips[box.current()]
            self.cfg.set(ip, "listen", "ip")
            self.cfg.save()
            self._toast("已切换监听网卡")
            self._probe_link()
            # 绑定地址变了必须重启服务：记住哪些在跑 → 停 → 换 700ms 后拉起
            was_on = [proto for proto in ("http", "ftp")
                      if self.manager.is_running(proto)]
            for proto in was_on:
                self._toggle_proto(proto, False)
            if was_on:
                self.root.after(
                    700, lambda: self._restart_protos(was_on))
            self.render_page()

        box.bind("<<ComboboxSelected>>", on_change)

    def _restart_protos(self, protos):
        """换网卡后把刚才跑着的服务按原样拉起来。"""
        for proto in protos:
            self._toggle_proto(proto, True)
        self.render_page()

    def _limit_block(self, p):
        t = self.theme
        up_k = int(self.cfg.get("limit", "up_kbps") or 0)
        dl_k = int(self.cfg.get("limit", "down_kbps") or 0)

        p.gap(2)
        p.body("服务端上行（别人从这里下载的总速度）", color=t["tx"],
               font=self.fonts.body_b)
        self._limit_row(p, "up_kbps", up_k)
        p.gap(10)
        p.body("内置下载器总速度", color=t["tx"], font=self.fonts.body_b)
        self._limit_row(p, "down_kbps", dl_k)
        p.gap(6)
        p.body("提示：千兆网卡实际能跑 110 MB/s 左右；不限速时会尽量吃满。"
               "限速对所有连接生效（不是每连接各一份）。",
               color=t["tx3"], font=self.fonts.tiny)

    def _limit_row(self, p, key, cur_kb):
        """一条限速设置行：预设按钮 + 滑块（拖动实时显示）+ 手动输入框。

        配置存 KB/s；界面单位统一 MB/s（1 MB/s = 1024 KB/s）。
        """
        t = self.theme

        def fmt_value(kb):
            # 数值标签：拖动时看的那个
            if kb <= 0:
                return "不限速"
            if kb >= 1024:
                s = ("%.1f" % (kb / 1024.0)).rstrip("0").rstrip(".")
                return "%s MB/s" % s
            return "%d KB/s" % kb

        # ---- 预设按钮（点了走 _set_limit → 整卡重绘，天然同步）----
        pairs = []
        for kb, label in SPEED_PRESETS:
            style = "primary" if kb == cur_kb else "secondary"
            pairs.append((label, lambda k=kb, kk=key: self._set_limit(kk, k),
                          style))
        p.actions(pairs, height=26)

        # ---- 滑块 + 实时数值 + 手动输入，同一行 ----
        rowy = p.y
        state = {"label_id": None, "var": None, "syncing": False}

        def refresh(kb, entry_too=True):
            # 数值标签（拖动过程中每个 motion 都刷）
            if state["label_id"] is not None:
                p.c.itemconfigure(state["label_id"], text=fmt_value(kb))
            if entry_too and state["var"] is not None:
                state["syncing"] = True          # 别触发 entry 自己的回调
                state["var"].set("" if kb <= 0
                                 else ("%.1f" % (kb / 1024.0)).rstrip("0")
                                 .rstrip("."))
                state["syncing"] = False

        slider = Slider(p.c, t, value=min(max(cur_kb, 0), 204800),
                        maximum=204800, step=512,
                        command=lambda v, kk=key: (
                            self._set_limit(kk, v, quiet=True),
                            refresh(v))[0])
        p.c.create_window(p.x, rowy, window=slider, anchor="nw")
        self._embedded.append(slider)
        # 松手才落盘 + 记日志（拖动过程只改内存，避免每像素写一次盘）
        slider.bind("<ButtonRelease-1>", lambda _e, kk=key, s=slider: (
            self.cfg.save(),
            self.log.add("info", "%s 限速设为 %s" % (
                "服务端上行" if kk == "up_kbps" else "下载器",
                fmt_value(s.value)), "限速")))

        # 实时数值标签（紧跟滑块右侧）
        lid = p.c.create_text(p.x + T.px(276), rowy + T.px(5),
                              text=fmt_value(cur_kb), anchor="nw",
                              font=self.fonts.body_b, fill=t["accent"])
        state["label_id"] = lid

        # ---- 手动输入框（仅数字，单位 MB/s）----
        var = tk.StringVar()
        state["var"] = var
        if cur_kb > 0:
            var.set(("%.1f" % (cur_kb / 1024.0)).rstrip("0").rstrip("."))
        ex = p.x + T.px(380)
        ew = T.px(76)
        p.c.create_rectangle(ex, rowy - T.px(2), ex + ew, rowy + T.px(26),
                             fill=t["card"], outline=t["border"])
        entry = ttk.Entry(p.c, textvariable=var, justify="center",
                          font=self.fonts.small)
        entry.configure(validate="key", validatecommand=(
            p.c.register(lambda P: P == "" or _is_number(P)), "%P"))
        # 必须用 create_window（随画布滚动）；place 是固定在视口上的
        p.c.create_window(ex + T.px(2), rowy, window=entry, anchor="nw",
                          width=ew - T.px(4), height=T.px(25))
        self._embedded.append(entry)

        def apply_entry(_e=None):
            try:
                mb = float(var.get())
            except ValueError:
                refresh(cur_kb)                  # 输入不合法，回显当前值
                return
            kb = max(0, min(204800, int(round(mb * 1024))))
            # 注意：_set_limit 非 quiet 路径会整页重绘（滑块/输入框都会
            # 用新值重建），这里绝不能再碰旧控件——它们已经销毁了。
            self._set_limit(key, kb)             # 落盘 + 日志 + 重绘

        entry.bind("<Return>", apply_entry)
        entry.bind("<FocusOut>", apply_entry)

        p.c.create_text(ex + ew + T.px(8), rowy + T.px(5), text="MB/s",
                        anchor="nw", font=self.fonts.tiny, fill=t["tx3"])
        p.c.create_text(ex + ew + T.px(56), rowy + T.px(5),
                        text="回车生效 · 0 = 不限速 · 最大 200",
                        anchor="nw", font=self.fonts.tiny, fill=t["tx3"])
        p.y = rowy + T.px(34)

    # ------------------------------------------------------------ 下载页
    def _render_download(self, p):
        t = self.theme
        tasks = self.downloader.tasks
        p.heading("下载", "多线程分片 + 断点续传 + 断线重连")
        p.gap(2)

        # 下载器带宽（从概览页移到这里：这是「我在下载」的速度，
        # 不属于共享流量，所以只在本页显示）
        running = [x for x in tasks if x.status in ("running", "connecting")]
        tot = self.manager.stats.totals()
        p.metric_tiles([
            ("下载速度", human_speed(self._last_speed[2])),
            ("近 30 秒平均", human_speed(self._avg30["dl"])),
            ("累计下载", human_bytes(tot["dl"])),
            ("任务", "%d 个 · 进行中 %d" % (len(tasks), len(running))),
        ], cols=4)

        # 新建任务
        p.section("新建下载")
        p.card(lambda: self._new_task_block(p), pad=16)

        done = [x for x in tasks if x.status == "done"]
        p.section("任务", "%d 个 · 进行中 %d" % (len(tasks), len(running)))

        if not tasks:
            p.empty_state(
                "还没有下载任务",
                "把链接粘到上面点「开始下载」。支持 HTTP/HTTPS，"
                "服务器支持 Range 时会自动分片（默认 8 线程）。",
                ["断点续传：暂停、关程序、下次启动都能接着下",
                 "断线重连：网络抖一下就自动从断点继续，不用重下",
                 "限速在「服务设置」页统一设置"])
            return

        for task in tasks:
            self._task_row(p, task)

        p.section("存放位置")
        p.kv("默认目录", self.cfg.get("download", "dir") or "")
        p.actions([("打开下载目录",
                    lambda: self._open_path(self.cfg.get("download", "dir")),
                    "secondary")])

    def _new_task_block(self, p):
        t = self.theme
        p.gap(2)
        ent = LineEntry(p.c, self.fonts, t, getattr(self, "_url_buf", ""),
                        width=52)
        p.c.create_window(p.x, p.y, window=ent, anchor="nw", height=T.px(28),
                          width=p.w - T.px(2))
        self._embedded.append(ent)
        self._url_entry = ent
        p.y += T.px(36)

        # 线程数
        p.c.create_text(p.x, p.y + T.px(5), text="线程数", anchor="nw",
                        font=self.fonts.small, fill=t["tx3"])
        threads = int(self.cfg.get("download", "threads") or 8)
        self._thread_val = threading_value = getattr(self, "_threads_tmp", threads)

        def set_threads(v):
            self._threads_tmp = int(v)
            self.cfg.set(int(v), "download", "threads")
            self.cfg.save()
            if self._threads_lbl is not None:
                self._threads_lbl.configure(text="%d" % int(v))

        self._threads_lbl = None
        slider = Slider(p.c, t, value=threading_value, maximum=32, minimum=1,
                        command=set_threads, width=200)
        p.c.create_window(p.x + T.px(58), p.y, window=slider, anchor="nw")
        self._embedded.append(slider)
        self._threads_lbl = tk.Label(p.c, text="%d" % threading_value,
                                     font=self.fonts.body_b, bg=t["card"],
                                     fg=t["accent"])
        p.c.create_window(p.x + T.px(266), p.y + T.px(2),
                          window=self._threads_lbl, anchor="nw")
        self._embedded.append(self._threads_lbl)
        p.y += T.px(36)

        p.actions([
            ("开始下载", self._add_download, "primary"),
            ("选择保存目录", self._choose_dl_dir, "secondary"),
            ("粘贴", self._paste_url, "subtle"),
        ])
        p.body("保存到：%s" % (self.cfg.get("download", "dir") or ""),
               color=t["tx3"], font=self.fonts.tiny)

    def _task_row(self, p, task):
        t = self.theme
        status_text, status_color = {
            "running": ("下载中", t["status_ok"]),
            "connecting": ("连接中", t["status_warn"]),
            "paused": ("已暂停", t["tx2"]),
            "done": ("已完成", t["accent"]),
            "error": ("失败", t["status_bad"]),
            "pending": ("等待", t["tx2"]),
        }.get(task.status, (task.status, t["tx2"]))

        def draw():
            p.gap(2)
            line_h = self.fonts.h3.metrics("linespace")
            _it, bb = p._text(p.x, p.y, task.name, self.fonts.h3, t["tx"],
                              width=p.w - T.px(80))
            p.c.create_text(p.x + p.w - T.px(4), p.y,
                            text=status_text, anchor="ne",
                            font=self.fonts.tiny_b, fill=status_color)
            p.y = bb[3] + T.px(2)

            # 进度条
            frac = task.progress
            p.progress_track(frac, height=6)
            # 数字行
            total = human_bytes(task.size) if task.size else "未知"
            if task.status == "running":
                right = "%s / %s · %s · 剩余 %s" % (
                    human_bytes(task.done), total, human_speed(task.speed),
                    human_time(task.eta) if task.eta >= 0 else "—")
            elif task.status == "done":
                right = "%s · 已保存 %s" % (total, task.final_path or "")
            else:
                right = "%s / %s%s" % (
                    human_bytes(task.done), total,
                    (" · " + task.error[:40]) if task.error else "")
            p.body(right, color=t["tx2"] if task.status != "error" else t["status_bad"],
                   font=self.fonts.tiny)
            p.mono_text(task.url, color=t["tx3"], width=p.w - T.px(6))

            acts = []
            if task.status in ("running", "connecting"):
                acts.append(("暂停", lambda: self.downloader.pause(task.id),
                             "secondary"))
            elif task.status in ("paused", "error"):
                acts.append(("继续", lambda: self.downloader.resume(task.id),
                             "primary"))
            if task.status == "done":
                acts.append(("打开文件",
                             lambda: self._open_path(task.final_path),
                             "secondary"))
            # 每个任务可以存到不同目录，光「打开文件」不够 ——
            # 还要能直接跳到它所在的文件夹。
            folder = task.folder or (os.path.dirname(task.final_path)
                                     if task.final_path else "")
            if folder:
                acts.append(("打开所在文件夹",
                             lambda: self._open_path(folder), "secondary"))
            acts.append(("删除任务",
                         lambda: self._remove_task(task), "danger"))
            p.actions(acts, height=26)
        p.card(draw, pad=15, gap=10)

    # ------------------------------------------------------------ 共享网络页
    def _render_peers(self, p):
        t = self.theme
        p.heading("共享网络", "同一局域网里，其他主机跑 FileShare 开的共享")
        p.gap(2)

        st = self._peer_scan
        if st["running"]:
            pct = (st["done"] * 100 // max(1, st["total"]))
            p.progress_track(min(1.0, pct / 100.0), height=6)
            p.body("正在扫描局域网（%d / %d）… 全程后台进行，不影响任何操作"
                   % (st["done"], st["total"]),
                   color=t["tx3"], font=self.fonts.tiny)
        else:
            ago = int(time.time() - st["last"]) if st["last"] else -1
            when = ("刚刚完成" if ago <= 1 else
                    "%d 秒前完成" % ago if ago > 0 else "尚未扫描")
            p.body("上次扫描：%s · 发现 %d 台主机 · 停留在本页时每 30 秒自动"
                   "重扫，切走即停" % (when, len(self._peers)),
                   color=t["tx3"], font=self.fonts.tiny)
        p.actions([("重新扫描", lambda: self._start_peer_scan(manual=True),
                    "secondary")], height=26)
        p.gap(2)

        # 只显示 75 秒内见过的主机（两轮半扫描没应答 = 已关闭，自动移除）
        now = time.time()
        peers = sorted((info for info in self._peers.values()
                        if now - info.get("seen", 0) <= 75),
                       key=lambda r: r.get("host") or r["ip"])
        if st["running"] and not peers:
            p.empty_state(
                "正在扫描局域网…",
                "第一次扫描大约需要一两秒，扫到会自动出现在这里。",
                ["其他主机也在跑 FileShare 并开了共享才能被看到",
                 "扫描不会发送任何数据，只是探测固定的端口"])
            return
        if not peers:
            p.empty_state(
                "没有发现其他主机",
                "同一网络里暂时没有别的主机在跑 FileShare（或没开共享）。",
                ["在另一台电脑上运行本软件并开启共享，几秒内就会出现在这里",
                 "对方关掉软件或共享后，这里的条目也会自动消失"])
            return

        p.section("发现的主机", "%d 台" % len(peers))
        for info in peers:
            self._peer_row(p, info)

    def _peer_row(self, p, info):
        t = self.theme
        host = info.get("host") or "未知主机"
        ip = info["ip"]
        http = info.get("http") or {}
        ftp = info.get("ftp") or {}
        smb = info.get("smb") or {}

        def draw():
            p.gap(2)
            line_h = self.fonts.h3.metrics("linespace")
            _it, bb = p._text(p.x, p.y, host, self.fonts.h3, t["tx"],
                              width=p.w - T.px(120))
            seen_ago = int(time.time() - info.get("seen", 0))
            p.c.create_text(p.x + p.w - T.px(4), p.y, anchor="ne",
                            text="%d 秒前在线" % seen_ago,
                            font=self.fonts.tiny_b, fill=t["tx3"])
            p.y = bb[3] + T.px(1)
            p.body("IP %s" % ip, color=t["tx3"], font=self.fonts.tiny)

            acts = []
            if http.get("on"):
                url = "http://%s:%d/" % (ip, http.get("port") or 8080)
                shares = http.get("shares") or []
                label = "HTTP：%s" % (", ".join(shares[:3]) if shares
                                      else "共享页面")
                acts.append((label, lambda u=url: self._open_http(u),
                             "primary"))
            if smb.get("on"):
                names = smb.get("names") or []
                if names:
                    target = "\\\\%s\\%s" % (ip, names[0])
                    label = "SMB：%s" % (", ".join(names[:3]))
                    acts.append((label,
                                 lambda d=target: self._open_path(d),
                                 "secondary"))
            if ftp.get("on"):
                target = "ftp://%s:%d/" % (ip, ftp.get("port") or 2121)
                acts.append(("FTP：%d" % (ftp.get("port") or 2121),
                             lambda u=target: self._open_ftp(u),
                             "secondary"))
            if not acts:
                p.body("对方在本页扫描时没有开任何共享", color=t["tx3"],
                       font=self.fonts.tiny)
            p.actions(acts, height=26)
        p.card(draw, pad=15, gap=10)

    def _start_peer_scan(self, manual=False):
        if self._peer_scan["running"]:
            return
        st = self._peer_scan
        st.update({"running": True, "done": 0, "total": 0, "error": ""})
        if manual:
            st["last"] = 0.0
            self.render_page()
        bind = self.cfg.get("listen", "ip") or "0.0.0.0"
        base = bind if bind != "0.0.0.0" else primary_ip()

        def work():
            try:
                found, total = scan_subnet(
                    base, port=int(self.cfg.get("listen", "discovery_port")
                                    or 47610),
                    exclude_ip=primary_ip())
                now = time.time()
                for info in found:
                    self._peers[info["ip"]] = info
                # 这轮没应答的主机不立刻删（防单次丢包），把 seen 打旧，
                # 渲染层按「75 秒内在线」过滤，自然消失。
                for ip, info in self._peers.items():
                    if info.get("seen", 0) < now - 0.5:
                        info["seen"] = min(info.get("seen", 0),
                                           now - 76)
            except Exception as exc:                        # noqa: BLE001
                st["error"] = str(exc)[:120]
            finally:
                st["running"] = False
                st["last"] = time.time()

        threading.Thread(target=work, daemon=True,
                         name="fs-peer-scan").start()

    # ---- 打开各协议入口 ----
    def _open_http(self, url):
        """HTTP 共享：用浏览器打开；装了多个浏览器时问一下用哪个。"""
        choices = self._browser_choices()
        if not choices:
            self._toast("没有找到可用的浏览器")
            return
        if len(choices) == 1:
            self._launch_browser(choices[0][0], url)
            return
        buttons = [("b%d" % i, name, "secondary")
                   for i, (_cmd, name) in enumerate(choices)]
        buttons.append(("cancel", "取消", "primary"))
        key = self._modal_choice(
            "用哪个浏览器打开？",
            "检测到本机安装了 %d 个浏览器，请选择打开\n%s 的程序："
            % (len(choices), url),
            buttons, default="cancel")
        if key and key.startswith("b"):
            self._launch_browser(choices[int(key[1:])][0], url)

    def _launch_browser(self, command, url):
        """按 StartMenuInternet 注册的命令行启动浏览器。"""
        import subprocess
        try:
            cmd = command.strip()
            if cmd.startswith('"'):
                exe = cmd[1:cmd.index('"', 1)]
            else:
                exe = cmd.split(" ", 1)[0]
            subprocess.Popen([exe, url])
            self._toast("已在浏览器打开共享页面")
        except Exception as exc:                            # noqa: BLE001
            self._toast("打开失败：%s" % exc)

    def _browser_choices(self):
        """枚举系统注册的浏览器（StartMenuInternet），[(命令行, 名称)]。"""
        import winreg
        out = []
        seen = set()
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                key = winreg.OpenKey(root,
                                     r"SOFTWARE\Clients\StartMenuInternet")
            except OSError:
                continue
            with key:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(key, i)
                        i += 1
                    except OSError:
                        break
                    try:
                        with winreg.OpenKey(key, sub) as sk:
                            name = winreg.QueryValueEx(sk, None)[0] or sub
                        with winreg.OpenKey(
                                key, sub + r"\shell\open\command") as ck:
                            cmd = winreg.QueryValueEx(ck, None)[0]
                    except OSError:
                        continue
                    if not cmd or cmd.lower() in seen:
                        continue
                    seen.add(cmd.lower())
                    out.append((cmd, name))
        return out

    def _open_ftp(self, url):
        """FTP 共享：交给系统默认处理方式（资源管理器 / 第三方客户端）。"""
        try:
            os.startfile(url)                              # noqa: S606
            self._toast("已用系统默认方式打开 FTP")
        except Exception as exc:                            # noqa: BLE001
            self._toast("打开失败：%s" % exc)

    # ------------------------------------------------------------ 日志页
    def _render_logs(self, p):
        pass

    def _sync_log(self, force=False):
        """增量追加：只把新产生的几十条塞进去，不整篇重建。

        日志量大时（几十个客户端同时下载）整篇重建会让界面明显发顿，
        这里按 ``log.seq`` 的差值取新增条目；一次涌进太多或触发上限才重排。
        """
        seq = self.log.seq
        if not force and seq == self._log_seq:
            return
        txt = self.logtext
        # 主题切换 / 退出重建时控件可能已被销毁：TclError 直接跳过，
        # 别让一次同步失败顺着 Tk 回调把整个程序带走
        try:
            alive = bool(txt.winfo_exists())
        except tk.TclError:
            return
        if not alive:
            return
        fresh = seq - self._log_seq if self._log_seq >= 0 else 0
        count = getattr(self, "_log_count", 0)
        rebuild = force or fresh <= 0 or fresh > 200 or count > 900

        def render(rows):
            for ts, kind, text, peer in rows:
                tag = kind if kind in ("ok", "warn", "err", "dl") else "info"
                txt.insert("end", "%s  " % ts, tag)
                txt.insert("end", "%-6s " % (peer or ""), "info")
                txt.insert("end", "%s\n" % (text or "").replace("\n", " "), tag)

        txt.configure(state="normal")
        if rebuild:
            rows = self.log.tail(400)
            txt.delete("1.0", "end")
            render(rows)
            self._log_count = len(rows)
        else:
            rows = self.log.tail(fresh)
            render(rows)
            self._log_count = count + len(rows)
            if self._log_count > 800:
                txt.delete("1.0", "%d.0" % (self._log_count - 400))
                self._log_count = 400
        txt.configure(state="disabled")
        if force or self._log_count <= 400:
            txt.see("end")
        self._log_seq = seq
        try:
            self.lbl_logcount.configure(text="%d 条" % self._log_count)
        except tk.TclError:
            pass

    def _clear_log(self):
        self.log.clear()
        self._sync_log(force=True)

    def _copy_log(self):
        rows = self.log.tail(2000)
        text = "\n".join("%s %s %s" % (ts, peer, msg) for ts, k, msg, peer in rows)
        self._copy(text)

    # ------------------------------------------------------------ 动作
    def _toggle_proto(self, proto, on=None):
        if proto in self._busy:
            return
        want = (not self.manager.is_running(proto)) if on is None else bool(on)
        self._busy.add(proto)
        self._toast("%s 正在%s…" % (proto.upper(), "启动" if want else "停止"))

        def work():
            try:
                ok, msg = self.manager.start(proto) if want else \
                    self.manager.stop(proto)
            except Exception as exc:                        # noqa: BLE001
                ok, msg = False, str(exc)
            self._q.put(("done", proto, ok, msg))

        threading.Thread(target=work, daemon=True).start()
        self.root.after(300, self._wait_busy)

    def _wait_busy(self):
        # 忙标志由 _pump 清除，这里只是触发一次重绘
        if self.page in ("overview", "servers"):
            self.render_page()

    # ---- SMB 局域网访问体检 / 修复 ----
    def _smb_check(self, force=False):
        """后台跑一次 SMB 体检，结果缓存起来供渲染读取。

        诊断要调 netsh/netstat/powershell，一次几十到上百毫秒，
        不能放在渲染路径上同步做，否则每次重绘都卡一下。
        """
        if getattr(self, "_smb_busy", False):
            return
        if not force and self._smb_issues is not None:
            return
        self._smb_busy = True

        def work():
            try:
                issues = self.manager.smb_diagnose()
            except Exception:                               # noqa: BLE001
                issues = []
            self._q.put(("smb-issues", issues))

        threading.Thread(target=work, daemon=True).start()

    def _smb_repair(self):
        """一键修复 SMB：建共享账户、放行 445、补目录权限。"""
        if getattr(self, "_smb_fixing", False):
            return
        if not self.manager.status()["admin"]:
            self._toast("需要管理员权限，点右上角「以管理员身份重启」")
            return
        self._smb_fixing = True
        self._toast("SMB 修复中…")

        def work():
            try:
                done, failed = self.manager.smb_repair()
            except Exception as exc:                        # noqa: BLE001
                done, failed = [], [str(exc)]
            self._q.put(("smb-repair", done, failed))

        threading.Thread(target=work, daemon=True).start()
        self.root.after(500, self._wait_repair)

    def _wait_repair(self):
        if getattr(self, "_smb_fixing", False):
            self.root.after(400, self._wait_repair)

    def _toggle_services(self):
        """顶栏（非下载页）：一键启停全部服务。"""
        any_on = any(self.manager.is_running(x) for x in ("http", "ftp", "smb"))
        for proto in ("http", "ftp"):
            if any_on:
                if self.manager.is_running(proto):
                    self._toggle_proto(proto, False)
            else:
                if not self.manager.is_running(proto):
                    self._toggle_proto(proto, True)
        if self.cfg.get("smb", "enabled") and is_admin():
            self._toggle_proto("smb", not any_on)
        self._refresh_all_button()

    def _refresh_all_button(self):
        """服务控制按钮文案跟着实际状态走（幂等）。"""
        any_on = any(self.manager.is_running(x) for x in ("http", "ftp", "smb"))
        want = "全部停止" if any_on else "全部启动"
        if self.btn_all.text != want:
            self.btn_all.config_text(want)

    def _sync_header_buttons(self):
        """按当前页决定槽位里显示哪个按钮。

        下载批量控制（全部暂停/继续）只属于下载页；其它页面一律是
        服务控制（全部启动/停止）——这两件事混在一个按钮上会让人懵。
        """
        on_dl = self.page == "download"
        self.btn_all.pack_forget()
        self.btn_dl.pack_forget()
        if on_dl:
            self.btn_dl.pack(side="right")
            self._refresh_dl_button()
        else:
            self.btn_all.pack(side="right")
            self._refresh_all_button()

    def _toggle_all(self):
        """顶栏按钮：批量暂停 / 继续所有下载任务。"""
        if self.downloader.active_count():
            n = self.downloader.pause_all()
            self._toast("已暂停 %d 个任务" % n)
        else:
            resumed = 0
            for task in self.downloader.tasks:
                if task.status in ("paused", "error"):
                    self.downloader.resume(task.id)
                    resumed += 1
            self._toast("已继续 %d 个任务" % resumed if resumed
                        else "没有可继续的任务")
        self._refresh_dl_button()
        if self.page in ("download",):
            self.render_page()

    def _refresh_dl_button(self):
        """下载按钮：无任务 → 置灰不可点；有任务 → 全部暂停 / 全部继续（幂等）。"""
        tasks = self.downloader.tasks
        active = self.downloader.active_count()
        want_on = bool(tasks)
        want_text = "全部暂停" if active else "全部继续"
        if not tasks:
            want_text = "全部暂停"
        if self.btn_dl._enabled != want_on:
            self.btn_dl.set_enabled(want_on)
        if self.btn_dl.text != want_text:
            self.btn_dl.config_text(want_text)

    def _add_share(self):
        path = filedialog.askdirectory(title="选择要共享的目录")
        if not path:
            return
        path = os.path.abspath(path)
        shares = self.cfg.get("shares") or []
        for s in shares:
            if os.path.normcase(s.get("path") or "") == os.path.normcase(path):
                self._toast("这个目录已经在共享列表里了")
                return
        base = os.path.basename(path.rstrip("\\/")) or "share"
        name = safe_filename(base, "share")
        used = set((s.get("name") or "").lower() for s in shares)
        cand, i = name, 2
        while cand.lower() in used:
            cand = "%s%d" % (name, i)
            i += 1
        shares.append({"name": cand, "path": path, "enabled": True})
        self.cfg.set(shares, "shares")
        self.cfg.save()
        self.log.add("ok", "添加共享目录：%s（%s）" % (path, cand), "共享")
        self._toast("已添加共享：%s" % cand)
        self.render_page()
        self._auto_apply()

    def _remove_share(self, share):
        shares = [s for s in (self.cfg.get("shares") or []) if s is not share]
        self.cfg.set(shares, "shares")
        self.cfg.save()
        self.log.add("warn", "移除共享目录：%s" % share.get("path"), "共享")
        self.render_page()
        self._auto_apply()

    def _toggle_share(self, share):
        for s in self.cfg.get("shares") or []:
            if s is share:
                s["enabled"] = not s.get("enabled", True)
        self.cfg.save()
        self.render_page()
        self._auto_apply()

    def _auto_apply(self):
        """已运行的服务需要感知共享变化（SMB 要重建共享，HTTP/FTP 读实时列表）。"""
        if self.manager.is_running("smb"):
            threading.Thread(target=self.manager.sync_smb, daemon=True).start()

    def _set_port(self, proto, value):
        try:
            port = int(str(value).strip())
        except ValueError:
            self._toast("端口必须是数字")
            return
        if not (1 <= port <= 65535):
            self._toast("端口范围 1-65535")
            return
        if port == int(self.cfg.get(proto, "port") or 0):
            return
        was_on = self.manager.is_running(proto)
        self.cfg.set(port, proto, "port")
        self.cfg.save()
        self.log.add("info", "%s 端口改为 %d" % (proto.upper(), port), proto.upper())
        if was_on:
            self._toggle_proto(proto, False)
            self.root.after(600, lambda: self._toggle_proto(proto, True))
        self._toast("%s 端口已改为 %d" % (proto.upper(), port))

    def _set_text(self, proto, key, value):
        value = str(value).strip()
        if not value:
            return
        if self.cfg.get(proto, key) == value:
            return
        self.cfg.set(value, proto, key)
        self.cfg.save()
        if key == "password":
            self.log.add("info", "FTP 密码已更新", "FTP")
        self._toast("已保存")

    def _set_bool(self, proto, key, value, restart=False):
        self.cfg.set(bool(value), proto, key)
        self.cfg.save()
        if restart and self.manager.is_running(proto):
            self._toggle_proto(proto, False)
            self.root.after(600, lambda: self._toggle_proto(proto, True))
        self._toast("已保存")

    def _set_limit(self, key, kb, quiet=False):
        kb = max(0, int(kb))
        self.cfg.set(kb, "limit", key)
        self.manager.apply_limits()
        if not quiet:
            self.cfg.save()
            self.log.add("info", "%s 限速设为 %s" % (
                "服务端上行" if key == "up_kbps" else "下载器",
                "不限速" if kb == 0 else "%d KB/s" % kb), "限速")
            self.render_page()
        # quiet = 拖动中的连续回调：只改内存限速，落盘和日志等松手再做

    def _add_download(self):
        url = (self._url_entry.get_value() if getattr(self, "_url_entry", None)
               else "").strip()
        if not url:
            self._toast("先粘贴一个下载链接")
            return
        if not (url.lower().startswith("http://") or
                url.lower().startswith("https://")):
            url = "http://" + url
        try:
            self.downloader.add(url)
        except Downloader.DuplicateError as exc:
            if exc.done:
                self._readd_done_duplicate(url, exc.task)
            else:
                self._toast(str(exc))            # 未完成任务的重复 → 直接拒绝
            return
        except ValueError as exc:
            self._toast(str(exc))                # 其它业务提示
            return
        except Exception as exc:                            # noqa: BLE001
            self._toast("添加失败：%s" % exc)
            return
        self._url_buf = ""
        if getattr(self, "_url_entry", None):
            self._url_entry.set_value("")
        self._toast("已开始下载")
        self.render_page()

    def _readd_done_duplicate(self, url, old_task):
        """重复下载一个已完成的链接 → 问覆盖 / 另存 / 取消（默认取消）。"""
        path = old_task.final_path
        choice = self._modal_choice(
            "文件已经下载过了",
            "这个链接之前已经下载完成：\n%s\n\n要重新下载吗？"
            "\n覆盖 = 删掉旧文件重下；另存 = 保留旧文件，"
            "新文件加序号保存。" % path,
            [("overwrite", "覆盖", "danger"),
             ("rename", "另存", "secondary"),
             ("cancel", "取消", "primary")],
            default="cancel")
        if choice in (None, "cancel"):
            return
        if choice == "overwrite":
            # 删旧文件 + 移除旧任务，然后按原链接重新下载
            try:
                if os.path.isfile(path):
                    os.remove(path)
                self.downloader.remove(old_task.id, delete_file=True)
            except OSError as exc:
                self._toast("删除旧文件失败：%s" % exc)
                return
            try:
                self.downloader.add(url, allow_duplicate=True)
                self._toast("开始重新下载（覆盖）")
            except Exception as exc:                        # noqa: BLE001
                self._toast("添加失败：%s" % exc)
        else:
            # 另存：换个文件名（加序号）再下，旧文件原样保留
            base, ext = os.path.splitext(old_task.filename or "download.bin")
            folder = os.path.dirname(path) or self.cfg.get("download", "dir")
            n = 2
            while os.path.exists(os.path.join(folder, "%s-%d%s" % (base, n, ext))):
                n += 1
            new_name = "%s-%d%s" % (base, n, ext)
            try:
                self.downloader.add(url, filename=new_name,
                                    allow_duplicate=True)
                self._toast("开始下载：%s（旧文件保留）" % new_name)
            except Exception as exc:                        # noqa: BLE001
                self._toast("添加失败：%s" % exc)
        if self.page == "download":
            self.render_page()

    def _paste_url(self):
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            text = ""
        if not text:
            self._toast("剪贴板是空的")
            return
        self._url_buf = text.strip().splitlines()[0]
        if getattr(self, "_url_entry", None):
            self._url_entry.set_value(self._url_buf)
        self._toast("已粘贴链接")

    def _choose_dl_dir(self):
        path = filedialog.askdirectory(title="选择下载保存目录",
                                       initialdir=self.cfg.get("download", "dir"))
        if not path:
            return
        self.cfg.set(os.path.abspath(path), "download", "dir")
        self.cfg.save()
        self.render_page()

    def _remove_task(self, task):
        """删除任务：已完成 → 问是否连文件删；未完成 → 确认后清缓存。"""
        if task.status == "done":
            self._remove_done_task(task)
        else:
            self._remove_active_task(task)

    def _remove_done_task(self, task):
        path = task.final_path or ""
        choice = self._modal_choice(
            "删除任务",
            "任务「%s」已完成。\n已下载文件：%s\n\n要把它下载的文件也一起删除吗？"
            % (task.name, path or "（路径未知）"),
            [("file", "删除任务和文件", "danger"),
             ("keep", "仅删除任务", "secondary"),
             ("cancel", "取消", "primary")],
            default="cancel")
        if choice in (None, "cancel"):
            return
        if choice == "file":
            self.downloader.remove(task.id, delete_file=True)
            self._toast("任务和文件已删除")
            return
        # 仅删除任务：文件保留，明确告诉用户在哪儿，可点击查看
        self.downloader.remove(task.id)
        if path:
            key = self._modal_choice(
                "文件已保留",
                "只删除了任务，文件没有动，还保存在：\n%s\n\n"
                "需要的时候可以随时打开目录查看。" % path,
                [("open", "打开目录", "primary"),
                 ("close", "好的", "secondary")],
                default="open")
            if key == "open":
                self._open_path(os.path.dirname(path) or path)

    def _remove_active_task(self, task):
        choice = self._modal_choice(
            "取消下载任务",
            "任务「%s」还没下载完成（已完成 %.0f%%）。\n"
            "删除任务后，已下载的缓存文件会被直接清理，不会保留。"
            % (task.name, task.progress * 100),
            [("remove", "删除", "secondary"),
             ("cancel", "取消", "primary")],
            default="cancel")
        if choice != "remove":
            return
        if task.status in ("running", "connecting"):
            self.downloader.pause(task.id)
        self.downloader.remove(task.id, delete_file=True)
        self._toast("任务已取消，缓存已清理")

    def _modal_choice(self, title, message, buttons, default=None):
        """模态选择框。buttons = [(key, 文本, 样式)]，返回被点的 key。

        ``default`` 指定的按钮拿到焦点、回车直接触发（引导用户点安全项）；
        Esc / 关窗等同最后一个按钮（约定放「取消」）。
        """
        result = {"key": None}
        t = self.theme
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=t["bg"])
        win.resizable(False, False)
        win.transient(self.root)
        w = T.px(480)
        h = T.px(170 + 26 * message.count("\n"))
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 3
        win.geometry("%dx%d+%d+%d" % (w, h, max(0, x), max(0, y)))

        tk.Label(win, text=message, font=self.fonts.body, bg=t["bg"],
                 fg=t["tx"], wraplength=w - T.px(56), justify="left").pack(
            anchor="w", padx=T.px(28), pady=(T.px(24), T.px(6)))
        row = tk.Frame(win, bg=t["bg"])
        row.pack(side="bottom", fill="x", padx=T.px(28), pady=T.px(18))

        def choose(k):
            result["key"] = k
            try:
                win.destroy()
            except tk.TclError:
                pass

        for key, label, style in reversed(buttons):
            b = PillButton(row, self.fonts, t, label,
                           command=lambda k=key: choose(k), style=style,
                           height=32)
            b.pack(side="right", padx=(T.px(8), 0))
            if key == default:
                b.focus_set()
                win.bind("<Return>", lambda _e, k=key: choose(k))
        win.bind("<Escape>", lambda _e: choose(buttons[-1][0]))
        win.protocol("WM_DELETE_WINDOW", lambda: choose(buttons[-1][0]))
        win.grab_set()
        self.root.wait_window(win)
        return result["key"]

    def _on_task_change(self):
        self._dl_version += 1

    # ------------------------------------------------------------ 小工具
    def _embed(self, p, widget, height, width=None):
        hid = p.c.create_window(p.x, p.y, window=widget, anchor="nw",
                                width=width or p.w, height=height)
        self._embedded.append(widget)
        p.y += height + T.px(8)
        return hid

    def _open_path(self, path):
        if not path:
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)                         # noqa: S606
            else:
                import subprocess
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:                            # noqa: BLE001
            self._toast("打开失败：%s" % exc)

    def _copy(self, text):
        if not text:
            self._toast("没有可复制的内容")
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self._toast("已复制：%s" % text[:60])
        except tk.TclError:
            pass

    def _toast(self, text):
        self._notice = text
        self._notice_until = time.time() + 4.0
        try:
            self.lbl_status.configure(text=text, fg=self.theme["tx"])
        except tk.TclError:
            pass

    def _refresh_admin_label(self):
        admin = is_admin()
        self.lbl_admin.configure(
            text="● 管理员模式\n三种协议全部可用" if admin else
            "○ 普通权限\nSMB 共享需要管理员")
        self.lbl_admin.configure(
            fg=self.theme["status_ok"] if admin else self.theme["status_warn"])

    def _restart_as_admin(self):
        ok = False
        try:
            import ctypes
            params = " ".join('"%s"' % a for a in sys.argv[1:])
            if getattr(sys, "frozen", False):
                exe, args = sys.executable, params
            else:
                exe = sys.executable
                args = '"%s" %s' % (os.path.abspath(sys.argv[0]), params)
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", exe, args, None, 1)
            ok = int(rc) > 32
        except Exception:                                   # noqa: BLE001
            ok = False
        if ok:
            self._quit(from_admin=True)
        else:
            self._toast("提权被取消")

    # ------------------------------------------------------------ 托盘 / 关闭
    def _make_tray(self):
        try:
            self.tray = TrayIcon(
                tooltip="%s · %s" % (APP_NAME, "就绪"),
                menu=[("打开主界面", self._show_window),
                      ("-", None),
                      ("全部启动", lambda: self._toggle_all()),
                      ("打开 HTTP 首页", self._open_http),
                      ("-", None),
                      ("退出", self._quit)],
                on_click=self._show_window,
                on_double=self._show_window,
                icon_path=self._icon_path())
            if not self.tray.show():
                self.tray = None
        except Exception:                                   # noqa: BLE001
            self.tray = None

    def _icon_path(self):
        try:
            from ..resources import asset_path
            p = asset_path("icon.ico")
            return p if os.path.isfile(p) else None
        except Exception:                                   # noqa: BLE001
            return None

    def _hide_to_tray(self):
        if self.tray is None:
            self.root.iconify()
            return
        self.root.withdraw()
        self._toast("已缩到托盘，双击图标回来")

    def _show_window(self):
        try:
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            self.root.focus_force()
            self._raise_foreground()
        except tk.TclError:
            pass

    def _raise_foreground(self):
        """Win32 置前台。

        Tk 的 ``lift()``/``focus_force()`` 在「前台属于别的进程」时不灵
        （Windows 只允许前台进程改前台）。先挂到当前前台线程的输入队列，
        再 SetForegroundWindow，设完摘掉 —— 单实例激活、托盘双击都用它。
        """
        if sys.platform != "win32":
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = ctypes.c_void_p(self.root.winfo_id())
            user32.GetWindowThreadProcessId.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
            pid = ctypes.c_ulong(0)
            tid_cur = user32.GetWindowThreadProcessId(
                user32.GetForegroundWindow(), ctypes.byref(pid))
            tid_me = ctypes.windll.kernel32.GetCurrentThreadId()
            attached = False
            if tid_cur and tid_cur != tid_me:
                attached = bool(user32.AttachThreadInput(tid_me, tid_cur,
                                                          True))
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            if attached:
                user32.AttachThreadInput(tid_me, tid_cur, False)
        except Exception:                                   # noqa: BLE001
            pass

    def activation_handler(self):
        """第二个实例启动时的「叫门」回调（跑在激活通道的线程里）。

        只负责往 Tk 消息队列塞一句话，真正的显示动作回到 UI 线程做。
        """
        self._q.put(("activate", None))

    def _on_minimize(self, event=None):
        if self._min_guard or self._closing:
            return
        if event is not None and event.widget is not self.root:
            return
        if self.tray is None or not self.tray.visible:
            return
        if self.root.state() != "iconic":
            return
        self._min_guard = True
        try:
            self._hide_to_tray()
        finally:
            self._min_guard = False

    def _on_close(self):
        """点 X 必须询问（除非用户在询问框里勾过「记住我的选择」）。"""
        if self._closing:
            return
        tray_ok = bool(self.tray and self.tray.visible)
        remembered = self.cfg.get("ui", "close_action")
        if not remembered:
            if not tray_ok:
                self._quit()          # 没有托盘可用，问也没意义
                return
            self._ask_close()
            return
        # 用户之前选过「记住我的选择」→ 按记忆执行
        if remembered == "tray" and tray_ok:
            self._hide_to_tray()
        else:
            self._quit()

    def _ask_close(self):
        if self._ask_win is not None:
            try:
                self._ask_win.lift()
                return
            except tk.TclError:
                self._ask_win = None
        t = self.theme
        win = tk.Toplevel(self.root)
        self._ask_win = win
        win.title("关闭 %s" % APP_NAME)
        win.configure(bg=t["bg"])
        win.resizable(False, False)
        win.transient(self.root)
        w, h = T.px(400), T.px(210)
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 3
        win.geometry("%dx%d+%d+%d" % (w, h, max(0, x), max(0, y)))

        tk.Label(win, text="要关闭程序，还是缩到托盘继续跑？",
                 font=self.fonts.h3, bg=t["bg"], fg=t["tx"]).pack(
            anchor="w", padx=T.px(20), pady=(T.px(18), T.px(4)))
        tk.Label(win, text="缩到托盘后共享服务继续运行，双击托盘图标即可回来。",
                 font=self.fonts.small, bg=t["bg"], fg=t["tx2"],
                 wraplength=T.px(350), justify="left").pack(
            anchor="w", padx=T.px(20))

        remember = tk.BooleanVar(value=False)
        chk = tk.Checkbutton(win, text="记住我的选择", variable=remember,
                             font=self.fonts.small, bg=t["bg"], fg=t["tx2"],
                             activebackground=t["bg"], selectcolor=t["card"],
                             highlightthickness=0, bd=0)
        chk.pack(anchor="w", padx=T.px(16), pady=T.px(8))

        row = tk.Frame(win, bg=t["bg"])
        row.pack(side="bottom", fill="x", padx=T.px(20), pady=T.px(14))

        def finish(action):
            if remember.get():
                self.cfg.set(action, "ui", "close_action")
                self.cfg.save()
            try:
                win.destroy()
            except tk.TclError:
                pass
            self._ask_win = None
            if action == "tray":
                self._hide_to_tray()
            else:
                self._quit()

        PillButton(row, self.fonts, t, "最小化到托盘",
                   command=lambda: finish("tray"), style="primary",
                   height=32).pack(side="left")
        PillButton(row, self.fonts, t, "退出程序",
                   command=lambda: finish("exit"), style="secondary",
                   height=32).pack(side="left", padx=(T.px(8), 0))
        PillButton(row, self.fonts, t, "取消",
                   command=lambda: (win.destroy(), setattr(self, "_ask_win", None)),
                   style="subtle", height=32).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", lambda: (win.destroy(),
                                                 setattr(self, "_ask_win", None)))
        win.bind("<Escape>", lambda e: (win.destroy(),
                                        setattr(self, "_ask_win", None)))

    def _quit(self, from_admin=False):
        if self._closing:
            return
        self._closing = True
        for job in (self._poll_job, self._tick_job):
            if job:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
        # 单实例：先关激活通道再停服务（不然第二个实例来叫门没人应答）
        si = self.single_instance
        if si is not None:
            try:
                si.release()
            except Exception:                               # noqa: BLE001
                pass
        try:
            self.cfg.save()
            self.downloader.stop_all()
        except Exception:                                   # noqa: BLE001
            pass
        try:
            self.manager.stop_all()
        except Exception:                                   # noqa: BLE001
            pass
        try:
            if self.tray is not None:
                self.tray.hide()
        except Exception:                                   # noqa: BLE001
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    # ------------------------------------------------------------ 主题
    def _toggle_theme(self):
        self.theme = dict(T.DARK if self.theme["name"] == "light" else T.LIGHT)
        self.cfg.set(self.theme["name"], "ui", "theme")
        self.cfg.save()
        for w in self.root.winfo_children():
            w.destroy()
        self.root.configure(bg=self.theme["bg"])
        self.fonts = T.Fonts(self.root)
        self._embedded = []
        self._last_w = 0
        self._build()

    # ------------------------------------------------------------ 启动与循环
    def _autostart(self):
        todo = [p for p in ("http", "ftp") if self.cfg.get(p, "enabled")]
        for proto in todo:
            self._toggle_proto(proto, True)
        if self.cfg.get("smb", "enabled") and is_admin():
            self._toggle_proto("smb", True)
        if not todo and not self.cfg.get("shares"):
            self.log.add("info", "首次使用：先到「共享目录」添加一个文件夹", "提示")
        # 启动时扫一次下载目录：能复原的缓存任务复原，无主的提示清理
        self.root.after(400, self._scan_download_cache)
        # 启动时自动探测一次局域网里其他主机的共享（后台线程，无感）
        self._start_peer_scan(manual=False)

    def _scan_download_cache(self):
        """启动扫描下载目录里的缓存（.fspart/.fsmeta）。

        .fsmeta 里有 url 和分段进度 → 复原成暂停任务，用户点继续就能续传；
        复原不了的（缺 url / 缺数据）→ 弹窗提示用户清理。
        """
        try:
            recovered, orphans = self.downloader.recover_orphans()
        except Exception as exc:                            # noqa: BLE001
            self.log.add("warn", "启动扫描下载缓存失败：%s" % exc, "下载")
            return
        if recovered:
            self._toast("已从缓存恢复 %d 个下载任务" % recovered)
        if not orphans:
            return
        total = 0
        for p in orphans:
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
        shown = "\n".join(orphans[:6])
        more = "\n…另有 %d 个" % (len(orphans) - 6) if len(orphans) > 6 else ""
        choice = self._modal_choice(
            "发现无主的下载缓存",
            "下载目录里有 %d 个缓存文件（共 %s），但找不到对应的任务，"
            "无法复原：\n%s%s\n\n这些文件留着也没用，要清理掉吗？"
            % (len(orphans), human_bytes(total), shown, more),
            [("keep", "保留", "primary"),
             ("clean", "清理这些文件", "secondary")],
            default="keep")
        if choice != "clean":
            return
        fail = 0
        for p in orphans:
            try:
                os.remove(p)
            except OSError:
                fail += 1
        self.log.add("ok" if not fail else "warn",
                     "清理无主缓存：删除 %d 个%s" % (
                         len(orphans) - fail,
                         "，%d 个失败" % fail if fail else ""), "下载")
        self._toast("已清理 %d 个缓存文件" % (len(orphans) - fail))

    def _poll(self):
        if self._closing:
            return
        if self.tray is not None:
            try:
                self.tray.poll()
            except Exception:                              # noqa: BLE001
                pass
        try:
            while True:
                msg = self._q.get_nowait()
                self._handle(msg)
        except queue.Empty:
            pass
        except Exception:                                   # noqa: BLE001
            pass
        self._sync_log()
        if self._dl_seen != self._dl_version:
            self._dl_seen = self._dl_version
            # 任务增减/状态变化都要重算下载按钮的可用性与文案
            self._refresh_dl_button()
            if self.page == "download":
                self.render_page()
        self._poll_job = self.root.after(100, self._poll)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "done":
            _k, proto, ok, text = msg
            self._busy.discard(proto)
            self._toast(("✓ " if ok else "✗ ") + (text or ""))
            self.render_page()
            self._refresh_all_button()
        elif kind == "activate":
            # 第二个实例来叫门：把本窗口提到前台
            self._show_window()
            self._toast("已在运行，已为你打开窗口")
        elif kind == "smb-repair":
            _k, done, failed = msg
            self._smb_fixing = False
            self._smb_last_repair = (list(done), list(failed))
            if failed:
                self._toast("SMB 修复完成 %d 项，%d 项失败（原因见面板）"
                            % (len(done), len(failed)))
            else:
                self._toast("SMB 修复完成：%d 项全部成功" % len(done))
            self._smb_issues = None        # 让界面重查一次
            self.render_page()
        elif kind == "smb-issues":
            _k, issues = msg
            self._smb_busy = False
            self._smb_issues = issues
            # 只在设置页才需要立刻把结果画出来
            if self.page == "servers":
                self.render_page()

    def _tick(self):
        if self._closing:
            return
        up, down, dl = self.manager.stats.sample()
        self._last_speed = (up, down, dl)
        self._refresh_all_button()          # 服务按钮随服务状态走（幂等）
        self._refresh_dl_button()           # 下载按钮随任务状态走（幂等）
        self._avg30 = self.manager.stats.avg(30)
        self._traffic_rows = self.manager.traffic.sample()
        self._traffic_agg = _by_ip(self._traffic_rows)
        # 每 30 秒重查一次网卡协商速率（插拔网线 / 断开重连能跟上）
        self._tick_n = getattr(self, "_tick_n", 0) + 1
        if self._tick_n % 30 == 0:
            threading.Thread(target=self._probe_link, daemon=True).start()
        # 「共享网络」页规则：只有停留在该页才每 30 秒自动扫一次
        # （切换到别的页就停，切回来由 show_page 立刻扫一次）
        if self.page == "peers" and self._tick_n % 30 == 0:
            self._start_peer_scan(manual=False)
        try:
            self.lbl_speed.configure(
                text="共享 %s   ↑%s ↓%s" % (
                    human_speed(up + down),
                    human_speed(up), human_speed(down)))
        except tk.TclError:
            return
        if self._notice and time.time() > self._notice_until:
            self._notice = ""
            try:
                self.lbl_status.configure(text="就绪", fg=self.theme["tx2"])
            except tk.TclError:
                pass
        if self.tray is not None:
            run = sum(1 for x in ("http", "ftp", "smb")
                      if self.manager.is_running(x))
            n_dev = len([a for a in self._traffic_agg if a["conns"] > 0])
            if n_dev:
                tip = "%s · %d 台设备在下载 · 共享 %s" % (
                    APP_NAME, n_dev, human_speed(up + down))
            else:
                tip = "%s · %d 个服务运行中 · 空闲" % (APP_NAME, run)
            try:
                self.tray.set_tooltip(tip)
            except Exception:                              # noqa: BLE001
                pass
        if self.page in ("overview", "traffic", "download", "peers"):
            # 这几页是实时数据，2 秒重绘一次（每秒重绘会白闪）
            if self._tick_n % 2 == 0:
                self.render_page()
        self._tick_job = self.root.after(1000, self._tick)

    def run(self):
        self._poll()
        self._tick()
        self.root.mainloop()


def human_link(bps):
    """bps → '100 Mbps' / '1 Gbps' 这种好读的写法。"""
    try:
        bps = float(bps or 0)
    except (TypeError, ValueError):
        return "未知"
    if bps >= 1e9:
        v = bps / 1e9
        s = ("%.1f" % v).rstrip("0").rstrip(".")
        return "%s Gbps" % s
    if bps >= 1e6:
        v = bps / 1e6
        s = ("%.1f" % v).rstrip("0").rstrip(".")
        return "%s Mbps" % s
    if bps >= 1e3:
        return "%d kbps" % int(round(bps / 1e3))
    return "%d bps" % int(bps)


def _is_number(text):
    """限速输入框的校验：只放行数字和一个小数点。"""
    if text.count(".") > 1:
        return False
    parts = text.split(".")
    return all(p.isdigit() for p in parts if p != "") and \
        (len(parts) <= 2)


def _by_ip(rows):
    """把连接明细按 IP 归并：一个 IP 可能开了 8 个线程在拉同一个文件。"""
    agg = {}
    for r in rows:
        a = agg.get(r["ip"])
        if a is None:
            a = {"ip": r["ip"], "speed": 0.0, "bytes": 0, "conns": 0,
                 "files": []}
            agg[r["ip"]] = a
        a["speed"] += r["speed"]
        a["bytes"] += r["bytes"]
        if not r["ended"]:
            a["conns"] += 1
        if r["name"]:
            a["files"].append(r["name"])
    out = list(agg.values())
    out.sort(key=lambda x: (-x["speed"], -x["bytes"]))
    return out


def _dir_size_text(path):
    """目录大小只数一层文件，够看且秒回（递归会把界面拖死）。"""
    if not path or not os.path.isdir(path):
        return "目录不存在"
    try:
        total = 0
        files = 0
        with os.scandir(path) as it:
            for i, e in enumerate(it):
                if i > 400:
                    break
                try:
                    if e.is_file():
                        total += e.stat().st_size
                        files += 1
                except OSError:
                    continue
        return "%s · %d 个文件" % (human_bytes(total), files)
    except OSError:
        return "无法读取"

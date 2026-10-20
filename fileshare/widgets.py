# -*- coding: utf-8 -*-
"""Win11 风格控件与页面排版辅助（全部基于 Canvas 自绘，保证跨 DPI 一致）。"""

import tkinter as tk

from . import theme as T


# ------------------------------------------------------------------ 按钮

class PillButton(tk.Canvas):
    """圆角按钮。style: primary / secondary / subtle / danger。"""

    def __init__(self, parent, fonts, theme, text, command=None,
                 style="secondary", icon=None, width=None, height=34,
                 bg=None):
        self.fonts = fonts
        self.t = theme
        self.text = text
        self.style = style
        self._cmd = command
        # icon 传入的是图标名（如 "refresh"），这里换成字体里的字形码位
        self.icon = T.icon(icon) if (fonts.has_icons and icon) else None
        self._state = "normal"

        f = fonts.btn_b if style in ("primary", "danger") else fonts.btn
        self._font = f
        tw = f.measure(text)
        iw = (fonts.icon.measure(self.icon) + T.px(7)) if self.icon else 0
        w = T.px(width) if width else (tw + iw + T.px(30))
        h = T.px(height)
        super().__init__(parent, width=w, height=h, bg=bg or theme["bg"],
                         highlightthickness=0, bd=0, cursor="hand2")
        self._bw, self._bh = w, h
        self._enabled = True
        self.bind("<Enter>", lambda e: self._set_state("hover"))
        self.bind("<Leave>", lambda e: self._set_state("normal"))
        self.bind("<ButtonPress-1>", lambda e: self._set_state("press"))
        self.bind("<ButtonRelease-1>", self._on_release)
        self._draw()

    # -- 颜色组合 --
    def _colors(self):
        t = self.t
        st = self._state if self._enabled else "disabled"
        if self.style == "primary":
            bg = {"normal": t["accent"], "hover": t["accent_hover"],
                  "press": t["accent_press"], "disabled": t["chip_bg"]}[st]
            return bg, t["accent_text"], None
        if self.style == "danger":
            bg = {"normal": t["status_bad"], "hover": t["status_bad"],
                  "press": t["status_bad"], "disabled": t["chip_bg"]}[st]
            return bg, "#FFFFFF", None
        if self.style == "subtle":
            bg = {"normal": "", "hover": t["hover"], "press": t["press"],
                  "disabled": ""}[st]
            return bg, (t["tx"] if self._enabled else t["tx3"]), None
        bg = {"normal": t["card"], "hover": t["card_hover"], "press": t["press"],
              "disabled": t["bg"]}[st]
        return bg, (t["tx"] if self._enabled else t["tx3"]), t["border_strong"]

    def _draw(self):
        self.delete("all")
        bg, fg, border = self._colors()
        if bg:
            T.draw_round_rect(self, 0, 0, self._bw, self._bh, radius=T.px(5),
                              fill=bg, outline=border, width=1,
                              photo_holder=self._holder())
        cx = self._bw / 2
        if self.icon:
            iw = self.fonts.icon.measure(self.icon)
            tw = self._font.measure(self.text)
            total = iw + T.px(7) + tw
            x0 = cx - total / 2
            self.create_text(x0, self._bh / 2, text=self.icon, anchor="w",
                             font=self.fonts.icon, fill=fg)
            self.create_text(x0 + iw + T.px(7), self._bh / 2, text=self.text,
                             anchor="w", font=self._font, fill=fg)
        else:
            self.create_text(cx, self._bh / 2, text=self.text,
                             font=self._font, fill=fg)

    def _holder(self):
        if not hasattr(self, "_imgs"):
            self._imgs = []
        self._imgs.clear()
        return self._imgs

    def _set_state(self, s):
        if not self._enabled:
            return
        if s != self._state:
            self._state = s
            self._draw()

    def _on_release(self, event):
        inside = 0 <= event.x <= self._bw and 0 <= event.y <= self._bh
        self._set_state("hover" if inside else "normal")
        if inside and self._enabled and self._cmd:
            self._cmd()

    def config_text(self, text):
        self.text = text
        self._draw()

    def set_enabled(self, on):
        self._enabled = bool(on)
        self.configure(cursor="hand2" if on else "arrow")
        self._draw()


# ------------------------------------------------------------------ 导航项

class NavItem(tk.Canvas):
    """NavigationView 左侧导航项：选中时高亮 + 强调色竖条。"""

    def __init__(self, parent, fonts, theme, text, icon_name, command=None,
                 width=232, height=42, bg=None):
        self.fonts, self.t = fonts, theme
        self.text, self._cmd = text, command
        self.icon = T.icon(icon_name) if fonts.has_icons else None
        self._bw, self._bh = T.px(width), T.px(height)
        super().__init__(parent, width=self._bw, height=self._bh,
                         bg=bg or theme["bg"], highlightthickness=0, bd=0,
                         cursor="hand2")
        self.selected = False
        self._hover = False
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._click)
        self._draw()

    def _click(self, event):
        if 0 <= event.x <= self._bw and 0 <= event.y <= self._bh and self._cmd:
            self._cmd()

    def _set_hover(self, on):
        if on != self._hover:
            self._hover = on
            self._draw()

    def _draw(self):
        self.delete("all")
        self._imgs = getattr(self, "_imgs", [])
        self._imgs.clear()
        t = self.t
        if self.selected or self._hover:
            fill = t["nav_sel"] if self.selected else t["hover"]
            T.draw_round_rect(self, T.px(6), T.px(3), self._bw - T.px(6),
                              self._bh - T.px(3), radius=T.px(6), fill=fill,
                              photo_holder=self._imgs)
        if self.selected:
            T.draw_round_rect(self, T.px(8), self._bh / 2 - T.px(8),
                              T.px(11), self._bh / 2 + T.px(8),
                              radius=T.px(2), fill=t["nav_sel_bar"],
                              photo_holder=self._imgs)
        color = t["tx"] if (self.selected or self._hover) else t["tx2"]
        x = T.px(26)
        if self.icon:
            self.create_text(x, self._bh / 2, text=self.icon, anchor="w",
                             font=self.fonts.icon, fill=color)
            x += self.fonts.icon.measure(self.icon) + T.px(12)
        else:
            x = T.px(20)
        self.create_text(x, self._bh / 2, text=self.text, anchor="w",
                         font=self.fonts.nav, fill=color)

    def set_selected(self, on):
        if on != self.selected:
            self.selected = on
            self._draw()


# ------------------------------------------------------------------ 滚动区

class ScrollArea(tk.Frame):
    """带自绘细滚动条的滚动容器；内容直接画在 .canvas 上。"""

    def __init__(self, parent, theme, bg=None):
        super().__init__(parent, bg=bg or theme["bg"])
        self.t = theme
        self.canvas = tk.Canvas(self, bg=bg or theme["bg"],
                                highlightthickness=0, bd=0,
                                yscrollincrement=T.px(24))
        self.bar = tk.Canvas(self, width=T.px(12), bg=bg or theme["bg"],
                             highlightthickness=0, bd=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bar.pack(side="right", fill="y")
        self._content_h = 1
        self._drag = None
        self._imgs = []

        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.bar.bind("<Button-1>", self._bar_press)
        self.bar.bind("<B1-Motion>", self._bar_drag)
        self.bar.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))
        self.canvas.bind("<Configure>", lambda e: self.refresh())
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.bar.bind("<MouseWheel>", self._wheel)

    # ---- 滚动 ----
    def _wheel(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120) * 3, "units")
        self._draw_bar()

    def _on_scroll(self, first, last):
        # Tk 的 yscrollcommand 回调会把参数以字符串传入
        try:
            first, last = float(first), float(last)
        except (TypeError, ValueError):
            return
        self._draw_bar(first, last)

    def _metrics(self):
        ch = max(1, self.canvas.winfo_height())
        bbox = self.canvas.bbox("all")
        content = max(1, (bbox[3] - bbox[1]) if bbox else 1)
        return ch, content

    def _draw_bar(self, first=None, last=None):
        self.bar.delete("all")
        self._imgs = []
        ch, content = self._metrics()
        if content <= ch + 2:
            return
        if first is None or last is None:
            first, last = self.canvas.yview()
        bh = max(T.px(28), int((last - first) * ch))
        top = int(first * ch)
        self._bar_geom = (top, bh, ch, content)
        T.draw_round_rect(self.bar, T.px(3), top, T.px(9), top + bh,
                          radius=T.px(3), fill=self.t["border_strong"],
                          photo_holder=self._imgs)
        self._thumb = (top, bh)

    def _bar_press(self, event):
        top, bh, _ch, _c = getattr(self, "_bar_geom", (0, 0, 1, 1))
        if top <= event.y <= top + bh:
            self._drag = ("inside", event.y - top)
        else:
            self._drag = ("jump", bh / 2)
            self._bar_drag(event)

    def _bar_drag(self, event):
        if getattr(self, "_drag", None) is None:
            return
        top, bh, ch, content = getattr(self, "_bar_geom", (0, 0, 1, 1))
        target = event.y - self._drag[1]
        denom = max(1, ch - bh)
        frac = max(0.0, min(1.0, target / denom))
        self.canvas.yview_moveto(frac * (content - ch) / max(1.0, content))
        self._draw_bar()

    def refresh(self):
        self.canvas.update_idletasks()
        self._draw_bar()

    def reset_top(self):
        self.canvas.yview_moveto(0)
        self.refresh()

    def set_scrollregion(self, height):
        self._content_h = int(height)
        self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(),
                                            int(height)))
        self.refresh()

    def bind_wheel_recursive(self, widget):
        widget.bind("<MouseWheel>", self._wheel)


# ------------------------------------------------------------------ 页面排版

class Painter(object):
    """按顺序在 Canvas 上排布内容，自动维护 y 坐标与卡片背景层级。

    用法：
        p = Painter(canvas, fonts, theme, width)
        p.heading("标题", "副标题")
        p.section("分组")
        p.card(lambda: (p.kv("键", "值"), p.body("...")) )
    """

    def __init__(self, canvas, fonts, theme, width):
        self.c = canvas
        self.f = fonts
        self.t = theme
        self.W = width
        self.pad = T.px(26)
        self.x = self.pad
        self.w = max(T.px(320), width - 2 * self.pad)
        self.y = T.px(22)
        self._sep = 0

    def _holder(self):
        """把 PhotoImage 的引用挂到 canvas 上：Painter 是短命对象，
        引用若留在自己身上，页面渲染完图片就会被回收，圆角与标签会消失。"""
        lst = getattr(self.c, "_img_refs", None)
        if lst is None:
            lst = []
            setattr(self.c, "_img_refs", lst)
        return lst

    # ---- 基础 ----
    def gap(self, n=10):
        self.y += T.px(n)

    def _text(self, x, y, text, font, fill, width=None, anchor="nw"):
        item = self.c.create_text(x, y, text=text, font=font, fill=fill,
                                  width=width, anchor=anchor,
                                  justify="left" if anchor == "nw" else "center")
        bbox = self.c.bbox(item) or (x, y, x, y)
        return item, bbox

    def heading(self, text, sub=None):
        x = self.x
        _it, bb = self._text(x, self.y, text, self.f.h1, self.t["tx"])
        self.y = bb[3] + T.px(4)
        if sub:
            _it2, bb2 = self._text(x, self.y, sub, self.f.small, self.t["tx2"],
                                   width=self.w)
            self.y = bb2[3] + T.px(6)
        self.gap(6)

    def section(self, title, right_text=None):
        self.gap(8)
        _it, bb = self._text(self.x, self.y, title, self.f.h2, self.t["tx"])
        if right_text:
            self._text(self.x + self.w, self.y + T.px(3), right_text,
                       self.f.small, self.t["tx3"], anchor="ne")
        self.y = bb[3] + T.px(8)
        self.c.create_line(self.x, self.y, self.x + self.w, self.y,
                           fill=self.t["border"])
        self.y += T.px(10)

    def body(self, text, color=None, font=None, indent=0, width=None):
        _it, bb = self._text(self.x + T.px(indent), self.y, text,
                             font or self.f.body, color or self.t["tx2"],
                             width=(width or self.w) - T.px(indent))
        self.y = bb[3] + T.px(4)
        return bb[3] - bb[1]

    def mono_text(self, text, color=None, indent=0, width=None):
        """等宽显示命令/路径，但**含非 ASCII 时退回中文安全字体**。

        Consolas / Courier 没有中文字形，而 Tk 不做字体回退：命令行里只要
        出现一个汉字（中文路径很常见），整行中文就变成豆腐块 ``□``。
        """
        text = text or ""
        ascii_only = all(ord(c) < 128 for c in text)
        font = self.f.mono if ascii_only else self.f.small
        return self.body(text, color=color, font=font, indent=indent,
                         width=width)

    def bullet(self, items, color=None, font=None, mark="•"):
        for it in items:
            x = self.x + T.px(4)
            self._text(x, self.y, mark, font or self.f.body,
                       self.t["accent"])
            _i, bb = self._text(x + T.px(14), self.y, it, font or self.f.body,
                                color or self.t["tx2"], width=self.w - T.px(20))
            self.y = bb[3] + T.px(3)

    def kv(self, key, value, kw=None):
        """一行键值：左键（窄）右值（宽）。"""
        kw = kw or T.px(110)
        _i, bb1 = self._text(self.x, self.y, key, self.f.small,
                             self.t["tx3"], width=kw)
        _j, bb2 = self._text(self.x + kw, self.y, value, self.f.body,
                             self.t["tx"], width=self.w - kw)
        self.y = max(bb1[3], bb2[3]) + T.px(3)

    def chip(self, text, fg, bg, x=None, y=None, font=None, pad=None):
        """小圆角标签，返回宽度。"""
        font = font or self.f.tiny_b
        pad = pad or T.px(8)
        x = self.x if x is None else x
        y = self.y if y is None else y
        w = font.measure(text) + 2 * pad
        h = font.metrics("linespace") + T.px(4)
        T.draw_round_rect(self.c, x, y, x + w, y + h, radius=h / 2,
                          fill=bg, photo_holder=self._holder())
        self.c.create_text(x + w / 2, y + h / 2, text=text, font=font,
                           fill=fg)
        return w, h

    def inline_chip(self, text, fg, bg, font=None):
        """在当前 y 行内从 self.x 起画一个标签并推进 y。"""
        _w, h = self.chip(text, fg, bg, font=font)
        self.y += h + T.px(4)
        return h

    def chip_row(self, chips, gap=8):
        """在同一行从左到右画多个标签，画完把游标推进到下一行。

        chips: ``[(text, fg, bg), ...]``，返回该行高度。

        注意：``chip()`` 本身**不推进游标**（它支持显式 x/y）。直接连着
        调用多个 ``chip()`` 会让它们叠在同一坐标上 —— 需要并排时用本方法，
        需要单个标签当行首时用 ``inline_chip()``。
        """
        x = self.x
        hmax = 0
        for text, fg, bg in chips:
            w, h = self.chip(text, fg, bg, x=x, y=self.y)
            x += w + T.px(gap)
            hmax = max(hmax, h)
        self.y += hmax + T.px(6)
        return hmax

    # ---- 卡片内操作按钮 ----
    def _action_colors(self, style):
        t = self.t
        if style == "primary":
            return t["accent"], t["accent_text"], None
        if style == "danger":
            return t["card"], t["status_bad"], t["border_strong"]
        if style == "secondary":
            return t["card"], t["tx"], t["border_strong"]
        return "", t["tx2"], None

    def actions(self, pairs, gap=8, height=28):
        """在当前位置画一行**可点击**的操作按钮，画完推进游标。

        pairs: ``[(文字, 回调, 样式), ...]``，样式 primary / secondary /
        danger / subtle；最后一张卡片里放「禁用 / 删除 / 打开位置」这类
        动作。

        为什么不直接用 ``PillButton``：它是独立 Tk 控件，要塞进 Canvas
        卡片里必须 ``create_window`` 定位，滚动、重绘、层级都会打架。
        这里直接把圆角矩形 + 文字画在 Canvas 上，再用 ``act:<key>`` 标签
        把点击映射回回调，滚动与重绘天然一致。

        回调按 ``act:<序号>`` 存在 ``canvas._act_map`` 上；``render_page``
        每次重绘前会重置它，避免旧回调残留（否则重绘后点按钮会作用到
        上一次扫描的条目上）。
        """
        canvas = self.c
        if not hasattr(canvas, "_act_map") or canvas._act_map is None:
            canvas._act_map = {}
        seq = getattr(canvas, "_act_seq", 0)
        # 10px 粗体而不是 9px：9px 下「删」这类笔画密的字在 150% DPI 上
        # 会糊成一团黑块（字形是对的，但看不清）。
        font = self.f.small_b
        h = T.px(height)
        x, y = self.x, self.y
        limit = self.x + self.w
        for label, cb, style in pairs:
            w = font.measure(label) + T.px(24)
            if x > self.x and x + w > limit:
                x = self.x
                y += h + T.px(6)
            seq += 1
            key = str(seq)
            canvas._act_map[key] = cb
            tags = ("scanact", "act:" + key)
            bg, fg, border = self._action_colors(style)
            if bg:
                T.draw_round_rect(canvas, x, y, x + w, y + h,
                                  radius=T.px(5), fill=bg, outline=border,
                                  width=1, tags=tags,
                                  photo_holder=self._holder())
            canvas.create_text(x + w / 2, y + h / 2, text=label, font=font,
                               fill=fg, tags=tags)
            x += w + T.px(gap)
        canvas._act_seq = seq
        self.y = y + h + T.px(6)
        return h

    def hline(self, dash=False):
        self.c.create_line(self.x, self.y, self.x + self.w, self.y,
                           fill=self.t["border"], dash=(2, 3) if dash else None)
        self.y += T.px(8)

    def button(self, x, y, w, h, label, cb, style="secondary", font=None):
        """在**指定坐标**画一个可点击按钮（不推进游标）。

        和 ``actions()`` 是同一套机制（Canvas item + ``act:`` 标签 + 回调表），
        区别只在于位置由调用方给 —— 画并排卡片内部的小按钮时需要它。
        """
        canvas = self.c
        if not hasattr(canvas, "_act_map") or canvas._act_map is None:
            canvas._act_map = {}
        seq = getattr(canvas, "_act_seq", 0) + 1
        canvas._act_seq = seq
        key = "b%d" % seq
        canvas._act_map[key] = cb
        tags = ("scanact", "act:" + key)
        bg, fg, border = self._action_colors(style)
        if bg:
            T.draw_round_rect(canvas, x, y, x + w, y + h, radius=T.px(5),
                              fill=bg, outline=border, width=1, tags=tags,
                              photo_holder=self._holder())
        canvas.create_text(x + w / 2, y + h / 2, text=label,
                           font=font or self.f.small_b, fill=fg, tags=tags)
        return w

    def metric_tiles(self, pairs, cols=4):
        """概览大数字磁贴。数值过宽时自动降字号，绝不溢出卡片。"""
        per = max(1, cols)
        tile_w = (self.w - T.px(10) * (per - 1)) / per
        lab_h = self.f.tiny.metrics("linespace")
        top = T.px(12)
        mid = T.px(4)
        avail = tile_w - T.px(16) * 2          # 左右各留 16px 内边距
        font_chain = (self.f.huge, self.f.h1, self.f.h2, self.f.h3)
        ch = 0
        rows = []
        for k, v in pairs:
            # 从最大字号往下找第一个放得下的；都放不下就用最小号
            font = font_chain[-1]
            for cand in font_chain:
                if cand.measure(str(v)) <= avail:
                    font = cand
                    break
            num_h = font.metrics("linespace")
            rows.append((k, str(v), font, num_h))
            ch = max(ch, top + num_h + mid + lab_h + T.px(10))
        for i, (k, v, font, num_h) in enumerate(rows):
            r, c = divmod(i, per)
            x = self.x + c * (tile_w + T.px(10))
            y = self.y + r * (ch + T.px(10))
            T.draw_round_rect(self.c, x, y, x + tile_w, y + ch,
                              radius=T.px(8), fill=self.t["card"],
                              outline=self.t["border"], width=1,
                              photo_holder=self._holder())
            self.c.create_text(x + T.px(16), y + top, text=v,
                               anchor="nw", font=font, fill=self.t["tx"])
            self.c.create_text(x + T.px(16), y + top + num_h + mid, text=k,
                               anchor="nw", font=self.f.tiny,
                               fill=self.t["tx2"])
        n = len(pairs)
        rows_n = (n + per - 1) // per
        self.y += rows_n * (ch + T.px(10))
        return ch

    def card(self, draw_fn, pad=18, fill=None, outline=None, radius=8, gap=12):
        """卡片容器：先画内容再补背景并压到最底层。"""
        y0 = self.y
        inset = T.px(pad)
        self.x += inset
        self.w -= 2 * inset
        self.y += inset
        draw_fn()
        self.y += inset
        self.x -= inset
        self.w += 2 * inset
        items = T.draw_round_rect(
            self.c, self.x, y0, self.x + self.w, self.y,
            radius=T.px(radius), fill=fill or self.t["card"],
            outline=outline or self.t["border"], width=1,
            photo_holder=self._holder())
        for it in items:
            self.c.tag_lower(it)
        self.y += T.px(gap)

    def progress_track(self, frac, height=6, width=None, fill=None):
        w = width or self.w
        h = T.px(height)
        T.draw_round_rect(self.c, self.x, self.y, self.x + w, self.y + h,
                          radius=h / 2, fill=self.t["track"],
                          photo_holder=self._holder())
        if frac > 0:
            fw = max(h, w * max(0.0, min(1.0, frac)))
            T.draw_round_rect(self.c, self.x, self.y, self.x + fw, self.y + h,
                              radius=h / 2, fill=fill or self.t["track_fill"],
                              photo_holder=self._holder())
        self.y += h + T.px(6)

    def table(self, headers, rows, widths=None, font=None, head_font=None):
        font = font or self.f.small
        head_font = head_font or self.f.tiny_b
        rows = list(rows)
        widths = widths or [self.w / len(headers)] * len(headers)
        scale = self.w / sum(widths)
        widths = [w * scale for w in widths]
        # 表头
        y0 = self.y
        for i, hcell in enumerate(headers):
            x = self.x + sum(widths[:i])
            self._text(x + T.px(4), self.y, str(hcell), head_font, self.t["tx3"],
                       width=widths[i] - T.px(8))
        self.y += max(font.metrics("linespace"), head_font.metrics("linespace")) \
            + T.px(8)
        self.c.create_line(self.x, self.y, self.x + self.w, self.y,
                           fill=self.t["border"])
        self.y += T.px(6)
        for row in rows:
            yline = self.y
            hmax = 0
            for i, cell in enumerate(row):
                x = self.x + sum(widths[:i])
                _it, bb = self._text(x + T.px(4), yline, str(cell), font,
                                     self.t["tx"], width=widths[i] - T.px(10))
                hmax = max(hmax, bb[3] - bb[1])
            self.y = yline + hmax + T.px(6)
            self.c.create_line(self.x, self.y - T.px(2), self.x + self.w,
                               self.y - T.px(2), fill=self.t["border"])
            self.y += T.px(2)
        self.y += T.px(4)

    def empty_state(self, title, detail, actions=()):
        self.gap(10)
        self.card(lambda: (
            self.gap(8),
            self._text_center_block(title, self.f.h3, self.t["tx"]),
            self.gap(6),
            self.body(detail, self.t["tx2"], font=self.f.small),
            self.gap(4),
            self.bullet(list(actions), mark="·") if actions else None,
            self.gap(6),
        ), fill=self.t["card"])

    def _text_center_block(self, text, font, color):
        item = self.c.create_text(self.x + self.w / 2, self.y, text=text,
                                  font=font, fill=color, anchor="n",
                                  width=self.w, justify="center")
        bb = self.c.bbox(item) or (0, self.y, 0, self.y)
        self.y = bb[3] + T.px(2)
        return item


# ------------------------------------------------------------------ 命中测试

def action_at(canvas, x, y):
    """返回画布坐标 (x, y) 处操作按钮的回调；没命中返回 None。

    ``Painter.actions`` 画出的按钮带 ``act:<key>`` 标签，这里自顶向下
    找第一个命中项。用 ``find_overlapping`` 而不是 Tk 的 ``current`` 标签：
    后者只对「已绑定事件的 item」生效，而我们的绑定在 canvas 上，
    ``current`` 拿不到具体按钮。
    """
    if canvas is None:
        return None
    try:
        items = canvas.find_overlapping(x - 1, y - 1, x + 1, y + 1)
    except tk.TclError:
        return None
    amap = getattr(canvas, "_act_map", None) or {}
    for item in reversed(items):
        for tag in canvas.gettags(item):
            if tag.startswith("act:"):
                cb = amap.get(tag[4:])
                if cb:
                    return cb
    return None


# ------------------------------------------------------------------ 开关

class Switch(tk.Canvas):
    """Win11 风格开关（胶囊 + 圆点），比 Checkbutton 轻得多也好看得多。"""

    def __init__(self, parent, fonts, theme, value=False, command=None,
                 width=44, height=24, bg=None):
        self.fonts, self.t = fonts, theme
        self._bw, self._bh = T.px(width), T.px(height)
        super().__init__(parent, width=self._bw, height=self._bh,
                         bg=bg or theme["card"], highlightthickness=0, bd=0,
                         cursor="hand2")
        self.value = bool(value)
        self._cmd = command
        self._imgs = []
        self.bind("<Button-1>", self._click)
        self._draw()

    def _click(self, _e=None):
        self.set(not self.value, notify=True)

    def set(self, value, notify=False):
        value = bool(value)
        if value != self.value:
            self.value = value
            self._draw()
            if notify and self._cmd:
                self._cmd(self.value)

    def _draw(self):
        self.delete("all")
        self._imgs = []
        t = self.t
        track = t["accent"] if self.value else t["border_strong"]
        h = self._bh
        T.draw_round_rect(self, 0, 0, self._bw, h, radius=h / 2, fill=track,
                          photo_holder=self._imgs)
        k = T.px(3)
        d = h - 2 * k
        cx = (self._bw - k - d / 2) if self.value else (k + d / 2)
        T.draw_round_rect(self, cx - d / 2, k, cx + d / 2, k + d,
                          radius=d / 2, fill="#FFFFFF", photo_holder=self._imgs)


# ------------------------------------------------------------------ 滑块

class Slider(tk.Canvas):
    """横向数值滑块。value 变化时回调 ``command(value)``（拖动过程中连续回调）。"""

    def __init__(self, parent, theme, value=0, maximum=100, minimum=0,
                 command=None, width=260, height=26, bg=None, step=1):
        self.t = theme
        self._bw, self._bh = T.px(width), T.px(height)
        super().__init__(parent, width=self._bw, height=self._bh,
                         bg=bg or theme["card"], highlightthickness=0, bd=0,
                         cursor="hand2")
        self.minimum = minimum
        self.maximum = max(minimum + 1, maximum)
        self.step = step
        self.value = min(self.maximum, max(minimum, value))
        self._cmd = command
        self._imgs = []
        self._drag = False
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._draw()

    def _ratio(self):
        return (self.value - self.minimum) / float(self.maximum - self.minimum)

    def _x_for(self, value):
        pad = T.px(9)
        return pad + (self._bw - 2 * pad) * (
            (value - self.minimum) / float(self.maximum - self.minimum))

    def _value_at(self, x):
        pad = T.px(9)
        r = (x - pad) / max(1.0, (self._bw - 2 * pad))
        r = min(1.0, max(0.0, r))
        v = self.minimum + r * (self.maximum - self.minimum)
        if self.step > 1:
            v = round(v / self.step) * self.step
        return int(round(v))

    def _on_press(self, e):
        self._drag = True
        self.set(self._value_at(e.x))

    def _on_drag(self, e):
        if self._drag:
            self.set(self._value_at(e.x))

    def _on_release(self, e):
        self._drag = False

    def set(self, value, notify=True):
        value = max(self.minimum, min(self.maximum, int(value)))
        if value != self.value:
            self.value = value
            self._draw()
        if notify and self._cmd:
            self._cmd(self.value)

    def _draw(self):
        self.delete("all")
        self._imgs = []
        t = self.t
        h = T.px(5)
        y = (self._bh - h) / 2
        T.draw_round_rect(self, T.px(9), y, self._bw - T.px(9), y + h,
                          radius=h / 2, fill=t["track"], photo_holder=self._imgs)
        cx = self._x_for(self.value)
        if cx > T.px(9) + h:
            T.draw_round_rect(self, T.px(9), y, cx, y + h, radius=h / 2,
                              fill=t["accent"], photo_holder=self._imgs)
        d = T.px(18)
        T.draw_round_rect(self, cx - d / 2, self._bh / 2 - d / 2,
                          cx + d / 2, self._bh / 2 + d / 2, radius=d / 2,
                          fill=t["accent"], outline=t["card"], width=2,
                          photo_holder=self._imgs)


# ------------------------------------------------------------------ 曲线

def blend(c1, c2, k):
    """把 c1 按 k 比例混到 c2 上（Tk 没有透明度，只能自己算颜色）。"""
    try:
        a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    except (ValueError, IndexError):
        return c1
    out = [int(a[i] + (b[i] - a[i]) * k) for i in range(3)]
    return "#%02X%02X%02X" % tuple(max(0, min(255, v)) for v in out)


class Sparkline(tk.Canvas):
    """速率曲线：多条序列、面积填充、纵向自适应。

    只重画自己的 item（不碰别的控件），每秒一次对 Canvas 来说毫无压力。
    """

    def __init__(self, parent, theme, height=96, bg=None):
        self.t = theme
        super().__init__(parent, height=T.px(height), bg=bg or theme["card"],
                         highlightthickness=0, bd=0)
        self._series = []          # [(values, color, name)]
        self._ymax = 1.0
        self._imgs = []
        self.bind("<Configure>", lambda e: self._redraw())

    def set_series(self, series, ymax=None):
        """series: ``[(values, color), ...]``，values 是最新的 N 个采样。"""
        self._series = series
        if ymax is not None:
            self._ymax = max(1.0, float(ymax))
        self._redraw()

    def _redraw(self):
        self.delete("all")
        self._imgs = []
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 10 or h < 10:
            return
        t = self.t
        # 网格
        for i in range(1, 4):
            y = h * i / 4.0
            self.create_line(0, y, w, y, fill=blend(t["border"], t["bg"], 0.55))
        vmax = max([self._ymax] + [max(v or [0]) for v, _c, _n in self._series])
        if vmax <= 0:
            vmax = 1.0
        for values, color, _name in self._series:
            if len(values) < 2:
                continue
            if not any(values):
                # 全 0 的序列画出来只是一条贴底的直线，看着像边框，干脆不画
                continue
            n = len(values)
            step = w / float(n - 1)
            pts = []
            for i, v in enumerate(values):
                x = i * step
                y = h - 2 - (h - 4) * min(1.0, (v or 0) / vmax)
                pts.extend([x, y])
            poly = list(pts) + [w, h, 0, h]
            self.create_polygon(poly, fill=blend(color, t["card"], 0.82),
                                outline="")
            self.create_line(pts, fill=color, width=max(1, T.px(2)),
                             smooth=False)
            # 末端圆点
            self.create_oval(pts[-2] - T.px(2.6), pts[-1] - T.px(2.6),
                             pts[-2] + T.px(2.6), pts[-1] + T.px(2.6),
                             fill=color, outline=t["card"], width=1)


# ------------------------------------------------------------------ 输入框

class LineEntry(tk.Entry):
    """扁平输入框：1px 边框、无 3D 浮雕，聚焦时描边变强调色。"""

    def __init__(self, parent, fonts, theme, text="", width=12, show=None,
                 command=None, justify="left"):
        self.t = theme
        var = tk.StringVar(value=str(text))
        super().__init__(
            parent, textvariable=var, width=width, font=fonts.body,
            bg=theme["card"], fg=theme["tx"], insertbackground=theme["tx"],
            relief="flat", highlightthickness=1,
            highlightbackground=theme["border_strong"],
            highlightcolor=theme["accent"], justify=justify, show=show or "",
            bd=0)
        self.var = var
        self._cmd = command
        if command:
            self.bind("<Return>", lambda e: command(var.get()))
            self.bind("<FocusOut>", lambda e: command(var.get()))

    def get_value(self):
        return self.var.get()

    def set_value(self, text):
        self.var.set(str(text))



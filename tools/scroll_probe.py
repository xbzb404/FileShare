# -*- coding: utf-8 -*-
"""滚轮 UI bug 专项探针  ·  内容不满一页时，页面必须钉死。

背景（用户原话）：「一个页面内容小于一页时，本应当固定页面，但还是可以
鼠标滚轮操控，把最上边的标题都拉到底部了」。

断言：
  A. 短页（内容高 ≤ 视口高）发再多滚轮事件，yview 恒为 0；
  B. 复刻真实病灶——region 先高后低（两次渲染间内容变短）残留的越界
     滚态，必须被拉回顶部；
  B2. 不靠滚轮，光 refresh（窗口 resize 会走到）也要回顶；
  C. 长页滚轮可滚，但绝不越过内容末端；
  D. 切页不继承上一页的滚动比例（长页滚到底 → 切短页，yview == 0）；
  E. 窗口从「装不下」放大到「装得下」时，滚动位置回顶；
  F. 拖动自绘滚动条同样受钳制。

    python tools/scroll_probe.py
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

OUT = os.path.join(ROOT, "screenshots", "_scrolltest")
os.makedirs(OUT, exist_ok=True)

SHORT_PAGES = ("traffic", "shares", "peers", "download")
LONG_PAGES = ("overview", "servers")


def wheel_down(canvas, times=1):
    for _ in range(times):
        canvas.event_generate("<MouseWheel>", x=60, y=60, delta=-120)


def geom(canvas):
    ch = canvas.winfo_height()
    sr = canvas.cget("scrollregion")
    region = int(sr.split()[3]) if sr else 0
    return ch, region


def max_first(ch, region):
    """内容不满一屏 → 0；否则 (region-ch)/region。"""
    if region <= ch:
        return 0.0
    return (region - ch) / float(region)


def tk_end(canvas):
    """Tk 自己允许的最大首行比例：滚到最底，把它报的 yview 读回来。

    不能只用 ``(region - winfo_height()) / region`` 推算：Tk 内部的可视
    高度和 ``winfo_height`` 实测差 9px（``yscrollincrement`` 取整导致），
    用推算值当上限会把「已经滚到真正的底部」误判成越界。
    """
    cur = canvas.yview()[0]
    canvas.yview_moveto(1.0)
    end = canvas.yview()[0]
    canvas.yview_moveto(cur)
    return end


def main():
    from fileshare.config import Config
    from fileshare.ui.app import App

    tmpcfg = os.path.join(OUT, "_probe_config.json")
    for p in (tmpcfg, tmpcfg + ".tmp", os.path.join(OUT, "downloads.json")):
        if os.path.exists(p):
            os.remove(p)
    cfg = Config(path=tmpcfg)
    cfg.data["http"]["enabled"] = False
    cfg.data["ftp"]["enabled"] = False

    app = App(cfg)
    root = app.root
    root.update()
    root.update_idletasks()
    time.sleep(0.3)
    c = app.scroll.canvas
    fails = []

    def check(name, cond, detail=""):
        print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))
        if not cond:
            fails.append(name)

    for key in SHORT_PAGES:
        app.show_page(key)
        root.update()
        root.update_idletasks()
        time.sleep(0.1)
        ch, region = geom(c)
        # A. 滚轮轰到底也不动
        before = c.yview()[0]
        wheel_down(c, 12)
        root.update()
        after = c.yview()[0]
        check("A:%s 短页滚轮不动" % key, after == 0.0 and before == 0.0,
              "region=%d viewport=%d yview %.3f->%.3f" % (region, ch,
                                                          before, after))
        # B. 复刻真实病灶：region 先高后低（两次渲染间内容变短），
        #    中间残留的越界滚态必须被拉回顶部
        app.scroll.set_scrollregion(region + 500)      # 假装之前内容更高
        c.yview_moveto(0.6)
        root.update()
        stuck = c.yview()[0]
        app.scroll.set_scrollregion(region)            # 重渲染后内容变短
        root.update()
        shrunk = c.yview()[0]
        wheel_down(c, 1)
        root.update()
        check("B:%s 越界残留被拉回" % key,
              c.yview()[0] == 0.0 and shrunk == 0.0,
              "stuck=%.3f region 变短后=%.3f 滚轮后=%.3f"
              % (stuck, shrunk, c.yview()[0]))
        # B2. 不靠滚轮，光 refresh（窗口 resize 会走到）也要回顶
        app.scroll.set_scrollregion(region + 500)
        c.yview_moveto(0.6)
        root.update()
        app.scroll.set_scrollregion(region)
        c.yview_moveto(0.6)
        root.update()
        app.scroll.refresh()
        root.update()
        check("B2:%s refresh 拉回顶部" % key, c.yview()[0] == 0.0,
              "yview=%.3f" % c.yview()[0])

    for key in LONG_PAGES:
        app.show_page(key)
        root.update()
        root.update_idletasks()
        time.sleep(0.1)
        ch, region = geom(c)
        # C. 能滚，但不越过末端
        wheel_down(c, 30)
        root.update()
        # 页面内容异步变高（SMB 体检结果回来会重绘）：上限必须用**当下**的
        # region 算，否则拿滚之前的高度去比，会误报「越界」。
        ch_now, region_now = geom(c)
        first = c.yview()[0]
        lim = tk_end(c)
        root.update()
        check("C:%s 长页滚动不越界" % key, 0.0 < first <= lim + 1e-6,
              "region=%d viewport=%d yview=%.4f 末端=%.4f 推算=%.4f"
              % (region_now, ch_now, first, lim,
                 max_first(ch_now, region_now)))
        # C2. 而且真的能滚到最底（钳制别把最后一行挡住）
        check("C2:%s 能滚到内容末尾" % key, first >= lim - 1e-3,
              "yview=%.4f 末端=%.4f" % (first, lim))

    # D. 切页不继承滚动比例
    app.show_page("servers")
    root.update()
    root.update_idletasks()
    time.sleep(0.1)
    wheel_down(c, 10)
    root.update()
    scrolled = c.yview()[0]
    app.show_page("shares")            # shares 是短页
    root.update()
    root.update_idletasks()
    time.sleep(0.1)
    check("D: 切页不回继承滚态", c.yview()[0] == 0.0 and scrolled > 0.05,
          "servers yview=%.3f -> shares yview=%.3f" % (scrolled, c.yview()[0]))

    # E. 窗口从「装不下」放大到「装得下」时，滚动位置回顶
    #    （resize 只走 <Configure> → refresh，不重渲染页面）
    #    用 download 页：默认窗 935 高于最小窗视口 749、低于大窗视口 959
    app.show_page("download")
    root.update()
    root.update_idletasks()
    time.sleep(0.1)
    big_w, big_h = root.winfo_width(), root.winfo_height()
    root.geometry("940x620")           # 最小尺寸：视口装不下 download
    root.update()
    root.update_idletasks()
    time.sleep(0.3)
    ch_small, region = geom(c)
    if region > ch_small:
        wheel_down(c, 8)
        root.update()
        scrolled_small = c.yview()[0]
        root.geometry("%dx%d" % (big_w, big_h))   # 放大到装得下
        root.update()
        root.update_idletasks()
        time.sleep(0.4)
        ch_big, _region = geom(c)
        check("E: 放大后短页回顶",
              c.yview()[0] == 0.0 and scrolled_small > 0.0,
              "小窗 region=%d viewport=%d yview=%.3f → 大窗 viewport=%d yview=%.3f"
              % (region, ch_small, scrolled_small, ch_big, c.yview()[0]))
    else:
        check("E: 放大后短页回顶", False,
              "前提不成立：小窗视口 %d 仍装得下 region=%d" % (ch_small, region))

    # F. 拖动自绘滚动条同样受钳制（短页拖了也不动）
    app.show_page("peers")
    root.update()
    root.update_idletasks()
    time.sleep(0.1)
    bar = app.scroll.bar
    bar.event_generate("<Button-1>", x=6, y=80)
    bar.event_generate("<B1-Motion>", x=6, y=200)
    bar.event_generate("<ButtonRelease-1>", x=6, y=200)
    root.update()
    check("F: 短页拖滚动条不动", c.yview()[0] == 0.0,
          "yview=%.3f" % c.yview()[0])

    root.destroy()
    print("\n%s" % ("SCROLL PROBE PASS" if not fails
                    else "SCROLL PROBE FAIL: %s" % fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())

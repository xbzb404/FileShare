# -*- coding: utf-8 -*-
"""生成 assets/icon.ico（多尺寸，纯标准库绘制）·  by zhb

图案：蓝色渐变的圆角方块 + 白色文件夹 + 镂空的向上箭头（共享 / 上传）。

纯数学光栅化 + 4×4 超采样抗锯齿，不依赖 Pillow —— 这样在只有系统 Python
的环境里也能重新生成图标。改图形就改 ``_in_folder`` / ``_in_arrow`` 重跑。

用法：python make_icon.py [输出路径]
"""

import os
import struct
import sys

BG_TOP = (32, 132, 226)       # #2084E2
BG_BOTTOM = (10, 68, 132)     # #0A4484

SIZES = [16, 24, 32, 48, 64, 128, 256]


def _in_round_rect(u, v, x0, y0, x1, y1, r):
    if not (x0 <= u <= x1 and y0 <= v <= y1):
        return False
    cx = min(max(u, x0 + r), x1 - r)
    cy = min(max(v, y0 + r), y1 - r)
    return (u - cx) ** 2 + (v - cy) ** 2 <= r * r


def _in_folder(u, v):
    """文件夹：左下主体 + 左上标签。"""
    if _in_round_rect(u, v, 0.125, 0.325, 0.875, 0.775, 0.075):
        return True
    if _in_round_rect(u, v, 0.125, 0.225, 0.475, 0.390, 0.055):
        return True
    return False


def _in_arrow(u, v):
    """向上箭头：竖杆 + 三角头（在文件夹上镂空出来）。"""
    if 0.447 <= u <= 0.553 and 0.455 <= v <= 0.705:
        return True
    if 0.370 <= v <= 0.510:
        t = (0.510 - v) / (0.510 - 0.370)      # 0=底边, 1=顶点
        half = 0.150 * (1.0 - t) + 0.004
        if abs(u - 0.5) <= half:
            return True
    return False


def _in_big_arrow(u, v):
    """小尺寸专用：只画一支粗壮的白箭头。

    16×16 的任务栏/标题栏图标只有 256 个像素，文件夹轮廓 + 镂空箭头的
    细节会糊成一团（实测像个十字）。这个尺寸下「蓝底一支白箭头」最好认。
    """
    if 0.395 <= u <= 0.605 and 0.415 <= v <= 0.845:
        return True
    if 0.155 <= v <= 0.530:
        t = (0.530 - v) / (0.530 - 0.155)
        half = 0.330 * (1.0 - t) + 0.006
        if abs(u - 0.5) <= half:
            return True
    return False


def render_rgba(size, ss=4):
    out = bytearray(size * size * 4)
    step = 1.0 / (size * ss)
    n = ss * ss
    simple = size <= 24                     # 小图标走简化字形
    for py in range(size):
        for px in range(size):
            acc_a = acc_r = acc_g = acc_b = 0.0
            for sy in range(ss):
                v = (py * ss + sy + 0.5) * step
                for sx in range(ss):
                    u = (px * ss + sx + 0.5) * step
                    if not _in_round_rect(u, v, 0.01, 0.01, 0.99, 0.99, 0.225):
                        continue
                    k = min(max((v - 0.01) / 0.98, 0.0), 1.0)
                    gr = BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * k
                    gg = BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * k
                    gb = BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * k
                    if simple:
                        if _in_big_arrow(u, v):
                            r = g = b = 255.0
                        else:
                            r, g, b = gr, gg, gb
                    elif _in_folder(u, v):
                        if _in_arrow(u, v):
                            r, g, b = gr, gg, gb          # 镂空 → 底色
                        else:
                            r = g = b = 255.0
                    else:
                        r, g, b = gr, gg, gb
                    acc_a += 1.0
                    acc_r += r
                    acc_g += g
                    acc_b += b
            if acc_a <= 0:
                continue
            a = acc_a / n
            i = (py * size + px) * 4
            out[i] = int(acc_b / acc_a + 0.5)
            out[i + 1] = int(acc_g / acc_a + 0.5)
            out[i + 2] = int(acc_r / acc_a + 0.5)
            out[i + 3] = int(a * 255 + 0.5)
    return bytes(out)


def write_ico(path, sizes=SIZES):
    frames = []
    for s in sizes:
        bgra = render_rgba(s)
        rows = [bgra[y * s * 4:(y + 1) * s * 4] for y in range(s)]
        bottom_up = b"".join(reversed(rows))
        header = struct.pack("<IiiHHIIiiII", 40, s, s * 2, 1, 32, 0,
                             len(bottom_up), 0, 0, 0, 0)
        mask = b"\x00" * (((s + 31) // 32) * 4 * s)
        frames.append((s, header + bottom_up + mask))

    out = bytearray(struct.pack("<HHH", 0, 1, len(frames)))
    offset = 6 + 16 * len(frames)
    entries = bytearray()
    blobs = bytearray()
    for s, blob in frames:
        dim = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                               len(blob), offset)
        offset += len(blob)
        blobs += blob
    out += entries + blobs
    with open(path, "wb") as fp:
        fp.write(bytes(out))
    return path


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        here, "assets", "icon.ico")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    write_ico(out)
    print("已生成 %s（%s 字节，尺寸 %s）" % (
        out, format(os.path.getsize(out), ","),
        "/".join(str(s) for s in SIZES)))


if __name__ == "__main__":
    main()

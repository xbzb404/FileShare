# -*- coding: utf-8 -*-
"""检查 Release 说明有没有「聊天口吻」和私有信息（发布前跑）。

为什么需要这个：Release 说明是**给所有人看的文档**，不是聊天回复。
写的时候如果顺手把对某位用户的答复粘进去，就会带上：

  * 第二人称（「你截图里…」「你现在要做的」）——读者不知道在跟谁说话；
  * 私有信息（192.168.x.x、DESKTOP-XXXX、E:\\ 本机路径）——泄漏用户环境；
  * 「我刚才」「我这边」这类对话残留。

用法::

    python tools/lint_notes.py docs/release_v1.1.3.md
    python tools/lint_notes.py docs/release_v1.*.md     # 支持多个/通配

退出码 0 = 干净，1 = 有问题。``publish_release.py`` 发布前会自动调用它。
"""
from __future__ import annotations

import os
import re
import sys

# 代码块 / 行内代码里允许出现系统的原始提示语，不做第二人称检查
FENCE = re.compile(r"```.*?```|~~~.*?~~~", re.S)
INLINE = re.compile(r"`[^`\n]*`")

# (规则名, 正则, 说明)
RULES = [
    ("第二人称", re.compile(r"[你您]"),
     "Release 说明是给所有读者看的，不要用「你/您」"),
    ("私有 IP", re.compile(r"\b192\.168\.\d+\.\d+|\b10\.\d+\.\d+\.\d+"
                          r"|\b172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+"),
     "换成 <共享机IP> 之类的占位符"),
    ("本机路径", re.compile(r"\b[A-Za-z]:\\\\?(?:Users|AI|\w)"),
     "不要写发布者本机的绝对路径"),
    ("机器名", re.compile(r"\bDESKTOP-[A-Z0-9]{5,}\b"),
     "不要写具体机器名"),
    ("聊天残留", re.compile(r"我刚才|我这边|你给我|你截图|截图里的"),
     "这是对话里的说法，改成面向所有读者的描述"),
]

ALLOW = [
    # 「附上截图会更快定位」是给报 issue 的人的通用建议，不是聊天残留
    re.compile(r"附上.{0,6}截图"),
]


def strip_code(text):
    text = FENCE.sub(" ", text)
    return INLINE.sub(" ", text)


def line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def lint(path):
    with open(path, encoding="utf-8") as fp:
        raw = fp.read()
    prose = strip_code(raw)
    hits = []
    for name, pattern, hint in RULES:
        target = prose if name == "第二人称" else raw
        for m in pattern.finditer(target):
            snippet = target[max(0, m.start() - 20):m.end() + 20]
            snippet = snippet.replace("\n", " ")
            if any(a.search(snippet) for a in ALLOW):
                continue
            hits.append((name, line_of(raw, m.start()) if target is raw else 0,
                         snippet.strip(), hint))
    return hits


def main(argv):
    files = []
    for arg in argv:
        if any(ch in arg for ch in "*?"):
            import glob
            files.extend(sorted(glob.glob(arg)))
        else:
            files.append(arg)
    if not files:
        print("用法：python tools/lint_notes.py docs/release_v1.1.4.md")
        return 2

    bad = 0
    for path in files:
        if not os.path.isfile(path):
            print("[跳过] %s 不存在" % path)
            continue
        hits = lint(path)
        if not hits:
            print("[OK]   %s" % path)
            continue
        bad += 1
        print("[问题] %s" % path)
        for name, line, snippet, hint in hits:
            where = ("第 %d 行" % line) if line else "（代码块外）"
            print("   %-8s %s  …%s…" % (name, where, snippet[:60]))
            print("            建议：%s" % hint)
    print("\n%s" % ("全部通过" if not bad else "%d 个文件有问题" % bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

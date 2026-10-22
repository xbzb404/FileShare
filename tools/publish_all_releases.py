# -*- coding: utf-8 -*-
"""一次性补齐所有版本的 Release（v1.1.0 ~ v1.1.4）。

规则：
  * 最新版（``--asset-tag``，默认 v1.1.4）挂当前 exe；
  * 历史版本**只放说明、不挂 exe** —— exe 已被新版本覆盖，
    挂上去会让人下到版本对不上的包；
  * 已存在的 Release 复用（publish_release.py 自带这个行为），可重复执行。

用法::

    python tools/publish_all_releases.py --dry-run     # 先看会做什么
    python tools/publish_all_releases.py               # 真发（需要 token）
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUB = os.path.join(ROOT, "tools", "publish_release.py")

TAGS = ("v1.1.0", "v1.1.1", "v1.1.2", "v1.1.3", "v1.1.4")
REPO = "xbzb404/FileShare"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--asset-tag", default="v1.1.4",
                    help="哪个版本挂 exe（默认最新版）")
    ap.add_argument("--asset", default=os.path.join(ROOT, "dist", "FileShare.exe"))
    ap.add_argument("--repo", default=REPO)
    args = ap.parse_args()

    failed = []
    for tag in TAGS:
        notes = os.path.join(ROOT, "docs", "release_%s.md" % tag)
        if not os.path.isfile(notes):
            print("[跳过] %s 没有说明文件 %s" % (tag, notes))
            continue
        cmd = [sys.executable, PUB, "--repo", args.repo, "--tag", tag,
               "--notes", notes]
        if tag == args.asset_tag and os.path.isfile(args.asset):
            cmd += ["--asset", args.asset]
        if args.dry_run:
            cmd.append("--dry-run")
        print("\n=== %s ===" % tag)
        rc = subprocess.call(cmd, cwd=ROOT)
        if rc != 0:
            failed.append(tag)
    print("\n%s" % ("全部完成" if not failed else "失败：%s" % "、".join(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""保存 GitHub token 并验证它能不能给这个仓库发 Release。

用法::

    python tools/setup_github_token.py            # 交互粘贴（推荐）
    python tools/setup_github_token.py --from-env GITHUB_TOKEN

做的事：
  1. 读 token（stdin 或环境变量），**不回显**、不写进日志；
  2. 调 ``GET /user`` 确认 token 有效，打印登录名；
  3. 调 ``GET /repos/xbzb404/FileShare`` 确认能访问，并读 ``permissions.push``；
  4. 写到 ``~/.github-token``，供 tools/publish_release.py 复用。

只做验证与落盘，不发任何 Release。
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
REPO = "xbzb404/FileShare"


def _get(path, token):
    req = urllib.request.Request(
        API + path,
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "FileShare-setup"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-env", default="", help="从哪个环境变量读 token")
    ap.add_argument("--repo", default=REPO)
    args = ap.parse_args()

    if args.from_env:
        token = os.environ.get(args.from_env, "")
    else:
        print("粘贴 GitHub token 后回车（输入不会回显）：")
        token = getpass.getpass("token: ")
    token = (token or "").strip()
    if not token:
        print("没有读到 token")
        return 2
    if " " in token:
        print("token 里不该有空格，可能粘多了")
        return 2

    try:
        _code, me = _get("/user", token)
    except urllib.error.HTTPError as exc:
        print("token 无效（HTTP %d）：%s" % (exc.code, exc.reason))
        return 3
    except OSError as exc:
        print("连不上 api.github.com：%s" % exc)
        return 1
    print("已认证：%s" % me.get("login"))

    try:
        _code, repo = _get("/repos/%s" % args.repo, token)
    except urllib.error.HTTPError as exc:
        print("这个 token 读不到 %s（HTTP %d）——检查仓库名或令牌权限"
              % (args.repo, exc.code))
        return 3
    perms = repo.get("permissions") or {}
    print("仓库：%s（%s）" % (repo.get("full_name"), repo.get("visibility")))
    if not perms.get("push"):
        print("⚠️  没有写权限：细粒度令牌要给 Contents: Read and write；"
              "经典令牌要勾 repo")
        return 4

    path = os.path.join(os.path.expanduser("~"), ".github-token")
    with open(path, "w", encoding="utf-8", newline="\n") as fp:
        fp.write(token + "\n")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    print("已保存到 %s（只读权限）" % path)
    print("接下来可以跑：python tools/publish_release.py --tag v1.1.4 ...")
    return 0


if __name__ == "__main__":
    sys.exit(main())

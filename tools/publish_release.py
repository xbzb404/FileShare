# -*- coding: utf-8 -*-
"""建 GitHub Release 并上传附件（纯标准库，不依赖 gh / 第三方包）。

token 的取用顺序（先命中的赢）：
  1. 环境变量 ``GITHUB_TOKEN`` / ``GH_TOKEN``
  2. ``~/.github-token``（或 ``%USERPROFILE%\\.github-token``）文件，一行纯 token
  3. ``gh auth token``（装了 GitHub CLI 且已登录时）
  4. ``git credential fill``（HTTPS 凭据管理器里存过 token 时）

用法：
    python tools/publish_release.py --repo xbzb404/FileShare --tag v1.1.0 \\
        --notes docs/release_v1.1.0.md --asset dist/FileShare.exe
    python tools/publish_release.py --repo xbzb404/FileShare --tag v1.1.0 \\
        --asset dist/FileShare.exe --dry-run

要点（踩过的坑）：
  * 附件上传域名是 **uploads.github.com**，写成 api.github.com 会 404；
  * 中文文件名要 ``urllib.parse.quote``；
  * 已存在同名 release → 复用，不硬建（422 already_exists）；
  * 同名附件重复上传会 422，先查再决定跳过；
  * 仓库 ``size`` 字段是异步更新的，刚传完可能还是旧值。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"


def _clean(tok):
    tok = (tok or "").strip()
    return tok if tok and " " not in tok else ""


def _env_no_prompt():
    """子进程一律禁止交互：否则 git credential fill 会等密码把进程挂住。"""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["GH_PROMPT_DISABLED"] = "1"
    return env


def get_token(verbose=True):
    """按优先级找 token；找不到返回空串。"""
    for env in ("GITHUB_TOKEN", "GH_TOKEN"):
        tok = _clean(os.environ.get(env))
        if tok:
            if verbose:
                print("token 来源：环境变量 %s" % env)
            return tok

    for path in (os.path.join(os.path.expanduser("~"), ".github-token"),
                 os.path.join(os.environ.get("USERPROFILE", ""),
                              ".github-token")):
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fp:
                    tok = _clean(fp.read())
            except OSError:
                tok = ""
            if tok:
                if verbose:
                    print("token 来源：%s" % path)
                return tok

    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True,
                             text=True, timeout=20,
                             env=_env_no_prompt())
        tok = _clean(out.stdout)
        if tok:
            if verbose:
                print("token 来源：gh auth token")
            return tok
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        out = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, timeout=15,
            env=_env_no_prompt())
        for line in (out.stdout or "").splitlines():
            if line.startswith("password="):
                tok = _clean(line.split("=", 1)[1])
                if tok:
                    if verbose:
                        print("token 来源：git credential（凭据管理器）")
                    return tok
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def api(path, token, method="GET", body=None, timeout=120):
    url = path if path.startswith("http") else API + path
    data = None
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "FileShare-release-tool"}
    if token:                     # 空 token 走匿名 GET（公开仓库能查，写操作会 401）
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def ensure_release(repo, tag, title, notes, dry=False, update_body=False):
    token = get_token()
    try:
        rel = api("/repos/%s/releases/tags/%s" % (repo, tag), token)
        print("复用已有 release：%s（id %s）" % (rel.get("html_url"), rel["id"]))
        if update_body and notes:
            if dry:
                print("[dry-run] 将更新已有 release 的说明（%d 字符）"
                      % len(notes))
            else:
                body = {"body": notes}
                if title:
                    body["name"] = title
                rel = api("/repos/%s/releases/%s" % (repo, rel["id"]),
                          token, "PATCH", body)
                print("已更新 release 说明（%d 字符）" % len(notes))
        return rel
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    body = {"tag_name": tag, "name": title or tag, "body": notes,
            "draft": False, "prerelease": False}
    if dry:
        print("[dry-run] 将创建 release %s ← tag %s" % (repo, tag))
        return {"id": 0, "html_url": "(dry-run)", "assets": []}
    rel = api("/repos/%s/releases" % repo, token, "POST", body)
    print("已创建 release：%s" % rel.get("html_url"))
    return rel


def upload(repo, rel, path, token, dry=False):
    name = os.path.basename(path)
    size = os.path.getsize(path)
    for a in rel.get("assets") or []:
        if a.get("name") == name:
            print("附件已存在，跳过：%s（%d 字节）" % (name, a.get("size") or 0))
            return
    if dry:
        print("[dry-run] 将上传 %s（%d 字节）" % (name, size))
        return
    url = "%s/repos/%s/releases/%s/assets?name=%s" % (
        UPLOADS, repo, rel["id"], urllib.parse.quote(name))
    with open(path, "rb") as fp:
        data = fp.read()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/octet-stream",
                 "Accept": "application/vnd.github+json",
                 "Content-Length": str(size),
                 "User-Agent": "FileShare-release-tool"})
    with urllib.request.urlopen(req, timeout=1800) as resp:
        info = json.loads(resp.read().decode("utf-8"))
    got = info.get("size") or 0
    print("已上传 %s：%d 字节（本地 %d）%s" %
          (name, got, size, "✓" if got == size else "⚠ 大小不一致"))


def main():
    ap = argparse.ArgumentParser(description="建 Release 并上传附件")
    ap.add_argument("--repo", required=True, help="owner/Name")
    ap.add_argument("--tag", required=True, help="如 v1.0.0")
    ap.add_argument("--title", default="", help="留空则用 tag")
    ap.add_argument("--notes", default="", help="说明文件路径（md）")
    ap.add_argument("--asset", action="append", default=[], help="附件，可多次")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--update-body", action="store_true",
                    help="release 已存在时，用 --notes 覆盖它的说明")
    args = ap.parse_args()

    notes = ""
    if args.notes:
        with open(args.notes, "r", encoding="utf-8") as fp:
            notes = fp.read()

    token = get_token()
    if not token and not args.dry_run:
        print("找不到 GitHub token。三种给法任选其一：\n"
              "  1) 设环境变量 GITHUB_TOKEN\n"
              "  2) 写文件 ~/.github-token（一行纯 token）\n"
              "  3) 装 GitHub CLI 并 gh auth login\n", file=sys.stderr)
        return 2

    rel = ensure_release(args.repo, args.tag, args.title, notes, args.dry_run,
                         args.update_body)
    for path in args.asset:
        if not os.path.isfile(path):
            print("附件不存在：%s" % path, file=sys.stderr)
            return 2
        upload(args.repo, rel, path, token, args.dry_run)
    print("完成：https://github.com/%s/releases/tag/%s" % (args.repo, args.tag))
    return 0


if __name__ == "__main__":
    sys.exit(main())

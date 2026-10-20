# -*- coding: utf-8 -*-
"""端到端冒烟：启动真实程序 → 验证 HTTP/FTP 真的能访问 → 干净退出。

    python tools/smoke.py [--exe dist/FileShare.exe]

要点：
  · 用临时 ``APPDATA`` 隔离配置，绝不碰用户真实设置；
  · 程序启动后会自动拉起 HTTP(8080) / FTP(2121)，这里真的去连；
  · 结束后检查有没有 ``FileShare_error.log``（打包版没有控制台，异常只在这里）。
"""

import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def wait_port(port, timeout=15.0, host="127.0.0.1"):
    end = time.time() + timeout
    while time.time() < end:
        s = socket.socket()
        s.settimeout(0.6)
        try:
            s.connect((host, port))
            return True
        except OSError:
            time.sleep(0.25)
        finally:
            s.close()
    return False


def main():
    use_exe = "--exe" in sys.argv
    exe = None
    if use_exe:
        idx = sys.argv.index("--exe")
        exe = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else \
            os.path.join(ROOT, "dist", "FileShare.exe")

    tmp = tempfile.mkdtemp(prefix="fs_smoke_")
    env = dict(os.environ)
    env["APPDATA"] = tmp                      # 隔离配置
    env["LOCALAPPDATA"] = tmp

    if exe:
        cmd = [exe]
        print("启动打包版：", exe)
    else:
        cmd = [PY, os.path.join(ROOT, "main.py")]
        print("启动源码版：", " ".join(cmd))

    proc = subprocess.Popen(cmd, env=env, cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    ok = True
    try:
        print("等待服务起来 …")
        if not wait_port(8080, 25):
            print("  !! HTTP 8080 没有监听")
            ok = False
        else:
            print("  HTTP 8080 已监听")

        # 首次运行没有共享目录，根页面应该仍然可访问并给出提示
        try:
            with urllib.request.urlopen("http://127.0.0.1:8080/", timeout=8) as r:
                body = r.read().decode("utf-8", "ignore")
                print("  GET / -> %d，返回 %d 字节" % (r.status, len(body)))
                if "文件共享" not in body:
                    print("  !! 页面内容不对")
                    ok = False
        except Exception as exc:                           # noqa: BLE001
            print("  !! HTTP 请求失败：", exc)
            ok = False

        if not wait_port(2121, 15):
            print("  !! FTP 2121 没有监听")
            ok = False
        else:
            print("  FTP 2121 已监听")
            try:
                import ftplib
                ftp = ftplib.FTP()
                ftp.encoding = "utf-8"
                ftp.connect("127.0.0.1", 2121, timeout=8)
                ftp.login("share", "fileshare")
                print("  FTP 登录成功，根目录：", ftp.nlst())
                ftp.quit()
            except Exception as exc:                       # noqa: BLE001
                print("  !! FTP 失败：", exc)
                ok = False

        # 内存/CPU 粗查
        try:
            import psutil                                 # noqa: F401
            p = psutil.Process(proc.pid)
            print("  进程内存 %.1f MB" % (p.memory_info().rss / 1048576.0))
        except Exception:                                  # noqa: BLE001
            pass

        print("  进程存活：", proc.poll() is None)
        ok = ok and proc.poll() is None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(0.8)
        log = os.path.join(ROOT, "FileShare_error.log")
        if os.path.exists(log):
            print("\n!! 发现崩溃日志：")
            with open(log, "r", encoding="utf-8") as fp:
                print(fp.read()[:2000])
            ok = False
            os.remove(log)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%s" % ("SMOKE PASS" if ok else "SMOKE FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

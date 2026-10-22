# -*- coding: utf-8 -*-
"""Windows 原生共享（SMB）管理。

不引第三方 SMB 服务端，直接用系统自带的「共享文件夹」能力（``net share``）：
Windows 自己的 SMB 服务是内核态实现，吞吐和兼容性比任何 Python 实现都好，
我们要做的只是把共享建好、删掉、查状态。

需要管理员权限（``net share`` 会返回系统错误 5）。

除了建共享，这里还负责「别人连不上」的自检与修复 —— Win10/11 默认把
三条路全堵上了，光把共享建好，局域网其它机器还是进不来：

1. **没有可用的登录账户**：``Guest`` 默认禁用，``LimitBlankPasswordUse=1``
   又禁止空密码网络登录，客户端拿不到凭据，连密码框都不弹就报「无法访问」；
2. **NTFS 权限不含访问者**：共享权限给了 Everyone，但文件系统 ACL 只有
   ``Authenticated Users``，来宾在第二道门被拒（报「拒绝访问」）；
3. **防火墙 445 只在「专用网络」放行**：网络类别是「公用」时入站包被直接丢弃。

本程序的修复策略是「建专用本地账户并只给它授权」，而不是开 Guest：
Guest 常被组策略锁死改不动，而且开了等于把整机匿名暴露。
"""

import os
import re
import sys

from ..netutil import is_admin, run_hidden

# 专用共享账户（只用于局域网文件共享，不给任何本机权限）
SHARE_USER = "FileShare"
SHARE_PASS = "fileshare"

# Everyone 的 SID：跨语言系统都一致。中文系统 icacls 认 "Everyone"，
# 但精简版可能去掉名称映射，SID 一定不会错
SID_EVERYONE = "*S-1-1-0"

FW_RULE = "FileShare SMB (TCP 445)"


class SmbError(Exception):
    pass


class SmbManager(object):
    def __init__(self, log=None):
        self.log = log
        self.managed = set()          # 本程序创建的共享名，退出时只删这些
        self.account = ""             # 上次修复用的共享账户名（空 = 未配置）

    # ------------------------------------------------------------ 环境
    def available(self):
        if not sys.platform.startswith("win"):
            return False, "仅支持 Windows"
        if not is_admin():
            return False, "需要管理员权限（点右上角「以管理员身份重启」）"
        return True, "OK"

    def _run(self, args, timeout=20):
        return run_hidden(args, timeout=timeout)

    def _ps(self, script, timeout=30):
        """跑一段 PowerShell 取一行输出（查系统配置比解析 net 输出稳）。"""
        return self._run(["powershell", "-NoProfile", "-NonInteractive",
                          "-Command", script], timeout=timeout)

    # ------------------------------------------------------------ 查询
    def exists(self, name):
        rc, _out = self._run(["net", "share", name])
        return rc == 0

    def list_all(self):
        """本机所有共享名 → 路径。解析 ``net share`` 的输出。"""
        rc, out = self._run(["net", "share"])
        result = {}
        if rc != 0:
            return result
        for line in out.splitlines():
            line = line.rstrip()
            if not line or line.startswith("-") or line.startswith("共享名") \
                    or line.startswith("Share name") or line.startswith("命令"):
                continue
            parts = line.split()
            if not parts:
                continue
            name = parts[0]
            for p in parts[1:]:
                if len(p) > 2 and p[1] == ":" or p.startswith("\\\\"):
                    result[name] = p
                    break
            else:
                result.setdefault(name, "")
        return result

    # ------------------------------------------------------------ 增删
    def add(self, name, path, readonly=True, remark="文件共享中心"):
        if not os.path.isdir(path):
            raise SmbError("目录不存在：%s" % path)
        perm = "READ" if readonly else "FULL"
        # 先删同名的（可能是上次没退干净，或用户改了路径/权限）
        if self.exists(name):
            self.remove(name)
        rc, out = self._run([
            "net", "share", "%s=%s" % (name, path),
            "/grant:Everyone,%s" % perm,
            "/remark:%s" % remark,
            "/cache:none",
        ])
        if rc != 0:
            raise SmbError(_clean(out) or "创建共享失败（返回码 %d）" % rc)
        self.managed.add(name)

    def remove(self, name):
        rc, out = self._run(["net", "share", name, "/delete"])
        if rc != 0:
            rc2, out2 = run_hidden(
                ["cmd", "/c", "echo Y| net share %s /delete" % name], timeout=20)
            if rc2 != 0:
                raise SmbError(_clean(out2 or out) or "删除共享失败")
        self.managed.discard(name)

    # ------------------------------------------------------------ NTFS 授权
    def grant_ntfs(self, path, readonly=True, account=None):
        """给共享目录授权，让局域网账户能读到文件。

        ``account=None`` 时授权给 Everyone（用户显式选了「无密码访问」）；
        否则只授给专用共享账户 —— 共享权限的 Everyone 是粗粒度开关，
        真正的细粒度拦截放在这里，比全目录放开安全得多。
        """
        if not os.path.isdir(path):
            return False, "目录不存在"
        who = account or "Everyone"
        grant = "(OI)(CI)(RX)" if readonly else "(OI)(CI)(M)"
        principal = SID_EVERYONE if not account else account
        rc, out = self._run(["icacls", path, "/grant",
                             "%s:%s" % (principal, grant)])
        if rc != 0:
            return False, _clean(out) or "icacls 返回 %d" % rc
        return True, "已授权 %s %s" % (who, "只读" if readonly else "读写")

    def _acl_grants(self, path):
        """目录 ACL 里被授权的身份集合（统一小写便于比较）。

        ``icacls`` 的输出形如 ``E:\\Share BUILTIN\\Users:(I)(RX)`` ——
        第一行路径和账户挤在一起，必须先按「传进去的路径」把前缀剥掉，
        否则会截出一个假的账户名。比较时统一成正斜杠，免得分隔符差异
        （icacls 自己也可能因参数写法输出 / 或 \\）导致剥前缀失败。
        """
        rc, out = self._run(["icacls", path])
        who = set()
        if rc != 0:
            return who

        def norm(s):
            return s.replace("\\", "/").lower()

        prefix = norm(os.path.normpath(path))
        for line in out.replace("\r", "").split("\n"):
            text = norm(line).strip()
            # 路径可能含空格（C:/Users/a b/Temp/x），icacls 输出里路径和
            # 账户之间没有固定分隔符，所以按「行首是路径」剥
            if text.startswith(prefix):
                text = text[len(prefix):]
            # 权限标记都在括号里，账户名紧贴在 "(" 前面；
            # [^()] 保证只吃到最近一个右括号之后，SID 里的冒号不受影响
            for m in re.finditer(r"([^()]+?):\(", text):
                token = m.group(1).strip()
                token = token.lstrip("*").strip()
                if token:
                    who.add(token)
        return who

    def _ntfs_allows(self, path, account=None):
        """``account``（或 Everyone）能否读这个目录。"""
        who = self._acl_grants(path)
        # SID 形式的 Everyone 解析后是 s-1-1-0（前缀的 * 已在解析时剥掉）
        if "s-1-1-0" in who or "everyone" in who \
                or "*s-1-1-0" in who:
            return True
        if account:
            low = account.lower()
            if low in who:
                return True
            host = (os.environ.get("COMPUTERNAME") or "").lower()
            if host and ("%s\\%s" % (host, low)) in who:
                return True
        # 本机 Users 组覆盖不到网络来宾，必须显式授权才算
        return False

    # ------------------------------------------------------------ 账户
    def ensure_account(self, password=SHARE_PASS):
        """建/修专用共享账户，返回 ``(ok, 说明, 账户名)``。

        幂等：已存在就把密码重置回已知值（用户改过密码也能连上）、
        确保启用、密码永不过期。
        """
        rc, out = self._run(["net", "user", SHARE_USER])
        created = rc != 0
        if created:
            rc, out = self._run([
                "net", "user", SHARE_USER, password, "/add",
                "/comment:文件共享中心 · 局域网共享账户",
                "/fullname:FileShare LAN",
            ])
            if rc != 0:
                return False, "创建账户失败：%s" % (_clean(out) or rc), ""
            msg = "已创建共享账户 %s" % SHARE_USER
        else:
            # 已存在：重置密码（幂等），并确保启用
            self._run(["net", "user", SHARE_USER, password])
            msg = "共享账户 %s 已存在，密码已重置" % SHARE_USER

        self._run(["net", "user", SHARE_USER, "/active:yes"])
        # 密码永不过期：否则几个月后所有客户端突然连不上
        self._ps("Set-LocalUser -Name '%s' -PasswordNeverExpires $true "
                 "-ErrorAction SilentlyContinue" % SHARE_USER)
        self.account = SHARE_USER
        return True, msg, SHARE_USER

    def account_state(self):
        """当前可用于局域网登录的账户 → ``(账户名, 说明)``；没有则 ``("", 原因)``。"""
        rc, out = self._run(["net", "user", SHARE_USER])
        if rc == 0:
            if self._user_enabled(SHARE_USER):
                return SHARE_USER, "共享账户 %s 可用" % SHARE_USER
            return "", "共享账户 %s 被禁用" % SHARE_USER
        rc, out = self._run(["net", "user", "Guest"])
        if rc == 0 and self._user_enabled("Guest"):
            return "Guest", "使用 Guest 来宾账户（无密码）"
        return "", "没有可用的登录账户"

    def _user_enabled(self, name):
        rc, out = self._run(["net", "user", name])
        if rc != 0:
            return False
        for line in out.splitlines():
            low = line.lower()
            # 中文「帐户启用  Yes/No」、英文「Account active  Yes/No」
            if "启用" in line or "account active" in low:
                tail = line.split()[-1] if line.split() else ""
                return tail.lower() not in ("no", "否")
        return True

    # ------------------------------------------------------------ 防火墙
    def firewall_open(self):
        """放行 445 入站（所有网络配置）。"""
        rc, out = self._run([
            "netsh", "advfirewall", "firewall", "add", "rule",
            "name=%s" % FW_RULE, "dir=in", "action=allow",
            "protocol=TCP", "localport=445", "profile=any",
        ], timeout=30)
        if rc != 0:
            return False, "放行 445 失败：%s" % (_clean(out) or rc)
        # 域/专用/公用三个配置都覆盖（profile=any 在部分系统只作用于当前配置）
        self._run([
            "netsh", "advfirewall", "firewall", "set", "rule",
            "name=%s" % FW_RULE, "new", "profile=any", "enable=yes",
        ], timeout=30)
        opened, _why = self.firewall_state()
        return opened, ("已放行 TCP 445 入站" if opened else "规则已加但未生效")

    def firewall_state(self):
        """445 入站是否放行 → ``(open, 说明)``。

        只认「入站 + 允许 + 已启用 + 覆盖当前网络配置」的规则；系统自带的
        「文件和打印机共享(SMB-In)」和本程序建的规则都算。

        三个踩过的坑，决定了这里为什么写得这么绕：
        * ``Get-NetFirewallPortFilter -Protocol TCP`` 在部分系统上直接返回空，
          过滤参数形同失效；
        * 反过来「枚举端口过滤 → 拿 InstanceID 查规则」会漏掉规则
          （本机那条启用的 SMB-In 就被漏了），而且慢到 12 秒；
        * 规则「存在」不等于「生效」：SMB-In 默认只在「专用网络」启用，
          当前网卡是「公用」时等于没放行，必须比对当前网络配置。

        所以走「先按名字筛规则（便宜），再逐条取端口过滤（贵但只几条）」。
        """
        ps = r"""
$cur = @((Get-NetConnectionProfile).NetworkCategory | ForEach-Object { [string]$_ })
$hit = @()
foreach ($r in Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True -ErrorAction SilentlyContinue) {
    if ($r.Name -match 'SMB' -or $r.DisplayName -match 'SMB') {
        $pf = $r | Get-NetFirewallPortFilter -ErrorAction SilentlyContinue
        $port = "$($pf.LocalPort)"
        if ($port -eq '445' -or $port -eq 'Any') {
            $p = [string]$r.Profile
            $cov = $false
            if ($p -match 'Any') { $cov = $true }
            else { foreach ($c in $cur) { if ($p -match $c) { $cov = $true } } }
            if ($cov) { $hit += $r.DisplayName }
        }
    }
}
if ($hit.Count -gt 0) { 'OPEN:' + (($hit | Sort-Object -Unique) -join ';') } else { 'CLOSED' }
"""
        rc, out = self._ps(ps, timeout=45)
        text = (out or "").strip()
        if rc == 0 and "OPEN:" in text:
            names = text.split("OPEN:", 1)[1].strip()
            return True, "445 已对当前网络放行"
        if rc == 0 and "CLOSED" in text:
            return False, "445 入站没有覆盖当前网络的放行规则"
        # PowerShell 不可用（被策略禁）才退回 netsh 粗查
        rc, out = self._run(["netsh", "advfirewall", "firewall", "show", "rule",
                             "name=%s" % FW_RULE], timeout=30)
        if rc == 0 and "445" in out and ("允许" in out or "Allow" in out):
            return True, "本程序已放行 445"
        return False, "445 入站未放行（其它机器会被防火墙拦住）"

    # ------------------------------------------------------------ 诊断
    def diagnose(self, shares=None):
        """体检「别人能不能连进来」，返回问题清单。

        每项 ``{key, level, title, detail, fixable}``：``error`` = 一定连不上，
        ``warn`` = 部分网络/部分客户端会失败。
        """
        issues = []
        if not sys.platform.startswith("win"):
            return [{"key": "os", "level": "error", "title": "仅支持 Windows",
                     "detail": "", "fixable": False}]

        # 1) 系统共享服务（sc 可能被安全软件拦截，netstat 才是硬证据）
        rc, out = self._run(["sc", "query", "LanmanServer"])
        service_up = (rc == 0 and "RUNNING" in out.upper())

        # 2) 445 是否监听
        rc2, nout = self._run(["netstat", "-ano", "-p", "TCP"], timeout=25)
        listening = False
        if rc2 == 0:
            listening = any(":445" in ln and "LISTENING" in ln.upper()
                            for ln in nout.splitlines())
        # 只认「端口没监听」这一条硬结论；sc 查不到不代表服务停了
        # （有些安全软件会拦 sc，或者系统版本输出格式不同）
        if rc2 == 0 and not listening:
            issues.append({
                "key": "service", "level": "error",
                "title": "445 端口没有监听，系统共享服务没起来",
                "detail": "共享服务没绑定端口。可在「服务」里把 Server"
                          "（LanmanServer）启动，或重启电脑再试。",
                "fixable": False,
            })

        # 3) 防火墙
        opened, why = self.firewall_state()
        if not opened:
            issues.append({
                "key": "firewall", "level": "error",
                "title": "防火墙没放行 TCP 445 入站",
                "detail": "%s。网络类别是「公用」时其它机器一定连不上。" % why,
                "fixable": True,
            })

        # 4) 登录账户
        account, why = self.account_state()
        if not account:
            issues.append({
                "key": "account", "level": "error",
                "title": "没有任何可用的网络登录账户",
                "detail": "%s。别的机器连过来会直接报「无法访问」，"
                          "连用户名密码框都不弹。" % why,
                "fixable": True,
            })

        # 5) 目录 NTFS 权限（真问题：连上了但读不到文件）
        bad_dirs = []
        for s in (shares if shares is not None else self._managed_shares()):
            path = s.get("path") if isinstance(s, dict) else s
            if not path or not os.path.isdir(path):
                continue
            if not self._ntfs_allows(path, account or None):
                bad_dirs.append(path)
        if bad_dirs:
            issues.append({
                "key": "ntfs", "level": "error",
                "title": "共享目录的 NTFS 权限没放给访问者",
                "detail": "共享权限给了 Everyone，但文件系统 ACL 只认本机账户，"
                          "对方连上后打开文件会报「拒绝访问」。涉及 %d 个目录。"
                          % len(bad_dirs),
                "fixable": True,
            })

        # 6) 网络类别（公用网络会拦发现和入站）
        rc, out = self._ps("(Get-NetConnectionProfile).NetworkCategory")
        if rc == 0 and "Public" in out:
            issues.append({
                "key": "profile", "level": "warn",
                "title": "有网卡处于「公用网络」",
                "detail": "公用网络下系统默认拦截共享，建议在「网络和共享中心」"
                          "改成专用网络。",
                "fixable": False,
            })

        # 7) 客户端只读到共享列表的情况：检查 Guest 空密码策略组合
        if account == "Guest":
            issues.append({
                "key": "guest", "level": "warn",
                "title": "当前靠 Guest 来宾账户访问",
                "detail": "很多系统的组策略会强制禁用来宾登录，不稳定；"
                          "建议改用专用共享账户。",
                "fixable": True,
            })
        return issues

    def _managed_shares(self):
        """本程序建的共享 → ``[{name, path}]``。"""
        all_shares = self.list_all()
        return [{"name": n, "path": all_shares.get(n, "")}
                for n in self.managed if all_shares.get(n)]

    # ------------------------------------------------------------ 一键修复
    def repair(self, shares=None, readonly=True, password=SHARE_PASS):
        """按诊断结果修复，返回 ``(done, failed)``。"""
        ok, msg = self.available()
        if not ok:
            return [], [msg]
        done, failed = [], []

        def step(name, fn):
            try:
                res = fn()
            except Exception as exc:                        # noqa: BLE001
                failed.append("%s：%s" % (name, exc))
                return
            if isinstance(res, tuple):
                good, why = res[0], (res[1] if len(res) > 1 else "")
            else:
                good, why = bool(res), ""
            (done if good else failed).append(
                "%s（%s）" % (name, why) if why else name)

        step("创建/修复共享账户", lambda: self.ensure_account(password)[:2])
        step("放行 TCP 445", self.firewall_open)

        targets = list(shares if shares is not None else self._managed_shares())
        for s in targets:
            path = s.get("path") if isinstance(s, dict) else s
            if not path or not os.path.isdir(path):
                continue
            label = "授权 %s" % os.path.basename(path.rstrip("\\/"))
            step(label, lambda p=path: self.grant_ntfs(
                p, readonly=readonly, account=SHARE_USER))

        if self.log is not None:
            for d in done:
                self.log.add("ok", "SMB 修复：" + d, "SMB")
            for f in failed:
                self.log.add("err", "SMB 修复失败：" + f, "SMB")
        return done, failed

    # ------------------------------------------------------------ 同步
    def sync(self, shares, readonly=True):
        """让系统共享与配置一致：多删少补。返回 (done, errors)。"""
        ok, msg = self.available()
        if not ok:
            return [], [("%s" % msg)]
        want = {}
        for s in shares:
            name = (s.get("name") or "").strip()
            if name and os.path.isdir(s.get("path") or ""):
                want[name] = s["path"]
        errors = []
        done = []
        for name in list(self.managed):
            if name not in want:
                try:
                    self.remove(name)
                    done.append(("remove", name))
                except SmbError as exc:
                    errors.append("%s：%s" % (name, exc))
        for name, path in want.items():
            try:
                self.add(name, path, readonly=readonly)
                done.append(("add", name))
            except SmbError as exc:
                errors.append("%s：%s" % (name, exc))
        if self.log is not None:
            for kind, name in done:
                self.log.add("ok", "SMB 共享%s：%s" % (
                    "已创建" if kind == "add" else "已移除", name), "SMB")
            for err in errors:
                self.log.add("err", "SMB 失败：%s" % err, "SMB")
        return done, errors

    def remove_all(self):
        """退出时清掉本程序建的共享（账户留着，下次还要用）。"""
        for name in list(self.managed):
            try:
                self.remove(name)
            except SmbError:
                pass


def _clean(text):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.startswith("命令成功完成")]
    return " ".join(lines)[:300]

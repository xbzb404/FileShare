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
    def __init__(self, log=None, cfg=None):
        self.log = log
        self.cfg = cfg
        self.managed = set()          # 本程序创建的共享名，退出时只删这些
        self.account = ""             # 上次修复用的共享账户名（空 = 未配置）
        # 本程序给目录加过的授权 → [[路径, 身份]]，取消共享时按它还原。
        # 必须落盘：程序重启后还能把上次加的权限撤掉。
        self.granted = []
        self._last_paths = []         # 本次进程里处理过的共享目录
        if cfg is not None:
            try:
                self.granted = [list(x) for x in
                                (cfg.get("smb", "granted") or []) if len(x) == 2]
            except Exception:                               # noqa: BLE001
                self.granted = []

    def _save_granted(self):
        if self.cfg is None:
            return
        try:
            self.cfg.set([list(x) for x in self.granted], "smb", "granted")
            self.cfg.save()
        except Exception:                                   # noqa: BLE001
            pass

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
        # 非管理员查共享会以「系统错误 5」结尾（返回码非 0），但共享名和路径
        # 其实已经打印出来了；所以不能只看返回码，否则会误判成「不存在」
        # 进而去重建，又把正在用共享的客户端踢掉。
        return self.info(name) is not None

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
    def info(self, name):
        """``net share <name>`` 的关键字段 → ``{path, perm, remark}``。

        非管理员查自己的共享也只能拿到前几行（后面报系统错误 5），
        所以只要解析出路径就算有效，权限行缺了就当「未知」。
        """
        rc, out = self._run(["net", "share", name])
        data = {}
        for line in (out or "").splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            key, val = parts[0], parts[1].strip()
            if key in ("路径", "Path"):
                data["path"] = val
            elif key in ("权限", "Permissions"):
                data["perm"] = val
            elif key in ("备注", "注解", "Remark"):
                data["remark"] = val
        return data or None

    def add(self, name, path, readonly=True, remark="文件共享中心"):
        if not os.path.isdir(path):
            raise SmbError("目录不存在：%s" % path)
        perm = "READ" if readonly else "FULL"

        # 已经是我们要的样子就什么都不做。
        # 以前是无条件「删了重建」：只要哪个客户端还开着共享里的文件，
        # ``net share /delete`` 就会问「继续会强制关闭文件，是否继续」，
        # 无头进程读不到回答 → 报「没有提供有效的响应」，共享反而建不起来，
        # 正在传输的连接也会被踢断。重复 sync（开服务、点修复、切页）
        # 本来就不该动一个健康的共享。
        cur = self.info(name)
        if cur and _same_path(cur.get("path"), path):
            perm_now = (cur.get("perm") or "").upper()
            if not perm_now or perm in perm_now:
                self.managed.add(name)
                return False                     # False = 没改动

        # 路径或权限真的变了才重建（``cur`` 就是刚查到的那份，别重复查）
        if cur:
            self.remove(name, force=False)
        rc, out = self._run([
            "net", "share", "%s=%s" % (name, path),
            "/grant:Everyone,%s" % perm,
            "/remark:%s" % remark,
            "/cache:none",
        ])
        if rc != 0:
            raise SmbError(_clean(out) or "创建共享失败（返回码 %d）" % rc)
        self.managed.add(name)
        return True

    def remove(self, name, force=True):
        """删共享。``force=True`` 时加 ``/y``：不弹「是否强制关闭文件」的确认。

        共享里有客户端开着文件时，``net share /delete`` 默认会问一句
        「继续这项操作会强制关闭文件，你想继续吗 (Y/N) [N]」，无头进程
        答不上来就失败。加 ``/y`` 直接按「继续」处理。
        """
        args = ["net", "share", name, "/delete"]
        if force:
            args.append("/y")
        rc, out = self._run(args)
        if rc != 0:
            rc2, out2 = run_hidden(
                ["cmd", "/c", "echo Y| net share %s /delete" % name], timeout=20)
            if rc2 != 0:
                raise SmbError(_clean(out2 or out) or "删除共享失败")
        self.managed.discard(name)

    # ------------------------------------------------------------ NTFS 授权
    def grant_ntfs(self, path, readonly=True, account=None):
        """给共享目录授权，让局域网账户能读到文件。

        ``account=None`` → 授权给 Everyone（免密模式）；
        否则只授给专用共享账户 —— 共享权限的 Everyone 是粗粒度开关，
        真正的细粒度拦截放在这里，比全目录放开安全得多。

        已经能访问就不重复加 ACE（否则取消共享时无法区分「本来就有」
        还是「我们加的」，还原权限会误删用户自己的授权）。
        """
        if not os.path.isdir(path):
            return False, "目录不存在"
        if path not in self._last_paths:
            self._last_paths.append(path)
        if self._ntfs_allows(path, account):
            return True, "已有权限，无需改动"
        who = account or "Everyone"
        grant = "(OI)(CI)(RX)" if readonly else "(OI)(CI)(M)"
        principal = SID_EVERYONE if not account else account
        rc, out = self._run(["icacls", path, "/grant",
                             "%s:%s" % (principal, grant)])
        if rc != 0:
            return False, _clean(out) or "icacls 返回 %d" % rc
        self._remember(path, who)
        return True, "已授权 %s %s" % (who, "只读" if readonly else "读写")

    def _remember(self, path, principal):
        item = [os.path.normpath(path), principal]
        if item not in self.granted:
            self.granted.append(item)
            self._save_granted()

    def revoke_ntfs(self, path, principal):
        """撤掉本程序加的某条 ACE（还原权限）。"""
        if not os.path.isdir(path):
            return False, "目录不存在"
        target = SID_EVERYONE if str(principal).lower() in ("everyone",
                                                            "s-1-1-0") \
            else principal
        rc, out = self._run(["icacls", path, "/remove:g", target])
        return rc == 0, _clean(out) or ""

    def has_explicit_grant(self, path, principal):
        """目录里是否有**显式**（非继承）授给 ``principal`` 的 ACE。

        用来判断「这条权限是不是本程序加的」：icacls 里继承来的权限带
        ``(I)`` 标记，我们手动 ``/grant`` 加的不带。取消共享时只撤显式
        的那些，不会把用户自己从上层目录继承的权限删掉。
        """
        rc, out = self._run(["icacls", path])
        if rc != 0:
            return False
        target = str(principal).lower()
        host = (os.environ.get("COMPUTERNAME") or "").lower()

        def norm(s):
            return s.replace("\\", "/").lower()

        prefix = norm(os.path.normpath(path))
        for line in out.replace("\r", "").split("\n"):
            text = norm(line).strip()
            if text.startswith(prefix):
                text = text[len(prefix):]
            for m in re.finditer(r"([^()]+?):\(([^)]*)\)", text):
                who = m.group(1).strip().lstrip("*")
                flags = m.group(2)
                if "i" == flags.lower() or "(i)" in flags.lower():
                    continue            # 继承来的，不是我们加的
                if who == target or who == "%s/%s" % (host, target) \
                        or (target in ("everyone", "s-1-1-0")
                            and who in ("everyone", "s-1-1-0")):
                    return True
        return False

    def revoke_all(self, principals=None, paths=None):
        """取消共享时把本程序加过的目录权限全部还原。

        ``principals`` 给定时（按当前模式算出 Everyone / 账户名），
        即使记录丢了也能尝试撤销显式 ACE —— 只要确认是「显式」而非继承，
        就不会误删用户自己的权限。``paths`` 是本次共享的目录（调用方给），
        这样连「从没点过修复、但上次运行授权过」的老权限也能还原。
        """
        done, failed = [], []
        todo = list(self.granted)
        # 兜底：记录里没有、但确实存在显式 ACE 的也补上（老版本授权过、
        # 或程序重装后 granted 为空的情况）
        if principals:
            scan = list(paths or []) + list(self._last_paths)
            seen = set()
            for path in scan:
                if not path or not os.path.isdir(path):
                    continue
                key = os.path.normpath(path).lower()
                if key in seen:
                    continue
                seen.add(key)
                for pr in principals:
                    if self.has_explicit_grant(path, pr) \
                            and [os.path.normpath(path), pr] not in todo:
                        todo.append([os.path.normpath(path), pr])

        for path, principal in todo:
            ok, why = self.revoke_ntfs(path, principal)
            name = "%s（%s）" % (os.path.basename(path.rstrip("\\/")) or path,
                                 principal)
            if ok:
                if [path, principal] in self.granted:
                    self.granted.remove([path, principal])
                done.append(name)
            else:
                failed.append("%s%s" % (name, "：" + why if why else ""))
        if done:
            self._save_granted()
        if self.log is not None:
            for d in done:
                self.log.add("ok", "SMB 权限已还原：%s" % d, "SMB")
            for f in failed:
                self.log.add("warn", "SMB 权限还原失败：%s" % f, "SMB")
        return done, failed

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
        who = self._acl_grants(path)          # 已统一成小写 + 正斜杠
        # SID 形式的 Everyone 解析后是 s-1-1-0（前缀的 * 已在解析时剥掉）
        if "s-1-1-0" in who or "everyone" in who or "*s-1-1-0" in who:
            return True
        if account:
            low = account.lower()
            if low in who:
                return True
            # icacls 会把本地账户输出成 "主机名\账户名"，而 who 里的分隔符
            # 已统一成正斜杠 —— 拼的时候也必须用正斜杠，否则永远不相等
            # （踩过：授权明明成功，界面却一直报「NTFS 没放给访问者」）
            host = (os.environ.get("COMPUTERNAME") or "").lower()
            if host and ("%s/%s" % (host, low)) in who:
                return True
        # 本机 Users 组覆盖不到网络来宾，必须显式授权才算
        return False

    # ------------------------------------------------------------ 账户
    def ensure_account(self, user=SHARE_USER, password=SHARE_PASS):
        """建/修专用共享账户，返回 ``(ok, 说明, 账户名)``。

        幂等：已存在就把密码重置成用户设定的值、确保启用、密码永不过期。
        用户名/密码都由界面传入，默认值只是兜底。
        """
        user = (user or SHARE_USER).strip()
        password = password or SHARE_PASS
        rc, out = self._run(["net", "user", user])
        created = rc != 0
        if created:
            rc, out = self._run([
                "net", "user", user, password, "/add",
                "/comment:文件共享中心 · 局域网共享账户",
                "/fullname:FileShare LAN",
            ])
            if rc != 0:
                # 最常见：密码不满足本机密码策略（长度/复杂度）
                return False, "创建账户失败：%s" % (
                    _clean(out) or rc), ""
            msg = "已创建共享账户 %s" % user
        else:
            # 已存在：把密码同步成界面上的值（幂等）
            rc2, out2 = self._run(["net", "user", user, password])
            if rc2 != 0:
                return False, "重置密码失败：%s" % (
                    _clean(out2) or rc2), ""
            msg = "共享账户 %s 已存在，密码已更新" % user

        self._run(["net", "user", user, "/active:yes"])
        # 密码永不过期：否则几个月后所有客户端突然连不上
        self._ps("Set-LocalUser -Name '%s' -PasswordNeverExpires $true "
                 "-ErrorAction SilentlyContinue" % user)
        self.account = user
        return True, msg, user

    # ------------------------------------------------------------ 免密模式
    def guest_state(self):
        """免密访问所需条件 → ``(ok, 缺什么)``。

        免密 = 别的机器不带凭据直连。Windows 要走通这条路，光给目录
        授权不够，还得让「空凭据」能登录成一个真实会话：
        * ``Guest`` 账户启用 —— 匿名连接会被映射到它；
        * ``LimitBlankPasswordUse=0`` —— 否则 Guest 的空密码在网络登录时被拒。
        """
        rc, out = self._run(["net", "user", "Guest"])
        if rc != 0:
            return False, "系统里没有 Guest 账户"
        enabled = False
        for line in out.splitlines():
            if "启用" in line or "account active" in line.lower():
                tail = (line.split() or [""])[-1].lower()
                enabled = tail not in ("no", "否")
        if not enabled:
            return False, "Guest 来宾账户被禁用"
        rc, out = self._ps(
            "(Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Lsa')"
            ".LimitBlankPasswordUse", timeout=20)
        if rc == 0 and out.strip() == "1":
            return False, "系统禁止空密码网络登录（LimitBlankPasswordUse=1）"
        return True, "免密访问条件已就绪"

    def enable_guest_access(self):
        """打开免密访问所需的系统开关（幂等）。"""
        rc, out = self._run(["net", "user", "Guest", "/active:yes"])
        if rc != 0:
            return False, "启用 Guest 失败：%s" % (_clean(out) or rc)
        # 允许空密码账户从网络登录（Guest 就是空密码）
        self._ps("Set-ItemProperty -Path "
                 "'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Lsa' "
                 "-Name LimitBlankPasswordUse -Value 0 -Type DWord", timeout=25)
        # 让匿名登录的身份包含 Everyone，否则 NTFS 的 Everyone 授权
        # 对匿名会话不生效，对方连上仍然读不到文件
        self._ps("Set-ItemProperty -Path "
                 "'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Lsa' "
                 "-Name EveryoneIncludesAnonymous -Value 1 -Type DWord",
                 timeout=25)
        ok, why = self.guest_state()
        return ok, ("已开启免密访问" if ok else "免密访问未完全生效：%s" % why)

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
    def diagnose(self, shares=None, mode="account"):
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

        # 4) 登录方式（免密模式看 Guest，账号模式看专用账户）
        if mode == "everyone":
            guest_ok, why = self.guest_state()
            if not guest_ok:
                issues.append({
                    "key": "guest", "level": "error",
                    "title": "免密（Everyone）访问条件不满足",
                    "detail": "%s。别的机器不带账号密码连过来会直接被拒。" % why,
                    "fixable": True,
                })
            account = None
        else:
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
            if not self._ntfs_allows(path, account):
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

        # 7) 免密模式的客户端兼容性提醒
        if mode == "everyone":
            issues.append({
                "key": "guest-client", "level": "warn",
                "title": "免密访问对内网设备最方便，但部分 Windows 客户端会拦",
                "detail": "Win10/11 默认拒绝「不安全的来宾登录」，对方可能提示"
                          "「安全策略阻止未经身份验证的来宾访问」。遇到时改用"
                          "账号模式（关闭「免密只读」后设账号密码）。",
                "fixable": False,
            })
        return issues

    def _managed_shares(self):
        """本程序建的共享 → ``[{name, path}]``。"""
        all_shares = self.list_all()
        return [{"name": n, "path": all_shares.get(n, "")}
                for n in self.managed if all_shares.get(n)]

    # ------------------------------------------------------------ 一键修复
    def repair(self, shares=None, readonly=True, mode="account",
               user=SHARE_USER, password=SHARE_PASS):
        """按诊断结果修复，返回 ``(done, failed)``。

        两种模式：
        * ``everyone``（免密只读）：开 Guest + 允许空密码网络登录 +
          目录授权 Everyone；
        * ``account``：建专用账户（用户名密码由界面给）+ 只授权该账户。
        """
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

        if mode == "everyone":
            step("开启免密访问（Guest）", self.enable_guest_access)
            principal = None
        else:
            step("创建/修复共享账户",
                 lambda: self.ensure_account(user, password)[:2])
            principal = (user or SHARE_USER).strip()

        step("放行 TCP 445", self.firewall_open)

        targets = list(shares if shares is not None else self._managed_shares())
        for s in targets:
            path = s.get("path") if isinstance(s, dict) else s
            if not path or not os.path.isdir(path):
                continue
            label = "授权 %s" % os.path.basename(path.rstrip("\\/"))
            step(label, lambda p=path: self.grant_ntfs(
                p, readonly=readonly, account=principal))

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
                changed = self.add(name, path, readonly=readonly)
                if changed:
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

    def remove_all(self, restore=True, principals=None, paths=None):
        """取消共享：删掉本程序建的共享，并把加过的目录权限还原。

        ``restore=True`` 时会把 ``grant_ntfs`` 记录过的 ACE 逐条撤掉——
        共享都取消了还留着 Everyone/共享账户的读写权限，等于给整机开了
        一个长期后门。账户和防火墙规则保留（前者没有目录权限就进不来，
        后者下次开共享还要用，反复改防火墙反而容易出错）。

        ``principals`` / ``paths`` 由调用方按当前模式与共享列表给出，
        让「记录丢了但上次确实授权过」的场景也能还原。
        """
        for name in list(self.managed):
            try:
                self.remove(name)
            except SmbError:
                pass
        if restore:
            return self.revoke_all(principals, paths)
        return [], []


def _norm_path(path):
    """路径归一化：分隔符、大小写、结尾斜杠都不该影响比较。"""
    return os.path.normpath(path or "").replace("\\", "/").rstrip("/").lower()


def _same_path(a, b):
    return bool(a) and bool(b) and _norm_path(a) == _norm_path(b)


# 交互确认留下的残渣：这些句子不该出现在界面上
_PROMPT_NOISE = (
    "(Y/N)", "Y/N", "没有提供有效的响应", "No valid response",
    "你想继续此操作吗", "Do you want to continue this operation",
)


def _clean(text):
    raw = text or ""
    low = raw.lower()
    if "强制关闭" in raw or "force the files closed" in low:
        # 共享正被客户端使用：这是「稍后再试」，不是配置错误
        return "共享正在被其它设备使用（有文件没关闭），本次操作已跳过"
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    lines = [ln for ln in lines
             if not ln.startswith("命令成功完成")
             and not ln.startswith("The command completed successfully")
             and not any(h in ln for h in _PROMPT_NOISE)]
    return " ".join(lines)[:300]


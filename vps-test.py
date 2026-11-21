#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vps-test.py — VPS 验货工具 v1.0.0（只做“测”，不做“调”）

公开项目，给国内买 VPS 的人用。一键测出这台 VPS 的真实带宽、
有没有限速器、限速器在哪、链路干不干净，最后给一句人话结论。

用法（root 身份，在 VPS 上跑）：
    python3 vps-test.py                 # 全自动：识别运营商 -> 选测速点 -> 拐点扫描 -> 报告
    python3 vps-test.py --isp 1         # 跳过识别，直接指定运营商（1 电信 / 2 移动 / 3 联通 / 4 广电）
    python3 vps-test.py --peer <地址>   # 手动指定 iperf3 测速点
    python3 vps-test.py --yes           # 跳过流量确认（无人值守用）

依赖：Python 3.7+ 标准库、iperf3（只当客户端）、tc（iproute2）。
本脚本不改任何系统配置，只在扫描期间临时挂测试整形器，跑完立即恢复原状。
"""

import argparse
import datetime
import ipaddress
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

VERSION = "1.0.0"

# 丢包率阈值（%）：超过即判定限速器介入。
# 依据：七组真实数据回归，干净链路最高 0.0017%，撞限速器最低 1.35%，
# 0.1% 距两侧分别有 59 倍和 13.5 倍余量。
LOSS_THRESHOLD = 0.1
# 每个速率点测试时长（秒）
DEFAULT_DUR = 12
# 两档之间的静置时间（秒），让上一条流的状态排空
GAP = 3
# 扫描前静置时间（秒）
PRE_SCAN_GAP = 15
# 扫描流量超过这个数（GB）时要用户再确认一次
TRAFFIC_CONFIRM_GB = 50
# iperf3 每个点最多重试次数
MAX_RETRIES = 3
# 跳变确认：3 次里 >= 2 次跳变才确认
SPIKE_HITS_NEEDED = 2
# MSS（字节），用于把重传数换算成丢包率
MSS = 1448

# 默认测速点：沿用 tcpfit 的公共 iperf3 节点池（亚太部分）+ iperf3serverlist.net 的中国电信节点。
# 来源：tcpfit PEER_POOL（Kylin010/tcpfit）与 https://iperf3serverlist.net/api/servers.json。
# 5 个中国电信内地节点（成都/福州/南京/宁波/郑州）2026-11-22 实测：5201 端口全通，30 天在线率 100%。
# 三元组：(主机, 名称, 运营商标签)。标签为 None 表示通用节点（任何运营商都能用）。
PEERS = [
    ("116.169.118.91", "成都 电信", "电信"),
    ("59.56.22.40", "福州 电信", "电信"),
    ("114.230.93.99", "南京 电信", "电信"),
    ("122.228.230.122", "宁波 电信", "电信"),
    ("123.53.44.221", "郑州 电信", "电信"),
    ("speedtest.lax12.us.leaseweb.net", "洛杉矶 Leaseweb", None),
    ("speedtest.hkg12.hk.leaseweb.net", "香港 Leaseweb", None),
    ("speedtest.sin1.sg.leaseweb.net", "新加坡 Leaseweb", None),
    ("sgp.proof.ovh.net", "新加坡 OVH", None),
    ("speedtest.syd12.au.leaseweb.net", "悉尼 Leaseweb", None),
    ("speedtest.tyo11.jp.leaseweb.net", "东京 Leaseweb", None),
]
IPERF_PORTS = [5201, 5202, 5203, 5204, 5205]

# 运营商：编号 -> （名称，识别关键词）
ISPS = {
    1: ("电信", ("telecom", "chinanet")),
    2: ("移动", ("mobile", "cmnet")),
    3: ("联通", ("unicom", "cunc", "cu-net")),
    4: ("广电", ("broadcast", "broadnet", "cbn")),
}
# 数据中心 / 云厂商 / VPN 关键词：命中即判定为梯子（先排除居民宽带运营商）。
DATACENTER_KEYWORDS = (
    "alibaba", "tencent", "huawei", "amazon", "aws", "google", "microsoft",
    "azure", "digitalocean", "vultr", "linode", "ovh", "hetzner",
    "leaseweb", "cloudflare", "datacenter", "data center", "hosting",
    "vps", "dedicated", "colocation", "idc",
    "peg tech", "raksmart",
)

IP_API_URL = "http://ip-api.com/json/{ip}?fields=status,message,country,regionName,city,isp,org,as,query"
IP_API_TIMEOUT = 10

HISTORY_DIR = os.path.expanduser("~/.vps-test")
HISTORY_FILE = os.path.join(HISTORY_DIR, "history.json")
HISTORY_KEEP = 20


# ---------------------------------------------------------------- 工具函数

def _use_color():
    """终端才上色；重定向/管道时保持纯文本（不污染日志）。"""
    return sys.stdout.isatty()


def red(text):
    return f"\033[31m{text}\033[0m" if _use_color() else text


def log(msg=""):
    print(msg)


def warn(msg):
    # 警告一律红色（用户定规矩：警告、错误、不可逆操作都是红色）。
    print(red(f"[!] {msg}"))


def ok(msg):
    print(f"[+] {msg}")


def info(msg):
    print(f"[*] {msg}")


def human_bytes(n):
    """字节数 -> 人话，如 12.75 GB / 34 MB。"""
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.0f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def run_cmd(argv, timeout=None):
    """跑一条命令（列表参数，绝不经过 shell）。返回 (returncode, stdout, stderr)。"""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=timeout, errors="replace",
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -1, "", "command not found"
    except OSError as exc:
        return -1, "", str(exc)


def confirm(prompt, default=False):
    """问用户 yes/no。无终端时按默认值（默认否）。"""
    if not sys.stdin.isatty():
        return default
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        ans = input(prompt + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not ans:
        return default
    return ans in ("y", "yes")


def require_root():
    if os.geteuid() != 0:
        warn("需要 root 身份运行（扫描期间要临时挂测试整形器）。")
        sys.exit(1)


def check_deps():
    missing = []
    for cmd in ("iperf3", "tc", "ip"):
        rc, _, _ = run_cmd(["which", cmd])
        if rc != 0:
            missing.append(cmd)
    if missing:
        warn("缺少依赖：" + "、".join(missing))
        log("  安装：apt install -y iperf3 iproute2   或   yum install -y iperf3 iproute")
        log("  注意：iperf3 只当客户端，装服务端时选 No。")
        sys.exit(1)


# ---------------------------------------------------------------- 运营商识别

def get_ssh_client_ip():
    """从 SSH 连接环境变量取登录者的源 IP。返回 ip 字符串或 None。"""
    for var in ("SSH_CLIENT", "SSH_CONNECTION"):
        val = os.environ.get(var, "")
        if not val:
            continue
        first = val.split()[0]
        try:
            ipaddress.ip_address(first)
            return first
        except ValueError:
            continue
    return None


def lookup_ip(ip):
    """查 IP 的运营商/归属（免费 ip-api）。失败返回 None。"""
    url = IP_API_URL.format(ip=ip)
    req = urllib.request.Request(url, headers={"User-Agent": "vps-test/1.0.0"})
    try:
        with urllib.request.urlopen(req, timeout=IP_API_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("status") != "success":
        return None
    return {
        "ip": data.get("query", ip),
        "isp": data.get("isp", ""),
        "org": data.get("org", ""),
        "as": data.get("as", ""),
        "country": data.get("country", ""),
        "region": data.get("regionName", ""),
        "city": data.get("city", ""),
    }


def classify_isp(info):
    """按 ASN/运营商归属分类。返回 (isp_no 或 None, is_datacenter)。"""
    text = " ".join([
        str(info.get("isp", "")),
        str(info.get("org", "")),
        str(info.get("as", "")),
    ]).lower()
    # 先认居民宽带运营商（顺序重要：广电全称含 "network"，不能先判机房词）。
    for no, (_name, keywords) in ISPS.items():
        if any(kw in text for kw in keywords):
            return no, False
    # 再看是不是数据中心 / 云厂商 / VPN。
    if any(kw in text for kw in DATACENTER_KEYWORDS):
        return None, True
    return None, False


def manual_isp_menu(detected_no=None):
    """手动选运营商。返回 1-4；选 0 直接退出（5/6/7 留给以后加功能）。"""
    log()
    log("请选择你的宽带运营商：")
    for no in sorted(ISPS):
        log(f"  {no}. {ISPS[no][0]}")
    log("  0. 退出")
    while True:
        try:
            ans = input("请选择 [0-4]：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            warn("已取消。")
            sys.exit(1)
        if ans == "0":
            log("已退出。")
            sys.exit(0)
        if ans in ("1", "2", "3", "4"):
            no = int(ans)
            if detected_no is not None and no != detected_no:
                warn(f"你选的是{ISPS[no][0]}，但检测到的是{ISPS[detected_no][0]}，"
                     "请确认没选错。")
                if not confirm("确定就用" + ISPS[no][0] + "？", default=False):
                    continue
            return no
        log("请输入 0-4。")


def detect_isp(cli_isp=None):
    """识别用户运营商。返回 (isp_no, detected_info 或 None)。"""
    if cli_isp is not None:
        return cli_isp, None

    ip = get_ssh_client_ip()
    if ip is None:
        warn("没检测到 SSH 连接（不是走 SSH 登录的？），请手动选择运营商。")
        return manual_isp_menu(), None

    info = lookup_ip(ip)
    if info is None:
        warn("运营商查询接口没通，请手动选择运营商。")
        return manual_isp_menu(), None

    isp_no, is_dc = classify_isp(info)
    addr = "".join(x for x in (info.get("country", ""), info.get("region", ""),
                               info.get("city", "")) if x)
    log()
    log("检测到你的网络：")
    log(f"  IP：      {info['ip']}")
    log(f"  运营商：  {ISPS[isp_no][0] if isp_no else info.get('isp') or info.get('org') or '未知'}")
    log(f"  地址：    {addr or '未知'}（仅供参考，大内网下地址可能不准确）")
    if is_dc:
        log(red("  类型：    数据中心/云厂商 IP——你现在可能挂着梯子"))

    if is_dc:
        # 梯子/机房 IP：不是用户真实的本地运营商，拿它调优没有意义。
        warn("这个 IP 属于数据中心/云厂商，你现在可能挂着梯子——")
        warn("它不是你真实的本地运营商，按它选测速点没有意义。")
        log("请手动选择你真实的宽带运营商：")
        return manual_isp_menu(), info

    if isp_no is None:
        warn("没认出这个 IP 属于哪家运营商，请手动选择：")
        return manual_isp_menu(), info

    # 居民宽带：自动选中，等用户回车确认，错了按 1-4 改。
    log()
    log(f"确认无误请按回车，选错了按 1-4 重新选（检测到你是{ISPS[isp_no][0]}）：")
    try:
        ans = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return isp_no, info
    if ans in ("1", "2", "3", "4"):
        no = int(ans)
        if no != isp_no:
            warn(f"你选的是{ISPS[no][0]}，但检测到的是{ISPS[isp_no][0]}，请确认没选错。")
            if not confirm("确定就用" + ISPS[no][0] + "？", default=False):
                return manual_isp_menu(detected_no=isp_no), info
        return no, info
    return isp_no, info


# ---------------------------------------------------------------- 测速点选择

def ping_avg(host, count=3, version=None):
    """ping 取平均延迟（毫秒）。version: "4" / "6" / None（默认）。失败返回 None。"""
    cmd = ["ping"]
    if version == "4":
        cmd.append("-4")
    elif version == "6":
        cmd.append("-6")
    cmd += ["-c", str(count), "-W", "2", host]
    rc, out, _ = run_cmd(cmd, timeout=15)
    if rc != 0:
        return None
    m = re.search(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/", out)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def detect_ip_stack():
    """检测本机 IP 栈。返回 (has_v4, has_v6, v4_addrs, v6_addrs)。

    只认全局单播地址（scope global），回环/链路本地不算。
    """
    v4_addrs, v6_addrs = [], []
    for fam, addrs in (("4", v4_addrs), ("6", v6_addrs)):
        rc, out, _ = run_cmd(["ip", "-j", f"-{fam}", "addr",
                              "show", "scope", "global"])
        if rc == 0:
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, list):
                for iface in data:
                    if not isinstance(iface, dict):
                        continue
                    for a in iface.get("addr_info", []):
                        if isinstance(a, dict) and a.get("local"):
                            addrs.append(a["local"])
                continue
        # JSON 不可用时退回纯文本解析
        rc, out, _ = run_cmd(["ip", f"-{fam}", "addr", "show", "scope", "global"])
        if rc != 0:
            continue
        for line in out.splitlines():
            m = re.search(r"inet6?\s+(\S+)/", line)
            if m:
                addrs.append(m.group(1))
    return bool(v4_addrs), bool(v6_addrs), v4_addrs, v6_addrs


def stack_label(has_v4, has_v6):
    """IP 栈人话：双栈 / 仅 IPv4 / 仅 IPv6 / 未检测到。"""
    if has_v4 and has_v6:
        return "双栈（IPv4 + IPv6）"
    if has_v4:
        return "仅 IPv4"
    if has_v6:
        return "仅 IPv6"
    return "未检测到公网 IP"


def resolve_peer_versions(host):
    """测速点支持的 IP 版本。返回 {"4", "6"} 的子集；IP 直给则返回其版本。"""
    try:
        ip = ipaddress.ip_address(host)
        return {"4"} if isinstance(ip, ipaddress.IPv4Address) else {"6"}
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        return set()
    versions = set()
    for fam, *_ in infos:
        if fam == socket.AF_INET:
            versions.add("4")
        elif fam == socket.AF_INET6:
            versions.add("6")
    return versions


def pick_peer(cli_peer=None, stack=(True, True), isp_name=None):
    """选测速点：先 ping 与用户运营商匹配的国内节点（测到中国的真实路径），
    没有可用的再 ping 通用节点（洛杉矶/香港/新加坡/悉尼/东京），各取延迟低的。

    返回 (host, label, version)，version 为 "4" 或 "6"。
    """
    has_v4, has_v6 = stack
    versions = []
    if has_v4:
        versions.append("4")
    if has_v6:
        versions.append("6")
    if not versions:
        # 检测不到 IP 栈：默认 IPv4，让 iperf3 自己决定。
        versions = ["4"]
    if cli_peer:
        groups = [[(cli_peer, "手动指定", None)]]
    elif isp_name:
        # 先 ping 与用户运营商匹配的国内节点，整组都不可用时再 ping 通用节点。
        groups = [
            [p for p in PEERS if p[2] == isp_name],
            [p for p in PEERS if p[2] is None],
        ]
    else:
        groups = [PEERS]
    best = None
    for group in groups:
        for host, label, _isp in group:
            peer_versions = resolve_peer_versions(host)
            use = [v for v in versions if v in peer_versions] or versions
            for ver in use:
                rtt = ping_avg(host, version=ver)
                if rtt is not None:
                    vlabel = "IPv4" if ver == "4" else "IPv6"
                    log(f"  测速点 {label}（{host}）[{vlabel}] 延迟 {rtt:.1f} ms")
                    if best is None or rtt < best[0]:
                        best = (rtt, host, label, ver)
        if best is not None:
            break
    if best is None:
        warn("测速点都 ping 不通，默认用香港 Leaseweb。")
        default = next(p for p in PEERS if p[0] == "speedtest.hkg12.hk.leaseweb.net")
        return default[0], default[1] + "（默认）", versions[0]
    return best[1], f"{best[2]}（延迟 {best[0]:.1f} ms）", best[3]


def read_sysctl(name):
    """读 /proc/sys 下的 sysctl 值。失败返回 None。"""
    path = "/proc/sys/" + name.replace(".", "/")
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def read_baseline(iface):
    """读未调优的系统参数（只读，供对照）。返回 dict。"""
    base = {}
    try:
        base["kernel"] = os.uname().release
    except OSError:
        base["kernel"] = None
    base["cc"] = read_sysctl("net.ipv4.tcp_congestion_control")
    base["rmem_max"] = read_sysctl("net.core.rmem_max")
    base["wmem_max"] = read_sysctl("net.core.wmem_max")
    base["tcp_rmem"] = read_sysctl("net.ipv4.tcp_rmem")
    base["tcp_wmem"] = read_sysctl("net.ipv4.tcp_wmem")
    base["qdisc"] = qdisc_root_kind(iface)
    base["htb_rate"] = None
    if base["qdisc"] == "htb":
        htb = QdiscGuard(iface)._snapshot_htb()
        if htb:
            base["htb_rate"] = htb.get("rate")
    rc, out, _ = run_cmd(["ip", "-j", "link", "show", "dev", iface])
    base["mtu"] = None
    if rc == 0:
        try:
            data = json.loads(out)
            if isinstance(data, list) and data and isinstance(data[0], dict):
                base["mtu"] = data[0].get("mtu")
        except (json.JSONDecodeError, ValueError):
            pass
    return base


def baseline_lines(base):
    """未调优参数的人话行，带达标/待优化标记。缺失的显示未知。
    缓冲 16MB 是千兆级链路的经验门槛（BDP 约 12MB@100ms），仅供参考。"""
    def v(key):
        val = base.get(key)
        return str(val) if val is not None else "未知"
    lines = [
        f"  内核：{v('kernel')}",
    ]
    cc = (base.get("cc") or "").lower()
    cc_mark = "（达标）" if cc.startswith("bbr") else ("（待优化，建议 BBR）" if cc else "")
    lines.append(f"  拥塞控制：{v('cc')}{cc_mark}")
    qdisc = v("qdisc")
    if base.get("htb_rate"):
        qdisc += f" {base['htb_rate']}（已有整形）"
    elif base.get("qdisc") in ("fq", "fq_codel"):
        qdisc += "（达标）"
    elif base.get("qdisc") in ("pfifo_fast", "noqueue"):
        qdisc += "（待优化，建议 fq）"
    lines.append(f"  根 qdisc：{qdisc}")
    for key, label in (("rmem_max", "接收缓冲上限"), ("wmem_max", "发送缓冲上限")):
        val = base.get(key)
        mark = ""
        if val is not None:
            try:
                mark = "（达标）" if int(val) >= 16777216 else "（待优化）"
            except ValueError:
                pass
        lines.append(f"  {label}：{v(key)}{mark}")
    lines.append(f"  TCP 读缓冲：{v('tcp_rmem')}")
    lines.append(f"  TCP 写缓冲：{v('tcp_wmem')}")
    lines.append(f"  MTU：{v('mtu')}")
    return lines


# ---------------------------------------------------------------- 网卡与 qdisc

def detect_iface():
    """找默认路由网卡。"""
    rc, out, _ = run_cmd(["ip", "-j", "route", "show", "default"])
    if rc == 0:
        try:
            routes = json.loads(out)
            for r in routes:
                if isinstance(r, dict) and r.get("dev"):
                    return r["dev"]
        except (json.JSONDecodeError, ValueError):
            pass
    rc, out, _ = run_cmd(["ip", "route", "show", "default"])
    if rc == 0:
        m = re.search(r"\bdev\s+(\S+)", out)
        if m:
            return m.group(1)
    return None


def tc(*args):
    """跑一条 tc 命令。返回 (rc, stdout)。"""
    return run_cmd(["tc"] + list(args))


def qdisc_root_kind(iface):
    """读根 qdisc 种类，如 fq / fq_codel / htb / mq / noqueue。"""
    rc, out, _ = tc("qdisc", "show", "dev", iface)
    if rc != 0:
        return ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "qdisc" and "root" in parts:
            # 只认本网卡的行（防御：输出里混进 lo/docker0 时不认错）。
            if len(parts) >= 5 and parts[3] == "dev" and parts[4] != iface:
                continue
            return parts[1]
    return ""


def _strip_unit(val):
    """去 tc 显示的单位后缀（473000b -> 473000，40960p -> 40960）；
    速率类（946Mbit）原样保留。"""
    if val.endswith(("b", "p")) and val[:-1].isdigit():
        return val[:-1]
    return val


class QdiscGuard:
    """扫描期间临时挂测试整形器，结束（包括被中断）时恢复原状。

    注意：只记 qdisc 种类不够。htb 这类带配置的 qdisc（速率、class、
    叶子）光重加一个裸 htb 会丢配置，而且裸 htb 没有 class 会断网。
    所以进场时把 htb 的关键参数也抓下来，恢复时原样重建；抓不到就
    退回 fq（不限速但不断网），并大声警告。
    """

    # 光重加种类就完整的无配置 qdisc。
    CLASSLESS = {
        "fq", "fq_codel", "sfq", "pfifo", "bfifo", "pfifo_fast",
        "noqueue", "mq", "fq_pie", "codel", "pie", "red", "choke",
        "drr", "qfq",
    }
    # 带配置的 qdisc：htb 尝试原样恢复，其余退回 fq + 警告。
    CLASSFUL = {"htb", "tbf", "hfsc", "netem", "cbq"}

    def __init__(self, iface):
        self.iface = iface
        self.saved_kind = ""
        self.saved_htb = None
        self._old_handlers = {}

    def __enter__(self):
        self.saved_kind = qdisc_root_kind(self.iface)
        if self.saved_kind == "htb":
            self.saved_htb = self._snapshot_htb()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                old = signal.getsignal(sig)
                self._old_handlers[sig] = old
                if sig == signal.SIGHUP and old == signal.SIG_IGN:
                    # nohup 下已忽略 SIGHUP：保持忽略，让扫描在后台跑完。
                    continue
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError):
                pass
        return self

    def _snapshot_htb(self):
        """抓 htb 的关键参数。只认 tcpfit 式布局（根 handle 1:、default 0x10、
        单 class 1:10、叶子挂在 1:10 下），对不上就返回 None。

        注意：rate/ceil/burst/cburst 各自独立抓（tc 显示字段顺序不固定，
        中间可能插 quantum 等字段，绑在一起的正则会抓不到）。
        """
        rc, out, _ = tc("class", "show", "dev", self.iface)
        if rc != 0:
            return None
        cls = None
        for line in out.splitlines():
            m = re.search(r"class htb (\S+) parent 1:", line)
            if not m:
                continue
            fields = {}
            for key in ("rate", "ceil", "burst", "cburst"):
                fm = re.search(r"\b" + key + r" (\S+)", line)
                if fm:
                    fields[key] = fm.group(1)
            if len(fields) < 4:
                continue
            cls = {
                "classid": m.group(1),
                "rate": fields["rate"],
                "ceil": fields["ceil"],
                "burst": _strip_unit(fields["burst"]),
                "cburst": _strip_unit(fields["cburst"]),
            }
            break
        if not cls or cls["classid"] != "1:10":
            return None
        rc, out, _ = tc("qdisc", "show", "dev", self.iface)
        if rc != 0:
            return None
        root_ok = False
        leaf = None
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[0] != "qdisc":
                continue
            # 只看本网卡的行（qdisc show 可能带出 lo/docker0）。
            if len(parts) < 5 or parts[3] != "dev" or parts[4] != self.iface:
                continue
            if "root" in parts:
                # qdisc htb 1: dev ens3 root ... default 0x10 ...
                if parts[1] != "htb":
                    return None
                if "default" in parts:
                    di = parts.index("default")
                    if di + 1 < len(parts) and parts[di + 1] == "0x10":
                        root_ok = True
                continue
            if len(parts) >= 7 and parts[5] == "parent" and parts[6] == "1:10":
                # 挂在 1:10 下的叶子 qdisc
                leaf = {"kind": parts[1]}
                m = re.search(r"\bmaxrate (\S+)", line)
                if m:
                    leaf["maxrate"] = m.group(1)
                m = re.search(r"\blimit (\S+)", line)
                if m:
                    leaf["limit"] = _strip_unit(m.group(1))
                m = re.search(r"\bflow_limit (\S+)", line)
                if m:
                    leaf["flow_limit"] = _strip_unit(m.group(1))
        if not root_ok or not leaf:
            return None
        cls["leaf"] = leaf
        return cls

    def _restore_htb(self):
        """按快照原样重建 htb（根 + class 1:10 + 叶子）。"""
        h = self.saved_htb
        tc("qdisc", "add", "dev", self.iface, "root",
           "handle", "1:", "htb", "default", "10")
        tc("class", "add", "dev", self.iface, "parent", "1:",
           "classid", "1:10", "htb",
           "rate", h["rate"], "ceil", h["ceil"],
           "burst", h["burst"], "cburst", h["cburst"], "quantum", "1514")
        leaf = h["leaf"]
        if leaf["kind"] == "fq":
            args = ["qdisc", "add", "dev", self.iface, "parent", "1:10",
                    "handle", "10:", "fq"]
            if leaf.get("limit"):
                args += ["limit", leaf["limit"]]
            if leaf.get("flow_limit"):
                args += ["flow_limit", leaf["flow_limit"]]
            if leaf.get("maxrate"):
                args += ["maxrate", leaf["maxrate"]]
            tc(*args)
        elif leaf["kind"] in self.CLASSLESS:
            tc("qdisc", "add", "dev", self.iface, "parent", "1:10",
               "handle", "10:", leaf["kind"])

    def _htb_restored_ok(self):
        """校验 htb 是否按快照恢复：根是 htb，且 class 1:10 存在。"""
        if qdisc_root_kind(self.iface) != "htb":
            return False
        rc, out, _ = tc("class", "show", "dev", self.iface)
        if rc != 0:
            return False
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "class" and parts[2] == "1:10":
                return True
        return False

    def _on_signal(self, signum, _frame):
        self.restore()
        try:
            # SIGHUP 时 stdout 可能已死（SSH 断了）：写不出去就跳过。
            print()
            warn("已中断，qdisc 已恢复。")
        except (BrokenPipeError, OSError):
            pass
        sys.exit(130)

    def __exit__(self, *_exc):
        self.restore()
        for sig, handler in self._old_handlers.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        return False

    def restore(self):
        """删掉测试整形器，恢复原来的根 qdisc。"""
        tc("qdisc", "del", "dev", self.iface, "root")
        kind = self.saved_kind
        if not kind or kind in ("noqueue", "pfifo_fast"):
            return
        if kind == "mq":
            # 多队列网卡：删根后内核一般会自动重建 mq；没重建才手动补。
            if qdisc_root_kind(self.iface) != "mq":
                tc("qdisc", "add", "dev", self.iface, "root", "mq")
            return
        if kind == "htb":
            if self.saved_htb:
                self._restore_htb()
                if self._htb_restored_ok():
                    return
                warn("htb 恢复校验没通过，已恢复为 fq（不限速）——"
                     "请重新下发原来的整形。")
            else:
                # 抓不到原配置：绝不能留裸 htb（没 class 会断网），
                # 退回 fq（不限速但不断网），让用户重新下发原整形。
                warn("原 htb 整形配置无法解析，已恢复为 fq（不限速）——"
                     "请重新下发原来的整形。")
            tc("qdisc", "del", "dev", self.iface, "root")
            tc("qdisc", "add", "dev", self.iface, "root", "fq")
            return
        if kind in self.CLASSFUL:
            # 其他带配置的 qdisc（tbf/hfsc/netem…）：同样不留裸配置，
            # 退回 fq + 警告。
            warn(f"原 {kind} 整形配置无法完整恢复，已恢复为 fq（不限速）——"
                 "请重新下发原来的整形。")
            tc("qdisc", "add", "dev", self.iface, "root", "fq")
            return
        tc("qdisc", "add", "dev", self.iface, "root", kind)

    def set_fq(self):
        """不限速探测用：根 qdisc 换成 fq（mq 网卡不动，叶子默认就是 fq）。"""
        kind = qdisc_root_kind(self.iface)
        if kind in ("fq", "mq"):
            return True
        tc("qdisc", "del", "dev", self.iface, "root")
        kind = qdisc_root_kind(self.iface)
        if kind == "mq":
            return True
        rc, _, _ = tc("qdisc", "add", "dev", self.iface, "root", "fq")
        return rc == 0

    def apply_rate(self, rate_mbit):
        """在网卡上挂一个 rate_mbit 的测试整形器。失败返回 False。"""
        burst = max(32768, rate_mbit * 500)
        tc("qdisc", "del", "dev", self.iface, "root")
        rc, _, _ = tc("qdisc", "add", "dev", self.iface, "root",
                      "handle", "1:", "htb", "default", "10")
        if rc != 0:
            return False
        rc, _, _ = tc("class", "add", "dev", self.iface, "parent", "1:",
                      "classid", "1:10", "htb",
                      "rate", f"{rate_mbit}mbit", "ceil", f"{rate_mbit}mbit",
                      "burst", str(burst), "cburst", str(burst), "quantum", "1514")
        if rc != 0:
            return False
        rc, _, _ = tc("qdisc", "add", "dev", self.iface, "parent", "1:10",
                      "handle", "10:", "fq", "limit", "40960",
                      "flow_limit", "8192", "maxrate", f"{rate_mbit}mbit")
        return rc == 0


# ---------------------------------------------------------------- iperf3

def run_iperf(peer, dur, version="4", streams=1):
    """跑一次 iperf3（客户端，JSON 输出）。返回 dict 或 None。

    version: "4" / "6"，传给 iperf3 的 -4 / -6。
    返回：{"sent_mbps": .., "retrans": .., "recv_mbps": ..}，失败返回 None。
    端口 5201 不通/被占时依次试 5202-5205。
    """
    for port in IPERF_PORTS:
        rc, out, _ = run_cmd(
            ["iperf3", f"-{version}", "-c", peer, "-p", str(port), "-t", str(dur),
             "-P", str(streams), "-J"],
            timeout=dur + 30,
        )
        if rc != 0 or not out.strip():
            continue
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            continue
        end = data.get("end")
        if not isinstance(end, dict):
            continue
        sent = end.get("sum_sent") or {}
        recv = end.get("sum_received") or {}
        try:
            sent_bps = float(sent.get("bits_per_second", 0) or 0)
            retrans = int(sent.get("retransmits", 0) or 0)
            recv_bps = float(recv.get("bits_per_second", 0) or 0)
        except (TypeError, ValueError):
            continue
        if sent_bps <= 0:
            continue
        if recv_bps <= 0:
            recv_bps = sent_bps
        return {
            "sent_mbps": sent_bps / 1e6,
            "retrans": retrans,
            "recv_mbps": recv_bps / 1e6,
        }
    return None


def loss_pct(retrans, sent_mbps, dur):
    """丢包率(%) = 重传数 / 发出的包数，包数按 1448 字节 MSS 估算。

    不用绝对重传数：阈值 100 在 300M 机上相当于 0.032% 丢包，
    在 7.4G 机上只有 0.0014% —— 严了 25 倍。用比率才适配任何带宽。
    """
    packets = sent_mbps * 1e6 * dur / 8 / MSS
    if packets < 1:
        packets = 1
    return retrans * 100.0 / packets


def calc_margin(knee_mbit):
    """整形安全余量（Mbit），按带宽分档：固定值在小带宽上浪费太大。"""
    if knee_mbit <= 30:
        return 1
    if knee_mbit <= 60:
        return 2
    if knee_mbit <= 100:
        return 5
    if knee_mbit <= 300:
        return 10
    if knee_mbit <= 600:
        return 15
    if knee_mbit <= 1000:
        return 25
    return 40


def parse_rate_mbit(rate_str):
    """'946Mbit' / '946M' / '946000Kbit' / '1Gbit' -> Mbit 整数。失败返回 None。"""
    if not rate_str:
        return None
    m = re.match(r"([\d.]+)\s*([KMG]?)(?:bit)?$", str(rate_str).strip(), re.IGNORECASE)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2).upper()
    if unit == "K":
        return int(round(val / 1000))
    if unit == "G":
        return int(round(val * 1000))
    return int(round(val))


def shaping_status(current_mbit, knee, recommend):
    """整形达标判断。返回 (status, text)。
    status: "ok"（已达标）/ "high"（太高会丢包）/ "low"（太低浪费带宽）/ "none"（没整形）。"""
    if current_mbit is None:
        return "none", f"建议整形 {recommend} Mbit"
    if current_mbit > knee:
        return "high", (f"当前整形 {current_mbit} Mbit 高于实测上限 {knee} Mbit，会丢包；"
                        f"建议降到 {recommend} Mbit")
    if current_mbit < recommend:
        return "low", (f"当前整形 {current_mbit} Mbit 低于建议值 {recommend} Mbit，"
                       f"可放宽到 {recommend} Mbit")
    return "ok", f"当前整形 {current_mbit} Mbit 已达标，无需调整"


def estimate_scan_traffic_gb(lo, hi, step, dur, fine_points=8):
    """估算一次扫描的流量（GB）：粗扫各档 + 细扫上限 fine_points 档。"""
    points = (hi - lo) // step + 2 + fine_points
    avg_rate = (lo + hi) / 2.0
    return points * avg_rate * dur / 8 / 1024


# ---------------------------------------------------------------- 拐点扫描

class Sweep:
    """拐点扫描：不限速探测 -> 估算流量 -> 粗扫 -> 细扫 -> 给出拐点。"""

    def __init__(self, peer, iface, dur=DEFAULT_DUR, assume_yes=False, version="4",
                 quick=False):
        self.peer = peer
        self.iface = iface
        self.dur = dur
        self.assume_yes = assume_yes
        self.version = version
        self.quick = quick  # 快速模式：少测几档、不细扫，流量省约 2/3，拐点较粗
        self.base_loss = None      # 干净链路的本底丢包率（%）
        self.last_ok = None        # 最后一个干净档位（Mbit）
        self.broke_at = None       # 第一个确认丢包的档位（Mbit）
        self.spike_min_loss = None
        self.slow_hits = 0
        self.peer_too_slow = False
        self.slow_at = None
        self.verified = 0          # 拿到有效样本的档数
        self.incomplete = False
        self.rows = []             # 扫描记录：(速率, 送达, 重传, 丢包%, 结论)
        self.tx0 = self.rx0 = 0

    # -- 流量记账（读网卡计数器，和 tcpfit 的 traffic_report 一个办法）
    def _traffic_mark(self):
        def read(which):
            try:
                with open(f"/sys/class/net/{self.iface}/statistics/{which}_bytes") as f:
                    return int(f.read().strip())
            except (OSError, ValueError):
                return 0
        self.tx0 = read("tx")
        self.rx0 = read("rx")

    def traffic_used(self):
        def read(which):
            try:
                with open(f"/sys/class/net/{self.iface}/statistics/{which}_bytes") as f:
                    return int(f.read().strip())
            except (OSError, ValueError):
                return 0
        tx = max(0, read("tx") - self.tx0)
        rx = max(0, read("rx") - self.rx0)
        return tx, rx

    # -- 判定
    def _is_spike(self, loss):
        """丢包是否跳变：既要超绝对阈值，也要明显高于本底（5 倍本底，封顶 1%）。"""
        need = LOSS_THRESHOLD
        if self.base_loss and self.base_loss * 5 > need:
            need = self.base_loss * 5
        if need > 1.0:
            need = 1.0
        return loss > need

    def _measure(self, streams=1):
        """跑一次 iperf3，最多重试 3 次。返回结果 dict 或 None。"""
        for _ in range(MAX_RETRIES):
            res = run_iperf(self.peer, self.dur, self.version, streams)
            if res is not None:
                return res
            time.sleep(8)
        return None

    def _print_row(self, rate, goodput, retrans, loss, verdict):
        """实时打印一行扫描结果（别让用户对着表头干等）。"""
        gp = f"{goodput:.1f}" if goodput is not None else "-"
        rt = str(retrans) if retrans is not None else "-"
        lp = f"{loss:.4f}" if loss is not None else "-"
        log("  %-10s %12s %9s %8s  %s" % (rate, gp, rt, lp, verdict))

    def _probe_point(self, rate):
        """在指定速率档测一次（含跳变复测）。
        返回 (goodput, retrans, loss, verdict)，测不到返回 (None, None, None, verdict)。
        """
        res = self._measure()
        if res is None:
            self.incomplete = True
            return None, None, None, "peer busy, skipped"
        goodput = res["recv_mbps"]
        retrans = res["retrans"]
        loss = loss_pct(retrans, res["sent_mbps"], self.dur)
        self.verified += 1

        # 干净样本才能建立本底。
        if self.base_loss is None and loss <= LOSS_THRESHOLD:
            self.base_loss = loss

        if self._is_spike(loss):
            # 单次跳变可能只是公共节点被别人占用：3 次里 >= 2 次才确认。
            hits = 1
            min_loss = loss
            clean = None
            for _ in range(2):
                time.sleep(GAP)
                r2 = self._measure()
                if r2 is None:
                    continue
                g2 = r2["recv_mbps"]
                l2 = loss_pct(r2["retrans"], r2["sent_mbps"], self.dur)
                self._print_row(f"{rate} (复测)", g2, r2["retrans"], l2, "recheck")
                self.rows.append((rate, g2, r2["retrans"], l2, "recheck"))
                min_loss = min(min_loss, l2)
                if self._is_spike(l2):
                    hits += 1
                elif clean is None:
                    clean = (g2, r2["retrans"], l2)
                    if self.base_loss is None:
                        self.base_loss = l2
            if hits >= SPIKE_HITS_NEEDED:
                self.spike_min_loss = min_loss
                return goodput, retrans, loss, f"loss spike ({hits}/3)"
            if clean is not None:
                return clean[0], clean[1], clean[2], "transient (1/3), clean recheck used"
            time.sleep(GAP)
            return None, None, None, "transient (1/3), no clean recheck"

        # 吞吐远低于限速值、重传却很低 = 整形器没被触发，瓶颈在对端。
        if goodput < rate * 0.7 and loss <= LOSS_THRESHOLD:
            self.slow_hits += 1
            if self.slow_hits >= 3:
                self.peer_too_slow = True
                self.slow_at = rate
            return goodput, retrans, loss, "only %d%% of target" % int(goodput * 100 / rate)

        self.slow_hits = 0
        return goodput, retrans, loss, "ok"

    def _scan_range(self, guard, lo, hi, step):
        """扫一个区间。返回 True 表示跑完，False 表示中途遇到对端太慢。"""
        points = list(range(lo, hi + 1, step))
        if points[-1] != hi:
            points.append(hi)
        prev_gp = 0.0
        for rate in points:
            if not guard.apply_rate(rate):
                warn(f"下发测试整形失败（{rate} Mbit），跳过这一档。")
                self.incomplete = True
                continue
            goodput, retrans, loss, verdict = self._probe_point(rate)
            if goodput is None:
                if verdict == "transient (1/3), no clean recheck":
                    continue
                self.rows.append((rate, None, None, None, verdict))
                self._print_row(rate, None, None, None, verdict)
                continue
            if "loss spike" in verdict:
                self.rows.append((rate, goodput, retrans, loss, verdict))
                self._print_row(rate, goodput, retrans, loss, verdict)
                self.broke_at = rate
                return True
            if "only " in verdict and verdict.endswith("of target"):
                self.rows.append((rate, goodput, retrans, loss, verdict))
                self._print_row(rate, goodput, retrans, loss, verdict)
                if self.peer_too_slow:
                    return False
                self.last_ok = rate
                prev_gp = goodput
                time.sleep(GAP)
                continue
            if prev_gp > 0 and goodput < prev_gp * 1.01:
                verdict = "no further gain"
            self.rows.append((rate, goodput, retrans, loss, verdict))
            self._print_row(rate, goodput, retrans, loss, verdict)
            self.last_ok = rate
            prev_gp = goodput
            time.sleep(GAP)
        return True

    def run(self):
        """执行完整扫描。返回结果 dict。"""
        self._traffic_mark()
        with QdiscGuard(self.iface) as guard:
            # -- 不限速探测：丢包低 = 没东西在打你 = 不用扫；丢包高 = 有限速器。
            log()
            info(f"Peer {self.peer}")
            info(f"不限速探测（{self.dur} 秒，单流）")
            log("  %-10s %12s %9s %8s  %s" % ("速率/Mbit", "送达/Mbps", "重传", "丢包%", "结论"))
            if not guard.set_fq():
                warn("切到 fq 失败，探测取消。")
                return {"status": "error", "error": "qdisc 设置失败"}
            res = self._measure()
            if res is None:
                return {"status": "error", "error": "不限速探测失败，对端可能太忙，换个时间或换个测速点再试"}
            unshaped_gp = res["recv_mbps"]
            unshaped_loss = loss_pct(res["retrans"], res["sent_mbps"], self.dur)
            verdict = "ok" if unshaped_loss <= LOSS_THRESHOLD else "loss -- policer present"
            log("  %-10s %12.1f %9d %8.4f  %s" % (
                "不限速", unshaped_gp, res["retrans"], unshaped_loss, verdict))
            result = {
                "status": "done",
                "unshaped_mbps": round(unshaped_gp, 1),
                "unshaped_loss": round(unshaped_loss, 4),
                "knee": None,
                "recommend": None,
                "rows": self.rows,
                "quick": self.quick,
            }
            if unshaped_loss <= LOSS_THRESHOLD:
                log()
                ok(f"不限速送达 {unshaped_gp:.1f} Mbps，丢包 {unshaped_loss:.4f}%，未检测到限速器。")
                result["status"] = "no_knee"
                return result

            # -- 拐点在真实送达量之上：区间从它稍下方起，往上扫。
            recv = unshaped_gp
            k = 1.25 + unshaped_loss / 100 * 2
            if k > 2.5:
                k = 2.5
            lo = max(1, int(recv * 0.95))
            hi = min(10000, int(recv * k))
            if hi <= lo:
                hi = lo + 2
            # 快速模式：粗步长（5 档）、不细扫，拐点精度约一个步长。
            npoints = 5 if self.quick else 10
            step = max(1, round((hi - lo) / npoints))

            # -- 扫之前先报流量账（教训：大带宽机器一次能扫掉上百 GB）。
            est_gb = estimate_scan_traffic_gb(lo, hi, step, self.dur,
                                              fine_points=0 if self.quick else 8)
            log()
            warn(f"有限速器，准备扫描 {lo} -> {hi} Mbit。")
            log(f"    按这个区间估算，本次扫描可能消耗约 {est_gb:.1f} GB 流量。")
            if not self.assume_yes:
                if not sys.stdin.isatty():
                    # 无终端时默认取消，宁可不扫也不能无人值守地烧流量。
                    log("    没有终端时默认取消；无人值守请加 --yes。")
                    log("已取消扫描，没有下发任何测试整形。")
                    result["status"] = "cancelled"
                    return result
                if est_gb > TRAFFIC_CONFIRM_GB:
                    go = confirm("    按这个估算继续扫描？", default=False)
                else:
                    go = confirm("    继续扫描？", default=True)
                if not go:
                    log("已取消扫描，没有下发任何测试整形。")
                    result["status"] = "cancelled"
                    return result

            log()
            info(f"扫描 {lo} -> {hi} Mbit，步长 {step}，每档 {self.dur} 秒，"
                 f"丢包阈值 > {LOSS_THRESHOLD}%")
            log("  %-10s %12s %9s %8s  %s" % ("速率/Mbit", "送达/Mbps", "重传", "丢包%", "结论"))
            info(f"静置 {PRE_SCAN_GAP} 秒再开始第一档")
            time.sleep(PRE_SCAN_GAP)
            self._scan_range(guard, lo, hi, step)

            # 首档就丢包：起点可能已越过一个很浅的拐点，也可能只是路径底噪。
            # 向下 25% 测控制点来区分，最多三次。
            attempts = 0
            while (self.last_ok is None and self.broke_at is not None
                   and attempts < 3):
                attempts += 1
                known_broke = self.broke_at
                known_loss = self.spike_min_loss
                control = max(1, known_broke * 3 // 4)
                if control >= known_broke:
                    break
                info(f"第一档就丢包，测 {control} Mbit 做低速对照")
                self.last_ok = None
                self.broke_at = None
                self.base_loss = None
                self.spike_min_loss = None
                self._scan_range(guard, control, control, 1)
                if self.last_ok is not None:
                    self.broke_at = known_broke
                    break
                control_loss = self.spike_min_loss
                if (control_loss is not None and known_loss is not None
                        and control_loss <= 0.5 and known_loss <= 0.5
                        and abs(control_loss - known_loss) <= 0.1):
                    self.base_loss = min(control_loss, known_loss)
                    self.last_ok = control
                    self.broke_at = None
                    info(f"确认为线路底噪（{self.base_loss:.4f}%），"
                         "从原区间继续扫")
                    self._scan_range(guard, known_broke, hi, step)
                    break
                if control_loss is None:
                    break
                known_broke = control
                known_loss = control_loss

            if self.peer_too_slow:
                log()
                warn(f"扫到 {self.slow_at} Mbit 时连续达不到目标速率。")
                log("  可能的原因：1) 这条线没有限速器，到顶了；"
                    "2) CPU 推不动这个速率；3) 对端不够快。")
                result["status"] = "inconclusive"
                result["error"] = "对端太慢或本机到顶，判不出限速器"
                return result

            # 粗扫只定位到拐点在 LAST_OK 与 BROKE_AT 之间，用 1/4 步长细扫。
            # 快速模式不细扫，拐点就是粗扫的 last_ok（精度约一个粗步长）。
            if (not self.quick and self.last_ok is not None
                    and self.broke_at is not None
                    and self.broke_at - self.last_ok > 1):
                coarse_broke = self.broke_at
                width = coarse_broke - self.last_ok
                fine = max(1, step // 4)
                fine = max(fine, (width + 7) // 8)  # 细扫压到 8 档以内
                log()
                info(f"拐点在 {self.last_ok} 与 {coarse_broke} 之间，"
                     f"用步长 {fine} 细扫")
                log("  %-10s %12s %9s %8s  %s" % ("速率/Mbit", "送达/Mbps", "重传", "丢包%", "结论"))
                self.broke_at = None
                self._scan_range(guard, self.last_ok + fine,
                                 coarse_broke - fine, fine)
                if self.broke_at is None:
                    # 细扫都没触发阈值，拐点就在粗扫那一档，保留粗扫结论。
                    self.broke_at = coarse_broke

            log()
            if self.last_ok is None:
                warn("没有测到可用速率，请检查测速点是否可达。")
                result["status"] = "error"
                result["error"] = "没有测到可用速率"
                return result
            if self.broke_at is None:
                if self.incomplete:
                    warn(f"扫描没跑完，实际只验证到 {self.last_ok} Mbit，判不出有没有限速器。")
                    result["status"] = "inconclusive"
                    result["error"] = "扫描不完整"
                    return result
                warn(f"扫到 {hi} Mbit 仍未出现丢包跳变，未检测到限速器。")
                result["status"] = "no_knee"
                result["scanned_to"] = hi
                return result

            knee = self.last_ok
            margin = calc_margin(knee)
            recommend = max(1, knee - margin)
            result["knee"] = knee
            result["recommend"] = recommend
            ok(f"实测上限 {knee} Mbit，按 {knee}M 档位退 {margin} 余量"
               f" → 建议整形值 {recommend} Mbit")
            log(f"  （v1.0.0 只做测试，不下发整形；调优功能后续版本再加）")
            return result


# ---------------------------------------------------------------- 报告

def verdict_text(res):
    """一句人话结论。"""
    status = res["status"]
    if status == "no_knee":
        return (f"不限速实测 {res['unshaped_mbps']} Mbps、"
                f"丢包 {res['unshaped_loss']}%，未检测到限速器，链路干净")
    if status == "done" and res.get("knee"):
        head = f"实测上限 {res['knee']} Mbit，超过就丢包；"
        shape = res.get("shaping_text")
        if shape:
            # 已达标时没有东西可下发，不加“暂不下发”。
            if res.get("shaping_status") == "ok":
                return head + shape
            return head + shape + "（v1.0.0 暂不下发）"
        return (head + f"建议整形 {res['recommend']} Mbit（v1.0.0 暂不下发）")
    if status == "cancelled":
        return "扫描已取消，只做了不限速探测"
    if status == "inconclusive":
        return "判不出有没有限速器：" + res.get("error", "")
    return "测试未完成：" + res.get("error", "")


def tuning_compare_lines(res, base):
    """调优前后对比。v1.0.0 只测不调，所以“调优后”是按建议值整形后的预期，
    不是实测——等“调”版本下发整形并复测后，这里会换成实测对比。"""
    lines = ["调优前后对比："]
    if res["status"] == "no_knee":
        lines.append("  未检测到限速器，链路干净，无需整形。")
        return lines
    if not res.get("knee"):
        lines.append("  本次未测出拐点，无法对比。")
        return lines
    knee = res["knee"]
    rec = res["recommend"]
    unshaped = res.get("unshaped_mbps")
    loss = res.get("unshaped_loss", 0)
    cur = None
    if base and base.get("qdisc") == "htb":
        cur = parse_rate_mbit(base.get("htb_rate"))
    if res.get("shaping_status") == "ok":
        lines.append(f"  当前整形 {cur} Mbit 已达标，无需调整。")
        return lines
    before = f"整形 {cur} Mbit" if cur is not None else "无整形"
    lines.append(f"  调优前（当前）：{before}，实测上限 {knee} Mbit，"
                 f"不限速送达 {unshaped:.1f} Mbps、丢包 {loss:.4f}%")
    lines.append(f"  调优后（建议整形 {rec} Mbit，预期）："
                 f"送达约 {rec} Mbit，丢包约 0%（不再撞限速器）")
    return lines


def build_report(res, isp_name, peer_label, tx_bytes, rx_bytes, elapsed_s,
                 stack_text=None, version_label=None, base=None):
    """生成干净的文字报告。"""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "VPS 测试报告（vps-test v%s）" % VERSION,
        "时间：%s" % now,
        "运营商：%s" % isp_name,
    ]
    if stack_text:
        lines.append("本机 IP 栈：%s" % stack_text)
    if version_label:
        lines.append("测试走：%s" % version_label)
    lines.append("测速点：%s" % peer_label)
    if res.get("quick"):
        lines.append("模式：快速粗测（每档 5 秒、不细扫，拐点精度较粗）")
    if base:
        lines.append("")
        lines.append("当前系统参数（未调优）：")
        lines.extend(baseline_lines(base))
    lines.append("")
    if res.get("unshaped_mbps") is not None:
        lines.append("不限速探测：%.1f Mbps，丢包 %.4f%%" % (
            res["unshaped_mbps"], res.get("unshaped_loss", 0)))
    else:
        lines.append("不限速探测：未完成（%s）" % res.get("error", "未知原因"))
    if res.get("knee"):
        lines.append("实测上限：%d Mbit" % res["knee"])
        shape = res.get("shaping_text")
        if shape:
            suffix = "" if res.get("shaping_status") == "ok" else "（仅供参考，v1.0.0 不下发）"
            lines.append("整形：%s%s" % (shape, suffix))
        else:
            lines.append("建议整形值：%d Mbit（仅供参考，v1.0.0 不下发）" % res["recommend"])
    elif res["status"] == "no_knee":
        lines.append("限速器：未检测到")
    lines.append("")
    lines.append("结论：" + verdict_text(res))
    lines.append("")
    lines.extend(tuning_compare_lines(res, base))
    lines.append("")
    lines.append("本次测试消耗流量：出向 %s / 入向 %s / 合计 %s" % (
        human_bytes(tx_bytes), human_bytes(rx_bytes),
        human_bytes(tx_bytes + rx_bytes)))
    lines.append("用时：约 %d 秒" % int(elapsed_s))
    return "\n".join(lines) + "\n"


def save_report(text, res):
    """报告存到当前目录；历史记录存 ~/.vps-test/history.json，供下次对比。"""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(os.getcwd(), f"vps-test-report-{stamp}.txt")
    n = 2
    while os.path.exists(path):  # 同一秒内跑两次也不覆盖
        path = os.path.join(os.getcwd(), f"vps-test-report-{stamp}-{n}.txt")
        n += 1
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        ok("报告已保存：" + path)
    except OSError as exc:
        warn("报告保存失败：" + str(exc))
        return None
    record = {
        "time": stamp,
        "unshaped_mbps": res.get("unshaped_mbps"),
        "unshaped_loss": res.get("unshaped_loss"),
        "knee": res.get("knee"),
        "recommend": res.get("recommend"),
        "status": res.get("status"),
    }
    try:
        os.makedirs(HISTORY_DIR, exist_ok=True)
        history = []
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, encoding="utf-8") as f:
                history = json.load(f)
        if not isinstance(history, list):
            history = []
        history.append(record)
        history = history[-HISTORY_KEEP:]
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        warn("历史记录保存失败：" + str(exc))
        return path
    return path


def show_history_compare(res):
    """跟上次测试比一比。"""
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            history = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return
    if not isinstance(history, list) or len(history) < 2:
        return
    prev = history[-2]
    log()
    log("和上次测试对比：")
    log("  上次（%s）：不限速 %s Mbps，实测上限 %s" % (
        prev.get("time", "?"), prev.get("unshaped_mbps", "?"),
        ("%s Mbit" % prev["knee"]) if prev.get("knee") else "未检测到限速器"))
    log("  这次：不限速 %s Mbps，实测上限 %s" % (
        res.get("unshaped_mbps"),
        ("%s Mbit" % res["knee"]) if res.get("knee") else "未检测到限速器"))


def self_cleanup():
    """跑完自动清除本工具：只删脚本自身。报告和历史记录保留——
    报告是本次测试的凭证，历史记录供下次自动对比（删了它对比功能就永远触发不了）。"""
    cleaned = []
    try:
        os.remove(os.path.abspath(__file__))
        cleaned.append("脚本")
    except OSError as exc:
        warn("脚本自删失败：" + str(exc))
    if cleaned:
        log("已自动清除：" + "、".join(cleaned) + "（报告和历史记录保留）。")


def peak_reminder():
    """晚高峰提醒：白天测的只代表白天，晚上 19:00-23:00 再跑一次验证。"""
    hour = datetime.datetime.now().hour
    log()
    if 19 <= hour < 23:
        log("现在正是晚高峰（19:00-23:00），本次成绩即为高峰表现。")
    else:
        log("提醒：现在是白天，成绩只代表白天；")
        log("建议晚上 19:00-23:00 再跑一次，看晚高峰表现。")


# ---------------------------------------------------------------- 主流程

def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="VPS 验货工具 v%s：测真实带宽、找限速器、给人话结论。" % VERSION)
    parser.add_argument("--isp", type=int, choices=[1, 2, 3, 4],
                        help="跳过识别，直接指定运营商：1 电信 / 2 移动 / 3 联通 / 4 广电")
    parser.add_argument("--peer", help="手动指定 iperf3 测速点（主机名或 IP）")
    parser.add_argument("--yes", action="store_true",
                        help="跳过流量确认（无人值守用）")
    parser.add_argument("--keep", action="store_true",
                        help="跑完不删除脚本和历史记录（默认自动清除，只留报告）")
    parser.add_argument("--dur", type=int, default=DEFAULT_DUR,
                        help="每个速率点测试时长（秒，默认 12，范围 5-60）")
    parser.add_argument("--quick", action="store_true",
                        help="快速模式：每档 5 秒、只粗扫不细扫，流量省约 2/3，"
                             "拐点精度较粗（约一个粗步长）")
    args = parser.parse_args(argv)
    if args.dur < 5 or args.dur > 60:
        parser.error("--dur 必须是 5-60 的整数")
    if args.peer and not re.fullmatch(r"[A-Za-z0-9._:-]+", args.peer):
        parser.error("--peer 只能是主机名或 IP")
    return args


def main(argv=None):
    args = parse_args(argv)
    print(f"vps-test v{VERSION} —— VPS 验货：测真实带宽、找限速器")
    print("（只做测试，不改任何系统配置）")
    require_root()
    check_deps()
    started = time.time()

    # 1. 运营商识别
    isp_no, _detected = detect_isp(args.isp)
    isp_name = ISPS[isp_no][0]
    log()
    log(f"运营商：{isp_name}")

    # 1b. 本机 IP 栈（双栈 / 仅 IPv4 / 仅 IPv6）
    has_v4, has_v6, v4_addrs, v6_addrs = detect_ip_stack()
    stack_text = stack_label(has_v4, has_v6)
    log()
    log(f"本机 IP 栈：{stack_text}")
    for a in v4_addrs:
        log(f"  IPv4：{a}")
    for a in v6_addrs:
        log(f"  IPv6：{a}")
    if not has_v4 and not has_v6:
        warn("没检测到公网 IP，测速可能失败。")

    # 2. 测速点选择
    log()
    log("选择测速点（ping 延迟低的优先）：")
    peer, peer_label, version = pick_peer(args.peer, (has_v4, has_v6), isp_name)
    version_label = "IPv4" if version == "4" else "IPv6"
    log(f"  选用：{peer_label}（{peer}）[{version_label}]")

    # 3. 网卡
    iface = detect_iface()
    if not iface:
        warn("找不到默认路由网卡，无法测试。")
        return 1
    log(f"  网卡：{iface}")

    # 3b. 未调优的系统参数（只读，供对照）
    base = read_baseline(iface)
    log()
    log("当前系统参数（未调优，仅供对照）：")
    for line in baseline_lines(base):
        log(line)

    # 4. 拐点扫描
    dur = min(args.dur, 5) if args.quick else args.dur
    if args.quick:
        log("快速模式：每档 5 秒、只粗扫不细扫（拐点精度较粗）。")
    sweep = Sweep(peer, iface, dur=dur, assume_yes=args.yes, version=version,
                  quick=args.quick)
    res = sweep.run()
    tx_bytes, rx_bytes = sweep.traffic_used()
    elapsed = time.time() - started

    # 4b. 整形达标评估：当前整形 vs 本次实测建议值。达标就不用调。
    if res.get("knee") and res.get("recommend"):
        cur = parse_rate_mbit(base.get("htb_rate")) if base.get("qdisc") == "htb" else None
        st, txt = shaping_status(cur, res["knee"], res["recommend"])
        res["shaping_status"] = st
        res["shaping_text"] = txt
        log()
        if st == "ok":
            ok(txt)
        else:
            warn(txt)

    # 5. 报告
    log()
    log("=" * 60)
    log("结论：" + verdict_text(res))
    log("=" * 60)
    report = build_report(res, isp_name, peer_label, tx_bytes, rx_bytes, elapsed,
                          stack_text, version_label, base)
    log()
    print(report)
    save_report(report, res)
    show_history_compare(res)
    peak_reminder()
    if not args.keep:
        self_cleanup()
    return 0 if res["status"] in ("done", "no_knee") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        warn("已中断。")
        sys.exit(130)

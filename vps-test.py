#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vps-test.py — VPS 验货工具 v2.0.2（“测”+“调”）

公开项目，给国内买 VPS 的人用。一键测出这台 VPS 的真实带宽、
有没有限速器、限速器在哪、链路干不干净、抖动大不大，最后给一句人话结论。
双点测：能力点（通用节点）测 VPS 自己的限速器，国内点（运营商匹配节点）测到中国的真实路径。
测完可下发调优（整形 + 基础参数），下发前会问一声，默认不改；回滚用 --rollback。

用法（root 身份，在 VPS 上跑）：
    python3 vps-test.py                 # 全自动：识别运营商 -> 选测速点 -> 拐点扫描 -> 报告（测完问要不要调优）
    python3 vps-test.py --isp 1         # 跳过识别，直接指定运营商（1 电信 / 2 移动 / 3 联通 / 4 广电）
    python3 vps-test.py --ip 6          # 手动指定 IP 版本（只测 IPv6；默认按延迟自动选）
    python3 vps-test.py --peer <地址>   # 手动指定 iperf3 测速点
    python3 vps-test.py --yes           # 跳过流量确认和调优确认（无人值守用）
    python3 vps-test.py --rollback      # 回滚本工具下发的调优，恢复下发前状态（不测速）

依赖：Python 3.7+ 标准库、iperf3（只当客户端）、tc（iproute2）。
测速时只在扫描期间临时挂测试整形器，跑完立即恢复原状；调优（整形 + sysctl）
只在用户确认后下发，下发前先存快照，--rollback 可恢复。
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

VERSION = "2.0.2"

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
# 节点清单：(host, label, isp_tag, provider)。
# isp_tag：国内节点所属运营商（电信/移动/联通/广电），通用节点为 None。
# provider：免费提供该公共 iperf3 节点的商家/组织（API 的 PROVIDER 字段，
# 公开信息，原样注明是给提供方署名，不构成任何隶属或背书）。
PEERS = [
    ("116.169.118.91", "成都 电信", "电信", "China Telecom"),
    ("59.56.22.40", "福州 电信", "电信", "China Telecom"),
    ("114.230.93.99", "南京 电信", "电信", "China Telecom"),
    ("122.228.230.122", "宁波 电信", "电信", "China Telecom"),
    ("123.53.44.221", "郑州 电信", "电信", "China Telecom"),
    ("speedtest.lax12.us.leaseweb.net", "洛杉矶 Leaseweb", None, "Leaseweb"),
    ("speedtest.hkg12.hk.leaseweb.net", "香港 Leaseweb", None, "Leaseweb"),
    ("speedtest.sin1.sg.leaseweb.net", "新加坡 Leaseweb", None, "Leaseweb"),
    ("sgp.proof.ovh.net", "新加坡 OVH", None, "OVH"),
    ("speedtest.syd12.au.leaseweb.net", "悉尼 Leaseweb", None, "Leaseweb"),
    ("speedtest.tyo11.jp.leaseweb.net", "东京 Leaseweb", None, "Leaseweb"),
]
IPERF_PORTS = [5201, 5202, 5203, 5204, 5205]

# 节点清单自动拉取：iperf3serverlist.net 的 JSON API。
# 一键命令后自动拉最新国内节点；拉不到（没网/接口挂了/改格式）就用本地缓存，再没有就用硬编码 PEERS。
PEERS_API_URL = "https://iperf3serverlist.net/api/servers.json"
PEERS_API_TIMEOUT = 8  # 秒
PEERS_CACHE_MAX_AGE = 7 * 24 * 3600  # 缓存 7 天内有效
PEERS_MIN_UPTIME = 95  # 只用 30 天在线率 >=95 的节点
# 运营商映射：API 的 PROVIDER -> 本项目运营商名（对不上号的国内节点不用）
PEER_PROVIDER_ISP = {
    "China Telecom": "电信",
    "China Mobile": "移动",
    "China Unicom": "联通",
    "China Broadcasting": "广电",
}

# API 的 SITE 是英文城市名，统一转中文（覆盖行、选点、报告都用中文）。
# 不在表里的原样保留（将来清单里出现新城市时再补）。
CITY_ZH = {
    "Beijing": "北京", "Shanghai": "上海", "Guangzhou": "广州",
    "Shenzhen": "深圳", "Chengdu": "成都", "Chongqing": "重庆",
    "Wuhan": "武汉", "Xi'an": "西安", "Hangzhou": "杭州",
    "Nanjing": "南京", "Tianjin": "天津", "Suzhou": "苏州",
    "Zhengzhou": "郑州", "Changsha": "长沙", "Ningbo": "宁波",
    "Qingdao": "青岛", "Fuzhou": "福州", "Xiamen": "厦门",
    "Hefei": "合肥", "Jinan": "济南", "Harbin": "哈尔滨",
    "Shenyang": "沈阳", "Changchun": "长春", "Dalian": "大连",
    "Shijiazhuang": "石家庄", "Taiyuan": "太原", "Nanchang": "南昌",
    "Guiyang": "贵阳", "Nanning": "南宁", "Kunming": "昆明",
    "Haikou": "海口", "Lanzhou": "兰州", "Yinchuan": "银川",
    "Xining": "西宁", "Urumqi": "乌鲁木齐", "Hohhot": "呼和浩特",
    "Lhasa": "拉萨", "Wuxi": "无锡", "Changzhou": "常州",
    "Nantong": "南通", "Jiaxing": "嘉兴", "Shaoxing": "绍兴",
    "Wenzhou": "温州", "Quanzhou": "泉州", "Dongguan": "东莞",
    "Foshan": "佛山", "Zhuhai": "珠海", "Huizhou": "惠州",
    "Zhongshan": "中山", "Shantou": "汕头", "Baotou": "包头",
    "Luoyang": "洛阳", "Yantai": "烟台", "Weifang": "潍坊",
}


def _clean_display_text(s):
    """清洗 API 里来的展示用文本（城市名、提供方）：去掉换行和控制字符，
    防止恶意条目往终端/报告里注入假行。只影响显示，不影响 host 校验。"""
    if not isinstance(s, str):
        return ""
    return re.sub(r"[\x00-\x1f\x7f]", "", s).strip()


def _valid_peer_host(host):
    """测速点 host 是否合法：合法 IP，或合法主机名。
    主机名只允许字母数字、点、连字符，且不能以连字符开头
    （以 - 开头的主机会被 ping/iperf3 当成命令行选项解析）。
    """
    if not isinstance(host, str) or not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return bool(re.fullmatch(r"[A-Za-z0-9.-]+", host) and "." in host
                and ".." not in host and not host.startswith("-"))


def _parse_peer_hostfield(field):
    """解析 API 的 IP/HOST 字段，如 "iperf3 -c 116.169.118.91 -p 5201-5220"。
    端口范围须包含 5201（本脚本默认先试 5201），host 须是合法 IP 或主机名。
    返回 host，解析失败或不合要求返回 None。
    """
    m = re.search(r"-c\s+(\S+)\s+-p\s+(\d+)(?:-(\d+))?", field or "")
    if not m:
        return None
    host = m.group(1)
    try:
        port_lo = int(m.group(2))
        port_hi = int(m.group(3)) if m.group(3) else port_lo
    except ValueError:
        return None
    if not (port_lo <= 5201 <= port_hi):
        return None
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if _valid_peer_host(host):
        return host
    return None


def fetch_peers_from_api(timeout=PEERS_API_TIMEOUT):
    """从 iperf3serverlist.net 拉取最新公共节点清单，只留中国内地节点。
    返回 [(host, label, isp_tag, provider)]，失败返回 None（调用方走缓存/硬编码）。
    provider 是 API 的 PROVIDER 字段原值（公开信息），用于给提供方署名。
    """
    try:
        req = urllib.request.Request(
            PEERS_API_URL, headers={"User-Agent": "vps-test/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            data = json.load(r)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    peers = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if entry.get("COUNTRY") != "CN":
            continue
        if entry.get("STATUS") != "green":
            continue
        try:
            uptime = float(entry.get("UPTIME_30D", 0))
        except (TypeError, ValueError):
            continue
        if uptime < PEERS_MIN_UPTIME:
            continue
        host = _parse_peer_hostfield(entry.get("IP/HOST", ""))
        if host is None:
            continue
        provider = _clean_display_text(entry.get("PROVIDER"))
        isp_tag = PEER_PROVIDER_ISP.get(provider)
        if isp_tag is None:
            # 提供方不是已知的运营商（比如志愿者个人）：按 IP 的 ASN 判运营商，
            # 这样志愿者架在某运营商网络上的节点也不会被丢掉。
            # 查不到或判不出运营商的，跳过（无法归到某个运营商的国内节点组）。
            info = lookup_ip(host)
            if info is None:
                continue
            isp_no, _dc = classify_isp(info)
            if isp_no is None:
                continue
            isp_tag = ISPS[isp_no][0]
        site = _clean_display_text(entry.get("SITE")) or "国内"
        site = CITY_ZH.get(site, site)  # 城市名统一中文；不在表里的原样保留
        peers.append((host, "%s %s" % (site, isp_tag), isp_tag, provider))
    return peers


def load_peers():
    """节点清单：API 拉最新国内节点 -> 本地缓存 -> 硬编码 PEERS。
    返回 (peers, source)，source 为 "api" / "cache" / "builtin"。
    通用节点（Leaseweb 等）始终用硬编码，只刷新国内节点。
    """
    general = [p for p in PEERS if p[2] is None]
    fetched = fetch_peers_from_api()
    if fetched:
        try:
            os.makedirs(HISTORY_DIR, exist_ok=True)
            with open(PEERS_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump([list(p) for p in fetched], f, ensure_ascii=False)
        except OSError:
            pass
        return fetched + general, "api"
    try:
        if os.path.exists(PEERS_CACHE_FILE):
            age = time.time() - os.path.getmtime(PEERS_CACHE_FILE)
            if age < PEERS_CACHE_MAX_AGE:
                with open(PEERS_CACHE_FILE, encoding="utf-8") as f:
                    cached = json.load(f)
                # 缓存是本机文件，理论上可信，但读出来仍按与 API 同样的标准校验
                # （防文件损坏或被改写后把非法 host 送进 ping/iperf3）。
                # 兼容旧版 3 元组缓存（无 provider，补空串）。
                cn = [(p[0], p[1], p[2], p[3] if len(p) == 4 else "")
                      for p in cached
                      if isinstance(p, list) and len(p) in (3, 4)
                      and _valid_peer_host(p[0])
                      and isinstance(p[1], str) and isinstance(p[2], str)
                      and (len(p) == 3 or isinstance(p[3], str))]
                if cn:
                    return cn + general, "cache"
    except (OSError, ValueError):
        pass
    return PEERS, "builtin"

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
PEERS_CACHE_FILE = os.path.join(HISTORY_DIR, "peers.json")
# v2.0 调优快照：下发前存的原始状态（qdisc + sysctl），--rollback 靠它恢复。
TUNE_SNAPSHOT_FILE = os.path.join(HISTORY_DIR, "tune-snapshot.json")
# v2.0 sysctl 持久化：调优的 sysctl 值写进这个文件，重启后仍生效。
SYSCTL_PERSIST_FILE = "/etc/sysctl.d/99-vps-tune.conf"
# v2.0 基础调优目标值：拥塞控制 BBR，缓冲 16MB（千兆级链路的经验门槛）。
TUNE_CC = "bbr"
TUNE_BUF = 16777216


# ---------------------------------------------------------------- 工具函数

def _use_color():
    """终端才上色；重定向/管道时保持纯文本（不污染日志）。
    ANSI 颜色码是终端通用标准，不是 FinalShell 专属——PuTTY、Termius、
    macOS 终端、iTerm2、Windows Terminal 等任何 SSH 客户端都支持。
    TERM=dumb 的终端不支持颜色，也不上色。"""
    if not sys.stdout.isatty():
        return False
    return os.environ.get("TERM", "") != "dumb"


def red(text):
    return f"\033[31m{text}\033[0m" if _use_color() else text


def green(text):
    return f"\033[32m{text}\033[0m" if _use_color() else text


def _strip_ansi(text):
    """去掉 ANSI 颜色码（报告存文件时用，文件里保持纯文本）。"""
    return re.sub(r"\033\[[0-9;]*m", "", text)


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


def ping_stats(host, count=10, version=None):
    """ping 测延迟抖动：发 count 个包（0.2 秒间隔），返回 (avg_ms, jitter_ms, n)。
    jitter 是 RTT 的总体标准差（毫秒），样本不足 2 个或 ping 失败返回 None。
    version: "4" / "6" / None（默认）。"""
    cmd = ["ping"]
    if version == "4":
        cmd.append("-4")
    elif version == "6":
        cmd.append("-6")
    cmd += ["-i", "0.2", "-c", str(count), "-W", "2", host]
    rc, out, _ = run_cmd(cmd, timeout=15)
    if rc != 0:
        return None
    samples = [float(x) for x in re.findall(r"time=([\d.]+)\s*ms", out)]
    if len(samples) < 2:
        return None
    avg = sum(samples) / len(samples)
    var = sum((x - avg) ** 2 for x in samples) / len(samples)
    return round(avg, 1), round(var ** 0.5, 1), len(samples)


def jitter_note(jitter_ms):
    """抖动定性（仅供参考）：<5 ms 稳定，5-20 ms 一般，>20 ms 偏高。"""
    if jitter_ms is None:
        return ""
    if jitter_ms < 5:
        return "抖动 %.1f ms，路径稳定。" % jitter_ms
    if jitter_ms <= 20:
        return "抖动 %.1f ms，一般。" % jitter_ms
    return "抖动 %.1f ms 偏高，晚高峰可能更差。" % jitter_ms


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


def mask_ip(ip):
    """IP 脱敏（报告和终端展示用，不存完整 IP）：
    主机名原样返回；IPv4 只留前 3 段（122.228.230.x）；IPv6 只留前 4 组。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip  # 主机名，原样返回
    if isinstance(addr, ipaddress.IPv4Address):
        return ".".join(ip.split(".")[:3]) + ".x"
    groups = [g.lstrip("0") or "0" for g in addr.exploded.split(":")]
    return ":".join(groups[:4]) + ":..."


def _pick_best_in_group(group, versions):
    """在一组测速点里 ping 一圈，取延迟最低的。
    返回 (host, label, version, rtt) 或 None。label 是干净的测速点名
    （如“南京 电信”），不带延迟——延迟是测量值，单独返回。
    之前标签里带延迟（“南京 电信（延迟 150.0 ms）”），导致按标签
    比对测速点时会误判：同一台服务器两次延迟不同，标签就不同，
    历史对比会错误地显示“测速点不同”。"""
    best = None
    for host, label, _isp, _prov in group:
        peer_versions = resolve_peer_versions(host)
        use = [v for v in versions if v in peer_versions] or versions
        for ver in use:
            rtt = ping_avg(host, version=ver)
            if rtt is not None:
                vlabel = "IPv4" if ver == "4" else "IPv6"
                log(f"  测速点 {label}（{mask_ip(host)}）[{vlabel}] 延迟 {rtt:.1f} ms")
                if best is None or rtt < best[0]:
                    best = (rtt, host, label, ver)
    if best is None:
        return None
    return best[1], best[2], best[3], best[0]


def domestic_coverage_lines(peers):
    """国内 iperf3 节点覆盖：每个运营商有没有内地节点、能不能测、谁提供的。
    能测和不能测都列出来，使用者一眼就知道工具的国内覆盖范围；
    将来清单里出现移动/联通/广电的节点，这里自动变成可测。
    提供方是节点清单里的公开信息（API 的 PROVIDER 字段），原样注明署名。"""
    lines = []
    for name in ("电信", "移动", "联通", "广电"):
        matched = [p for p in peers if p[2] == name]
        if matched:
            cities = "/".join(p[1].split(" ")[0] for p in matched)
            providers = sorted(set(p[3] for p in matched if p[3]))
            suffix = "，提供方：%s" % "/".join(providers) if providers else ""
            lines.append("  %s：%d 个（%s）可测%s" % (name, len(matched), cities, suffix))
        else:
            lines.append("  %s：0 个，不可测" % name)
    return lines


def pick_peers(cli_peer=None, stack=(True, True), isp_name=None, peers=None,
               force_version=None):
    """双点选择（2026-11-22：一个点测不了两件事）：
    - 能力点 cap：延迟最低的通用节点（洛杉矶/香港/新加坡/悉尼/东京），测 VPS 自己的限速器；
    - 国内点 dom：延迟最低的运营商匹配国内节点，测到中国的真实路径；没有匹配节点时为 None。
    返回 (cap, dom)，每项为 (host, label, version, rtt) 或 None。
    label 是干净的测速点名，不带延迟；rtt 是选点时的 ping 延迟（毫秒），
    默认 fallback 时为 None（没 ping 过）。
    手动指定 --peer 时只用那一个点，dom 为 None。
    force_version="4"/"6" 时只考虑该 IP 版本（--ip 手动指定）；
    强制 IPv6 时国内节点大概率没有（清单里的国内节点目前都是 IPv4），dom 为 None。
    """
    peers = peers if peers is not None else PEERS
    has_v4, has_v6 = stack
    if force_version in ("4", "6"):
        versions = [force_version]
    else:
        versions = []
        if has_v4:
            versions.append("4")
        if has_v6:
            versions.append("6")
        if not versions:
            # 检测不到 IP 栈：默认 IPv4，让 iperf3 自己决定。
            versions = ["4"]
    if cli_peer:
        cap = _pick_best_in_group([(cli_peer, "手动指定", None, "")], versions)
        dom = None
    else:
        general = [p for p in peers if p[2] is None]
        cap = _pick_best_in_group(general, versions)
        dom = None
        if isp_name:
            matched = [p for p in peers if p[2] == isp_name]
            if matched:
                dom = _pick_best_in_group(matched, versions)
                if dom is not None and cap is not None and dom[0] == cap[0]:
                    dom = None  # 国内点与能力点是同一个，不重复测
    if cap is None:
        warn("测速点都 ping 不通，默认用香港 Leaseweb。")
        default = next((p for p in peers
                        if p[0] == "speedtest.hkg12.hk.leaseweb.net"), None)
        if default is None:
            default = peers[0]
        cap = (default[0], default[1] + "（默认）", versions[0], None)
    return cap, dom


def pick_peer(cli_peer=None, stack=(True, True), isp_name=None, peers=None,
              force_version=None):
    """兼容旧接口：只返回能力点（单点时代的用法）。新代码请用 pick_peers。"""
    cap, _dom = pick_peers(cli_peer, stack, isp_name, peers, force_version)
    return cap


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


def apply_htb_rate(iface, rate_mbit):
    """在网卡上挂一个 rate_mbit 的 htb 整形器（根 handle 1:、class 1:10、叶子 fq）。
    失败返回 False。QdiscGuard 的测试整形和 v2.0 的持久下发都走这里，
    保证两边挂的整形器结构一致（回滚时才认得出来）。"""
    if not isinstance(rate_mbit, int) or isinstance(rate_mbit, bool) or rate_mbit <= 0:
        return False
    burst = max(32768, rate_mbit * 500)
    tc("qdisc", "del", "dev", iface, "root")
    rc, _, _ = tc("qdisc", "add", "dev", iface, "root",
                  "handle", "1:", "htb", "default", "10")
    if rc != 0:
        return False
    rc, _, _ = tc("class", "add", "dev", iface, "parent", "1:",
                  "classid", "1:10", "htb",
                  "rate", f"{rate_mbit}mbit", "ceil", f"{rate_mbit}mbit",
                  "burst", str(burst), "cburst", str(burst), "quantum", "1514")
    if rc != 0:
        return False
    rc, _, _ = tc("qdisc", "add", "dev", iface, "parent", "1:10",
                  "handle", "10:", "fq", "limit", "40960",
                  "flow_limit", "8192", "maxrate", f"{rate_mbit}mbit")
    return rc == 0


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
            # class 行两种写法都认：根下的 class 在 tc 里显示为
            # "parent 1:" 或 "root"（cc-dc2 真机是 root，mock 里是
            # parent 1:；只认一种会抓不到自己下的整形，见 v2.0.1）。
            m = re.search(r"class htb (\S+) (?:parent 1:|root\b)", line)
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
            # 只看本网卡的行：这里跑的是 tc qdisc show dev <iface>，输出
            # 里没有 "dev <iface>"（每行直接是 "qdisc htb 1: root …" /
            # "qdisc fq 10: parent 1:10 …"）；只有行里带 dev 且不是本
            # 网卡时才跳过（v2.0.2，按 qdisc_root_kind 的容错写法；旧
            # 代码要求 parts[3] 必须是 dev，两行全被跳过，快照永远 None）。
            if len(parts) >= 5 and parts[3] == "dev" and parts[4] != self.iface:
                continue
            if "root" in parts:
                # qdisc htb 1: root ... default 0x10 ...
                if parts[1] != "htb":
                    return None
                if "default" in parts:
                    di = parts.index("default")
                    if di + 1 < len(parts) and parts[di + 1] == "0x10":
                        root_ok = True
                continue
            # 叶子：parent 1:10 的位置不固定（带 dev 时在 parts[5:7]，
            # 不带 dev 时在 parts[3:5]），扫 parts 找，不用固定下标。
            if "parent" in parts:
                pi = parts.index("parent")
                if pi + 1 < len(parts) and parts[pi + 1] == "1:10":
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

    def restore(self, advise_reapply=True):
        """删掉测试整形器，恢复原来的根 qdisc。
        advise_reapply=False 时不提示重新下发（回滚场景：用户刚亲手
        拆了整形，再叫他下回去是错的，见 v2.0.1）。"""
        tc("qdisc", "del", "dev", self.iface, "root")
        kind = self.saved_kind
        if not kind or kind in ("noqueue", "pfifo_fast"):
            return
        if kind == "mq":
            # 多队列网卡：删根后内核一般会自动重建 mq；没重建才手动补。
            if qdisc_root_kind(self.iface) != "mq":
                tc("qdisc", "add", "dev", self.iface, "root", "mq")
            return
        reapply = "请重新下发原来的整形。" if advise_reapply else ""
        if kind == "htb":
            if self.saved_htb:
                self._restore_htb()
                if self._htb_restored_ok():
                    return
                warn("htb 恢复校验没通过，已恢复为 fq（不限速）——" + reapply)
            else:
                # 抓不到原配置：绝不能留裸 htb（没 class 会断网），
                # 退回 fq（不限速但不断网），让用户重新下发原整形。
                warn("原 htb 整形配置无法解析，已恢复为 fq（不限速）——" + reapply)
            tc("qdisc", "del", "dev", self.iface, "root")
            tc("qdisc", "add", "dev", self.iface, "root", "fq")
            return
        if kind in self.CLASSFUL:
            # 其他带配置的 qdisc（tbf/hfsc/netem…）：同样不留裸配置，
            # 退回 fq + 警告。
            warn(f"原 {kind} 整形配置无法完整恢复，已恢复为 fq（不限速）——"
                 + reapply)
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
        return apply_htb_rate(self.iface, rate_mbit)


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


# ---------------------------------------------------------------- v2.0 调优（整形 + 基础参数 + 回滚）

# 只允许改这 5 个 sysctl 键（白名单）。写别的键一律拒绝，防手滑/注入。
TUNE_SYSCTL_KEYS = {
    "net.ipv4.tcp_congestion_control",
    "net.core.rmem_max",
    "net.core.wmem_max",
    "net.ipv4.tcp_rmem",
    "net.ipv4.tcp_wmem",
}


def write_sysctl(name, value):
    """写一个 sysctl 值（只走 /proc/sys，不走 shell）。键必须在白名单里，
    值只能是字母/数字/下划线/空格/点（tcp_rmem 这类三段值含空格），
    含换行或 shell 字符一律拒绝。成功返回 True。"""
    if name not in TUNE_SYSCTL_KEYS:
        return False
    value = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_ .]+", value):
        return False
    path = "/proc/sys/" + name.replace(".", "/")
    try:
        with open(path, "w") as f:
            f.write(value)
    except OSError:
        return False
    return True


def persist_sysctl(changes):
    """把改过的 sysctl 值写进 /etc/sysctl.d/99-vps-tune.conf，重启后仍生效。
    changes 是 {key: value}。只写白名单里的键、值符合安全格式的，
    其他一律跳过（防脏数据进持久化文件）。写失败只警告（/proc 已即时生效，
    只是重启后会丢），不算调优失败。"""
    lines = ["# vps-tune v2.0 调优（下发时写入，--rollback 时删除）"]
    for key in sorted(changes):
        val = str(changes[key])
        if key not in TUNE_SYSCTL_KEYS:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_ .]+", val):
            continue
        lines.append(f"{key} = {val}")
    try:
        os.makedirs(os.path.dirname(SYSCTL_PERSIST_FILE), exist_ok=True)
        with open(SYSCTL_PERSIST_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as exc:
        warn("sysctl 持久化失败（重启后会丢，需手动写 /etc/sysctl.conf）：" + str(exc))
        return False
    return True


def _tcp_buf_max(val):
    """tcp_rmem/tcp_wmem 的三段值（min default max）取 max。取不到返回 None。"""
    if not val:
        return None
    try:
        parts = str(val).split()
        return int(parts[2]) if len(parts) >= 3 else None
    except (ValueError, IndexError):
        return None


def plan_tuning(base, res):
    """按实测结果和当前参数，列出需要调的项。返回 change 列表，每项是 dict：
    shaping: {"kind": "shaping", "rate": 956, "desc": "..."}
    sysctl:  {"kind": "sysctl", "key": ..., "old": ..., "new": ..., "desc": "..."}
    qdisc:   {"kind": "qdisc", "desc": "根 qdisc：pfifo_fast → fq"}
    全达标返回空列表。"""
    plan = []
    # 整形：只有测出拐点、且当前状态不是 ok 时才动。
    recommend = res.get("recommend")
    if res.get("knee") and recommend:
        st = res.get("shaping_status")
        cur = parse_rate_mbit(base.get("htb_rate")) if base.get("qdisc") == "htb" else None
        if st == "none":
            plan.append({"kind": "shaping", "rate": recommend,
                         "desc": f"整形：下发 htb {recommend} Mbit（当前无整形）"})
        elif st == "high":
            plan.append({"kind": "shaping", "rate": recommend,
                         "desc": f"整形：{cur} Mbit → {recommend} Mbit（当前高于实测上限，会丢包）"})
        elif st == "low":
            plan.append({"kind": "shaping", "rate": recommend,
                         "desc": f"整形：{cur} Mbit → {recommend} Mbit（当前低于建议值）"})
    shaping = any(c["kind"] == "shaping" for c in plan)
    # 基础参数：拥塞控制 BBR、缓冲 16MB。只改不达标的。
    cc = (base.get("cc") or "").lower()
    if cc and not cc.startswith("bbr"):
        plan.append({"kind": "sysctl", "key": "net.ipv4.tcp_congestion_control",
                     "old": base.get("cc"), "new": TUNE_CC,
                     "desc": f"拥塞控制：{base.get('cc')} → {TUNE_CC}"})
    for key, label in (("net.core.rmem_max", "接收缓冲上限"),
                       ("net.core.wmem_max", "发送缓冲上限")):
        val = base.get(key.replace("net.core.", ""))
        try:
            cur = int(val) if val is not None else None
        except ValueError:
            cur = None
        if cur is not None and cur < TUNE_BUF:
            plan.append({"kind": "sysctl", "key": key, "old": val,
                         "new": str(TUNE_BUF),
                         "desc": f"{label}：{val} → {TUNE_BUF}"})
    for key, label in (("net.ipv4.tcp_rmem", "TCP 读缓冲上限"),
                       ("net.ipv4.tcp_wmem", "TCP 写缓冲上限")):
        short = key.split(".")[-1]
        mx = _tcp_buf_max(base.get(short))
        if mx is not None and mx < TUNE_BUF:
            parts = str(base.get(short)).split()
            new = f"{parts[0]} {parts[1]} {TUNE_BUF}"
            plan.append({"kind": "sysctl", "key": key, "old": base.get(short),
                         "new": new,
                         "desc": f"{label}：{mx} → {TUNE_BUF}"})
    # 根 qdisc：只有不下发整形、且当前是 pfifo_fast/noqueue 时才换 fq。
    # 下发整形时根会变成 htb（叶子就是 fq），不用再单独换。
    if not shaping and base.get("qdisc") in ("pfifo_fast", "noqueue"):
        plan.append({"kind": "qdisc", "desc": f"根 qdisc：{base.get('qdisc')} → fq"})
    return plan


def tuning_plan_lines(plan):
    """调优方案的人话行。"""
    return ["  " + c["desc"] for c in plan]


def snapshot_tuning_state(iface, base, plan):
    """下发前抓原始状态，供 --rollback 恢复。返回 dict，存到快照文件。
    qdisc：原始种类 + htb 快照（原是 htb 才有）；sysctl：要改的键的原始值。
    qdisc 种类读当场 live 的值，不用开跑时的旧 base——扫描期间
    QdiscGuard 可能已换过 qdisc，旧 base 会和下发前实际状态对不上
    （v2.0.0 第二跑的快照就因此写成 qdisc_kind=htb、htb=null）。"""
    live_kind = qdisc_root_kind(iface) or base.get("qdisc") or ""
    snap = {"version": VERSION, "qdisc_kind": live_kind, "htb": None,
            "sysctl": {}}
    if live_kind == "htb":
        snap["htb"] = QdiscGuard(iface)._snapshot_htb()
    for c in plan:
        if c["kind"] == "sysctl" and c.get("old") is not None:
            snap["sysctl"][c["key"]] = c["old"]
    return snap


def save_tune_snapshot(snap):
    """快照写文件。写失败返回 False（此时绝不能下发，否则回滚不了）。"""
    try:
        os.makedirs(HISTORY_DIR, exist_ok=True)
        with open(TUNE_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        warn("调优快照保存失败（无法回滚，已停止下发）：" + str(exc))
        return False
    return True


def htb_applied_ok(iface, rate_mbit):
    """校验整形是否生效：根是 htb，且 class 1:10 的 rate 就是下发的值。"""
    if qdisc_root_kind(iface) != "htb":
        return False
    rc, out, _ = tc("class", "show", "dev", iface)
    if rc != 0:
        return False
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "class" and parts[2] == "1:10":
            m = re.search(r"\brate (\S+)", line)
            if m and parse_rate_mbit(m.group(1)) == rate_mbit:
                return True
    return False


def apply_tuning(iface, base, plan):
    """按方案下发调优。先存快照（存不上就停手），再逐项下发并校验。
    返回 [(desc, ok)]。单项失败不中断后面的，最后如实汇报。"""
    snap = snapshot_tuning_state(iface, base, plan)
    if not save_tune_snapshot(snap):
        return []
    results = []
    sysctl_new = {}
    for c in plan:
        if c["kind"] == "shaping":
            ok_flag = apply_htb_rate(iface, c["rate"])
            if ok_flag:
                ok_flag = htb_applied_ok(iface, c["rate"])
            results.append((c["desc"], ok_flag))
            if not ok_flag:
                warn("整形下发失败（htb 未生效）。")
        elif c["kind"] == "sysctl":
            ok_flag = write_sysctl(c["key"], c["new"])
            if ok_flag:
                sysctl_new[c["key"]] = c["new"]
            results.append((c["desc"], ok_flag))
            if not ok_flag:
                warn(f"sysctl 下发失败：{c['key']}（内核可能不支持该值）。")
        elif c["kind"] == "qdisc":
            tc("qdisc", "del", "dev", iface, "root")
            rc, _, _ = tc("qdisc", "add", "dev", iface, "root", "fq")
            ok_flag = rc == 0 and qdisc_root_kind(iface) == "fq"
            results.append((c["desc"], ok_flag))
            if not ok_flag:
                warn("根 qdisc 换 fq 失败。")
    if sysctl_new:
        persist_sysctl(sysctl_new)
    return results


def rollback_tuning(iface):
    """--rollback：把本工具下发的调优全部恢复。读快照文件，
    qdisc 按快照原样恢复（htb 按快照重建，抓不到就退 fq + 警告），
    sysctl 写回原始值并删持久化文件，最后删快照。无快照返回 False。"""
    try:
        with open(TUNE_SNAPSHOT_FILE, encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        warn("没有调优快照（~/.vps-test/tune-snapshot.json），无需回滚。")
        return False
    if not isinstance(snap, dict):
        warn("调优快照损坏，无法回滚。")
        return False
    log("正在回滚调优（恢复下发前状态）……")
    # qdisc：按快照原样恢复，复用 QdiscGuard.restore() 的逻辑。
    # 快照里的 qdisc 种类必须像个 qdisc 名（字母/数字/下划线），否则不认
    # （防脏快照），退回 fq。tc 走 subprocess 列表、无 shell，脏值本身注不进去。
    kind = snap.get("qdisc_kind") or ""
    guard = QdiscGuard(iface)
    if kind and not re.fullmatch(r"[a-z0-9_]+", kind):
        warn(f"快照里的 qdisc 种类异常（{kind}），已恢复为 fq（不限速）。")
        guard.saved_kind = "fq"
        guard.saved_htb = None
    else:
        guard.saved_kind = kind
        guard.saved_htb = snap.get("htb")
    guard.restore(advise_reapply=False)
    kind = qdisc_root_kind(iface)
    log(f"  根 qdisc 已恢复：{kind or '未知'}")
    # sysctl：写回原始值，删持久化文件。
    restored = []
    for key, val in (snap.get("sysctl") or {}).items():
        if write_sysctl(key, val):
            restored.append(key)
        else:
            warn(f"sysctl 恢复失败：{key}（请手动检查）。")
    try:
        os.remove(SYSCTL_PERSIST_FILE)
    except OSError:
        pass
    if restored:
        log("  sysctl 已恢复：" + "、".join(restored))
    try:
        os.remove(TUNE_SNAPSHOT_FILE)
    except OSError:
        pass
    ok("回滚完成。")
    return True


def estimate_scan_traffic_gb(lo, hi, step, dur, fine_points=8):
    """估算一次扫描的流量（GB）：粗扫各档 + 细扫上限 fine_points 档。"""
    points = (hi - lo) // step + 2 + fine_points
    avg_rate = (lo + hi) / 2.0
    return points * avg_rate * dur / 8 / 1024


# ---------------------------------------------------------------- 拐点扫描

def _iface_bytes(iface, which):
    """读网卡计数器（tx/rx bytes），失败返回 0。"""
    try:
        with open(f"/sys/class/net/{iface}/statistics/{which}_bytes") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def probe_unshaped(peer, iface, dur, version="4"):
    """只做不限速探测（不扫拐点），供国内点测到中国的路径用。
    返回 (goodput_mbps, loss_pct)，测不到返回 None。
    探测前切到 fq（不限速），测完由 QdiscGuard 恢复原状。
    """
    with QdiscGuard(iface) as guard:
        if not guard.set_fq():
            return None
        for _ in range(MAX_RETRIES):
            res = run_iperf(peer, dur, version, streams=1)
            if res is not None:
                gp = res["recv_mbps"]
                loss = loss_pct(res["retrans"], res["sent_mbps"], dur)
                return round(gp, 1), round(loss, 4)
            time.sleep(8)
    return None


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
            log(f"  （测完会问要不要下发整形，默认不改；回滚用 --rollback）")
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
            # 已达标或已下发时没有东西可下发，不加后缀；否则注明未下发。
            if res.get("shaping_status") in ("ok", "applied"):
                return head + shape
            return head + shape + "（未下发）"
        return (head + f"建议整形 {res['recommend']} Mbit（未下发）")
    if status == "cancelled":
        return "扫描已取消，只做了不限速探测"
    if status == "inconclusive":
        return "判不出有没有限速器：" + res.get("error", "")
    return "测试未完成：" + res.get("error", "")


def domestic_verdict_line(dom_probe, dom_skip_reason=None):
    """结论框里的国内路径一行。测了就给测速点、速度、丢包和链路干净与否；
    没测就写明原因，不让人以为漏测了。手动 --peer 是故意只测一个点，
    没测速点也没原因，这时不显示这一行。"""
    if dom_probe is not None:
        verdict = "链路干净" if dom_probe["loss"] <= LOSS_THRESHOLD else "丢包偏高"
        return "到国内（%s）：%.1f Mbps，丢包 %.4f%%，%s" % (
            dom_probe["label"], dom_probe["mbps"], dom_probe["loss"], verdict)
    if dom_skip_reason:
        return "到国内：未测（%s）" % dom_skip_reason
    return None


def tuning_compare_lines(res, base):
    """调优前后对比。“调优后”是按建议值整形后的预期，不是实测——
    等下发整形并复测后，这里会换成实测对比。v2.0 下发后会在“调优后”里
    注明“已下发”，但送达/丢包仍是预期值（未复测）。
    颜色（终端才上色，存文件时去掉）：调优前红色（当前状态、有丢包），
    调优后绿色（预期改善）；已达标/无需整形的好状态也是绿色。"""
    lines = ["调优前后对比："]
    if res["status"] == "no_knee":
        lines.append(green("  未检测到限速器，链路干净，无需整形。"))
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
        lines.append(green(f"  当前整形 {cur} Mbit 已达标，无需调整。"))
        return lines
    before = f"整形 {cur} Mbit" if cur is not None else "无整形"
    lines.append(red(f"  调优前（当前）：{before}，实测上限 {knee} Mbit，"
                     f"不限速送达 {unshaped:.1f} Mbps、丢包 {loss:.4f}%"))
    if res.get("shaping_status") == "applied":
        lines.append(green(f"  调优后（已下发整形 {rec} Mbit）："
                           f"送达约 {rec} Mbit，丢包约 0%（不再撞限速器）"))
    else:
        lines.append(green(f"  调优后（建议整形 {rec} Mbit，预期）："
                           f"送达约 {rec} Mbit，丢包约 0%（不再撞限速器）"))
    return lines


def build_report(res, isp_name, peer_label, tx_bytes, rx_bytes, elapsed_s,
                 stack_text=None, version_label=None, base=None, dom_probe=None,
                 dom_skip_reason=None, cap_host=None):
    """生成干净的文字报告。cap_host 是能力点的主机名/IP（报告里展示，
    主机名原样显示，IP 只显示前 3 段脱敏）。"""
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
    cap_line = "能力点：%s" % peer_label
    if cap_host:
        cap_line += "（%s）" % mask_ip(cap_host)
    lines.append(cap_line)
    if res.get("quick"):
        lines.append("模式：快速粗测（每档 5 秒、不细扫，拐点精度较粗）")
    if base:
        lines.append("")
        lines.append("当前系统参数（未调优）：")
        lines.extend(baseline_lines(base))
    lines.append("")
    if res.get("unshaped_mbps") is not None:
        cap_line = "不限速探测：%.1f Mbps，丢包 %.4f%%" % (
            res["unshaped_mbps"], res.get("unshaped_loss", 0))
        if res.get("cap_jitter") is not None:
            cap_line += "，抖动 %.1f ms" % res["cap_jitter"]
        lines.append(cap_line)
    else:
        lines.append("不限速探测：未完成（%s）" % res.get("error", "未知原因"))
    if res.get("knee"):
        lines.append("实测上限：%d Mbit" % res["knee"])
        shape = res.get("shaping_text")
        if shape:
            suffix = "" if res.get("shaping_status") in ("ok", "applied") else "（仅供参考，未下发）"
            lines.append("整形：%s%s" % (shape, suffix))
        else:
            lines.append("建议整形值：%d Mbit（仅供参考，未下发）" % res["recommend"])
    elif res["status"] == "no_knee":
        lines.append("限速器：未检测到")
    if dom_probe is not None:
        lines.append("")
        dom_detail = []
        if dom_probe.get("peer"):
            dom_detail.append(mask_ip(dom_probe["peer"]))
        if dom_probe.get("provider"):
            dom_detail.append(dom_probe["provider"] + " 提供")
        if dom_detail:
            lines.append("到国内路径：%s（%s）" % (dom_probe["label"], "，".join(dom_detail)))
        else:
            lines.append("到国内路径：%s" % dom_probe["label"])
        dom_line = "  不限速送达 %.1f Mbps，丢包 %.4f%%" % (
            dom_probe["mbps"], dom_probe["loss"])
        if dom_probe.get("jitter") is not None:
            dom_line += "，抖动 %.1f ms" % dom_probe["jitter"]
        lines.append(dom_line)
        if dom_probe["loss"] <= LOSS_THRESHOLD:
            lines.append("  链路干净。")
        else:
            lines.append("  丢包偏高，晚高峰可能更差。")
        note = jitter_note(dom_probe.get("jitter"))
        if note:
            lines.append("  " + note)
    elif dom_skip_reason:
        # 国内点没测：报告里也要写明原因，不然拿到报告的人会以为漏测了。
        lines.append("")
        lines.append("国内路径：未测（%s）。" % dom_skip_reason)
    lines.append("")
    lines.append("结论：" + verdict_text(res))
    dom_line = domestic_verdict_line(dom_probe, dom_skip_reason)
    if dom_line:
        lines.append(dom_line)
    if res.get("tuning_applied") or res.get("tuning_failed"):
        lines.append("")
        lines.append("本次调优（v2.0 已下发）：")
        for d in res.get("tuning_applied") or []:
            lines.append("  已应用：" + d)
        for d in res.get("tuning_failed") or []:
            lines.append("  未生效：" + d)
        lines.append("  回滚：python3 vps-test.py --rollback")
    lines.append("")
    lines.extend(tuning_compare_lines(res, base))
    lines.append("")
    lines.append("本次测试消耗流量：出向 %s / 入向 %s / 合计 %s" % (
        human_bytes(tx_bytes), human_bytes(rx_bytes),
        human_bytes(tx_bytes + rx_bytes)))
    lines.append("用时：约 %d 秒" % int(elapsed_s))
    return "\n".join(lines) + "\n"


def save_report(text, res, dom_probe=None):
    """报告存到当前目录；历史记录存 ~/.vps-test/history.json，供下次对比。"""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(os.getcwd(), f"vps-test-report-{stamp}.txt")
    n = 2
    while os.path.exists(path):  # 同一秒内跑两次也不覆盖
        path = os.path.join(os.getcwd(), f"vps-test-report-{stamp}-{n}.txt")
        n += 1
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(_strip_ansi(text))  # 文件里不带 ANSI 颜色码，保持纯文本
        ok("报告已保存：" + path)
    except OSError as exc:
        warn("报告保存失败：" + str(exc))
        return None
    record = {
        "time": stamp,
        "unshaped_mbps": res.get("unshaped_mbps"),
        "unshaped_loss": res.get("unshaped_loss"),
        "cap_jitter": res.get("cap_jitter"),
        "knee": res.get("knee"),
        "recommend": res.get("recommend"),
        "status": res.get("status"),
    }
    if dom_probe is not None:
        record["domestic"] = {
            "label": dom_probe["label"],
            "mbps": dom_probe["mbps"],
            "loss": dom_probe["loss"],
            "provider": dom_probe.get("provider", ""),
            "jitter": dom_probe.get("jitter"),
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


def _base_peer_label(label):
    """去掉标签末尾的“（延迟 X ms）”，得到干净的测速点名。
    兼容老历史记录（v1.0.6 及以前，标签里带延迟）与新记录（干净标签），
    这样按标签比对测速点时，同一台服务器两次延迟不同也不会误判为换了测速点。"""
    if not label:
        return label
    return re.sub(r"（延迟 [\d.]+ ms）$", "", label)


def _dom_compare_text(dom):
    """把一条国内点记录格式化成对比用的一小段：测速点 + 速度 + 丢包。
    dom 是历史记录里的 domestic 字典或本次的 dom_probe，都是
    label/mbps/loss 三个字段；None 表示那次没测。
    标签先去掉末尾的延迟（老记录里带），显示和比对都用干净的测速点名。"""
    if dom is None:
        return "未测"
    mbps = dom.get("mbps")
    if mbps is None:
        return "%s 未测出速度" % _base_peer_label(dom.get("label", "?"))
    loss = dom.get("loss")
    if loss is None:
        loss = 0
    return "%s %.1f Mbps、丢包 %.4f%%" % (_base_peer_label(dom.get("label", "?")), mbps, loss)


def show_history_compare(res, dom_probe=None):
    """跟上次测试比一比。能力点比不限速和拐点；国内点比测速点、速度和丢包。
    国内点每次选的服务器可能不一样（按延迟最低选），换了就注明，
    不然会误以为是同一条路变快/变慢了。老记录里没有 domestic 字段时，
    上次按“未测”显示，不报错。"""
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
    prev_dom = prev.get("domestic") if isinstance(prev.get("domestic"), dict) else None
    if prev_dom is not None or dom_probe is not None:
        note = ""
        if (prev_dom is not None and dom_probe is not None
                and _base_peer_label(prev_dom.get("label")) != _base_peer_label(dom_probe.get("label"))):
            note = "（测速点不同）"
        log("  国内点：上次 %s；这次 %s%s" % (
            _dom_compare_text(prev_dom), _dom_compare_text(dom_probe), note))


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
    parser.add_argument("--ip", choices=["4", "6"],
                        help="手动指定 IP 版本：4 只测 IPv4，6 只测 IPv6"
                             "（默认按延迟自动选，双栈机器一般选 IPv4）。"
                             "注意：国内节点目前只有 IPv4，强制 IPv6 时国内路径跳过")
    parser.add_argument("--yes", action="store_true",
                        help="跳过流量确认和调优确认（无人值守用）")
    parser.add_argument("--rollback", action="store_true",
                        help="回滚本工具下发的调优，恢复下发前状态（不测速）")
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
    if args.peer and not re.fullmatch(r"(?!-)[A-Za-z0-9._:-]+", args.peer):
        parser.error("--peer 只能是主机名或 IP（不能以 - 开头）")
    return args


def main(argv=None):
    args = parse_args(argv)
    print(f"vps-test v{VERSION} —— VPS 验货：测真实带宽、找限速器，测完可下发调优")
    print("（测完问一声才下发调优，默认不改系统配置；回滚用 --rollback）")
    require_root()
    # --rollback：只做回滚，不测速；不用 iperf3，所以放 check_deps() 前面。
    if args.rollback:
        iface = detect_iface()
        if not iface:
            warn("找不到默认路由网卡，无法回滚。")
            return 1
        return 0 if rollback_tuning(iface) else 1
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
    # --ip 手动指定版本：本机没有该版本就直接报错，别等选点时才失败。
    if args.ip == "6" and not has_v6:
        warn("本机没有 IPv6，不能用 --ip 6。")
        return 1
    if args.ip == "4" and not has_v4:
        warn("本机没有 IPv4，不能用 --ip 4。")
        return 1

    # 2. 测速点选择：先自动拉最新国内节点清单（拉不到就用缓存/硬编码）
    peers, peers_source = load_peers()
    if peers_source == "api":
        log("测速节点清单已自动更新（iperf3serverlist.net）。")
    elif peers_source == "cache":
        log("测速节点清单拉取失败，用本地缓存。")
    log()
    log("国内 iperf3 节点覆盖：")
    for line in domestic_coverage_lines(peers):
        log(line)
    log()
    log("选择测速点（ping 延迟低的优先）：")
    log("  能力点：测 VPS 自己的限速器（通用节点，延迟最低）；")
    log("  国内点：测到中国的真实路径（运营商匹配节点，延迟最低）。")
    cap, dom = pick_peers(args.peer, (has_v4, has_v6), isp_name, peers,
                          force_version=args.ip)
    peer, peer_label, version, cap_rtt = cap
    version_label = "IPv4" if version == "4" else "IPv6"
    if args.ip:
        version_label += "（手动指定）"
    cap_rtt_txt = "（延迟 %.1f ms）" % cap_rtt if cap_rtt is not None else ""
    log(f"  能力点：{peer_label}{cap_rtt_txt}（{mask_ip(peer)}）[{version_label}]")
    if dom is not None:
        dom_peer, dom_label, dom_version, dom_rtt = dom
        dom_skip_reason = None
        dom_provider = next((p[3] for p in peers if p[0] == dom_peer), "")
        dom_rtt_txt = "（延迟 %.1f ms）" % dom_rtt if dom_rtt is not None else ""
        dom_label_full = dom_label + dom_rtt_txt + ("（%s 提供）" % dom_provider if dom_provider else "")
        log(f"  国内点：{dom_label_full}（{mask_ip(dom_peer)}）")
    else:
        dom_peer = None
        dom_provider = ""
        # 国内点没有：必须明确告诉使用者原因，不能悄悄跳过。
        # 手动 --peer 是故意只测一个点，不提示；否则分三种情况：
        # 1) 清单里根本没有该运营商的内地节点（移动/联通/广电现在就是这样）；
        # 2) 强制 IPv6，但清单里的国内节点都是 IPv4，没有 IPv6 节点；
        # 3) 有节点但都 ping 不通。
        dom_skip_reason = None
        if not args.peer and isp_name:
            matched = [p for p in peers if p[2] == isp_name]
            if not matched:
                dom_skip_reason = "%s在内地没有公开的 iperf3 测速节点（节点清单里只有电信的 5 个）" % isp_name
            elif args.ip == "6" and not any(
                    "6" in resolve_peer_versions(p[0]) for p in matched):
                dom_skip_reason = "国内节点目前只有 IPv4，没有 IPv6 节点（强制 IPv6 时国内路径跳过）"
            else:
                dom_skip_reason = "%s的国内节点都 ping 不通" % isp_name
            warn("  [!] 国内点未测：%s，国内路径这段跳过，只测能力点（限速器/真实带宽不受影响）。" % dom_skip_reason)

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

    # 4. 拐点扫描（能力点：测 VPS 自己的限速器）
    dur = min(args.dur, 5) if args.quick else args.dur
    if args.quick:
        log("快速模式：每档 5 秒、只粗扫不细扫（拐点精度较粗）。")
    sweep = Sweep(peer, iface, dur=dur, assume_yes=args.yes, version=version,
                  quick=args.quick)
    res = sweep.run()
    tx_bytes, rx_bytes = sweep.traffic_used()
    elapsed = time.time() - started

    # 4a. 能力点抖动：ping 10 次算 RTT 标准差（只报路径稳定性，不参与拐点判定）。
    if res["status"] in ("done", "no_knee"):
        cap_j = ping_stats(peer, count=10, version=version)
        if cap_j is not None:
            res["cap_jitter"] = cap_j[1]
            log(f"  能力点抖动：{cap_j[1]:.1f} ms（10 次 ping，平均延迟 {cap_j[0]:.1f} ms）")

    # 4b. 国内点探测（只做不限速探测，不扫拐点）：测到中国的真实路径。
    # 一个点测不了两件事：能力点的限速器会被到国内的路径瓶颈盖住，
    # 所以国内点只报路径速度和丢包，不参与拐点判定。
    dom_probe = None
    if dom_peer is not None and res["status"] != "cancelled":
        dom_version = dom[2]
        log()
        log(f"国内点探测（{dom_label}，{dur} 秒，单流）：")
        tx0, rx0 = _iface_bytes(iface, "tx"), _iface_bytes(iface, "rx")
        got = probe_unshaped(dom_peer, iface, dur, dom_version)
        tx_bytes += max(0, _iface_bytes(iface, "tx") - tx0)
        rx_bytes += max(0, _iface_bytes(iface, "rx") - rx0)
        elapsed = time.time() - started
        if got is None:
            warn("  国内点探测失败（对端可能太忙）。")
        else:
            dom_gp, dom_loss = got
            dom_probe = {"peer": dom_peer, "label": dom_label,
                         "mbps": dom_gp, "loss": dom_loss,
                         "provider": dom_provider}
            dom_j = ping_stats(dom_peer, count=10, version=dom_version)
            if dom_j is not None:
                dom_probe["jitter"] = dom_j[1]
                log(f"  不限速送达 {dom_gp:.1f} Mbps，丢包 {dom_loss:.4f}%，抖动 {dom_j[1]:.1f} ms")
            else:
                log(f"  不限速送达 {dom_gp:.1f} Mbps，丢包 {dom_loss:.4f}%")

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

    # 4c. v2.0 调优：按实测结果列方案，问一声才下发，默认不改。
    # 达标（plan 为空）就不问；--yes 跳过确认（无人值守）。
    plan = plan_tuning(base, res)
    if plan:
        log()
        log("调优方案（按本次实测）：")
        for line in tuning_plan_lines(plan):
            log(line)
        do_tune = args.yes or confirm("是否应用以上调优？", default=False)
        if do_tune:
            results = apply_tuning(iface, base, plan)
            if results:
                log()
                for desc, ok_flag in results:
                    if ok_flag:
                        ok("已应用：" + desc)
                    else:
                        warn("未生效：" + desc)
                res["tuning_applied"] = [d for d, f in results if f]
                res["tuning_failed"] = [d for d, f in results if not f]
                # 整形已下发：报告里的整形状态改为已下发。
                if any(c["kind"] == "shaping" for c in plan):
                    res["shaping_text"] = f"已下发整形 {res['recommend']} Mbit（本次调优）"
                    res["shaping_status"] = "applied"
            else:
                log("调优未下发（快照保存失败，系统未改动）。")
        else:
            log("已跳过调优（系统未改动）。")

    # 5. 报告
    log()
    log("=" * 60)
    log("结论：" + verdict_text(res))
    dom_line = domestic_verdict_line(dom_probe, dom_skip_reason)
    if dom_line:
        log(dom_line)
    log("=" * 60)
    report = build_report(res, isp_name, peer_label, tx_bytes, rx_bytes, elapsed,
                          stack_text, version_label, base, dom_probe,
                          dom_skip_reason=dom_skip_reason, cap_host=peer)
    log()
    print(report)
    save_report(report, res, dom_probe)
    show_history_compare(res, dom_probe)
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

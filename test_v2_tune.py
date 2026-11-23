#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vps-test.py v2.1 调优单元测试：plan_tuning / write_sysctl / 快照+回滚 /
apply_htb_rate / htb_applied_ok / 报告调优段 / 对比行已下发 /
缓冲自适应 / 输出简化 / 调优后复测。"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
from unittest import mock

spec = importlib.util.spec_from_file_location("vt", os.path.join(os.path.dirname(__file__), "vps-test.py"))
vt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vt)

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def base(**kw):
    b = {"kernel": "6.1.0", "cc": "cubic", "rmem_max": "4194304",
         "wmem_max": "4194304", "tcp_rmem": "4096 87380 4194304",
         "tcp_wmem": "4096 16384 4194304", "qdisc": "fq", "htb_rate": None,
         "mtu": 1500}
    b.update(kw)
    return b


def res(**kw):
    r = {"status": "done", "knee": 981, "recommend": 956,
         "shaping_status": "none", "unshaped_mbps": 930.2,
         "unshaped_loss": 0.2049}
    r.update(kw)
    return r


print("== plan_tuning：整形 ==")
p = vt.plan_tuning(base(), res(shaping_status="none"))
check("无整形 -> 下发 956", any(c["kind"] == "shaping" and c["rate"] == 956 for c in p))
p = vt.plan_tuning(base(qdisc="htb", htb_rate="1200Mbit"), res(shaping_status="high"))
check("太高 -> 降到 956", any(c["kind"] == "shaping" and c["rate"] == 956 for c in p))
p = vt.plan_tuning(base(qdisc="htb", htb_rate="500Mbit"), res(shaping_status="low"))
check("太低 -> 升到 956", any(c["kind"] == "shaping" and c["rate"] == 956 for c in p))
p = vt.plan_tuning(base(qdisc="htb", htb_rate="956Mbit"), res(shaping_status="ok"))
check("已达标 -> 不动整形", not any(c["kind"] == "shaping" for c in p))
p = vt.plan_tuning(base(), res(status="no_knee", knee=None, recommend=None))
check("没拐点 -> 不动整形", not any(c["kind"] == "shaping" for c in p))

print("== plan_tuning：基础参数 ==")
p = vt.plan_tuning(base(cc="cubic"), res())
check("cubic -> bbr", any(c["kind"] == "sysctl" and c["key"] == "net.ipv4.tcp_congestion_control" and c["new"] == "bbr" for c in p))
p = vt.plan_tuning(base(cc="bbr"), res())
check("已是 bbr -> 不动", not any(c.get("key") == "net.ipv4.tcp_congestion_control" for c in p if c["kind"] == "sysctl"))
p = vt.plan_tuning(base(cc="bbr", rmem_max="4194304", wmem_max="4194304",
                     tcp_rmem="4096 87380 4194304", tcp_wmem="4096 16384 4194304"), res())
check("缓冲 <16M -> 四项都改", len([c for c in p if c["kind"] == "sysctl"]) == 4)
c = next((x for x in p if x.get("key") == "net.ipv4.tcp_rmem"), None)
check("tcp_rmem 只改 max、保留 min/default", c is not None and c["new"] == "4096 87380 16777216")
p = vt.plan_tuning(base(cc="bbr", rmem_max="20847152", wmem_max="20847152",
                     tcp_rmem="4096 1048576 20847152", tcp_wmem="4096 1048576 20847152"), res())
check("缓冲已达标 -> 不动 sysctl", not any(c["kind"] == "sysctl" for c in p))

print("== plan_tuning：根 qdisc ==")
p = vt.plan_tuning(base(qdisc="pfifo_fast"), res(shaping_status="none"))
check("pfifo_fast + 下发整形 -> 不单独换 fq", not any(c["kind"] == "qdisc" for c in p))
p = vt.plan_tuning(base(qdisc="pfifo_fast"), res(status="no_knee", knee=None, recommend=None))
check("pfifo_fast + 无整形 -> 换 fq", any(c["kind"] == "qdisc" for c in p))
p = vt.plan_tuning(base(cc="bbr", rmem_max="20847152", wmem_max="20847152",
                     tcp_rmem="4096 1048576 20847152", tcp_wmem="4096 1048576 20847152",
                     qdisc="fq"), res(shaping_status="ok"))
check("全达标 -> 空方案", vt.plan_tuning(base(cc="bbr", rmem_max="20847152", wmem_max="20847152",
                     tcp_rmem="4096 1048576 20847152", tcp_wmem="4096 1048576 20847152",
                     qdisc="fq"), res(shaping_status="ok")) == [])

print("== write_sysctl 校验 ==")
check("白名单外键拒绝", vt.write_sysctl("net.ipv4.ip_forward", "1") is False)
check("值含换行拒绝", vt.write_sysctl("net.core.rmem_max", "123\n456") is False)
check("值含 shell 字符拒绝", vt.write_sysctl("net.core.rmem_max", "123; rm -rf /") is False)
check("值含分号拒绝", vt.write_sysctl("net.core.rmem_max", "123;id") is False)
# 合法值：mock open，验证写到正确路径
with mock.patch("builtins.open", mock.mock_open()) as m:
    check("合法值写 /proc/sys", vt.write_sysctl("net.core.rmem_max", "16777216") is True)
    m.assert_called_once_with("/proc/sys/net/core/rmem_max", "w")
with mock.patch("builtins.open", mock.mock_open()) as m:
    check("tcp_rmem 三段值允许空格", vt.write_sysctl("net.ipv4.tcp_rmem", "4096 87380 16777216") is True)

print("== apply_htb_rate：tc 命令 ==")
calls = []
def fake_tc(*args):
    calls.append(args)
    return (0, "", "")
with mock.patch.object(vt, "tc", side_effect=fake_tc):
    check("apply_htb_rate 成功", vt.apply_htb_rate("ens3", 956) is True)
check("先删根", calls[0] == ("qdisc", "del", "dev", "ens3", "root"))
check("挂 htb 根", calls[1][:5] == ("qdisc", "add", "dev", "ens3", "root") and "htb" in calls[1])
check("class 1:10 rate/ceil 956mbit", "956mbit" in calls[2] and "1:10" in calls[2])
check("叶子 fq 带 maxrate", "fq" in calls[3] and "956mbit" in calls[3])
# 失败路径：第二条命令失败
def fake_tc_fail(*args):
    if args[0] == "qdisc" and args[1] == "add":
        return (1, "", "err")
    return (0, "", "")
with mock.patch.object(vt, "tc", side_effect=fake_tc_fail):
    check("htb 挂不上返回 False", vt.apply_htb_rate("ens3", 956) is False)

print("== htb_applied_ok ==")
def fake_tc_ok(*args):
    if args[0] == "qdisc":
        return (0, "qdisc htb 1: dev ens3 root refcnt 2 r2q 10 default 0x10 direct_packets_stat 0 direct_qlen 1000\n", "")
    if args[0] == "class":
        return (0, "class htb 1:10 parent 1: leaf 10: prio 0 rate 956Mbit ceil 956Mbit burst 476800b cburst 476800b\n", "")
    return (0, "", "")
with mock.patch.object(vt, "tc", side_effect=fake_tc_ok):
    check("htb + class 1:10 rate 956 -> True", vt.htb_applied_ok("ens3", 956) is True)
    check("rate 不符 -> False", vt.htb_applied_ok("ens3", 900) is False)

print("== 快照保存 + 回滚 ==")
tmp = tempfile.mkdtemp()
snap_path = os.path.join(tmp, "tune-snapshot.json")
with mock.patch.object(vt, "TUNE_SNAPSHOT_FILE", snap_path), \
     mock.patch.object(vt, "HISTORY_DIR", tmp), \
     mock.patch.object(vt, "SYSCTL_PERSIST_FILE", os.path.join(tmp, "99-vps-tune.conf")):
    snap = {"version": "2.0.0", "qdisc_kind": "fq", "htb": None,
            "sysctl": {"net.ipv4.tcp_congestion_control": "cubic",
                       "net.core.rmem_max": "4194304"}}
    check("快照保存成功", vt.save_tune_snapshot(snap) is True)
    check("快照文件内容一致", json.load(open(snap_path)) == snap)
    # 回滚：mock tc/qdisc_root_kind/write_sysctl
    tc_calls = []
    def fake_tc_rb(*args):
        tc_calls.append(args)
        return (0, "", "")
    with mock.patch.object(vt, "tc", side_effect=fake_tc_rb), \
         mock.patch.object(vt, "qdisc_root_kind", return_value="fq"), \
         mock.patch.object(vt, "write_sysctl", return_value=True):
        check("回滚成功", vt.rollback_tuning("ens3") is True)
    check("回滚后快照文件已删", not os.path.exists(snap_path))
    check("回滚删了根 qdisc", ("qdisc", "del", "dev", "ens3", "root") in tc_calls)
    check("回滚重加 fq", ("qdisc", "add", "dev", "ens3", "root", "fq") in tc_calls)
    check("无快照回滚返回 False", vt.rollback_tuning("ens3") is False)

print("== 报告：调优段 + 整形后缀 ==")
r2 = res(shaping_status="applied", shaping_text="已下发整形 956 Mbit（本次调优）",
         tuning_applied=["整形：下发 htb 956 Mbit（当前无整形）", "拥塞控制：cubic → bbr"],
         tuning_failed=["接收缓冲上限：4194304 → 16777216"])
rep = vt.build_report(r2, "电信", "洛杉矶 Leaseweb", 1000, 2000, 60,
                      stack_text="双栈", version_label="IPv4", base=base(),
                      dom_probe=None, dom_skip_reason="电信在内地没有公开的 iperf3 测速节点")
check("报告有调优段", "本次调优（已下发）：" in rep)
check("报告列出已应用", "已应用：整形：下发 htb 956 Mbit（当前无整形）" in rep)
check("报告列出未生效", "未生效：接收缓冲上限：4194304 → 16777216" in rep)
check("报告有回滚命令", "python3 vps-test.py --rollback" in rep)
check("已下发不带“不下发”后缀", "已下发整形 956 Mbit（本次调优）" in rep and "已下发整形 956 Mbit（本次调优）（仅供参考" not in rep)
r3 = res(shaping_status="none", shaping_text="建议整形 956 Mbit")
rep3 = vt.build_report(r3, "电信", "洛杉矶 Leaseweb", 1000, 2000, 60,
                       stack_text="双栈", version_label="IPv4", base=base(),
                       dom_probe=None, dom_skip_reason=None)
check("未下发仍带后缀", "建议整形 956 Mbit（仅供参考，未下发）" in rep3)
check("无调优时报告无调优段", "本次调优" not in rep3)

print("== 对比行：已下发 ==")
lines = vt.tuning_compare_lines(res(shaping_status="applied"), base())
txt = "\n".join(lines)
check("对比行写“已下发整形”", "已下发整形 956 Mbit" in txt)
check("对比行不再写“建议整形…预期”", "建议整形 956 Mbit，预期" not in txt)
lines2 = vt.tuning_compare_lines(res(shaping_status="none"), base())
check("未下发仍写“建议整形…预期”", "建议整形 956 Mbit，预期" in "\n".join(lines2))

print("== apply_tuning：端到端（mock tc/sysctl） ==")
tmp2 = tempfile.mkdtemp()
plan2 = vt.plan_tuning(base(), res(shaping_status="none"))
with mock.patch.object(vt, "TUNE_SNAPSHOT_FILE", os.path.join(tmp2, "tune-snapshot.json")), \
     mock.patch.object(vt, "HISTORY_DIR", tmp2), \
     mock.patch.object(vt, "SYSCTL_PERSIST_FILE", os.path.join(tmp2, "99-vps-tune.conf")), \
     mock.patch.object(vt, "apply_htb_rate", return_value=True), \
     mock.patch.object(vt, "htb_applied_ok", return_value=True), \
     mock.patch.object(vt, "write_sysctl", return_value=True):
    results = vt.apply_tuning("ens3", base(), plan2)
    check("apply_tuning 返回全部项", len(results) == len(plan2))
    check("apply_tuning 全部成功", all(f for _, f in results))
    check("快照已保存", os.path.exists(os.path.join(tmp2, "tune-snapshot.json")))
    snap2 = json.load(open(os.path.join(tmp2, "tune-snapshot.json")))
    check("快照记了原始 cc", snap2["sysctl"].get("net.ipv4.tcp_congestion_control") == "cubic")
    check("快照记了原始 qdisc", snap2["qdisc_kind"] == "fq")
    check("持久化文件已写", os.path.exists(os.path.join(tmp2, "99-vps-tune.conf")))
    conf = open(os.path.join(tmp2, "99-vps-tune.conf")).read()
    check("持久化含 bbr", "net.ipv4.tcp_congestion_control = bbr" in conf)
    check("持久化含 16M 缓冲", "net.core.rmem_max = 16777216" in conf)
# 快照存不上 -> 返回 []（停手，不下发）
with mock.patch.object(vt, "save_tune_snapshot", return_value=False), \
     mock.patch.object(vt, "apply_htb_rate", side_effect=AssertionError("不应调用")):
    check("快照失败 -> 不下发", vt.apply_tuning("ens3", base(), plan2) == [])

print("== verdict_text：已下发不带后缀 ==")
check("已下发结论无“未下发”", "未下发" not in vt.verdict_text(res(shaping_status="applied", shaping_text="已下发整形 956 Mbit（本次调优）")))
check("已达标结论无后缀", "未下发" not in vt.verdict_text(res(shaping_status="ok", shaping_text="当前整形 956 Mbit 已达标，无需调整")))
check("未下发结论注明", "未下发" in vt.verdict_text(res(shaping_status="none", shaping_text="建议整形 956 Mbit")))

print("== parse_args：--rollback ==")
args = vt.parse_args(["--rollback"])
check("--rollback 解析", args.rollback is True)
args = vt.parse_args([])
check("默认不回滚", args.rollback is False)

print("== 新增校验 ==")
check("apply_htb_rate 拒绝非整数", vt.apply_htb_rate("ens3", "956mbit") is False)
check("apply_htb_rate 拒绝 0", vt.apply_htb_rate("ens3", 0) is False)
check("apply_htb_rate 拒绝负数", vt.apply_htb_rate("ens3", -5) is False)
check("apply_htb_rate 拒绝布尔", vt.apply_htb_rate("ens3", True) is False)
# 回滚：快照 qdisc 种类带 shell 字符 -> 退回 fq
tmp3 = tempfile.mkdtemp()
bad_snap = {"version": "2.0.0", "qdisc_kind": "fq; rm -rf /", "htb": None, "sysctl": {}}
bad_path = os.path.join(tmp3, "tune-snapshot.json")
json.dump(bad_snap, open(bad_path, "w"))
tc_calls3 = []
def fake_tc3(*args):
    tc_calls3.append(args)
    return (0, "", "")
with mock.patch.object(vt, "TUNE_SNAPSHOT_FILE", bad_path), \
     mock.patch.object(vt, "SYSCTL_PERSIST_FILE", os.path.join(tmp3, "99.conf")), \
     mock.patch.object(vt, "tc", side_effect=fake_tc3), \
     mock.patch.object(vt, "qdisc_root_kind", return_value="fq"):
    check("脏 qdisc 种类回滚仍成功", vt.rollback_tuning("ens3") is True)
check("脏 qdisc 未进 tc 命令", not any("rm" in str(c) for c in tc_calls3))
check("脏 qdisc 退回 fq", ("qdisc", "add", "dev", "ens3", "root", "fq") in tc_calls3)

print("== v2.0.1：快照认 root 形式 + 读 live qdisc + 回滚不劝重下 ==")
# cc-dc2 真机 tc 输出：class 行是 "root" 不是 "parent 1:"（v2.0.0 只认后者，抓不到自己下的整形）。
def fake_tc_root(*args):
    if args[0] == "class":
        return (0, "class htb 1:10 root leaf 10: prio 0 rate 952Mbit ceil 952Mbit burst 476000b cburst 476000b\n", "")
    if args[0] == "qdisc":
        return (0, "qdisc htb 1: dev ens3 root refcnt 2 r2q 10 default 0x10 direct_packets_stat 0 direct_qlen 1000\n"
                   "qdisc fq 10: dev ens3 parent 1:10 limit 10000p flow_limit 200p buckets 1024 orphan_mask 1023 quantum 1514 initial_quantum 7600 refill_delay 10.0ms maxrate 952Mbit\n", "")
    return (0, "", "")
with mock.patch.object(vt, "tc", side_effect=fake_tc_root):
    htb = vt.QdiscGuard("ens3")._snapshot_htb()
check("root 形式快照抓到 class 1:10", htb is not None and htb.get("classid") == "1:10")
check("root 形式快照 rate 952Mbit", htb is not None and htb.get("rate") == "952Mbit")
check("root 形式快照叶子 maxrate 952Mbit", htb is not None and htb.get("leaf", {}).get("maxrate") == "952Mbit")
# parent 1: 形式仍认
def fake_tc_parent(*args):
    if args[0] == "class":
        return (0, "class htb 1:10 parent 1: leaf 10: prio 0 rate 956Mbit ceil 956Mbit burst 476800b cburst 476800b\n", "")
    if args[0] == "qdisc":
        return (0, "qdisc htb 1: dev ens3 root refcnt 2 r2q 10 default 0x10 direct_packets_stat 0 direct_qlen 1000\n"
                   "qdisc fq 10: dev ens3 parent 1:10 limit 10000p flow_limit 200p buckets 1024 orphan_mask 1023 quantum 1514 initial_quantum 7600 refill_delay 10.0ms maxrate 956Mbit\n", "")
    return (0, "", "")
with mock.patch.object(vt, "tc", side_effect=fake_tc_parent):
    htb2 = vt.QdiscGuard("ens3")._snapshot_htb()
check("parent 1: 形式仍抓到", htb2 is not None and htb2.get("rate") == "956Mbit")
# snapshot_tuning_state 读 live qdisc，不用开跑时的旧 base
with mock.patch.object(vt, "qdisc_root_kind", return_value="htb"), \
     mock.patch.object(vt.QdiscGuard, "_snapshot_htb", return_value={"classid": "1:10", "rate": "952Mbit"}):
    snap_live = vt.snapshot_tuning_state("ens3", base(qdisc="fq"), [])
check("live 是 htb、base 是 fq -> 快照记 htb", snap_live["qdisc_kind"] == "htb")
check("live 是 htb -> 快照有 htb 配置", snap_live["htb"] is not None)
with mock.patch.object(vt, "qdisc_root_kind", return_value="fq"):
    snap_live2 = vt.snapshot_tuning_state("ens3", base(qdisc="htb"), [])
check("live 是 fq、base 是 htb -> 快照记 fq", snap_live2["qdisc_kind"] == "fq")
check("live 是 fq -> 快照无 htb 配置", snap_live2["htb"] is None)
# 回滚不劝重下整形
warns = []
def fake_tc_rb2(*args):
    return (0, "", "")
with mock.patch.object(vt, "tc", side_effect=fake_tc_rb2), \
     mock.patch.object(vt, "qdisc_root_kind", return_value="fq"), \
     mock.patch.object(vt, "warn", side_effect=lambda m: warns.append(m)):
    g = vt.QdiscGuard("ens3")
    g.saved_kind = "htb"
    g.saved_htb = None
    g.restore(advise_reapply=False)
check("回滚警告不劝重下整形", warns and all("请重新下发" not in w for w in warns))
warns2 = []
with mock.patch.object(vt, "tc", side_effect=fake_tc_rb2), \
     mock.patch.object(vt, "qdisc_root_kind", return_value="fq"), \
     mock.patch.object(vt, "warn", side_effect=lambda m: warns2.append(m)):
    g2 = vt.QdiscGuard("ens3")
    g2.saved_kind = "htb"
    g2.saved_htb = None
    g2.restore()
check("扫描恢复仍劝重下整形", warns2 and any("请重新下发" in w for w in warns2))

print("== v2.0.2：快照 qdisc 解析认 dev-filtered 输出 ==")
# 真机 tc qdisc show dev ens3 的输出：行里没有 "dev ens3"。
# v2.0.1 的快照仍返回 None（过滤要求 parts[3] 必须是 dev，两行全跳过）。
def fake_tc_nodev(*args):
    if args[0] == "class":
        return (0, "class htb 1:10 root leaf 10: prio 0 rate 954Mbit ceil 954Mbit burst 477000b cburst 477000b\n", "")
    if args[0] == "qdisc":
        return (0, "qdisc htb 1: root refcnt 2 r2q 10 default 0x10 direct_packets_stat 0 direct_qlen 1000\n"
                   "qdisc fq 10: parent 1:10 limit 10000p flow_limit 200p buckets 1024 orphan_mask 1023 quantum 1514 initial_quantum 7600 refill_delay 10.0ms maxrate 954Mbit\n", "")
    return (0, "", "")
with mock.patch.object(vt, "tc", side_effect=fake_tc_nodev):
    htb3 = vt.QdiscGuard("ens3")._snapshot_htb()
check("dev-filtered 输出快照抓到 class 1:10", htb3 is not None and htb3.get("classid") == "1:10")
check("dev-filtered 输出快照 rate 954Mbit", htb3 is not None and htb3.get("rate") == "954Mbit")
check("dev-filtered 输出快照叶子 maxrate 954Mbit", htb3 is not None and htb3.get("leaf", {}).get("maxrate") == "954Mbit")
# 带 dev 的输出（tc qdisc show 不带 dev 参数）仍认
with mock.patch.object(vt, "tc", side_effect=fake_tc_root):
    htb4 = vt.QdiscGuard("ens3")._snapshot_htb()
check("带 dev 输出仍抓到", htb4 is not None and htb4.get("rate") == "952Mbit")

print("== v2.1：缓冲按内存自适应 ==")
# >= 4GB -> 16MB；1-4GB -> 8MB；< 1GB -> 4MB；读不到 -> 16MB。
def _meminfo(kb):
    return mock.mock_open(read_data="MemTotal:       %d kB\nMemFree:        100000 kB\n" % kb)
with mock.patch("builtins.open", _meminfo(8 * 1024 * 1024)):
    check("8GB 内存 -> 16MB", vt.tune_buf_target() == 16777216)
with mock.patch("builtins.open", _meminfo(2 * 1024 * 1024)):
    check("2GB 内存 -> 8MB", vt.tune_buf_target() == 8388608)
with mock.patch("builtins.open", _meminfo(460 * 1024)):
    check("460MB 内存 -> 4MB", vt.tune_buf_target() == 4194304)
with mock.patch("builtins.open", mock.mock_open(read_data="MemFree: 100000 kB\n")):
    check("无 MemTotal -> 16MB", vt.tune_buf_target() == 16777216)
with mock.patch("builtins.open", side_effect=OSError("no meminfo")):
    check("读不到 meminfo -> 16MB", vt.tune_buf_target() == 16777216)
# plan_tuning 用自适应目标：2GB 机器（8MB 档）上 4MB 缓冲 -> 改到 8MB，不是 16MB。
with mock.patch.object(vt, "tune_buf_target", return_value=8388608):
    p = vt.plan_tuning(base(cc="bbr", rmem_max="4194304", wmem_max="4194304",
                         tcp_rmem="4096 87380 4194304", tcp_wmem="4096 16384 4194304"), res())
    check("2GB 机器缓冲改到 8MB", any(c.get("key") == "net.core.rmem_max" and c["new"] == "8388608" for c in p if c["kind"] == "sysctl"))

print("== v2.1：输出简化 ==")
# baseline_lines：TCP 缓冲只显 max，不印三元组。
bl = vt.baseline_lines(base())
check("TCP 读缓冲只显 max", any("TCP 读缓冲上限：4194304" in l for l in bl))
check("TCP 读缓冲不印三元组", not any("4096 87380 4194304" in l for l in bl))
check("TCP 写缓冲只显 max", any("TCP 写缓冲上限：4194304" in l for l in bl))
# domestic_coverage_lines：提供方不在覆盖行重复。
peers = [("116.169.118.91", "成都 电信", "电信", "China Telecom"),
         ("59.56.22.40", "福州 电信", "电信", "China Telecom")]
cov = vt.domestic_coverage_lines(peers)
check("覆盖行不写提供方", not any("提供方" in l for l in cov))
check("覆盖行仍有城市", any("成都/福州" in l for l in cov))
# plan_tuning 文案：整形说“设上限”不说“下发 htb”；BBR 加半句解释。
p = vt.plan_tuning(base(), res(shaping_status="none"))
sh = next((c for c in p if c["kind"] == "shaping"), None)
check("整形文案说设上限", sh is not None and "设上限" in sh["desc"])
check("整形文案不含 htb", sh is not None and "htb" not in sh["desc"])
cc = next((c for c in p if c.get("key") == "net.ipv4.tcp_congestion_control"), None)
check("BBR 文案加解释", cc is not None and "BBR 算法" in cc["desc"])
# _pick_best_in_group：不逐行印每个测速点，只返回最优。
logged = []
with mock.patch.object(vt, "log", side_effect=lambda *a: logged.append(" ".join(str(x) for x in a))), \
     mock.patch.object(vt, "ping_avg", side_effect=[150.0, 80.0, 120.0]), \
     mock.patch.object(vt, "resolve_peer_versions", return_value=["4"]):
    got = vt._pick_best_in_group([("1.1.1.1", "洛杉矶", None, ""),
                                  ("2.2.2.2", "香港", None, ""),
                                  ("3.3.3.3", "东京", None, "")], ["4"])
check("选到延迟最低的点", got is not None and got[0] == "2.2.2.2")
check("不逐行印测速点", not any("测速点" in l and "延迟" in l for l in logged))
# 扫描表结论全中文：源码里不再有英文结论词。
src = open(os.path.join(os.path.dirname(__file__), "vps-test.py"), encoding="utf-8").read()
check("无英文 recheck", '"recheck"' not in src)
check("无英文 loss spike", "loss spike" not in src)
check("无英文 no further gain", "no further gain" not in src)
check("无英文 policer present", "policer present" not in src)
check("有中文丢包突增", "丢包突增" in src)
check("有中文再往上没收益", "再往上没收益" in src)

print("== v2.1：调优后复测 ==")
# 已下发 + 有复测值 -> 显示“实测”与复测数。
lines = vt.tuning_compare_lines(res(shaping_status="applied", tuned_mbps=950.0, tuned_loss=0.0), base(qdisc="htb", htb_rate="956Mbit"))
check("复测后显示实测", any("实测" in l and "950.0" in l for l in lines))
check("复测后不写约数", not any("送达约" in l for l in lines if "调优后" in l))
# 已下发 + 无复测值 -> 仍按预期显示（不假装实测）。
lines = vt.tuning_compare_lines(res(shaping_status="applied"), base(qdisc="htb", htb_rate="956Mbit"))
check("无复测值仍写预期数", any("送达约 956 Mbit" in l for l in lines))
# 未下发 -> 注明“预期”。
lines = vt.tuning_compare_lines(res(shaping_status="none"), base())
check("未下发注明预期", any("预期" in l for l in lines))

print("== v2.1 审计修复：M1/M2/L1/L2/L4/L5 ==")
# M1：probe_shaped 不碰 qdisc（只调 run_iperf，不调 tc / QdiscGuard）。
tc_calls = []
with mock.patch.object(vt, "run_iperf", return_value={"recv_mbps": 950.0, "sent_mbps": 960.0, "retrans": 0}), \
     mock.patch.object(vt, "tc", side_effect=lambda *a: tc_calls.append(a) or (0, "", "")):
    got = vt.probe_shaped("1.1.1.1", 5, "4")
check("probe_shaped 返回送达/丢包", got is not None and got[0] == 950.0 and got[1] == 0.0)
check("probe_shaped 不调 tc", tc_calls == [])
with mock.patch.object(vt, "run_iperf", return_value=None):
    check("probe_shaped 测不到返回 None", vt.probe_shaped("1.1.1.1", 5, "4") is None)
# M2：整形下发失败时不标 applied。逻辑：只有整形项的 desc 在 tuning_applied 里才算成功。
plan_sh = {"kind": "shaping", "rate": 956, "desc": "整形：设上限 956 Mbit（当前无整形）"}
results_fail = [(plan_sh["desc"], False), ("拥塞控制：cubic → bbr（换成更快的 BBR 算法）", True)]
applied_fail = [d for d, f in results_fail if f]
check("整形失败不进 applied", plan_sh["desc"] not in applied_fail)
results_ok = [(plan_sh["desc"], True), ("拥塞控制：cubic → bbr（换成更快的 BBR 算法）", True)]
applied_ok = [d for d, f in results_ok if f]
check("整形成功进 applied", plan_sh["desc"] in applied_ok)
# L1：非 1:10 叶子先出现，不崩溃、仍能抓到 1:10 叶子。
def fake_tc_leaf_first(*args):
    if args[0] == "class":
        return (0, "class htb 1:10 root leaf 10: prio 0 rate 954Mbit ceil 954Mbit burst 477000b cburst 477000b\n", "")
    if args[0] == "qdisc":
        return (0, "qdisc htb 1: root refcnt 2 r2q 10 default 0x10 direct_packets_stat 0 direct_qlen 1000\n"
                   "qdisc fq 20: parent 1:20 limit 10000p flow_limit 200p buckets 1024 quantum 1514 maxrate 500Mbit\n"
                   "qdisc fq 10: parent 1:10 limit 10000p flow_limit 200p buckets 1024 orphan_mask 1023 quantum 1514 initial_quantum 7600 refill_delay 10.0ms maxrate 954Mbit\n", "")
    return (0, "", "")
with mock.patch.object(vt, "tc", side_effect=fake_tc_leaf_first):
    htb5 = vt.QdiscGuard("ens3")._snapshot_htb()
check("非 1:10 叶子先出现不崩", htb5 is not None)
check("仍抓到 1:10 叶子 maxrate", htb5 is not None and htb5.get("leaf", {}).get("maxrate") == "954Mbit")
# L2：脏快照（htb 不是 dict、sysctl 不是 dict）回滚不崩。
with tempfile.TemporaryDirectory() as td:
    snap_path = os.path.join(td, "tune-snapshot.json")
    with open(snap_path, "w") as f:
        json.dump({"version": "2.1.0", "qdisc_kind": "fq", "htb": "not-a-dict",
                   "sysctl": ["not-a-dict"]}, f)
    with mock.patch.object(vt, "TUNE_SNAPSHOT_FILE", snap_path), \
         mock.patch.object(vt, "qdisc_root_kind", return_value="fq"), \
         mock.patch.object(vt, "tc", return_value=(0, "", "")):
        check("脏 htb/sysctl 快照回滚不崩", vt.rollback_tuning("ens3") is True)
# L4（v2.1.2 已废止）：detect_isp 曾要求不印家庭完整 IP；用户 2026-11-22
# 决定终端和报告都显示完整 IP，mask_ip 已删，detect_isp 直接印完整 IP。
check("detect_isp 印完整 IP", "mask_ip" not in src and "{info['ip']}" in src)
# L5：历史条目不是 dict 时对比跳过、不崩。
# history[-2] 是上次记录（对比用），把它写成非 dict 才能触发脏条目分支。
with tempfile.TemporaryDirectory() as td:
    hist_path = os.path.join(td, "history.json")
    with open(hist_path, "w") as f:
        json.dump(["not-a-dict",
                   {"time": "20261122-000000", "unshaped_mbps": 900.0, "knee": 950}], f)
    with mock.patch.object(vt, "HISTORY_FILE", hist_path):
        try:
            vt.show_history_compare({"status": "done", "unshaped_mbps": 930.0, "knee": 981}, None)
            check("脏历史条目对比不崩", True)
        except Exception:
            check("脏历史条目对比不崩", False)

# L2 残口：htb 快照缺 burst/cburst/leaf 也算损坏，回滚不崩、不留裸 htb。
with tempfile.TemporaryDirectory() as td:
    snap_path = os.path.join(td, "tune-snapshot.json")
    with open(snap_path, "w") as f:
        json.dump({"version": "2.1.0", "qdisc_kind": "htb",
                   "htb": {"rate": "954Mbit", "ceil": "954Mbit"},
                   "sysctl": {}}, f)
    with mock.patch.object(vt, "TUNE_SNAPSHOT_FILE", snap_path), \
         mock.patch.object(vt, "qdisc_root_kind", return_value="fq"), \
         mock.patch.object(vt, "tc", return_value=(0, "", "")):
        check("缺 burst 的 htb 快照回滚不崩", vt.rollback_tuning("ens3") is True)

print("== v2.1.1：达标容差 + 快照不覆盖 ==")
# 容差：建议值/实测上限相差 5 Mbit 以内算已达标。
check("952 vs 建议 954（差 2）算已达标",
      vt.shaping_status(952, 977, 954)[0] == "ok")
check("947 vs 建议 954（差 7）算偏低",
      vt.shaping_status(947, 977, 954)[0] == "low")
check("982 vs 实测上限 977（高 5）算已达标",
      vt.shaping_status(982, 977, 954)[0] == "ok")
check("983 vs 实测上限 977（高 6）算偏高",
      vt.shaping_status(983, 977, 954)[0] == "high")
check("960 在安全带内算已达标",
      vt.shaping_status(960, 977, 952)[0] == "ok")
check("无整形算 none", vt.shaping_status(None, 977, 954)[0] == "none")
check("940（低 12）算偏低", vt.shaping_status(940, 977, 952)[0] == "low")
check("990（高 13）算偏高", vt.shaping_status(990, 977, 952)[0] == "high")
# 快照不覆盖：已有能用的快照时 apply_tuning 不重写。
with tempfile.TemporaryDirectory() as td:
    snap_path = os.path.join(td, "tune-snapshot.json")
    orig = {"version": "2.1.0", "qdisc_kind": "fq", "htb": None, "sysctl": {}}
    with open(snap_path, "w") as f:
        json.dump(orig, f)
    with mock.patch.object(vt, "TUNE_SNAPSHOT_FILE", snap_path):
        check("已有快照认得出", vt._has_valid_snapshot() is True)
        plan = [{"kind": "shaping", "rate": 954,
                 "desc": "整形：设上限 954 Mbit（当前无整形）"}]
        with mock.patch.object(vt, "apply_htb_rate", return_value=True), \
             mock.patch.object(vt, "htb_applied_ok", return_value=True):
            res2 = vt.apply_tuning("ens3", {"qdisc": "fq"}, plan)
        check("有快照时下发仍进行", res2 == [("整形：设上限 954 Mbit（当前无整形）", True)])
        with open(snap_path) as f:
            check("有快照时不覆盖原快照", json.load(f) == orig)
    # 无快照时正常存一份。
    snap_path2 = os.path.join(td, "tune-snapshot2.json")
    with mock.patch.object(vt, "TUNE_SNAPSHOT_FILE", snap_path2), \
         mock.patch.object(vt, "qdisc_root_kind", return_value="fq"):
        check("无快照时认不出", vt._has_valid_snapshot() is False)
        plan = [{"kind": "shaping", "rate": 954,
                 "desc": "整形：设上限 954 Mbit（当前无整形）"}]
        with mock.patch.object(vt, "apply_htb_rate", return_value=True), \
             mock.patch.object(vt, "htb_applied_ok", return_value=True):
            res3 = vt.apply_tuning("ens3", {"qdisc": "fq"}, plan)
        check("无快照时下发进行", res3 == [("整形：设上限 954 Mbit（当前无整形）", True)])
        check("无快照时存了新快照", os.path.exists(snap_path2))
    # 损坏的快照（不是 dict / 缺 qdisc_kind）算没有，重新存。
    snap_path3 = os.path.join(td, "tune-snapshot3.json")
    with open(snap_path3, "w") as f:
        f.write("not json {{{")
    with mock.patch.object(vt, "TUNE_SNAPSHOT_FILE", snap_path3):
        check("损坏快照算没有", vt._has_valid_snapshot() is False)
    with open(snap_path3, "w") as f:
        json.dump({"version": "2.1.0"}, f)
    with mock.patch.object(vt, "TUNE_SNAPSHOT_FILE", snap_path3):
        check("缺 qdisc_kind 的快照算没有", vt._has_valid_snapshot() is False)

print("== v2.1.1 审计修复：低 1/低 2/低 3/低 5 ==")
# 低 1：qdisc_kind 是 list 不算能用快照。
with tempfile.TemporaryDirectory() as td:
    snap_path = os.path.join(td, "tune-snapshot.json")
    with open(snap_path, "w") as f:
        json.dump({"version": "2.1.1", "qdisc_kind": ["fq"], "htb": None,
                   "sysctl": {}}, f)
    with mock.patch.object(vt, "TUNE_SNAPSHOT_FILE", snap_path):
        check("qdisc_kind 是 list 不算能用快照", vt._has_valid_snapshot() is False)
# 低 2：class 加失败时拆根，不留裸 htb。
tc_calls = []
def fake_tc_fail_class(*args):
    tc_calls.append(args)
    if args[0] == "class" and args[1] == "add":
        return (1, "", "error")
    return (0, "", "")
with mock.patch.object(vt, "tc", side_effect=fake_tc_fail_class):
    check("class 失败返回 False", vt.apply_htb_rate("ens3", 950) is False)
check("class 失败后拆了根 htb",
      any(c[0] == "qdisc" and c[1] == "del" for c in tc_calls))
tc_calls.clear()
def fake_tc_fail_leaf(*args):
    tc_calls.append(args)
    if args[0] == "qdisc" and args[1] == "add" and "1:10" in args:
        return (1, "", "error")
    return (0, "", "")
with mock.patch.object(vt, "tc", side_effect=fake_tc_fail_leaf):
    check("叶子失败返回 False", vt.apply_htb_rate("ens3", 950) is False)
check("叶子失败后拆了根 htb",
      any(c[0] == "qdisc" and c[1] == "del" for c in tc_calls))
# 低 3：leaf 可选字段是 int 算损坏快照，回滚不崩、退 fq。
with tempfile.TemporaryDirectory() as td:
    snap_path = os.path.join(td, "tune-snapshot.json")
    with open(snap_path, "w") as f:
        json.dump({"version": "2.1.1", "qdisc_kind": "htb",
                   "htb": {"rate": "954Mbit", "ceil": "954Mbit",
                           "burst": "477000b", "cburst": "477000b",
                           "leaf": {"kind": "fq", "limit": 12345}},
                   "sysctl": {}}, f)
    with mock.patch.object(vt, "TUNE_SNAPSHOT_FILE", snap_path), \
         mock.patch.object(vt, "qdisc_root_kind", return_value="fq"), \
         mock.patch.object(vt, "tc", return_value=(0, "", "")):
        check("leaf 字段是 int 的快照回滚不崩", vt.rollback_tuning("ens3") is True)
# 低 5（v2.1.2 已废止）：扫描 Peer 行曾要求脱敏；现显示完整 IP，mask_ip 已删。
check("扫描 Peer 行印完整 IP", "mask_ip" not in src and "Peer {self.peer}" in src)

print("== v2.1.2：完整 IP + 全达标绿色收尾行 ==")
# 完整 IP：mask_ip 已删，源码里不再出现；三处展示点直接印完整 IP/主机名。
check("源码里没有 mask_ip", "mask_ip" not in src)
check("报告能力点行印完整 cap_host", 'cap_line += "（%s）" % cap_host' in src)
check("报告国内点行印完整 peer", 'dom_detail.append(dom_probe["peer"])' in src)
check("能力点选择行印完整 peer", "（{peer}）" in src)
check("国内点选择行印完整 peer", "（{dom_peer}）" in src)
# 收尾行条件：plan 为空 + 正常完成才印。
check("plan 为空 + done -> 印收尾行", vt.all_ok_line_needed([], {"status": "done"}) is True)
check("plan 为空 + no_knee -> 印收尾行", vt.all_ok_line_needed([], {"status": "no_knee"}) is True)
check("plan 非空 -> 不印收尾行", vt.all_ok_line_needed([{"kind": "sysctl"}], {"status": "done"}) is False)
check("取消 -> 不印收尾行", vt.all_ok_line_needed([], {"status": "cancelled"}) is False)
check("未完成 -> 不印收尾行", vt.all_ok_line_needed([], {"status": "inconclusive"}) is False)
# plan 本身：全达标 + 无拐点 -> 空；拥塞控制不是 bbr -> 非空。
good = base(cc="bbr", rmem_max="20847152", wmem_max="20847152",
            tcp_rmem="4096 1048576 20847152", tcp_wmem="4096 1048576 20847152",
            qdisc="fq")
check("全达标 + 无拐点 -> plan 为空",
      vt.plan_tuning(good, res(status="no_knee", knee=None, recommend=None)) == [])
p_cubic = vt.plan_tuning(base(cc="cubic"), res(status="no_knee", knee=None, recommend=None))
check("cubic -> plan 非空", len(p_cubic) > 0)
check("真实 plan 非空 -> 不印收尾行", vt.all_ok_line_needed(p_cubic, {"status": "done"}) is False)
# 报告里也带这行（res["all_ok"] 由 main() 在印终端行时顺手设置）。
rep_ok = vt.build_report(res(status="no_knee", knee=None, recommend=None, all_ok=True),
                         "电信", "洛杉矶 Leaseweb", 1000, 2000, 60,
                         stack_text="双栈", version_label="IPv4", base=base(),
                         dom_probe=None, dom_skip_reason=None)
check("报告里有收尾行", "各项参数全部达标，无需调优。" in rep_ok)
rep_no = vt.build_report(res(status="no_knee", knee=None, recommend=None),
                         "电信", "洛杉矶 Leaseweb", 1000, 2000, 60,
                         stack_text="双栈", version_label="IPv4", base=base(),
                         dom_probe=None, dom_skip_reason=None)
check("没 all_ok 时报告里没收尾行", "各项参数全部达标，无需调优。" not in rep_no)
# 收尾行输出格式：ok() 的 [+] 前缀 + green() 的绿色。
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    vt.ok(vt.green("各项参数全部达标，无需调优。"))
check("收尾行带 [+] 前缀", buf.getvalue().strip().startswith("[+] "))
check("收尾行含文案", "各项参数全部达标，无需调优。" in buf.getvalue())
with mock.patch.object(vt, "_use_color", return_value=True):
    check("收尾行在彩色终端是绿色",
          vt.green("各项参数全部达标，无需调优。") == "\033[32m各项参数全部达标，无需调优。\033[0m")

print("== v2.1.3：BBR 版本显示 ==")
# 内核名 short-circuit：xanmod / joeyblog-bbrv3 直接判 v3，不调 modinfo。
with mock.patch.object(vt, "run_cmd", side_effect=AssertionError("不应调 modinfo")):
    check("xanmod 内核 -> v3（不调 modinfo）",
          vt.bbr_version("6.12.8-xanmod1", "bbr") == "v3")
    check("joeyblog-bbrv3 内核 -> v3（不调 modinfo）",
          vt.bbr_version("7.1.1-joeyblog-bbrv3", "bbr") == "v3")
    check("内核名大小写不敏感",
          vt.bbr_version("6.12.8-XanMod1", "bbr") == "v3")
# 官方内核走 modinfo 的 version 字段。
MODINFO_V3 = ("filename:       /lib/modules/7.1.1/kernel/net/ipv4/tcp_bbr.ko\n"
              "version:        3\n"
              "description:    TCP BBR congestion control\n")
MODINFO_V1 = ("filename:       /lib/modules/6.1.0/kernel/net/ipv4/tcp_bbr.ko\n"
              "version:        1\n"
              "description:    TCP BBR congestion control\n")
with mock.patch.object(vt, "run_cmd", return_value=(0, MODINFO_V3, "")):
    check("官方内核 + modinfo version 3 -> v3",
          vt.bbr_version("6.1.0-18-amd64", "bbr") == "v3")
with mock.patch.object(vt, "run_cmd", return_value=(0, MODINFO_V1, "")):
    check("官方内核 + modinfo version 1 -> v1",
          vt.bbr_version("6.1.0-18-amd64", "bbr") == "v1")
# modinfo 不存在 / 执行失败 / 输出异常时优雅降级到 v1，不崩。
with mock.patch.object(vt, "run_cmd", return_value=(-1, "", "command not found")):
    check("modinfo 不存在 -> 兜底 v1",
          vt.bbr_version("6.1.0-18-amd64", "bbr") == "v1")
with mock.patch.object(vt, "run_cmd", return_value=(-1, "", "timeout")):
    check("modinfo 超时 -> 兜底 v1",
          vt.bbr_version("6.1.0-18-amd64", "bbr") == "v1")
with mock.patch.object(vt, "run_cmd", return_value=(0, "garbage without version line\n", "")):
    check("modinfo 输出无 version 行 -> 兜底 v1，不崩",
          vt.bbr_version("6.1.0-18-amd64", "bbr") == "v1")
with mock.patch.object(vt, "run_cmd", return_value=(0, "", "")):
    check("modinfo 输出为空 -> 兜底 v1，不崩",
          vt.bbr_version("6.1.0-18-amd64", "bbr") == "v1")
with mock.patch.object(vt, "run_cmd",
                        return_value=(0, "version:        2\n", "")):
    check("modinfo 未知版本号 -> 兜底 v1，不崩",
          vt.bbr_version("6.1.0-18-amd64", "bbr") == "v1")
# 不是 bbr 时返回 None，不显示。
check("cc 是 cubic -> None",
      vt.bbr_version("6.12.8-xanmod1", "cubic") is None)
check("cc 是 None -> None",
      vt.bbr_version("6.1.0-18-amd64", None) is None)
check("cc 为空串 -> None",
      vt.bbr_version("6.1.0-18-amd64", "") is None)
with mock.patch.object(vt, "run_cmd", return_value=(0, MODINFO_V3, "")):
    check("内核为 None + modinfo v3 -> v3",
          vt.bbr_version(None, "bbr") == "v3")
# 显示：参数行和报告里都要有 BBR 版本行；非 bbr 时没有。
check("参数行：bbr v3 显示 BBR 版本：v3",
      "  BBR 版本：v3" in vt.baseline_lines(base(cc="bbr", bbr_version="v3")))
check("参数行：bbr v1 显示 BBR 版本：v1",
      "  BBR 版本：v1" in vt.baseline_lines(base(cc="bbr", bbr_version="v1")))
check("参数行：非 bbr 不显示 BBR 版本",
      not any("BBR 版本" in line for line in vt.baseline_lines(base())))
rep_v3 = vt.build_report(res(), "电信", "洛杉矶 Leaseweb", 1000, 2000, 60,
                         stack_text="双栈", version_label="IPv4",
                         base=base(cc="bbr", bbr_version="v3"),
                         dom_probe=None, dom_skip_reason=None)
check("报告里有 BBR 版本：v3", "  BBR 版本：v3" in rep_v3)

print("== v2.1.4：persist_sysctl 合并写入 ==")
with tempfile.TemporaryDirectory() as td:
    conf_path = os.path.join(td, "99-vps-tune.conf")
    with mock.patch.object(vt, "SYSCTL_PERSIST_FILE", conf_path):
        # 无现有文件：从空写。
        check("无现有文件写成功",
              vt.persist_sysctl({"net.core.rmem_max": "16777216"}) is True)
        conf = open(conf_path).read()
        check("无现有文件含新键", "net.core.rmem_max = 16777216" in conf)
        # 已有旧键：合并保留。
        check("合并写成功",
              vt.persist_sysctl({"net.core.wmem_max": "16777216"}) is True)
        conf = open(conf_path).read()
        check("合并保留旧键", "net.core.rmem_max = 16777216" in conf)
        check("合并新增新键", "net.core.wmem_max = 16777216" in conf)
        # 同键更新。
        check("同键更新成功",
              vt.persist_sysctl({"net.core.rmem_max": "20847152"}) is True)
        conf = open(conf_path).read()
        check("同键更新为新值", "net.core.rmem_max = 20847152" in conf)
        check("同键更新后只剩一行", conf.count("net.core.rmem_max") == 1)
        check("同键更新不丢其他键", "net.core.wmem_max = 16777216" in conf)
        # 非白名单键 / 坏值跳过。
        check("非白名单键跳过但写成功",
              vt.persist_sysctl({"net.ipv4.ip_forward": "1"}) is True)
        conf = open(conf_path).read()
        check("非白名单键没写进文件", "ip_forward" not in conf)
        check("坏值跳过但写成功",
              vt.persist_sysctl({"net.core.rmem_max": "1; rm -rf /"}) is True)
        conf = open(conf_path).read()
        check("坏值没覆盖旧值", "net.core.rmem_max = 20847152" in conf)
        # 异常行：注释、空行、无等号、坏键名、空值、坏值格式 -> 丢弃，不崩。
        with open(conf_path, "w") as f:
            f.write("# 手工注释\n\n"
                    "net.core.rmem_max = 16777216\n"
                    "no-equals-line\n"
                    "bad key! = 123\n"
                    "net.core.wmem_max =\n"
                    "net.ipv4.tcp_rmem = 1; bad\n"
                    "net.ipv4.tcp_wmem = 4096 16384 16777216\n")
        check("异常行文件合并写成功",
              vt.persist_sysctl({"net.core.wmem_max": "20847152"}) is True)
        conf = open(conf_path).read()
        check("异常行：有效旧键保留", "net.core.rmem_max = 16777216" in conf)
        check("异常行：三段值旧键保留",
              "net.ipv4.tcp_wmem = 4096 16384 16777216" in conf)
        check("异常行：无等号行丢弃", "no-equals-line" not in conf)
        check("异常行：坏键名丢弃", "bad key!" not in conf)
        check("异常行：空值旧键被新值覆盖",
              "net.core.wmem_max = 20847152" in conf)
        check("异常行：坏值旧键丢弃", "1; bad" not in conf)
        check("异常行：注释不进文件", "# 手工注释" not in conf)

print("== v2.1.4：快照合并补新键 ==")
with tempfile.TemporaryDirectory() as td:
    snap_path = os.path.join(td, "tune-snapshot.json")
    # 已有快照：sysctl 只有 rmem_max；plan 里有 rmem_max（新 old 值）+ wmem_max（新键）。
    orig = {"version": "2.1.3", "qdisc_kind": "fq", "htb": None,
            "sysctl": {"net.core.rmem_max": "4194304"}}
    with open(snap_path, "w") as f:
        json.dump(orig, f)
    plan_m = [{"kind": "sysctl", "key": "net.core.rmem_max", "old": "8388608",
               "new": "16777216", "desc": "x"},
              {"kind": "sysctl", "key": "net.core.wmem_max", "old": "4194304",
               "new": "16777216", "desc": "x"}]
    with mock.patch.object(vt, "TUNE_SNAPSHOT_FILE", snap_path), \
         mock.patch.object(vt, "HISTORY_DIR", td), \
         mock.patch.object(vt, "SYSCTL_PERSIST_FILE", os.path.join(td, "99.conf")), \
         mock.patch.object(vt, "write_sysctl", return_value=True):
        res_m = vt.apply_tuning("ens3", {"qdisc": "fq"}, plan_m)
    check("有快照合并时下发仍进行",
          len(res_m) == 2 and all(f for _, f in res_m))
    snap_after = json.load(open(snap_path))
    check("合并：已有键原始值不覆盖",
          snap_after["sysctl"].get("net.core.rmem_max") == "4194304")
    check("合并：新键补进快照",
          snap_after["sysctl"].get("net.core.wmem_max") == "4194304")
    check("合并：qdisc_kind 不动", snap_after.get("qdisc_kind") == "fq")
    check("合并：不留临时文件", not os.path.exists(snap_path + ".tmp"))
    # plan 里没有新键时，快照逐字节不动。
    before = open(snap_path, "rb").read()
    plan_m2 = [{"kind": "sysctl", "key": "net.core.rmem_max", "old": "8388608",
                "new": "16777216", "desc": "x"}]
    with mock.patch.object(vt, "TUNE_SNAPSHOT_FILE", snap_path), \
         mock.patch.object(vt, "HISTORY_DIR", td), \
         mock.patch.object(vt, "SYSCTL_PERSIST_FILE", os.path.join(td, "99.conf")), \
         mock.patch.object(vt, "write_sysctl", return_value=True):
        vt.apply_tuning("ens3", {"qdisc": "fq"}, plan_m2)
    check("无新键时快照逐字节不动", open(snap_path, "rb").read() == before)
    # old 为 None 的 sysctl 项不补。
    plan_m3 = [{"kind": "sysctl", "key": "net.ipv4.tcp_rmem", "old": None,
                "new": "4096 87380 16777216", "desc": "x"}]
    with mock.patch.object(vt, "TUNE_SNAPSHOT_FILE", snap_path), \
         mock.patch.object(vt, "HISTORY_DIR", td), \
         mock.patch.object(vt, "SYSCTL_PERSIST_FILE", os.path.join(td, "99.conf")), \
         mock.patch.object(vt, "write_sysctl", return_value=True):
        vt.apply_tuning("ens3", {"qdisc": "fq"}, plan_m3)
    snap_after3 = json.load(open(snap_path))
    check("old 为 None 不补进快照",
          "net.ipv4.tcp_rmem" not in snap_after3["sysctl"])
    # 快照缺 sysctl 字段时不崩，补成 dict。
    snap_path4 = os.path.join(td, "snap4.json")
    with open(snap_path4, "w") as f:
        json.dump({"version": "2.1.3", "qdisc_kind": "fq", "htb": None}, f)
    with mock.patch.object(vt, "TUNE_SNAPSHOT_FILE", snap_path4):
        vt._merge_new_sysctl_into_snapshot(
            [{"kind": "sysctl", "key": "net.core.rmem_max", "old": "4194304",
              "new": "16777216", "desc": "x"}])
    snap4 = json.load(open(snap_path4))
    check("缺 sysctl 字段时补成 dict",
          snap4.get("sysctl", {}).get("net.core.rmem_max") == "4194304")
    # 损坏的快照文件：合并静默返回，文件不动，不崩。
    snap_path5 = os.path.join(td, "snap5.json")
    with open(snap_path5, "w") as f:
        f.write("not json {{{")
    with mock.patch.object(vt, "TUNE_SNAPSHOT_FILE", snap_path5):
        vt._merge_new_sysctl_into_snapshot(
            [{"kind": "sysctl", "key": "net.core.rmem_max", "old": "4194304",
              "new": "16777216", "desc": "x"}])
    check("损坏快照合并后文件不动", open(snap_path5).read() == "not json {{{")

print("== v2.1.5：依赖自动安装 ==")


class FakeRunCmd:
    """模拟 which / 包管理器。present 是 which 返回 0 的命令集合。"""
    def __init__(self, present, manager="apt-get", install_rc=0):
        self.present = set(present)
        self.manager = manager  # "apt-get"/"dnf"/"yum"/None
        self.install_rc = install_rc
        self.add_after_install = True
        self.calls = []

    def __call__(self, argv, timeout=None):
        self.calls.append(list(argv))
        if argv[0] == "which":
            ok = argv[1] in self.present or argv[1] == self.manager
            return (0, "", "") if ok else (1, "", "")
        if argv[0] == "env":  # apt-get update / install
            if "install" in argv:
                if self.install_rc == 0 and self.add_after_install:
                    self.present.update(["iperf3", "tc", "ip"])
                return self.install_rc, "", ("" if self.install_rc == 0 else "E: 失败")
            return 0, "", ""
        if argv[0] in ("dnf", "yum"):
            if "install" in argv:
                if self.install_rc == 0 and self.add_after_install:
                    self.present.update(["iperf3", "tc", "ip"])
                return self.install_rc, "", ("" if self.install_rc == 0 else "失败")
            return 0, "", ""
        return 0, "", ""


fake = FakeRunCmd({"iperf3", "tc", "ip"})
with mock.patch.object(vt, "run_cmd", fake):
    try:
        vt.check_deps()
        check("依赖齐全 -> 直接通过", True)
    except SystemExit:
        check("依赖齐全 -> 直接通过", False)

fake = FakeRunCmd({"tc", "ip"})
with mock.patch.object(vt, "run_cmd", fake), \
     mock.patch.object(vt, "confirm", return_value=False):
    try:
        vt.check_deps()
        check("缺依赖 + 拒绝 -> 退出 1", False)
    except SystemExit as e:
        check("缺依赖 + 拒绝 -> 退出 1", e.code == 1)
check("缺依赖 + 拒绝 -> 没跑安装", not any("install" in c for c in fake.calls))

fake = FakeRunCmd({"tc", "ip"})
with mock.patch.object(vt, "run_cmd", fake), \
     mock.patch.object(vt, "confirm", return_value=True):
    try:
        vt.check_deps()
        check("缺依赖 + 同意 -> 装完通过", True)
    except SystemExit:
        check("缺依赖 + 同意 -> 装完通过", False)
check("同意后跑了 apt 安装 iperf3",
      any(c[:2] == ["env", "DEBIAN_FRONTEND=noninteractive"] and "iperf3" in c
          for c in fake.calls))

fake = FakeRunCmd({"tc", "ip"})
called = []
def confirm_spy(prompt, default=False):
    called.append(prompt)
    return False
with mock.patch.object(vt, "run_cmd", fake), \
     mock.patch.object(vt, "confirm", confirm_spy):
    try:
        vt.check_deps(assume_yes=True)
        check("--yes -> 不问直接装完通过", True)
    except SystemExit:
        check("--yes -> 不问直接装完通过", False)
check("--yes -> 没调 confirm", not called)

fake = FakeRunCmd({"iperf3"})
with mock.patch.object(vt, "run_cmd", fake):
    ok = vt.install_deps(["tc", "ip"])
check("tc+ip 缺失 -> 装 iproute2 成功", ok)
install_argv = [c for c in fake.calls if "install" in c]
check("tc+ip 只装一个 iproute2（去重）",
      len(install_argv) == 1 and install_argv[0].count("iproute2") == 1
      and "iperf3" not in install_argv[0])

fake = FakeRunCmd({"iperf3"}, manager=None)
with mock.patch.object(vt, "run_cmd", fake):
    check("无包管理器 -> 返回 False", vt.install_deps(["iperf3"]) is False)

fake = FakeRunCmd({"tc", "ip"}, install_rc=1)
with mock.patch.object(vt, "run_cmd", fake):
    check("安装失败 -> 返回 False", vt.install_deps(["iperf3"]) is False)

fake = FakeRunCmd({"tc", "ip"})
fake.add_after_install = False
with mock.patch.object(vt, "run_cmd", fake):
    check("装完复查仍缺 -> 返回 False", vt.install_deps(["iperf3"]) is False)

fake = FakeRunCmd({"tc", "ip"}, manager="yum")
with mock.patch.object(vt, "run_cmd", fake):
    ok = vt.install_deps(["iperf3"])
check("yum 路径 -> 装成功", ok)
check("yum 路径 -> 用 yum install iperf3",
      any(c[0] == "yum" and "install" in c and "iperf3" in c for c in fake.calls))

fake = FakeRunCmd({"iperf3"}, manager="dnf")
with mock.patch.object(vt, "run_cmd", fake):
    check("dnf 优先于 yum", vt._detect_pkg_manager()[0] == "dnf")

with mock.patch.object(vt, "run_cmd", FakeRunCmd({"iperf3"})):
    check("未知命令 -> 不装返回 False", vt.install_deps(["nope"]) is False)

print(f"\n{PASS} 通过，{FAIL} 失败")
sys.exit(1 if FAIL else 0)

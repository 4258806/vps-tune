# vps-tune

VPS 验货工具：测真实带宽、找限速器、给人话结论。给国内买 VPS 的人用。

一键测出这台 VPS 的真实带宽、有没有限速器、限速器在哪、链路干不干净，最后给一句人话结论，帮你验货、避坑。

## v1.0.0：只做“测”，不做“调”

- **运营商识别**：自动从 SSH 连接取你的源 IP，查运营商后自动选择；检测到梯子（数据中心 IP）会警告并让你手动选。手动菜单：1 电信 / 2 移动 / 3 联通 / 4 广电。
- **带宽与限速器**：不限速探测 + 多速率点拐点扫描，找出真实上限和限速器位置。
- **人话报告**：结论 + 流量消耗 + 用时，保存为文本报告，并与上次测试对比。
- **晚高峰提醒**：白天跑的只代表白天，建议晚上 19:00–23:00 再跑一次。

## 用法

在 VPS 上以 root 身份运行：

```bash
curl -fsSL https://raw.githubusercontent.com/4258806/vps-tune/main/vps-test.py -o vps-test.py
python3 vps-test.py
```

常用选项：

```bash
python3 vps-test.py --isp 1         # 跳过识别，直接指定运营商
python3 vps-test.py --peer <地址>   # 手动指定 iperf3 测速点
python3 vps-test.py --yes           # 跳过流量确认（无人值守用）
python3 vps-test.py --keep          # 跑完不删除脚本和历史记录
```

依赖：Python 3.7+ 标准库、iperf3（只当客户端）、tc（iproute2）。

```bash
apt install -y iperf3   # Debian/Ubuntu
yum install -y iperf3   # RHEL/CentOS
```

## 注意

- **流量**：拐点扫描会消耗流量，大带宽机器一次可能扫掉几十 GB。扫之前会先报预计流量，确认了再扫；无终端时默认取消。
- **自动清除**：跑完默认自动删除脚本自身和历史记录，只留报告文件。想多跑几次（比如晚高峰复测）请加 `--keep`。
- **不改系统配置**：只在扫描期间临时挂测试整形器，跑完立即恢复原状（已有 htb 整形会原样恢复）。
- **测速点**：v1.0.0 默认用 Leaseweb 公共节点（洛杉矶、东京），国内测速节点正在调研中。

## 许可

MIT License。

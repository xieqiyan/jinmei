# 透明协议中转设备

`relay/` 是 CKL、XTL、ZZW 三类设备的透明中转程序。每个 NS 只连接自己的
Relay（组 A 为 `10.88.0.101`，其他组使用 `.102~.104`）。NS 发往协议设备的
IPv4 包必须以本组 Relay 地址为目的地址，Relay 根据 payload 选择设备，只改
目的 IP；设备回包再改目的 IP 为 NS。源 IP、端口和 payload 均保持不变。

## 路由规则

| 报文 | 判断字段 | 设备 |
| --- | --- | --- |
| 查询 | `0a 01 01` | 本组 CKL（组 A 为 `10.89.1.11`） |
| 查询 | `0a 01 02` | 本组 XTL（组 A 为 `10.89.2.11`） |
| 查询 | `0a 01 04` | 本组 ZZW（组 A 为 `10.89.3.11`） |
| 短加注 | `01 01` | CKL |
| 短加注 | `01 02` | XTL |
| 短加注 | `01 03` | ZZW |

Relay 在 NS 侧和三张协议子网均使用 `AF_PACKET` 捕获 IPv4，并在对应链路上
发送二层以太网帧：

```text
NS -> Relay NS IP -> 原始包(源 IP=NS、目的 IP=设备) -> 设备
设备响应/ACK/拓扑 -> Relay 协议 IP -> 原始包(源 IP=设备、目的 IP=NS) -> NS
```

因此设备现有参数服务仍会看到真实 NS 源 IP，并会继续将它写入
`businessIp`。查询 `0A 01 01/02/04` 和短加注 `01 01/02/03` 之外的未知
协议包会被丢弃并记录日志，不会误投到其他设备。UDP/TCP 改址后会重新计算
校验和。

设备侧发送使用协议桥上的二层广播帧，不依赖 Relay 与设备之间的动态 ARP；
拓扑脚本会把对应协议桥 MAC 写入每台设备的静态邻居表，保证设备回包可以
到达 Relay。

## 网络前提

每个 NS 组使用一个独立的 Relay 进程和三个设备侧接口。NS 地址统一规划在
`10.88.0.0/24`，但每个 NS 使用独立二层链路和 `/32` 地址；每个 Relay
使用该规划中唯一的地址，不需要额外的
`.253` 下一跳。例如组 A 为：

- NS 网段：`10.88.0.0/24`，Relay NS 侧：`10.88.0.101`，NS：`10.88.0.1`；
- CKL 子网：`10.89.1.0/24`，本组 Relay 入口：`10.89.1.101`，CKL：`10.89.1.11`；
- XTL 子网：`10.89.2.0/24`，本组 Relay 入口：`10.89.2.101`，XTL：`10.89.2.11`；
- ZZW 子网：`10.89.3.0/24`，本组 Relay 入口：`10.89.3.101`，ZZW：`10.89.3.11`。

组 B/C/D 使用 `.102/.103/.104` 的 Relay NS 侧地址和 `.2/.3/.4` 的 NS 地址。
每个协议设备只接入自己
类型的子网，设备回包直接发往本组 Relay 在该类型子网中的入口地址（如组 A
为 `.101`），由 Relay 转发到 NS。

NS 侧每个组使用独立的 `br-<组>-ns` 链路，不存在共享 `br-ns`，因此 NS
不会通过共享交换机直接绕过 Relay。三张协议桥仍分别承载同协议设备。Relay 进程需要
`CAP_NET_RAW`，网络初始化需要 `CAP_NET_ADMIN` 或 root。

初始化接口和路由：

```bash
sudo RELAY_IP=10.88.0.101/24 \
  DEVICE_IFS=br-ckl,br-xtl,br-zzw NS_IF=br-A-ns \
  bash relay/setup_network.sh
```

启动：

```bash
sudo RELAY_CONFIG=relay/relay_config.json relay/run_relay.sh
```

可以先验证配置和协议分类，不打开原始 socket：

```bash
python3 relay/relay.py --dry-run
```

## 接口和路由要求

设备侧需要把 NS 业务网段的下一跳指向 Relay，或者通过设备侧二层网络
发送到 Relay。NS 侧请求统一发往本组 Relay NS 地址的 `:10001`，NS 侧不直接访问设备地址。Relay
按 payload 的协议类型选择本组 CKL/XTL/ZZW 接口和设备。

主机上必须关闭反向路径过滤，否则保留原始源 IP 的报文可能被内核丢弃：

```text
net.ipv4.ip_forward=1
net.ipv4.conf.all.rp_filter=0
net.ipv4.conf.br-ckl.rp_filter=0
net.ipv4.conf.br-xtl.rp_filter=0
net.ipv4.conf.br-zzw.rp_filter=0
net.ipv4.conf.br-A-ns.rp_filter=0
```

## 配置

默认配置位于 `relay_config.json`：

```json
{
  "relay": {
    "listenIp": "10.88.0.101",
    "nsCidr": "10.88.0.0/24",
    "devices": {
      "ckl": "10.89.1.11",
      "xtl": "10.89.2.11",
      "zzw": "10.89.3.11"
    },
    "deviceInterfaces": {
      "ckl": "br-ckl",
      "xtl": "br-xtl",
      "zzw": "br-zzw"
    },
    "deviceListenIps": {
      "ckl": "10.89.1.101",
      "xtl": "10.89.2.101",
      "zzw": "10.89.3.101"
    }
  }
}
```

`nsCidr` 是安全边界。Relay 只接受源地址等于配置 `nsIp`、目的地址等于
配置 `listenIp` 的 IPv4 包；协议字段无法识别或长度校验失败的包会丢弃。
设备侧只接受配置设备源地址并将其回包改址到本组 NS。

## 测试

```bash
python3 -m pytest -q relay/tests/test_relay.py
```

测试覆盖协议分类、短报文长度检查、原始 IPv4/UDP 地址保留、校验和和
默认路由配置。真实环境还需要验证两个接口、策略路由、OVS/namespace
以及设备回包路径，不能仅用本机回环测试代替。

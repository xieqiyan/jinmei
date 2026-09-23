# UDP 中转网关

Relay 是按 IP/UDP 端点工作的业务中转网关。它不抓取网卡、不构造 Ethernet
帧、不修改收到的 IPv4 目的地址，也不依赖固定的 `eth-up`、`eth-ns`
接口名。

## 业务流程

```
NS -- UDP/10001 --> Relay(NS IP)
Relay -- UDP/10001 --> device IP
device -- UDP/10009 或 8882 --> Relay(device-side IP)
Relay -- UDP/10009 或 8882 --> NS IP
```

Relay 根据 payload 中的协议类型选择 CKL、XTL 或 ZZW。发往设备时，外层
IP 源地址是 Relay 在该协议子网中的地址，外层目的地址是设备地址，MAC
由操作系统根据正常路由/ARP 处理。

## NS IP 封装字段

由于设备看到的外层源 IP 是 Relay 地址，Relay 在发往设备的 UDP payload
前加 `RLY1` 封装头。封装头包含：

| 字段 | 含义 |
| --- | --- |
| magic/version | `RLY1`、版本 1 |
| device | CKL/XTL/ZZW 编号 |
| nsIp | 原始 NS IPv4 地址 |
| replyPort | 回 NS 的端口，查询为 10009，ACK/拓扑为 8882 |
| requestPort | 原始请求端口 |
| payloadLen | 后续原始协议 payload 长度 |

协议参数服务收到封装后先解包，再使用 `nsIp` 作为 requester/businessIp；
响应和 ACK 带上相同的 `nsIp` 封装发回 Relay。Relay 解包后把原始 payload
发送给对应 NS。

没有封装头的报文仍按旧的直连兼容模式处理。

## 配置

生产 Relay 只需要 IP 和端口，不需要网卡名称：

```json
{
  "relay": {
    "listenIp": "10.88.0.101",
    "nsCidr": "10.88.0.0/24",
    "nsIp": "10.88.0.1",
    "allowedNsIps": ["10.88.0.1"],
    "requestPort": 10001,
    "responsePort": 10009,
    "eventPort": 8882,
    "devices": {
      "ckl": "10.89.1.11",
      "xtl": "10.89.2.11",
      "zzw": "10.89.3.11"
    },
    "deviceListenIps": {
      "ckl": "10.89.1.101",
      "xtl": "10.89.2.101",
      "zzw": "10.89.3.101"
    },
    "logFile": "/tmp/radio-relay-A.log"
  }
}
```

旧配置中的 `deviceInterfaces`、`nsInterface` 会被忽略，仅为测试拓扑兼容
保留。Relay 通过绑定 IP 的 UDP socket 工作，上行和下行接口可以使用任意名称。

启动：

```bash
RELAY_CONFIG=/etc/radio/relay-A.json relay/run_relay.sh
```

校验配置：

```bash
python3 relay/relay.py --config /etc/radio/relay-A.json --dry-run
```

## 安全边界

- NS 请求源地址必须属于 `nsCidr` 和 `allowedNsIps`，且不能是 Relay 自身地址。
- 设备回包必须来自配置的对应设备 IP。
- Relay 只接受 CKL/XTL/ZZW 已知类型和合法长度。
- 每个 NS、Relay、协议设备和 Relay 协议侧地址必须唯一。
- 拓扑脚本只是 namespace/容器测试工具，不是 Relay 的运行时依赖。

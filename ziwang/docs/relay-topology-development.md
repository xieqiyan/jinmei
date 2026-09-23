# Relay 拓扑改造开发文档

## 1. 目标

拓扑包含四个 NS 业务节点。每个 NS 节点配套一个独立 Relay、一个 CKL
设备、一个 XTL 设备和一个 ZZW 设备。一个 NS 组最多三个协议设备，且三台
设备的协议类型不能重复。

NS 是业务通信主体。NS 到协议设备的业务必须先到本组 Relay；NS 不直接
访问设备地址，也不能使用其他 NS 的 Relay。

## 2. 地址规划

NS 使用同一地址规划但每个 NS 只接入自己的二层链路（容器内使用 `/32`），
NS 到协议设备的业务统一发往本组 Relay，不存在共享 NS 交换机：

| 组 | NS | Relay 的 NS 侧 IP |
| --- | --- | --- |
| A | 10.88.0.1 | 10.88.0.101 |
| B | 10.88.0.2 | 10.88.0.102 |
| C | 10.88.0.3 | 10.88.0.103 |
| D | 10.88.0.4 | 10.88.0.104 |

协议设备按协议类型组成三张全局子网：

| 子网 | 组 A 设备 | 组 B 设备 | 组 C 设备 | 组 D 设备 |
| --- | --- | --- | --- | --- |
| CKL 10.89.1.0/24 | .11 | .12 | .13 | .14 |
| XTL 10.89.2.0/24 | .11 | .12 | .13 | .14 |
| ZZW 10.89.3.0/24 | .11 | .12 | .13 | .14 |

每个 Relay 在三张协议子网中使用与 NS 组对应的 `.101~.104` 地址。例如
组 A 的 CKL Relay 地址为 `10.89.1.101`，组 A 的 CKL 设备地址为
`10.89.1.11`。地址不复用，不使用额外的 `10.88.0.253` 下一跳。

## 3. 拓扑结构

```text
        nsA .1 -- br-A-ns -- relay-A .101
        nsB .2 -- br-B-ns -- relay-B .102
        nsC .3 -- br-C-ns -- relay-C .103
        nsD .4 -- br-D-ns -- relay-D .104
                                  |  |  |
             br-ckl: 10.89.1.0/24  br-xtl: 10.89.2.0/24  br-zzw: 10.89.3.0/24
             CKL devices .11~.14   XTL devices .11~.14   ZZW devices .11~.14
```

设备容器内的 `br0` 使用设备管理地址；配置文件中的 `businessIp` 使用
对应 NS 地址（例如组 A 的三台设备都写 `10.88.0.1`）。这样现有 daemon
仍以 NS 地址建立业务白名单，协议设备的管理地址只用于本协议子网的 UDP
参数业务。

## 4. 请求和回包路径

NS 查询/加注只发往自己的 Relay NS 侧 IP，例如 nsA 发往
`10.88.0.101:10001`。Relay 要求 UDP 源地址必须等于配置中的 `nsIp`，
因此 nsB 发往 Relay-A 会被丢弃。

Relay 按 payload 选择设备：

- 查询 `0A 01 01/02/04` 选择 CKL/XTL/ZZW；
- 短加注 `01 01/02/03` 选择 CKL/XTL/ZZW。

Relay 通过对应协议接口发送原始 IPv4/UDP 包，保留 NS 源 IP。设备收到
请求后仍把请求源 IP作为 `businessIp` 语义；查询响应发往 NS IP，ACK 和
拓扑上报发往设备所属 Relay 的协议子网 IP。Relay 只接受本组设备源地址，
并且只接受目标为本组 NS IP 或本组 Relay 协议接口 IP 的响应，之后通过
NS 侧接口转发给本组 NS。

## 5. 隔离规则

1. 每个 NS 只接入自己的 `br-<组>-ns`，不存在共享 `br-ns`。
2. NS namespace 不配置 CKL/XTL/ZZW 子网路由，不能直接访问协议设备。
3. Relay 请求入口按精确 `nsIp` 和 `listenIp` 校验，其他 NS 不能借用该 Relay。
4. Relay 回包按本组设备地址校验，其他组设备的报文被丢弃。
5. 协议 daemon 的 OVS 流表继续使用 `businessIp` 白名单，只放行已匹配的
   业务流；组网广播仍使用 `0x88B5` 管理流。

## 6. 代码变更范围

- `scripts/setup_topology.sh`：创建四条独立 NS-Relay 链路、三张协议子网、12 个设备容器
  和 4 个 Relay 进程。
  脚本使用 `TOPOLOGY_GROUPS=(A B C D)`，避免与 Bash 内置只读数组
  `GROUPS` 冲突；启动结束会校验 12 个容器、4 个 namespace 和 4 个 Relay。
- `relay/relay.py`：改为 NS/设备两侧 AF_PACKET 捕获，精确校验 NS 源/目的，
  按 payload 选择三类协议设备，改写 IPv4 地址并重算 UDP/TCP 校验和；设备
  侧通过二层广播发送，避免依赖未就绪的 ARP 邻居。
- `scripts/setup_topology.sh`：通过 `docker exec -i` 执行容器初始化，并把协议
  桥 MAC 写入设备静态邻居表，保证设备回包能到达 Relay。
- `relay/relay_config.json`：改为组 A 默认配置，字段与拓扑脚本生成的组配置
  一致。
- `scripts/tests/test_common.sh`：测试目标改为本组 Relay IP，并增加跨 Relay
  隔离验证。
- `relay/tests/test_relay.py`：验证新的地址和接口配置。

## 7. 验证命令

```bash
bash -n scripts/setup_topology.sh
python3 -m pytest -q relay/tests/test_relay.py
sudo bash scripts/setup_topology.sh
sudo ip netns exec nsA python3 tools/radio_param_client.py query 10.88.0.101 ckl --summary
```

跨 Relay 访问应失败：

```bash
sudo ip netns exec nsA python3 tools/radio_param_client.py query 10.88.0.102 ckl --timeout 2
```

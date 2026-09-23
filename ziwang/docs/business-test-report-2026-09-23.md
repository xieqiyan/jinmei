# 业务实测报告（2026-09-23）

## 结论

当前管理类业务（NS 经本组 Relay 查询/加注协议设备、设备回传拓扑）在运行中的四组拓扑里实测通过。协议层一开始确实存在参数问题：CKL、XTL、ZZW 的 4 个节点最初全部 `hostPrime=1`，peer 状态为 `MULTI_MASTER`。将 A 设为唯一主节点、B/C/D 设为从节点后，所有协议 peer 收敛为 `OK`，每个协议节点出现 12 条 `priority=200` 白名单流表。

但这没有解决 NS 之间的 ping。当前 4 个 NS 的 12 个有向互 ping 全部失败，且每个失败都由本地路由表直接返回 `Network is unreachable`。这不是继续调 `hostPrime` 或协议匹配参数能解决的问题：NS 只配置了到本组 Relay 的单个 `/32` 路由；Relay 是处理查询、加注、拓扑 UDP 的应用层端点，不转发 ICMP/IP 数据包。NS 间经协议设备互 ping 是尚未实现的数据面需求。

## 测试环境与临时调整

- 使用当前已运行的 `nsA..nsD`、4 个 Relay、12 个协议容器；没有执行会销毁/重建拓扑的测试脚本。
- 每个协议子网均设置 A 为唯一主节点：A `hostPrime=1`，B/C/D `hostPrime=0`。三个协议的匹配参数保持现值不变。
- 通过现有短参数加注接口动态修改，CKL 每次收到 6 个成功 ACK，XTL/ZZW 各收到 7 个成功 ACK；三种协议共 60 个成功 ACK。
- 这些参数落在当前容器配置中，重建容器/拓扑后会按拓扑脚本的默认值重新生成。没有修改协议参数或业务代码。

## 测试结果

| 测试项 | 结果 | 观测 |
|---|---|---|
| CKL/XTL/ZZW peer 组网 | 通过（调参后） | 12 个节点的 3 个 peer 均为 `reachable=true, reason=OK`；每节点 12 条 `priority=200` 流表 |
| NS 到本组 Relay 的连通 | 通过 | `nsA..nsD` 分别 ping 本组 `.101..104` Relay，均 0% 丢包 |
| NS 经 Relay 查询协议参数 | 通过 | 12/12 组查询成功；CKL 响应 750 字节、XTL 6173 字节、ZZW 5682 字节，均与预期一致 |
| NS 经 Relay 短参数加注 | 通过 | 三种协议 B/C/D 的动态从节点配置均收到成功 ACK，配置文件随之更新 |
| 四个 NS 的设备拓扑上报 | 通过 | 4/4 收到并解析 51 字节报告；`nodeIP` 与各 NS 相符，`chainPathNum=3` |
| NS 间 ICMP 全互 ping | **失败** | 12/12 个有向 ping 失败；`ip route get` 对跨 NS 地址返回 `Network is unreachable` |

拓扑报告中，NS A 的 3 条链路同步标记均为 `isSync=1`。这代表协议发现/拓扑报告状态正常，不代表 NS 的普通 IP/ICMP 数据包已经通过协议网。

## 为什么 ping 不通

目前每个 NS 的路由表只有一项，例如：

```text
10.88.0.101 dev relay-A-ns scope link src 10.88.0.1
```

没有到 `10.88.0.2/3/4` 的路由。更重要的是，即使添加一条路由，当前 Relay 也没有 ICMP 转发逻辑：

1. Relay 接收的是 UDP 查询/加注请求，根据请求中的协议类型选择 CKL/XTL/ZZW 设备。
2. `RLY1` 封装承载的是这些业务报文和原始 NS IP 元数据，不是完整的 IP/ICMP 隧道。
3. 协议设备上的 `priority=200` OVS 规则只允许设备侧指定端口之间、源/目的业务 IP 匹配的 IP/ARP 帧。NS 没有 L2/L3 数据接口接入该协议数据面；它到 Relay 的 UDP 管理连接不能自动变成这些 OVS 规则所需的数据帧。

所以：最初 `MULTI_MASTER` 是协议 peer 不成网的真实参数错误；但修正为单主后，NS 互 ping 仍失败的直接原因是数据面/路由/封装处理缺失，而不是协议匹配参数。

## 可复核命令

以下命令在具备 root 权限的 Linux 主机执行。查询示例：

```bash
sudo ip netns exec nsA python3 tools/radio_param_client.py query 10.88.0.101 ckl --summary --timeout 6
sudo ip netns exec nsA python3 tools/radio_param_client.py query 10.88.0.101 xtl --summary --timeout 6
sudo ip netns exec nsA python3 tools/radio_param_client.py query 10.88.0.101 zzw --summary --timeout 6
```

协议 peer 和白名单检查：

```bash
docker exec conA-ckl cat /tmp/radio_test/peers.json
docker exec conA-ckl ovs-ofctl dump-flows br0 | grep priority=200
```

NS 间路由与 ping 检查：

```bash
sudo ip netns exec nsA ip route
sudo ip netns exec nsA ip route get 10.88.0.2
sudo ip netns exec nsA ping -c 1 -W 1 10.88.0.2
```

当前预期的失败特征是 `ip route get`/`ping` 返回 `Network is unreachable`。拓扑报告接收：

```bash
sudo ip netns exec nsA python3 tools/topology_receiver.py
```

## 后续实现边界

要满足“NS 是通信主体，NS 与其他 NS 必须经 CKL/XTL/ZZW 组成的协议网络互 ping”，需要设计并实现真正的数据面：NS 的目的 NS IP 必须选择本组 Relay；Relay 要能识别并封装/转交普通 IP 数据包（至少 ICMP），协议设备/协议网络要承载它，并在目标侧解封装后交给对应 NS；回程也必须通过协议网和 Relay。与此同时需要精确配置 NS 路由和防止跨组 Relay/设备被旁路访问。单独增加 NS 直连网段、仅修改 `hostPrime` 或只修改 UDP 查询 Relay 的目的地址，都不满足该需求。

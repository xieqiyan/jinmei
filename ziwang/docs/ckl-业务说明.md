# CKL 业务说明

本文档说明当前版本 CKL 协议容器的业务定位、参数查询、参数加注、动态组网和拓扑上报流程。文中“软件侧”指与被管 CKL 电台交互的软件运行侧。

## 1. 业务定位

CKL 容器用于模拟一台被管 CKL 测控链电台。每个 CKL 容器只处理 CKL 协议，不处理 XTL、ZZW 协议业务。

容器内部包含以下能力：

- 参数查询服务：监听 `10001/UDP`，按当前 CKL 参数返回查询响应。
- 参数加注服务：监听 `10001/UDP`，接收 CKL 短加注报文。
- CKL 动态组网守护进程：周期广播本机 CKL 组网参数，接收对端参数，动态维护业务转发规则。
- 设备拓扑上报：根据邻居状态生成拓扑报文，周期上报到软件侧 `8882/UDP`。

容器启动后，`entrypoint.sh` 只负责启动基础运行环境、创建 `/tmp/radio_test` 和 `br0`，不会自动完成参数服务、组网 daemon、拓扑上报程序的业务启动。

## 2. 网络角色

以 `conA` 为例：

```text
软件侧 A
  业务 IP: 10.0.0.1
      |
      | veth
      |
CKL 容器 conA
  eth-ns
      |
  br0
      |
  eth-up
      |
外部二层网络
      |
其他 CKL 容器
```

容器内 `br0` 配置管理地址：

```text
conA br0: 10.0.0.101/24
conB br0: 10.0.0.102/24
conC br0: 10.0.0.103/24
conD br0: 10.0.0.104/24
```

软件侧配置业务地址：

```text
软件侧 A: 10.0.0.1/24
软件侧 B: 10.0.0.2/24
软件侧 C: 10.0.0.3/24
软件侧 D: 10.0.0.4/24
```

其中：

- `10.0.0.101` 是 conA 容器内 `br0` 的管理 IP，用于软件侧访问容器内参数查询/加注服务。
- `10.0.0.1` 是软件侧 A 的业务 IP，用于参与 CKL 组网通信。
- `businessIp` 记录软件侧业务 IP，不是容器 `br0` 管理 IP。
- 当前 CKL 短加注报文不携带 `businessIp`，容器会把加注报文源 IP 写入 `businessIp`。

## 3. CKL 组网参数

CKL 组网配置文件：

```text
/tmp/radio_test/ckl_config.cfg
```

字段含义：

| 字段 | 含义 | 是否参与匹配 |
| --- | --- | --- |
| `nodeNo` | 节点号 | 否 |
| `businessIp` | 软件侧业务 IP | 用于业务白名单和拓扑上报，不作为参数匹配条件 |
| `radioFreq` | 定频频率 | 是 |
| `radioPower` | 发射功率 | 是 |
| `workFreqMode` | 工作模式 | 是 |
| `radioRate` | 速率 | 是 |
| `hostPrime` | 主属台标志，`1` 表示主节点 | 用于主节点唯一性判断 |

子网判定规则：

1. 从邻居表和本机配置中筛选出与本机参数匹配的节点集合 `S`。
2. 匹配条件为 `radioFreq`、`radioPower`、`workFreqMode`、`radioRate` 完全一致。
3. 统计集合 `S` 中 `hostPrime == 1` 的节点数量 `M`。
4. 若 `M == 1`，当前子网有效。
5. 若 `M == 0` 或 `M > 1`，当前子网无效。
6. 子网有效时，仅放行集合 `S` 内软件侧业务 IP 之间的通信。
7. 子网无效时，普通业务 IP 流量被阻断，但 CKL 管理广播仍保持畅通。

## 4. 参数查询业务

软件侧向容器 `10001/UDP` 发送查询请求。

查询请求固定长度 `157B`：

| 偏移 | 长度 | 字段 | CKL 取值 |
| --- | --- | --- | --- |
| 0 | 1B | `msgType` | `0x0a` |
| 1 | 1B | `superOpt` | `0x01` |
| 2 | 1B | `optEquip` | `0x01` |
| 3 | 154B | 保留 | `0x00` |

容器返回到软件侧 `10009/UDP`。

当前 CKL 查询响应固定长度为 `750B`：

```text
310B 响应头 + CKL_PARAM(440B)
```

当前版本不再返回 `ALL_EQUIP_PARAM`，也不再拼接 XTL、ZZW 参数结构。CKL 容器只返回 CKL 自身参数。

返回值规则：

```text
如果之前成功加注过，返回加注后的值
如果没有加注过，返回默认值
```

查询示例：

```bash
sudo ip netns exec nsA python3 tools/radio_param_client.py query 10.0.0.101 ckl --summary
```

验收点：

```text
请求发往 10.0.0.101:10001/UDP
响应从 10009/UDP 接收
响应长度为 750B
offset 310 开始为 CKL_PARAM
```

## 5. 参数加注业务

软件侧向容器 `10001/UDP` 发送 CKL 短加注报文。当前版本不再使用完整 `CKL_PARAM(440B)` 作为加注报文。

CKL 短加注报文长度：

```text
9B
```

字段布局：

| 偏移 | 长度 | 字段 | 说明 |
| --- | --- | --- | --- |
| 0 | 1B | `fmtVersion` | 固定 `0x01` |
| 1 | 1B | `radioType` | CKL 固定 `0x01` |
| 2 | 1B | `nodeNo` | 节点号 |
| 3 | 2B | `radioFreq` | 定频频率，uint16 BE |
| 5 | 1B | `radioPower` | 发射功率 |
| 6 | 1B | `workFreqMode` | 工作模式 |
| 7 | 1B | `radioRate` | 速率 |
| 8 | 1B | `hostPrime` | 主属台 |

容器收到加注报文后：

1. 校验报文长度是否为 `9B` 的整数倍。
2. 校验 `fmtVersion == 0x01`、`radioType == 0x01`。
3. 按 9B 拆分短加注记录。
4. 解析 CKL 字段。
5. 把字段写入内存中的完整 `CKL_PARAM(440B)`。
6. 使用加注报文源 IP 写入 `businessIp` / `nodeIp`。
7. 保存完整 CKL 参数状态到 `/tmp/radio_test/ckl_params.json`。
8. 提取组网字段写入 `/tmp/radio_test/ckl_config.cfg`。
9. 按字段逐个返回 2B 回执到软件侧 `8882/UDP`。

回执格式：

| 偏移 | 长度 | 字段 | 含义 |
| --- | --- | --- | --- |
| 0 | 1B | `cmd` | 固定 `0x13` |
| 1 | 1B | `result` | `0` 成功，非 `0` 失败 |

当前 CKL 一条 9B 短报文包含 6 个业务字段：

```text
nodeNo
radioFreq
radioPower
workFreqMode
radioRate
hostPrime
```

因此一条 CKL 短加注成功后，服务端返回 6 个成功 ACK：

```text
13 00
13 00
13 00
13 00
13 00
13 00
```

加注示例：

```bash
sudo ip netns exec nsA python3 tools/radio_param_client.py inject 10.0.0.101 ckl \
  --set nodeNo=1 \
  --set radioFreq=1000 \
  --set radioPower=1 \
  --set workFreqMode=1 \
  --set radioRate=1 \
  --set hostPrime=1
```

客户端预期输出：

```text
inject ckl: ack 1/6 ... data=1300 result=success
...
inject ckl: ack 6/6 ... data=1300 result=success
```

加注成功后生成/更新：

```text
/tmp/radio_test/ckl_config.cfg
/tmp/radio_test/ckl_params.json
```

## 6. 动态组网业务

`ckl_daemon` 使用 AF_PACKET 原始套接字绑定监听接口，默认绑定 `br0`。

默认启动等价于：

```bash
ckl_daemon -i br0 -b br0 -n eth-ns -u eth-up
```

参数含义：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `-i` | `br0` | 组网广播收发接口 |
| `-b` | `br0` | 要控制的网桥 |
| `-n` | `eth-ns` | 软件侧接入口 |
| `-u` | `eth-up` | 上联接入口 |

广播帧类型：

```text
EtherType: 0x88B5
```

广播内容包含本机 CKL 组网参数：

```text
nodeNo
businessIp
radioFreq
radioPower
workFreqMode
radioRate
hostPrime
```

daemon 接收到对端广播后：

1. 解析对端 CKL 参数。
2. 更新邻居表。
3. 老化超时邻居。
4. 根据 CKL 匹配字段重新计算有效子网。
5. 根据有效子网结果更新业务白名单转发规则。
6. 输出邻居状态到 `/tmp/radio_test/peers.json`。

配置热更新：

```text
ckl_daemon 会监听 /tmp/radio_test/ckl_config.cfg 变化。
参数服务加注成功并写入配置文件后，daemon 会自动重新加载配置并更新组网状态。
```

邻居状态文件：

```text
/tmp/radio_test/peers.json
```

该文件用于查看当前本机感知到的对端节点、参数是否匹配、是否可达等状态，也供拓扑上报程序读取。

## 7. 拓扑上报业务

`topology_reporter.py` 周期读取：

```text
/tmp/radio_test/ckl_config.cfg
/tmp/radio_test/peers.json
```

然后向软件侧 `8882/UDP` 发送设备拓扑上报报文。

拓扑上报包头：

| 字段 | 长度 | CKL 取值 |
| --- | --- | --- |
| `infoType1` | 1B | `0x14` |
| `commandLength1` | 2B | 按报文长度计算 |
| `infoType2` | 1B | `0x33` |
| `commandLength2` | 2B | 按链路内容计算 |
| `nodeIP` | 4B | 本机软件侧业务 IP |
| `routeNum` | 1B | `3`，表示测控链 |
| `chainPathNum` | 1B | 链路条数 |

每条链路信息 `13B`：

| 字段 | 长度 | 含义 |
| --- | --- | --- |
| `targetAddress` | 4B | 对端软件侧业务 IP |
| `isSync` | 1B | `1` 同步，`0` 失步 |
| `snr` | 4B | 信噪比 |
| `fieldIntensity` | 4B | 场强 |

当前无法从真实硬件获取 `snr` 和 `fieldIntensity`，程序会生成合理范围内的模拟值。

默认上报周期：

```text
2 秒
```

## 8. 容器内程序启动顺序

推荐顺序：

```text
1. 容器启动，entrypoint.sh 初始化基础运行环境、/tmp/radio_test 和 br0
2. 启动参数查询/加注服务
3. 软件侧执行参数加注
4. 软件侧执行参数查询确认
5. 启动 ckl_daemon
6. 启动 topology_reporter.py
```

手动启动参数服务：

```bash
python3 /usr/local/bin/radio_param_service.py
```

手动启动组网 daemon：

```bash
ckl_daemon
```

手动启动拓扑上报：

```bash
python3 /usr/local/lib/radio_protocol/topology_reporter.py
```

## 9. 关键文件

| 文件 | 生成方 | 作用 |
| --- | --- | --- |
| `/tmp/radio_test/ckl_config.cfg` | 参数服务或外部初始配置 | CKL 组网配置 |
| `/tmp/radio_test/ckl_params.json` | 参数服务 | 保存完整 CKL 参数状态和短加注摘要 |
| `/tmp/radio_test/peers.json` | `ckl_daemon` | 保存邻居发现和可达状态 |

## 10. 验收要点

参数查询验收：

- 软件侧向 `10.0.0.101:10001/UDP` 发送 `157B` 查询请求。
- 软件侧 `10009/UDP` 收到 `750B` 响应。
- 响应中 offset `310` 开始为 `CKL_PARAM(440B)`。

参数加注验收：

- 软件侧向 `10.0.0.101:10001/UDP` 发送 `9B` CKL 短加注报文。
- 一条 CKL 短加注成功后，软件侧 `8882/UDP` 收到 6 个 `0x13 0x00` 回执。
- 容器内 `/tmp/radio_test/ckl_config.cfg` 更新。
- 容器内 `/tmp/radio_test/ckl_params.json` 更新。

组网验收：

- 参数相同且仅一个主节点时，对应软件侧业务 IP 可互通。
- 无主节点时，业务 IP 不互通。
- 多主节点时，业务 IP 不互通。
- 参数不匹配的节点之间业务 IP 不互通。
- CKL 管理广播持续可达，后续参数恢复后业务可自动恢复。

拓扑上报验收：

- `topology_reporter.py` 周期向软件侧 `8882/UDP` 发送首字节 `0x14` 的拓扑报文。
- `routeNum == 3`。
- 链路条数与 `/tmp/radio_test/peers.json` 中可上报邻居一致。

# ZZW 协议容器说明

`zzw/` 是 ZZW 自组网电台的独立协议目录，包含 ZZW 镜像构建、参数查询、参数加注、动态组网控制和设备拓扑上报业务代码。

## 目录结构

- `Dockerfile`：构建 ZZW 独立镜像 `jm-zzw:v1`
- `entrypoint.sh`：容器启动入口，启动 OVS 数据库和 `ovs-vswitchd`
- `radio_param_service.py`：ZZW 容器内参数服务包装入口，限制只处理 ZZW
- `radio_protocol/`：参数查询、参数加注、拓扑上报和协议编解码实现
- `subnet_daemon/`：ZZW C 版组网守护进程源码

## 业务功能

ZZW 容器模拟被管 ZZW 电台，面向 namespace 侧软件提供以下业务：

- 参数查询：namespace 向容器 `10001/UDP` 发送查询请求，容器从 `10009/UDP` 返回 `310B 响应头 + ZZW_PARAM(5372B)`，总长度 `5682B`。
- 参数加注：namespace 向容器 `10001/UDP` 发送短加注参数 `2B封装头 + 10B字段区 = 12B`，字段区末尾为 `hostPrime`。一条 ZZW 短报文包含 7 个业务字段，服务端按字段逐个返回 `[0x13, result]` 到 namespace `8882/UDP`。加注报文不携带 `businessIp`，容器会把报文源 IP 写入配置文件。
- 配置落盘：加注成功后保存原始参数状态，并提取组网字段写入 `/tmp/radio_test/zzw_config.cfg`。
- 动态组网：`zzw_daemon` 通过二层广播同步 ZZW 参数，根据子网有效性动态维护 OVS 业务流表。
- 拓扑上报：`topology_reporter.py` 读取 `/tmp/radio_test/peers.json`，周期向 namespace `8882/UDP` 上报设备拓扑。

## ZZW 组网参数

配置文件路径：

```text
/tmp/radio_test/zzw_config.cfg
```

字段：

- `nodeNum`：节点号，仅作为节点身份
- `businessIp`：namespace 业务 IP，用于业务白名单和拓扑上报
- `radioPower`：发射功率
- `freqType`：频率类型
- `freq`：工作频率
- `userRate`：用户速率
- `superiorNetNo`：上级网号
- `hostPrime`：主属台标志，`1` 表示主节点

匹配字段：

- `radioPower`
- `freqType`
- `freq`
- `userRate`
- `superiorNetNo`

判定规则：

- 与本地匹配的节点集合中，`hostPrime == 1` 的节点数量恰好为 `1` 时，当前子网有效。
- 子网有效时，仅对白名单内同子网节点放行业务 IP。
- 无主或多主时，当前匹配子网无效，业务 IP 被阻断。
- ZZW 二层管理广播始终放行。

## 构建镜像

在项目根目录执行：

```bash
./scripts/build_images.sh
```

只构建 ZZW 镜像：

```bash
docker build --network=host -t jm-zzw:v1 zzw
```

## 启动拓扑

```bash
sudo bash scripts/setup_zzw_topology.sh
```

启动后会创建：

- 容器：`conA`、`conB`、`conC`、`conD`
- namespace：`nsA`、`nsB`、`nsC`、`nsD`
- namespace 业务 IP：`10.0.0.1` 到 `10.0.0.4`
- 容器管理 IP：`10.0.0.101` 到 `10.0.0.104`

## 查询示例

```bash
sudo ip netns exec nsA python3 tools/radio_param_client.py query 10.0.0.101 zzw --summary
```

## 加注示例

ZZW 短加注字段顺序：

```text
nodeNum, radioPower, freqType, freq, userRate, superiorNetNo, hostPrime
```

```bash
sudo ip netns exec nsA python3 tools/radio_param_client.py inject 10.0.0.101 zzw \
  --set nodeNum=1 \
  --set radioPower=1 \
  --set freqType=1 \
  --set freq=300.0 \
  --set userRate=1 \
  --set superiorNetNo=0 \
  --set hostPrime=1
```

预期结果：一条 ZZW 短加注报文包含 7 个业务字段，客户端应打印 7 次成功 ACK：

```text
inject zzw: ack 1/7 ... data=1300 result=success
...
inject zzw: ack 7/7 ... data=1300 result=success
```

如果只看到 `ack 1/1`，说明当前运行的客户端或容器镜像还是旧版本，需要重新构建镜像并重启容器。

## 查看状态

查看 ZZW daemon 日志：

```bash
docker exec conA tail -f /tmp/radio_test/zzw_daemon.log
```

查看邻居状态：

```bash
docker exec conA cat /tmp/radio_test/peers.json
```

查看 OVS 流表：

```bash
docker exec conA ovs-ofctl dump-flows br0
```

查看配置：

```bash
docker exec conA cat /tmp/radio_test/zzw_config.cfg
```

接收一次拓扑上报：

```bash
sudo ip netns exec nsA python3 tools/topology_receiver.py
```

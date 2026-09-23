# CKL 协议容器说明

`ckl/` 是 CKL 测控链电台的独立协议目录，包含 CKL 镜像构建、参数查询、参数加注、动态组网控制和设备拓扑上报业务代码。

## 目录结构

- `Dockerfile`：构建 CKL 独立镜像 `jm-ckl:v1`
- `entrypoint.sh`：容器启动入口，启动 OVS 数据库和 `ovs-vswitchd`
- `radio_param_service.py`：CKL 容器内参数服务包装入口，限制只处理 CKL
- `radio_protocol/`：参数查询、参数加注、拓扑上报和协议编解码实现
- `ckl_config/`：CKL 配置结构定义
- `ckl_daemon/`：CKL C 版组网守护进程源码

## 业务功能

CKL 容器模拟被管 CKL 电台，面向 namespace 侧软件提供以下业务：

- 参数查询：namespace 向容器 `10001/UDP` 发送查询请求，容器从 `10009/UDP` 返回 `310B 响应头 + CKL_PARAM(440B)`，总长度 `750B`。
- 参数加注：namespace 向容器 `10001/UDP` 发送短加注参数 `2B封装头 + 7B字段区 = 9B`，一条 CKL 短报文包含 6 个业务字段，服务端按字段逐个返回 `[0x13, result]` 到 namespace `8882/UDP`。加注报文不携带 `businessIp`，容器会把报文源 IP 写入配置文件。
- 配置落盘：加注成功后保存原始参数状态，并提取组网字段写入 `/tmp/radio_test/ckl_config.cfg`。
- 动态组网：`ckl_daemon` 通过二层广播同步 CKL 参数，根据子网有效性动态维护 OVS 业务流表。
- 拓扑上报：`topology_reporter.py` 读取 `/tmp/radio_test/peers.json`，周期向 namespace `8882/UDP` 上报设备拓扑。

## CKL 组网参数

配置文件路径：

```text
/tmp/radio_test/ckl_config.cfg
```

字段：

- `nodeNo`：节点号，仅作为节点身份
- `businessIp`：namespace 业务 IP，用于业务白名单和拓扑上报
- `radioFreq`：定频频率
- `radioPower`：发射功率
- `workFreqMode`：工作模式
- `radioRate`：速率
- `hostPrime`：主属台标志，`1` 表示主节点

匹配字段：

- `radioFreq`
- `radioPower`
- `workFreqMode`
- `radioRate`

判定规则：

- 与本地匹配的节点集合中，`hostPrime == 1` 的节点数量恰好为 `1` 时，当前子网有效。
- 子网有效时，仅对白名单内同子网节点放行业务 IP。
- 无主或多主时，当前匹配子网无效，业务 IP 被阻断。
- CKL 二层管理广播始终放行。

## 构建镜像

在项目根目录执行：

```bash
./scripts/build_images.sh
```

只构建 CKL 镜像：

```bash
docker build --network=host -t jm-ckl:v1 ckl
```

## 启动拓扑

```bash
sudo bash scripts/setup_ckl_topology.sh
```

启动后会创建：

- 容器：`conA`、`conB`、`conC`、`conD`
- namespace：`nsA`、`nsB`、`nsC`、`nsD`
- namespace 业务 IP：`10.0.0.1` 到 `10.0.0.4`
- 容器管理 IP：`10.0.0.101` 到 `10.0.0.104`

## 查询示例

```bash
sudo ip netns exec nsA python3 tools/radio_param_client.py query 10.0.0.101 ckl --summary
```

## 加注示例

CKL 短加注字段顺序：

```text
nodeNo, radioFreq, radioPower, workFreqMode, radioRate, hostPrime
```

```bash
sudo ip netns exec nsA python3 tools/radio_param_client.py inject 10.0.0.101 ckl \
  --set nodeNo=1 \
  --set radioFreq=1000 \
  --set radioPower=1 \
  --set workFreqMode=1 \
  --set radioRate=1 \
  --set hostPrime=1
```

预期结果：一条 CKL 短加注报文包含 6 个业务字段，客户端应打印 6 次成功 ACK：

```text
inject ckl: ack 1/6 ... data=1300 result=success
...
inject ckl: ack 6/6 ... data=1300 result=success
```

如果只看到 `ack 1/1`，说明当前运行的客户端或容器镜像还是旧版本，需要重新构建镜像并重启容器。

## 查看状态

查看 CKL daemon 日志：

```bash
docker exec conA tail -f /tmp/radio_test/ckl_daemon.log
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
docker exec conA cat /tmp/radio_test/ckl_config.cfg
```

接收一次拓扑上报：

```bash
sudo ip netns exec nsA python3 tools/topology_receiver.py
```

# 协议容器业务测试

测试脚本位于容器业务目录之外，不修改 CKL、XTL、ZZW 的协议实现。

## 运行方式

在 Linux 宿主机上以 root 权限运行：

```bash
sudo bash scripts/tests/test_ckl.sh
sudo bash scripts/tests/test_xtl.sh
sudo bash scripts/tests/test_zzw.sh
```

每个脚本默认重新构建协议镜像，然后通过 `scripts/setup_topology.sh` 创建
`nsA` 到 `nsD`。每个 NS 组包含一个 Relay、一个 CKL、一个 XTL 和一个
ZZW；同一协议类型的四个设备接入各自的类型子网。请勿在存在同名生产资源
的宿主机上运行。

## 覆盖场景

1. 独立 Docker 构建上下文与镜像构建。
2. 四容器、四 namespace、OVS 网桥和业务进程启动。
3. 四节点普通参数查询与应用层分片查询。
4. 错误设备类型查询拒绝。
5. 完整短参数加注、逐字段 ACK、`businessIp` 来源绑定和配置落盘。
6. 非法短报文拒绝与失败 ACK。
7. 设备拓扑上报的报文结构、`routeNum`、链路数和同步状态。
8. 单主正常全互通。
9. 单节点参数失配隔离与恢复。
10. 无主隔离、主节点迁移和恢复。
11. 多主冲突隔离与恢复。
12. 匹配参数滚动迁移，验证两节点、三节点到四节点逐步合网。
13. OVS `priority=200` 业务白名单流表的动态增删。
14. daemon 停止、邻居超时淘汰、业务隔离和重启重入网。
15. 参数服务停止、查询失败、服务重启和加注状态持久化。
16. 最终四节点查询、拓扑、流表和全互通回归。

## 可配置参数

- `SKIP_BUILD=1`：跳过镜像构建，使用现有镜像。
- `RADIO_IMAGE`：覆盖默认镜像名。
- `BUILD_NETWORK`：Docker build 网络模式，默认 `host`。
- `BASE_IMAGE`：覆盖 Dockerfile 的基础镜像。
- `PLATFORM`：覆盖 Docker 构建平台。
- `CONVERGENCE_TIMEOUT`：动态组网收敛超时，默认 15 秒。
- `QUERY_TIMEOUT`：查询和加注超时，默认 6 秒。
- `TOPOLOGY_TIMEOUT`：等待一次拓扑上报的超时，默认 8 秒。
- `TEST_LOG_DIR`：覆盖测试日志输出目录。

例如，使用已有 CKL 镜像并将收敛超时改为 25 秒：

```bash
sudo SKIP_BUILD=1 CONVERGENCE_TIMEOUT=25 bash scripts/tests/test_ckl.sh
```

测试失败时会自动保存各容器进程、配置、`peers.json`、OVS 流表和进程日志。测试结束后保留现场，便于继续排查。

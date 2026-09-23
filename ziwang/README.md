# 自组网协议容器项目

本项目包含 CKL、XTL、ZZW 三套相互独立的协议容器业务代码，以及容器外部的构建、测试和说明资源。

## 目录结构

- `ckl/`：CKL 镜像业务代码，可作为独立 Docker 构建上下文
- `xtl/`：XTL 镜像业务代码，可作为独立 Docker 构建上下文
- `zzw/`：ZZW 镜像业务代码，可作为独立 Docker 构建上下文
- `scripts/`：镜像构建和测试拓扑搭建脚本
- `tools/`：参数客户端、拓扑接收器、监听器和流表查看工具
- `relay/`：NS 到 CKL/XTL/ZZW 的透明协议中转程序，保持原业务报文格式
- `docs/`：业务说明、启动说明和接口协议文档

透明中转设备说明见 [`relay/README.md`](relay/README.md)。

## 构建镜像

构建全部协议镜像：

```bash
./scripts/build_images.sh
```

单独构建一个协议镜像：

```bash
docker build --network=host -t jm-ckl:v1 ckl
docker build --network=host -t jm-xtl:v1 xtl
docker build --network=host -t jm-zzw:v1 zzw
```

## 测试拓扑

先构建本地协议镜像，拓扑脚本不会自动从 Docker Hub 拉取镜像：

```bash
sudo bash scripts/build_images.sh
```

如果希望拓扑脚本显式负责构建：

```bash
sudo BUILD_IMAGES=1 bash scripts/setup_topology.sh
```

```bash
sudo bash scripts/setup_topology.sh
```

启动完成后应看到 12 个协议容器：`conA-ckl`、`conA-xtl`、`conA-zzw`，
以及 B/C/D 三组同样的三类容器。脚本会同时校验 `nsA~nsD` 和四个 Relay
 进程；如果只看到 `con0-*`，说明使用了旧版本脚本，需要重新执行上述命令。

拓扑脚本会通过 `docker exec -i` 把容器初始化脚本传入容器；如果容器只有
OVS、没有协议 daemon 和参数服务，说明初始化没有执行，应重新运行拓扑脚本。

各协议的详细业务说明位于 `docs/ckl/README.md`、`docs/xtl/README.md` 和 `docs/zzw/README.md`。


## 业务测试

三个协议分别提供完整业务与动态组网测试：

```bash
sudo bash scripts/tests/test_ckl.sh
sudo bash scripts/tests/test_xtl.sh
sudo bash scripts/tests/test_zzw.sh
```

详细场景和可配置参数见 `scripts/tests/README.md`。

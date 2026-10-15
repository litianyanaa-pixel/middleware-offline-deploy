# 第三方组件与开源合规声明（THIRD-PARTY NOTICES）

本仓库（middleware-offline-deploy）包含的第三方代码及其许可信息如下。
**本仓库自身的许可不覆盖下列第三方代码**，各第三方代码沿用其原始许可。

## 1. KubeKey（源码收编）

- 位置：[`third_party/kubekey/`](../third_party/kubekey/)
- 上游：https://github.com/kubesphere/kubekey
- 许可：Apache License 2.0（原文随目录分发：`third_party/kubekey/LICENSE`）
- 本地版本：`v4.0.7-xc1`（信创适配修改版）
- 修改声明：见 [`third_party/kubekey/MODIFICATIONS.md`](../third_party/kubekey/MODIFICATIONS.md)
  （依 Apache-2.0 §4(b) 声明，仅 4 个 YAML 角色文件改动，无 Go 代码改动）
- 用途：构建支持国产操作系统（kylin/uos/openEuler/anolis/alinux）的 kk 离线装机二进制；
  构建产物经 GitHub Releases 分发，同样遵循 Apache-2.0。

## 2. 运行期使用的第三方软件（不入本仓库，随物料仓库分发）

以下组件**不存放在本仓库内**，由用户经补料脚本/官方渠道获取后进入本地
`warehouse/`（该目录不入 git），其许可随各自官方分发：

| 组件 | 来源 | 许可 |
|---|---|---|
| MySQL / Redis / NGINX / MinIO / Kafka / MongoDB / PostgreSQL 等容器镜像 | 各官方镜像仓库 | 各自项目许可 |
| kubeadm/kubelet/kubectl/etcd/CNI/containerd/runc 等 | kk artifact 导出 | Apache-2.0 / BSD 等随上游 |
| crane | go-containerregistry | Apache-2.0 |

本仓库的打包器仅"搬运"上述物料（复制 tar / 生成 compose 配置），不修改、不重分发其内容。

## 3. 本仓库自有代码

打包器（`packer.py`, `packerlib/`, `plugins/`, `templates/`, `server/`）、
集群物料脚本（`prepare_cluster.py`）、测试与文档为本项目自有代码，
许可见仓库根 LICENSE（如未单独声明，默认保留所有权利/见仓库声明）。

# MODIFICATIONS — 本目录源码相对上游 KubeKey 的修改声明

本目录为 [KubeKey](https://github.com/kubesphere/kubekey) 的修改版源码快照，
遵循其 **Apache License 2.0** 分发（原始许可文本见本目录 [LICENSE](LICENSE)）。
依 Apache-2.0 第 4(b) 条，在此声明对上游源码的全部修改。

## 溯源

| 项 | 值 |
|---|---|
| 上游项目 | https://github.com/kubesphere/kubekey |
| 上游许可 | Apache License 2.0 |
| 本快照版本标记 | `v4.0.7-xc1`（xc = 信创适配版；上游 v4 系列） |
| 快照说明 | 源码树整体收编于本项目 `third_party/kubekey/`，不含 `bin/` 构建产物与运行日志 |
| 修改范围 | **仅 4 个 YAML 角色文件，无任何 Go 代码改动** |

## 修改清单（全部）

官方 kk 二进制的发行版白名单不含部分国产操作系统，为支持信创环境离线装机
（kylin/uos/openEuler/anolis/alinux），修改如下 4 个内置角色 YAML：

| 文件 | 改动内容 |
|---|---|
| `builtin/core/roles/defaults/defaults/main/01-cluster_require.yaml` | 发行版白名单追加 alinux / uos / openEuler / anolis（含各带引号的变体写法） |
| `builtin/core/roles/native/repository/tasks/install_package.yaml` | 包管理映射：uos→ubuntu（deb 系）；kylin / alinux / openEuler / anolis→centos（rpm 系） |
| `builtin/core/roles/native/nfs/tasks/main.yaml` | NFS 服务分支兼容上述 rpm 系发行版 |
| `builtin/core/roles/native/ntp/tasks/main.yaml` | `timedatectl set-ntp` 失败容错（chrony 新装时 timedated 缓存问题的根治） |

补丁的设计说明与验证记录见上层项目文档：`docs/k8s-信创离线部署.md` 第 2.1 节、
`docs/k8s-容器验证记录.md`。

## 构建产物

由本源码构建的 kk 二进制（`v4.0.7-xc1`，amd64/arm64）随上层项目
[middleware-offline-deploy 的 GitHub Releases](https://github.com/litianyanaa-pixel/middleware-offline-deploy/releases)
分发，同样遵循 Apache-2.0；也可自行构建：
`make kk`（amd64）或 `GOARCH=arm64 GOOS=linux go build -tags builtin -o kk ./cmd/kk`。

---
KubeKit/KubeKey 版权归 The KubeSphere Authors 及其贡献者所有，
修改部分遵循同一许可。本项目对其余自有代码不适用 KubeKey 的许可。

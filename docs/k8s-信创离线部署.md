# K8s 集群信创离线部署 — 设计文档 (v2, 已集成进打包页面)

> 配套代码: `packerlib/cluster.py`(集群配置生成与物料, 打包入口 `python packer.py`) + `k8s/server/deploy-cluster.sh`(部署脚本)。
> 本文回答三件事: 整体架构怎么设计、物料怎么准备(含信创)、怎么用和怎么回滚。

## 一、总体架构

沿用本仓库"打包决策、服务器零交互"的哲学, K8s 集群作为**基础平台组件**融入打包器主流程:

```
打包机(有网/内网)                              交付目标(离线机房)
┌───────────────────────────────────┐        ┌──────────────────────────────────────┐
│ 1. 准备物料 → warehouse/cluster/   │         │ tar -xzf xxx-offline.tar.gz          │
│ 2. 页面勾选 [⎈ Kubernetes 集群]    │ tar.gz  │ cd xxx-*-offline                     │
│    + 任意中间件, 填集群参数,       │ ──────→ │ ./deploy.sh   ← 只此一条              │
│    在部署形态区给服务器池节点      │ + sha256 │  【0.5/9】建集群: 校验→OS包预装→      │
│    分配 control-plane/worker 角色  │         │   kk create cluster→kubectl 验证     │
│ 3. 预览(含 K8s tab) → 一键打包     │         │  【1/9..9/9】中间件(勾选了才有)       │
└───────────────────────────────────┘        └──────────────────────────────────────┘
                                               kk 从部署机 SSH 到各节点完成安装
```

- **纯集群包**(只勾集群): deploy.sh 是薄壳, 直接执行 deploy-cluster.sh, 建完集群即结束
- **混合包**(集群+中间件): 先建集群再走原中间件流程, 失败修复后重跑(幂等)
- 复用页面已有的「部署服务器池」(IP/SSH 账号/密码), 集群角色在其下分配, 与 MySQL/Kafka
  多机形态同一套交互

为什么用 KubeKey 而不是 kubeadm 裸装: kk 自带 SSH 编排(密码/密钥)、离线产物机制、
国产 OS 适配点集中在少数几个 YAML 角色里(我们已打过补丁), 且扩容/升级/卸载同一条工具链。

### 两种部署模式(镜像只做纯离线或全在线, 不做混合)

| | mode=cache(纯离线, 推荐) | mode=online(在线) |
|---|---|---|
| 物料 | 二进制缓存 + **离线镜像包** + CNI chart, 全部随包 | 仅 kk 与配置, 无镜像物料 |
| 镜像 | 打包时 docker pull(国内源)+docker save 按原生 tag 收集; 部署时 deploy-cluster.sh 在 kk create 前导入各节点本地 containerd, **全程不访问外网** | 节点直接联网拉取; zone=cn 走国内源, **无需境外外网**(2026-10-01 双节点真机验证) |
| 升级 | upgrade_to 连带收集目标版本镜像包与全套组件缓存(crictl/helm/etcd 等, kk upgrade --all 按目标版本 manifest 取件), upgrade-cluster.sh 导入镜像+铺缓存后再升级 | 联网拉取 |
| kk 侧开关 | `download.fetch=false` + `cri.containerd.config_policy: overwrite`(kk 每次重写 containerd 配置) | `download.fetch=true` |

**online+zone=cn 的国内源链路(真机验证通过)**: 二进制走 `kubekey.pek3b.qingstor.com`(青云,
kube 三件套/crictl/helm/etcd/cni/containerd/runc 全有, 但**只同步非 static 版 containerd**,
打包器对 online+cn 自动回退 static_binary=false 并告警); 镜像走 `hub.kubesphere.com.cn`
(kk 内置 CN 映射: kube 三件套与 etcd/pause 在 `kubernetes/` 命名空间, CoreDNS 在
`coredns/coredns`, flannel 在 `flannel-io/`)。打包器**不写** `registry.imageRepository`
覆盖——裸域名会让 kubeadm 丢命名空间(hub 实测 400/404)。CentOS7 节点 yum 指向阿里源即可。

镜像清单由 prepare_cluster.py 的 `k8s_image_specs` 按 k8s 版本精确推导(控制面/CoreDNS/
NodeLocalDNS/etcd/CNI/HA/存储), 版本对照表在 versions.json(coredns_tag/nodelocaldns_tag/
sandbox_image_tag)。注意两个坑: docker.io 单路径镜像必须写 `docker.io/library/` 全称
(containerd 会规范化); flannel chart 镜像在 ghcr.io。手工收集入口:
`python prepare_cluster.py --images flannel --kube-version v1.28.15`

kk 消费 artifact 的机制(源码验证): `download/tasks/main.yaml` 对 `download.artifact_file`
直接 `tar -zxvf -C binary_dir`, 布局由 kk 自己保证; cache 模式则因 `download.fetch=false`
跳过一切联网下载, 只用预置缓存(`binary_dir` 下 `fileExists` 即跳过下载)。

## 二、物料准备

> **Windows 一键备料**(推荐, 与打包器同环境): `python k8s/prepare_materials.py`
> - `--check` 物料盘点; `--download` 自动下载缺失二进制(多源多连接)
> - `--download --upgrade-to v1.34.12` 连升级目标版本一起下
> - `--os-packages ubuntu kylin` 容器提取 OS 依赖包(需 Docker Desktop)
> - `--artifact-export` 生成 artifact 导出配置 + 一键 `export-artifact.bat`
> - `--kk-build` 生成 kk 交叉编译脚本 `build-kk.bat`
> 以下小节是各步骤的手工路径(等价, 供理解原理与排查)。

> **免编译获取 kk**: 打过信创补丁的 kk 二进制已随 [Releases v1.2.0](https://github.com/litianyanaa-pixel/middleware-offline-deploy/releases/tag/v1.2.0)
> 提供(amd64/arm64), 下载后放入 `warehouse/cluster/kk/v4.0.7-xc1/<arch>/kk` 即可,
> 打包/部署全程不需要自行编译; 仅在更新信创补丁时才需要 `--kk-build` 重新构建。

### 2.1 打补丁的 kk 二进制(信创关键)

官方 kk 二进制的发行版白名单不含 uos/openEuler/anolis/alinux, 必须自己构建。补丁点
(源码仓库 `E:\project\kubekey`, 均为 YAML 角色修改, 无 Go 代码改动):

| 文件 | 改动 |
|---|---|
| `builtin/core/roles/defaults/defaults/main/01-cluster_require.yaml` | 白名单 += alinux/uos/openEuler/anolis(各带引号变体) |
| `builtin/core/roles/native/repository/tasks/install_package.yaml` | current_host_type: uos→ubuntu(deb 系); kylin/alinux/openEuler/anolis→centos(rpm 系) |
| `builtin/core/roles/native/nfs/tasks/main.yaml` | NFS 服务分支兼容上述 rpm 系发行版 |
| `builtin/core/roles/native/ntp/tasks/main.yaml` | `timedatectl set-ntp` 失败容错(chrony 新装时 timedated 缓存问题的根治) |

构建(任意有 Go 1.22+ 的机器, Linux/WSL):

```bash
cd kubekey && make kk                                   # 产出 _output/bin/kk (amd64, 含 builtin)
GOARCH=arm64 GOOS=linux go build -tags builtin -o kk-arm64 ./cmd/kk   # 鲲鹏/飞腾
# 放置: warehouse/cluster/kk/v4.0.7-xc1/{amd64,arm64}/kk
```

### 2.2 artifact 模式物料(推荐路径)

在有网机器上(kk 版本与交付一致):

```bash
# kk artifact export 用一份 manifest 描述要带上的东西(镜像清单/组件版本/OS)
# 具体清单写法见 kk 官方文档 "artifact" 章节; 生成的 tar.gz 即为离线产物
./kk artifact export -m manifest.yaml -o kubekey-artifact-v1.34.11-amd64.tar.gz
# 放置: warehouse/cluster/artifact/kubekey-artifact-v1.34.11-amd64.tar.gz
```

产物包含: kubeadm/kubelet/kubectl/etcd/containerd/runc/helm/crictl/cni-plugins 全部二进制
+ 全部容器镜像(pause/apiserver/ scheduler/controller-manager/proxy/calico...)。
镜像默认来自 registry.k8s.io 与 docker.io, 国内网络用 `zone: cn` 或镜像加速。

### 2.3 cache 模式物料(备选)

按 kk 下载缓存布局放置(版本对应仓库根 versions.json, 路径已实测核对):

```
cache/
├── kube/v1.34.11/<arch>/{kubeadm,kubelet,kubectl}     ← dl.k8s.io(国内慢用多连接并行下载)
├── etcd/v3.6.5/<arch>/etcd-v3.6.5-linux-<arch>.tar.gz
├── cni/plugins/v1.9.1/<arch>/cni-plugins-linux-<arch>-v1.9.1.tgz
├── helm/v3.18.5/<arch>/helm-v3.18.5-linux-<arch>.tar.gz
└── crictl/v1.34.0/<arch>/crictl-v1.34.0-linux-<arch>.tar.gz
```

### 2.4 信创 OS 依赖包

kk 会在每个节点安装少量系统包(rpm: socat conntrack-tools ipset ebtables chrony ipvsadm nfs-utils;
deb: 同名 conntrack/nfs-common 变体)。**预装后 kk 检测到已装会自动跳过**, 从而完全离线:

```bash
# 以麒麟 V10 (rpm) 为例, 在同版本系统上提取(含依赖):
yumdownloader --resolve socat conntrack-tools ipset ebtables chrony ipvsadm nfs-utils
# 放置: warehouse/cluster/os/kylin/<arch>/*.rpm
# UOS(deb): apt-get download $(apt-cache depends --recurse ... ) 或在内网仓库导出
```

## 三、信创适配矩阵

> **当前开放范围(v1):仅 amd64(x86_64)**。页面在 arm64 下禁用集群卡片并提示;
> arm64(鲲鹏/飞腾)的 kk 构建已备好(`kk/v4.0.7-xc1/arm64`),开放时按 2.2/2.3 补齐 arm64
> 二进制/镜像物料并在 packer 中放开即可。

| OS | family | amd64 | arm64 | 说明 |
|---|---|---|---|---|
| 银河麒麟 V10 | rpm | ✓ | ✓ | kk 原生白名单; 内核 4.19 → 必须 iptables 模式 |
| 统信 UOS 服务器 | deb | ✓ | ✓ | 走 deb 分支(基于 deepin), 需打补丁的 kk |
| openEuler | rpm | ✓ | ✓ | 需打补丁的 kk(白名单 + 分支) |
| Anolis 龙蜥 | rpm | ✓ | ✓ | 同上; ID_LIKE 含 rhel 系 |
| Alibaba Cloud Linux | rpm | ✓ | ✓ | 内核 5.10 → iptables 模式; 需打补丁的 kk |
| Rocky / CentOS | rpm | ✓ | ✓ | 注意: 官方 kk 的 `ID_LIKE` 精确匹配对 Rocky 有缺陷, 打补丁构建同样受益 |
| Ubuntu / Debian | deb | ✓ | ✓ | 官方即支持 |

硬件: x86_64 通用服务器 + 鲲鹏 920 / 飞腾 S2500/2000 等 aarch64 机型(选 `arch: arm64`,
kk/k8s 二进制/镜像全部有 arm64 版本)。混架构集群不支持, 部署脚本会逐节点校验架构一致。

硬性边界:
- **cgroup**: K8s ≤ v1.34 支持 cgroup v1 与 v2; **v1.35 起仅 v2**(部署脚本会按目标版本提示)。
  麒麟/龙蜥等默认 v1 的系统升 1.35+ 前须 `grubby --update-kernel=ALL --args="systemd.unified_cgroup_hierarchy=1"` 并逐节点重启。
- **内核 ≥ 5.13** 才能用 kube-proxy nftables 模式; 打包器默认 iptables。
- K8s 版本范围 v1.23 ~ v1.37(kk 内置), 信创推荐固定在 v1.34.x 线。

## 四、使用与运维

```bash
# 打包(全部在 Web 页面完成: python packer.py 打开)
#   基础设置 → 勾选 [⎈ Kubernetes 集群] (+任意中间件) → 部署形态区:
#     · K8s 版本 / CNI / Pod+Service CIDR / kube-proxy 模式 / 时区
#     · 部署模式: 纯离线(二进制+镜像全本地, 推荐) 或 在线安装(联网)
#     · OS 依赖包: 按节点发行版勾选(麒麟/UOS/openEuler/龙蜥/阿里云Linux/Rocky/CentOS/Ubuntu/Debian)
#     · 集群角色分配: 服务器池中每台节点选 控制面/工作节点/镜像仓库
#   → 生成预览(多一个「K8s 集群」tab 展示 inventory/config) → 开始打包

# 服务器部署(部署机: 与各节点 SSH 可达, root)
./deploy.sh            # 含集群的包自动先建集群【0.5/9】; 纯集群包建完即结束
                       # deploy-cluster.sh 可单独重跑: bash deploy-cluster.sh [--skip-node-prep]

# 部署后日常(部署机即控制面之一)
export KUBECONFIG=/etc/kubernetes/admin.conf && kubectl get nodes

# 扩容: 页面服务器池加节点并分配角色 → 重新打包 → 服务器(包根目录):
./cluster/kk add nodes -i cluster/inventory.yaml -c cluster/config.yaml
# 升级: ./cluster/kk upgrade cluster -i cluster/inventory.yaml -c cluster/config.yaml --with-kubernetes v1.34.12
# 卸载: ./uninstall-cluster.sh   (kk delete cluster --all 一键卸载, 危险, 会清空集群)
#       --keep-cri 保留节点 containerd 与镜像(重装更快); --yes 跳过交互确认
```

deploy-cluster.sh 步骤(独立编号【集群 n】): 0 环境(架构/OS/信创提示) → 1 幂等(已有集群给指引退出)
→ 2 节点 OS 依赖预装 → 3 物料就位(artifact 校验 md5 / cache 铺缓存) → 4 `kk create cluster`
→ 5 `kubectl get nodes` 验证 + `cluster-k8s-报告.txt`。失败修复后直接重跑, 幂等。

## 五、回滚与安全

- **代码回滚**: 本仓库集成前已打 git 标签 `backup-before-k8s`, 另有外部完整备份
  `E:\project\中间件离线部署包-backup-*.tar.gz`; 回滚: `git reset --hard backup-before-k8s`
  (k8s/ 目录全部为新增文件, 删除即净)。
- **集群回滚**: `./kk delete cluster` 后重跑 deploy-cluster.sh; 单节点故障不影响其余节点,
  kk 按 serial 批次执行。
- **安全**: manifest.json/manifest.sh/inventory.yaml 含 SSH 凭据, 包内权限 600, 分发按密件管理
  (与本仓库现有 .env 密码管理约定一致)。

## 六、v1 已知边界与后续

- 高可用控制面(kube-vip/haproxy)走 kk 原生配置, 打包器暂无表单, 可手改生成的 config.yaml 再打包(勿在服务器改)
- 观测/存储 add-on 不含, 走集群建好后的应用侧交付(可复用本仓库 compose 体系在 K8s 上跑)
- 升级场景的离线物料(新版本二进制+镜像)同样按 2.2/2.3 准备后放入 cache/artifact 再执行 upgrade

# k8s/ — Kubernetes 集群部署模块 (KubeKey · 信创)

K8s 集群已作为**基础平台组件**集成进打包器主流程: 在 Web 页面勾选「Kubernetes 集群」卡片、
配置集群参数并给服务器池中的节点分配角色, 与普通中间件一样**一键打包**; 服务器上解压后
`./deploy.sh` 一条命令 —— 先建集群(可再继续装勾选的中间件), 全程零公网。

```
页面: 基础设置 → 勾选 [⎈ Kubernetes 集群] + 中间件 → 部署形态里填集群参数/分配角色 → 预览(有 K8s tab) → 打包
服务器: tar -xzf xxx.tar.gz && cd xxx && ./deploy.sh    # 集群【0.5/9】先建好, 再走中间件【1/9..9/9】
```

两种用法:
- **纯集群包**: 只勾集群、不勾中间件 → deploy.sh 直接执行 `deploy-cluster.sh`, 建完集群即结束
- **混合包**: 集群 + 中间件 → 先建集群, 集群就绪后自动继续中间件部署(幂等可重跑)

## 文件

```
k8s/
├── versions.json               # (仓库根) 物料目录 cluster 节: 版本/CNI矩阵/发行版
├── prepare_materials.py        # Windows 原生物料准备(盘点/下载/OS包/artifact/kk构建)
├── server/deploy-cluster.sh    # 集群部署脚本(由 packer.py 打入包根目录; deploy.sh 在 CLUSTER_ENABLED=1 时调用)
├── server/uninstall-cluster.sh # 集群卸载脚本(随包分发, kk delete cluster + 二次确认)
└── README.md                   # 本文件
```

打包决策/生成/物料检查逻辑在 `packerlib/`(`cluster.py` 集群配置与物料、`builder.py` 打包主流程;
入口 `python packer.py` 兼容薄壳), 物料目录定义在
`versions.json` 的 `cluster` 节, 物料放置与信创适配见 **[docs/k8s-信创离线部署.md](../docs/k8s-信创离线部署.md)**。

## 物料(服务器离线部署前, 打包机需备齐)

**Windows 上一键备料**(打包机同款环境, 纯标准库):

```bash
python k8s/prepare_materials.py --check            # 物料盘点(缺什么一目了然)
python k8s/prepare_materials.py --download         # 自动下载缺失二进制(多源多连接, 支持断点重试)
python k8s/prepare_materials.py --download --upgrade-to v1.34.12   # 连升级目标版本一起下
python k8s/prepare_materials.py --os-packages ubuntu kylin   # 容器提取 OS 依赖包(需 Docker Desktop)
python k8s/prepare_materials.py --artifact-export  # 生成 kk artifact 导出配置 + 一键 export-artifact.bat
python k8s/prepare_materials.py --kk-build         # 生成 kk 构建 build-kk.bat(本机 Go 交叉编译)
```

手动放置参考(目录约定):

```
warehouse/cluster/kk/<版本>/<arch>/kk                        # 打过信创补丁的 kk(build-kk.bat 产物)
warehouse/cluster/artifact/kubekey-artifact-<k8s版本>-<arch>.tar.gz   # artifact 模式(推荐)
warehouse/cluster/{kube,etcd,cni,helm,crictl,containerd,runc}/...   # cache 模式的二进制
warehouse/cluster/os/<发行版>/<arch>/*.rpm|*.deb              # 信创 OS 依赖包(页面按需勾选)
```

物料缺失时: 集群卡片显示「缺料」红标; 预览会列出缺失清单, 打包直接终止并提示放置路径。

## 部署后运维(包内自带 kk)

```bash
cd <部署目录或解压目录>
export KUBECONFIG=/etc/kubernetes/admin.conf && kubectl get nodes
./cluster/kk add nodes -i cluster/inventory.yaml -c cluster/config.yaml      # 扩容(改配置重打包后)
./upgrade-cluster.sh                                                          # 升级包一键升级(选了升级包时)
./uninstall-cluster.sh                                                        # 卸载(危险, 二次确认)
```

## 能力边界与回归

- 集群暂仅支持 amd64;arm64(鲲鹏/飞腾)后续开放(kk 构建已备好, 页面勾选时给出提示)
- 控制面高可用:≥2 台控制面时页面出现 HA 表单;**kube-vip 填同网段空闲 VIP**(kk 自动选网卡并漂移,
  同步写入 `control_plane_endpoint.kube_vip.address`),**haproxy** 端点地址填域名或 127.0.0.2;单控制面用 local
- **三种安装方式**: 纯离线(二进制+镜像全本地) / **在线安装**(联网, 国区自动 zone=cn 走国内源)
- **CRI 运行时可选**: containerd(默认, 离线+在线均可) / docker(仅在线安装, K8s ≥1.24 由 kk 自动装 cri-dockerd)
- **部署目录可定制**:containerd/docker 数据目录、etcd 数据目录(写入 `etcd.env.data_dir`, v4 只认 env 下的键)、kubelet root-dir
- **etcd 调优**:心跳/选举/压缩/快照/配额/请求上限/日志级别等 9 个白名单参数(`etcd.env.*`, 见 kk etcd.env 模板)
- **CNI 扩展**:每节点 Pod 子网掩码(`ipv4_mask_size`)、Multi-CNI multus(离线镜像自动收集)、
  各 CNI values 原样透传 helm(`cni.<type>.values`, calico/cilium/flannel/kubeovn 全支持; 页面高级配置按所选 CNI 显示注释默认值模板)
- **DNS 覆盖**:CoreDNS/NodeLocalDNS 镜像 tag 与启停(`dns.*`; 离线镜像清单同步用覆盖后的 tag)
- **运行时参数**:kubelet max-pods/extra_args/extra_config;containerd 静态构建(glibc<2.35 老系统)与版本覆盖
- **containerd 镜像加速**:页面配置(作用于 docker.io 拉取);K8s 组件镜像在离线/zone=cn 下自动走集群内仓库或国内源
- NTP 自定义 / 存储类(localpv、NFS)/ 私有镜像仓库(Harbor/Docker Registry, 需 registry 角色节点) —— 均在页面高级区
- **证书与备份**:安装时 kubeadm certs renew 开关(`kubernetes.certs.renew`, 默认开)+ 续期 crontab
  (部署后写入主部署机 crontab)+ kubeadm 配置带时间戳备份目录(`kubernetes.backup.kubeadm_config_dir`, 留空禁用)
- 升级包:页面选目标版本 → 包内附带 kube 三件套 + upgrade-cluster.sh;目标版本二进制打包时自动下载
- 离线完整性:cache 二进制随包携带 sha256 清单, 部署机校验通过才铺缓存
- 下载源自定义:versions.json `cluster.download_mirrors.github_mirrors`(GitHub 资源镜像列表, 备料脚本与打包时自动下载共用)
- 冒烟回归:`python tests/pack_cluster_smoke.py`(自备物料→打包→断言 11 项产物结构)
- 本地容器端到端验证与修复记录:[docs/k8s-容器验证记录.md](../docs/k8s-容器验证记录.md)

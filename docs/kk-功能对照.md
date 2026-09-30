# KubeKey 能力对照 — 已覆盖 / 暂未覆盖

> 对照基准:kk v4 CLI 与 Config 字段(源码 v4.0.7)。本仓库的集群离线交付围绕
> 「新建集群的常规单/多机场景」, 以下按三类罗列差距, 供排期参考。

## 一、已覆盖(页面/离线包直接可用)

| kk 能力 | 本仓库实现 |
|---|---|
| kk create cluster / delete cluster | 页面打包 → deploy.sh 建集群;uninstall-cluster.sh 卸载 |
| kk add / delete nodes | 改配置重新打包 → 包内 kk add nodes |
| kk upgrade cluster + 组件 | 升级包模式(upgrade-cluster.sh);单组件可用包内 kk upgrade etcd/cri/cni/storageclass |
| kk certs renew | 包内 kk 直接可用(容器验证中已实际使用) |
| kk artifact export | prepare_materials.py --artifact-export 生成配置与一键 .bat |
| 控制面 HA(local/kube-vip/haproxy) | 页面 HA 表单(≥2 控制面联动) |
| kube-proxy 模式(iptables/nftables) | 页面选择 + 内核前置提示 |
| CNI 四选一(calico/cilium/flannel/kubeovn) | 页面选择, 版本按 kk per-minor vars 自动配套 |
| CNI×K8s 兼容矩阵校验 | versions.json cni_matrix + 打包前置拦截 |
| 多节点 SSH(密码/统一凭据) | 页面服务器池 + 角色分配 |
| 离线部署(artifact / 二进制缓存) | 双模式 + sha256 清单 + 打包时自动下载缺失组件 |
| OS 依赖预装(信创 9 发行版) | OS 依赖包随包 + 节点预检自动分发安装 |

## 二、包内 kk 已具备, 页面未包装(直接用包内 kk 即可, 无需开发)

| kk 能力 | 用法 |
|---|---|
| kk web | 内置 Web 管理台(Playbook/Inventory/Task/日志可视化):`./cluster/kk web` |
| kk playbook / kk run | 执行任意自定义 playbook 项目(高级扩展通道) |
| kk artifact images | 从 artifact 向私有 registry 推送镜像 |
| kk create manifest | 生成 artifact 镜像清单模板(备料脚本已覆盖主场景) |
| kk certs renew 周期续期 | `./cluster/kk certs renew -i ... -c ...`(建议每年执行) |

## 三、暂未覆盖(多数可手改生成的 config.yaml 后重新打包达成)

| kk 能力 | 现状 | 备注 |
|---|---|---|
| 私有镜像仓库部署(harbor/docker-registry + HA) | 页面无表单 | config `image_registry.type`;需 registry 节点角色配合(角色已支持) |
| 外部 etcd 集群(独立部署) | 页面无表单 | inventory `etcd` 组指向独立节点 + config etcd 部署类型 |
| 双栈 IPv6 | 页面无表单 | pod_cidr 双族 + inventory internal_ipv6 |
| 多 CNI(multus/spiderpool) | 页面无表单 | config `cni.multi_cni` |
| 存储配置(localpv/nfs storageclass、节点磁盘格式化) | 页面无表单 | config `storage.*` / inventory `storage` 字段 |
| NTP 服务器自定义 | 页面无表单(固定关) | config `native.ntp`(内网 NTP 场景常用, 优先级较高) |
| HTTP 代理 | 页面无表单 | config 代理段(有网代理环境) |
| kubelet/kubeadm 参数自定义 | 页面无表单 | config `kubernetes.kubelet` 等 |
| 节点标签/taint 自定义 | 页面无表单 | config `kubernetes.custom_labels/taints` |
| hooks(pre/post install 脚本) | 页面无表单 | 项目 hooks 目录 |
| 逐节点差异化凭据(密钥/密码混用) | 统一凭据 | inventory `connector.private_key` |
| 集群内 NFS/存储服务器角色 | 角色已支持(registry/nfs 组), 页面无 nfs 角色项 | inventory `nfs` 组 |

## 四、优先级建议(结合离线交付场景)

1. **NTP 自定义**(内网 NTP 源常见, 配置项小)
2. **私有镜像仓库表单**(harbor 型交付常见; registry 角色已有)
3. **存储配置表单**(storageclass 是集群交付的常见后续)
4. 外部 etcd / 双栈 IPv6(按客户环境按需)

# K8s 集群离线部署 — 本地容器验证记录

> 验证时间:2026-09-30 · 环境:Windows Docker Desktop(WSL2 后端,Docker 27.3.1)
> 拓扑:2 台 `--privileged` systemd 容器(kk-node:22.04 自建镜像,sshd + sshpass),静态 IP 172.30.0.11/12
> 物料:cache 模式真实物料(kk v4.0.7-xc1 补丁构建 + kubeadm/kubelet/kubectl v1.34.11 + etcd v3.6.5 +
> cni v1.9.1 + helm v3.18.5 + crictl v1.34.0 + containerd v2.2.6 + runc v1.3.6 + Ubuntu OS 依赖包 124 deb)
> 结果:**deploy.sh 全流程成功**(kk 340 个任务 success 327 / failed 0,双节点注册,
> `kubectl get nodes` 可见 master1/worker1 v1.34.11),幂等重跑验证通过。

## 一、验证通过的能力

| 能力 | 结果 |
|---|---|
| Web 页面勾选集群卡片/参数表单/角色分配 → 一键打包 | 通过(UI 截图见 README) |
| 纯集群包(deploy.sh 薄壳直通 deploy-cluster.sh) | 通过 |
| 包内物 md5/sha256 校验、kk 二进制可执行 | 通过 |
| 节点 SSH 探测(架构/OS/sudo)与 OS 依赖包分发安装 | 通过(124 deb × 2 节点,dpkg rc=0) |
| sudo 缺失自动远程安装 | 通过(worker 无 sudo,自动装好) |
| cache 二进制缓存铺设 + download.fetch=false 离线跳过 | 通过 |
| kk create cluster 全流程(precheck→系统包→CRI→镜像→init→join→CNI) | 通过(340 任务 0 失败) |
| kubectl 验证节点注册 | 通过(master1/worker1 v1.34.11) |
| 幂等重跑(已有集群检测 + 运维指引) | 通过 |

## 二、发现并修复的异常(交付物 bug,均已修复)

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1 | 部署报 `缺少 cluster/kk 二进制` | 打包器对 big_files 统一 644,kk 执行位丢失 | packer.py:kk 入包强制 0755;deploy-cluster.sh 增加解压后 `chmod +x` 自愈 |
| 2 | gather_facts 全部 127 | kk 连接器(本地+SSH)前置 `sudo`,容器/精简系统没有 sudo | deploy-cluster.sh:部署机缺 sudo 自动安装(die 兜底);节点预检增加 sudo 探测+远程安装 |
| 3 | 只预检了第一个节点 | `${CLUSTER_NODES}` 只展开数组首元素 | 改 `${CLUSTER_NODES[@]}` |
| 4 | `v1.34.11 要求 cgroup v2` 误报 | case glob `v1.3[0-4]` 未算 patch 后缀 | glob 补 `v1.3[0-4].*` |
| 5 | worker1 无 OS 依赖包目录时整个预检段被短路 | elif 分支把"无 OS 包"与"跳过预检"混同 | 重构:预检(连通性/架构/sudo)始终执行,OS 包变为可选子步骤 |
| 6 | 内核 semver 检查崩溃(invalid semantic version) | WSL 内核 `6.18.33.2-...` 为 4 段版本,非合法 semver(kk 上游问题,信创新内核同样可能触发) | kk 源码 precheck/os:先取前三段再 semverCompare |
| 7 | kk 报 `etcd inventory group must not be empty` | 生成的 inventory 缺 etcd 组 | packer.py:始终输出 etcd 组(堆叠=控制面节点) |
| 8 | 物料目录里的断点下载残留(*.p0/*.part)会被打进包 | cache 目录只查"非空" | packer.py:物料扫描排除 `*.p[0-9]/part*/tmp` 临时文件 |
| 9 | 集群后置:apiserver 静态 pod CrashLoop(kk 上游时序隐患) | kk 的 certs 角色在 etcd 启动后重新生成了 etcd CA:etcd 服务端证书由旧 CA 签发、apiserver 信任新 CA,两套失配;任一次 apiserver 容器重启即触发 | 容器内以 `kk certs renew` 整批重生成对齐(26/26 成功,etcd 重启后 SERVING 且不再拒绝连接);已作为 kk 源码待修项记录(见下) |

## 二.5、环境级遗留(排查到边界,真实多机环境不存在)

证书对齐后 apiserver 静态 pod 仍不稳定(启动后数十秒被 SIGKILL,exitCode 137)。排查链:
etcd SERVING 正常 → `crictl inspect` 1 秒/38 秒即死 → 无 OOM 记录(容器内看不到宿主 dmesg)→
`ctr -n k8s.io` 手动运行 apiserver 镜像(挂载真实 PKI)启动输出完全正常 →
定位为 **DinD 嵌套环境 kubelet/containerd 层干扰**(外层 Docker Desktop 与内层 kubelet 管理的
containerd 共享 WSL2 内核,静态 pod 的 cgroup/挂载传播在嵌套下不完整)。
kinD 等方案使用专门的节点镜像入口与特权配置绕过此层,非本交付物范畴。
部署脚本与 kk 全流程的成功结论不受影响(340 任务 0 失败发生在同一环境)。

## 三、容器环境限制(非交付物问题,真实服务器不存在)

| 限制 | 现象 | 规避 |
|---|---|---|
| Docker-in-Docker overlay 嵌套 | 容器内 containerd 在 docker overlay 之上再叠 overlay 报 invalid argument,静态 pod 起不来 | 节点容器将 /var/lib/containerd、/var/lib/kubelet 挂独立 volume |
| /etc/hosts 为 bind mount | kk 的 `sed -i`(改名语义)改 /etc/hosts 报 Device or resource busy | 验证配置 `native.set_hostname: false`(容器 hostname 已由 docker 对齐);真实服务器保持默认 true |
| apiserver 静态 pod CrashLoopBackOff 5 分钟退避 | 证书修复后需等退避或等待自动重试 | 等待即可 |

## 四、遗留 kk 上游待修项(源码已定位,后续单独提交)

1. **certs 角色与 etcd 角色的证书时序**:`certs/init` 在 etcd 启动后重新生成 `/etc/ssl/etcd/ssl/ca`(etcd 角色已用旧 CA 签发服务端证书并启动)——重装/续装场景必触发 #9。建议 certs 角色对已存在的 etcd CA 幂等跳过,或 etcd 证书统一由 certs 角色签发。
2. `precheck/kubernetes` 中 nftables 内核检查(5.13)存在与 #6 相同的 4 段版本号问题(默认 iptables 模式不触发)。

## 五、容器验证操作速记

```bash
docker network create --subnet 172.30.0.0/24 kknet
docker run -d --name kk-master --hostname master1 --network kknet --ip 172.30.0.11 \
  --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  -v kk-master-ctd:/var/lib/containerd -v kk-master-ctd-kubelet:/var/lib/kubelet kk-node:22.04
# worker 同理;bundle docker cp 进 master 后 ./deploy.sh
```

## 六、UI 打包流程测试(浏览器自动化,同日)

| 步骤 | 结果 |
|---|---|
| 集群卡片渲染(基础平台区,双架构标签,110MB) | ✓ |
| 勾选卡片 → 部署形态区出现集群参数/OS 包复选/角色分配 | ✓ |
| 状态栏实时合并("中间件 18 项 + K8s 集群 v1.34.11 · 6.23 GB") | ✓ |
| 生成预览 → 「K8s 集群」标签页展示 inventory/config | ✓ |
| Web 打包(集群 + 18 项中间件混合包) | ✓(产物 4323 MB,sha256 生成,完成弹窗含集群提示) |
| arm64 下卡片禁用 + toast 提示 | ✓(代码路径,amd64-only 约束) |

测试中发现并修复 2 个前端 bug:
1. 填写服务器 IP 后角色分配行的节点标签不实时刷新(现 oninput 同步更新标签);
2. 页面刷新后集群勾选状态不回显(applyConfig 恢复配置后未重渲染集群卡片与角色区)。

备注:浏览器后台标签页会被 timer 节流,打包进度弹窗在后台时可能暂停刷新,切回前台即恢复;前台使用无此现象。

## 七、v3 迭代(2026-09-30 第二批 15 项需求)

- 版本目录扩至 kk 全部 15 个 minor(v1.23.17~v1.37.0, 组件版本自 kk per-minor vars 提取)
- 打包时自动下载缺失物料(国内可达源: dl.k8s.io/华为云/GitHub 镜像), 弹窗显示每组件进度
- 集群配置区迁移至第 1 步「基础平台」; K8s logo 换官方七边形舵轮矢量; 修复顶部胶囊宽度不随勾选同步
- 新增配置面: 部署目录(containerd/docker/etcd)、kubelet 参数(max-pods/extra_args/extra_config)、
  containerd 静态构建(glibc<2.35)与版本覆盖、containerd 镜像加速、在线安装模式(zone=cn)、
  NTP 自定义、存储类(localpv/NFS)、私有镜像仓库(Harbor/Docker Registry)、证书自动续期 crontab
- OS 依赖包容器提取修复(容器工作目录问题), rpm 系逐包容错下载
- 回归: 冒烟 11 项 + 全字段 config 断言 17 项全绿

## 八、真机部署验证(2026-09-30,CentOS 7.9 双机)

用户授权两台真实服务器(10.100.11.112/113, CentOS 7.9, 内核 3.10, /data1 数据盘):
- **结果:部署完整跑通,双节点 Ready,全组件 Running,0 异常 Pod**(340 任务)
- 版本适配:CentOS7 内核 3.10 → K8s v1.28.15 + `cluster_require.min_kernel_version` 覆盖 + containerd 1.7.28
- 数据目录全部落 /data1(containerd/docker/etcd/kubelet rootDir/localpv)
- 在线模式 zone=cn 实测可用(镜像从国内源拉取,与用户既有实践一致)
- 真机暴露并修复 3 个 kk 上游 centos7 兼容 bug:
  1. `systemctl show --value` 需 systemd≥230,centos7 的 219 不支持(11 处,改为 `| cut -d= -f2`)
  2. workdir 相对化导致 binary_dir 错位(部署脚本显式 `--workdir` 固定)
  3. "Kubernetes Already Installed" 幂等误判(半成品 kubelet active 即跳过 init)——
     重放场景需先 `kubeadm reset -f` 并清理 kubelet service/二进制(文档化操作路径)
- 部署脚本修复:报告节点清单数组展开、kubectl 非交互 PATH

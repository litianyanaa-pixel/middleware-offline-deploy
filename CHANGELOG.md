# 更新说明 (Changelog)

按时间倒序记录每次功能与修复。`docs/` 演示站随前端改动同步更新。

## 2026-10-14（物料缓存命中展示 + 逐物料子进度条 + 中间件镜像自动补齐）

- **预览页新增「待下载物料」标签页**: 生成预览时即列出本次打包涉及的全部物料(集群控制端 kk/
  artifact、集群二进制、K8s 离线镜像包、私有仓库镜像 tar、CNI/存储 chart、Java 运行时、中间件
  镜像), 深底彩色行渲染——绿色 ✓ 已缓存 / 橙色高亮 ↓ 待下载一眼可辨; 待下载项直接展示下载源
  URL(带序号)并注明「按顺序逐源尝试, 失败自动换下一源(30 秒无速度判失败)」; 汇总「已缓存 N/M」。
  仅静态存在性检查不做网络探测, 预览速度不受影响。
- **物料行内换源(免改配置文件)**: 集群二进制(含 kube 三件套)与 chart 行带「换源」按钮, 展开编辑
  框直接改源列表(每行一个, 支持 http(s):// 与 GH/ 前缀, 按顺序尝试), 保存即写回 versions.json
  的 cluster.download_mirrors.component_sources / chart_sources(新增接口 /api/material_sources,
  校验 URL 协议白名单/条数/键名)并自动重新生成预览——源失效不再需要手工改配置文件。kube 三件套
  与 chart 源相应支持 versions.json 覆盖。
- **物料缓存命中展示**: 打包进度弹窗新增「已缓存 N/M」统计(绿色 chip, 悬浮显示命中名单)——
  warehouse 已有的物料(集群二进制/chart/registry 镜像 tar/k8s 镜像包/Java/中间件镜像)在收集阶段
  逐项打 ✓ 标记跳过下载, 命中明细直接可见; 统计从收集开始一直保留到打包完成(全缓存不触发收集时
  由主页物料清单兜底展示)。
- **速度双通道 + 物料就绪提示**: 修复拉取 docker 镜像期间左下角速度恒显示 0 的观感问题——
  此前速度只统计压缩阶段字节流。现在速度取「当前活跃通道」: 收集阶段=网络下载速率(bytes 型
  子进度增量, 约 8 秒滑动窗口), 压缩阶段=压缩字节流速率; docker pull 无字节回调、窗口无流量时
  明确显示「镜像拉取/处理中(无字节统计)」而非误导性的 0。物料收集转入压缩打包时弹一次
  「物料准备完毕, 开始压缩打包」提示(打包线程阶段文案同步, CLI 日志亦可见)。
- **取消即时生效 + 原子落盘**: 取消信号此前只在压缩打包阶段被检查, 下载/收集阶段完全不响应
  (点取消后 docker pull/文件下载继续跑数分钟); 现在取消钩子(abort_check=prog.check)穿透全部
  下载循环与 docker pull/save(可取消 Popen 轮询, 约 0.4s 内杀进程停止), 并根治两处会把取消
  吞成普通失败的 except(自动补料/中间件拉取的 except Exception; fetch_simple 的网络异常分支;
  prepare_java 换源循环)。落盘全部原子化(先写 .tmp 完整后改名): fetch_simple/fetch_parallel
  合并/OCI tar 合并/docker save —— 进程被杀只会留下不入物料库的 .tmp, 不再可能产生
  「截断但 >1MB 过检」的半截 kube 二进制/镜像 tar。
- **下载源可见 + 卡死快断 + 可自行换源**: curl 全部加停滞检测(30 秒速度 <1KB/s 判失败换
  下一源, 替代此前连接假死最长干等 30 分钟且零反馈); 正在尝试的源/源失败换源实时显示在
  进度状态行(src/srcfail 事件); 组件下载源支持 versions.json
  `cluster.download_mirrors.component_sources.<组件名>` 覆盖默认源(源失效自行改配置换源,
  下载失败提示附指引)。排查确认 crictl/helm 各源 HEAD 均 200——「一直下不了也无提示」实为
  传输假死 + 零反馈 + 取消无效三者的叠加, 由本条与上条共同根治。
- **物料状态全程追溯**: 收集过程四态可见——「· 排队待下载」(提前知道要下哪些)、「下载中」
  实时条、「↓ 已下载」常驻行(修复完成行立即消失导致看不出下载过什么的问题)、「✓ 已缓存」;
  标题行双 chip: 已缓存 N/M + 本次下载 N。prepare_cluster 事件协议扩展 pending/downloaded
  (事件名编码沿用, CLI 旧回调零兼容), Java/中间件镜像/集群物料全链路接入。
- **逐物料独立子进度条**: 打包器所有自动收集链路全部接上子进度: 集群二进制(字节)、chart(字节,
  fetch_simple 新增逐 MiB 回调)、k8s 镜像收集(个数: 拉取镜像 x/N + 导出镜像 tar x/N)、registry
  镜像(不确定进度动画条)、Java 运行时(字节, 曾完全无进度)、中间件镜像(个数 + 当前镜像动画条)。
  进度行按单位分流显示: MB / 个数 / 下载中…(dlc=个数、dli=不确定, 事件名编码, CLI 旧回调零兼容风险)。
- **中间件镜像缺失自动拉取(行为变更)**: 此前 warehouse 缺中间件镜像 tar 时打包直接报错拦截; 现在
  打包时自动 docker pull(国内源候选逐试 + retag 原生名) + docker save 落盘 warehouse(单镜像 save
  规避 Docker Desktop 流式 502), 拉取后仍缺才报错。validate.check_warehouse 移出 validate_config
  (打包器不再被缺物料直接拦截), /api/preview 改为警告提示; docker/compose 安装包仍需手工放置。
- **修复**: ensure_charts 混合缓存时命中项不发 skip 事件的怪癖(曾只在全缓存时发); 收集阶段状态
  文本不再污染阶段标签(file() 的「压缩打包」副作用改由新增 status() 承担); PackProgress 私有属性
  `_downloads` 直捅改为公开 API(download/download_done/downloads_reset)。
- 验证: pytest 71 passed 1 skipped(新增 test_progress.py; test_auto_download_wiring 扩展锁死
  plan/skip/dl/dlc/dli 事件映射; 新增备料层事件协议 + service 镜像三态测试) + build_webui 同步门禁。

## 2026-10-12（上线前全检修复 + 真机部署验证）

- **上线前全检**: 产品 / 安全 / QA 三视角并行审查(代码审查 + 安全审计 + 测试基线核查),
  修复全部 P0/P1 发现。产品结论: 代码达到可上线水平。
- **Web 打包器 API 三道闸(安全修复)**: ①写操作要求 `X-Packer-Token` 请求头(token 由页面
  注入, 浏览器跨站简单请求无法携带自定义头, 挡 drive-by CSRF); ②Host 头回环校验(挡
  DNS rebinding); ③写接口强制 `Content-Type: application/json`(纵深)。GET 页面不受影响。
- **部署脚本注入面根治(安全修复)**: deploy-cluster.sh 中用户可控值(数据目录等)弃用
  sed 占位符 / heredoc 直接展开, 改为 `printf %q` 注入脚本头变量(IMP_*), 引号逃逸
  即 RCE 的链路关闭; 配套校验器白名单加固(Linux 路径字符集、crontab 表达式、YAML
  文档标记)。
- **打包产物权限**: 集群配置(含凭据)在包内 0o600; BUNDLE_README 增加明文凭据警告。
- **测试门禁**: pytest conftest 增加 check() 断言钩子(有失败必 ERROR, 杜绝"记录了但
  没人看"的虚假绿灯); CI 跑全量测试 + 三个部署脚本 bash -n + shellcheck。
- **部署健壮性**: 打包自动补料(compose/registry 物料)接线修复; 镜像 save 失败路径
  清理残缺 tar; docker 引擎未就绪时自动拉起自愈; 节点 docker < 25 版本预检 fail-fast
  (离线镜像为 OCI 合并 tar, 旧引擎 load 不支持)。
- **真机验证**: 本地安全矩阵 curl 8/8(伪造 token/Host/Content-Type 全拒) + jsdom 全链路
  e2e 5/5(从真实 HTTP 服务取页面防假阳性) + 真实打包产物核验(690MB 包内权限/manifest);
  **真实服务器部署验证**(CentOS 7.9 + docker-ce 26.1.4, 两台): deploy.sh 中间件九步
  全过、健康实测通过、密码替换正负验证全过(mysql/redis 对密码通过、错密码/无密码拒绝),
  测试后服务器恢复原状。

## 2026-10-10（docker CRI 离线支持 + 私有仓库账号密码/数据目录）

- **docker 运行时离线可用**: 去掉「docker 仅在线安装」限制。docker 静态二进制 tgz 与
  cri-dockerd 按 kk 缓存布局(binary_dir/docker|cri-dockerd/...)随包携带(版本对齐 kk 各
  minor 内置的 docker_version, 如 v1.28→28.5.2 / v1.30+→29.6.2), 部署脚本在无 docker
  节点预装(写 daemon.json: data-root 与 config 一致 + systemd unit)并以 `docker load`
  导入离线镜像; 离线模式 kk config 写 `cri.docker.daemon_policy: override`, kk 以同版本
  跳过重装并重写 daemon.json(带镜像加速器), 数据目录一致重启不丢镜像; artifact 模式 +
  docker 给出建议 cache 的告警。备料多源(阿里云/USTC/docker 官方静态包), 打包时按需
  自动下载(containerd 用户不会被连带 87MB 的 docker 包)。
- **私有镜像仓库账号密码**: 「私有镜像仓库」新增账号/密码(留空走 kk 默认 admin/Harbor12345,
  只填一个拒绝), 写入 kk config `image_registry.auth`, 由 kk 下发到仓库部署配置与各节点
  CRI 配置(containerd hosts.toml / docker certs.d)。
- **私有仓库数据目录**: docker-registry 类型新增数据目录(默认 kk 的
  /var/lib/docker-registry/data), 写入 `docker_registry.storage.filesystem.rootdir`;
  并修复 kk 原生 compose 不挂数据卷的缺陷——部署完成后自动把数据目录 bind 进
  docker-compose.yml 并重启仓库容器(幂等, 数据真正落盘到主机)。
- **私有仓库离线物料补齐**: docker-compose 二进制与 registry:2.8.3 镜像 tar(打包时
  docker pull+save 自动收集)随 kk 缓存携带, 修复离线部署私有仓库缺料问题。
- **UI**: 切换仓库类型即时显示/隐藏账号密码数据目录(修复选了类型表单不刷新); 双语
  README 同步。
- 验证: pytest 62 passed 1 skipped(新增 registry auth/rootdir、docker daemon_policy、
  manifest CLUSTER_CRI/DOCKER/REGISTRY 变量、deploy 脚本分支断言) + jsdom 全链路端到端。

## 2026-10-10（角色多选 + kube-proxy ipvs 模式）

- **集群角色分配支持多选**: 每节点由单选下拉改为「控制面 / 工作节点 / 镜像仓库」三个
  复选框, 同一台机器可兼任多角色(如控制面兼工作节点); 兼任控制面的节点在 kk Inventory
  中同时进入 kube_control_plane 与 kube_worker 组(kk 官方语义), 并提示默认 NoSchedule
  污点需手动去除才能调度业务 Pod; 兼容旧版单值配置。
- **kube-proxy 模式补齐 ipvs**: 下拉新增 `ipvs (大集群推荐, 需内核 IPVS 模块)`, 选中时
  打包告警提示节点需加载 ip_vs/ip_vs_rr/ip_vs_wrr/ip_vs_sh/nf_conntrack 模块(缺模块时
  kube-proxy 会静默回退 iptables); 写入 kk config `kube_proxy.mode: "ipvs"`。
- **修复**: 部署私有镜像仓库时校验器在角色解析前引用未定义的节点列表会直接报错
  (角色解析前移至镜像仓库校验之前)。
- 新增单测 test_cluster_multi_role_and_ipvs_proxy_mode + jsdom 全链路端到端验证
  (复选框→localStorage→/api/preview→Inventory 双组/config ipvs/双重告警); pytest 61 passed 1 skipped。

## 2026-10-10（Java 表单合并两行 + 角色分配间距收紧）

- **Java 运行时表单合并**: 「启用/版本」合为一行、「默认版本/Java 安装目录」合为一行
  (未启用时仅显示启用行); 标签 140px 对齐规范不变。
- **集群角色分配间距**: 双列网格中节点标签不再拉伸占满单元格, 紧贴自己的角色下拉框。

## 2026-10-10（角色分配双列 + 镜像加速归位 + Java 真机安装测试）

- **集群角色分配双列显示**: 节点角色下拉改为两列网格(与表单区同宽 780px), 每格
  「节点别名 · IP」左对齐 + 角色下拉 150px, 服务器多时更紧凑。
- **镜像加速归位**: 「镜像加速 (docker.io)」从证书/NTP 附近移到「containerd 版本覆盖」
  行的下一行(运行时/镜像相关字段聚拢), 相关提示文案同步更新。
- **Java 运行时真机安装测试通过**: 用打包管线生成纯 Java 节点安装脚本 + 真实 Temurin
  物料, 在 Ubuntu 22.04(kk-node) 容器实测两轮:
  - 单版本 jdk17: sha256 校验 → 解压 `/data/java/jdk17` → `java -version`
    (Temurin-17.0.20.1+1) → `/etc/profile.d/java.sh` 写入 `JAVA_HOME=/data/java/jdk17`;
  - 多版本 jdk8+jdk17 共存: 两版本并行解压独立可运行(`1.8.0_504` / `17.0.20.1`),
    默认版本 JAVA_HOME 指向 jdk17;
  - 幂等重跑: 第二次执行识别已安装并跳过, 不重复解压。

## 2026-10-09（集群高级配置: 部署顺序重排 + 默认值注释模板 + 全 CNI values 覆盖）

- **高级配置字段按 kk 部署顺序重排**: 运行时/数据目录 → kubeadm 备份目录(置顶, 配置生成
  先于 etcd/kubelet 安装) → etcd → kubelet → CNI → DNS → 证书 → 镜像 → NTP → 存储 → 私有仓库。
- **默认值注释模板预填**: etcd 高级参数(9 个白名单参数+kk 默认值)、kubelet 扩展参数/扩展配置
  的输入框预填带中文注释的常用默认值(行首 `#` 不生效), 取消注释即生效; 后端校验跳过注释行。
- **CNI values 覆盖泛化到四种插件**: 此前无论选什么 CNI 高级配置恒显示「Calico values 覆盖」;
  现按所选 CNI 动态显示对应 values 模板(calico/cilium/flannel/kubeovn, 各插件覆盖内容独立保留),
  后端写 `cni.<type>.values` 透传 helm -f(kk v4 四种 CNI 角色均支持, 已核对上游 role tasks),
  注释-only 内容归一为空不写入 config, 非当前 CNI 的 values 忽略并告警。
- 新增测试 `test_cluster_cni_values_per_plugin_and_comments`(values 透传/注释跳过/非法 YAML 拒绝)。
- i18n: CNI values 行标签与提示词条、高级配置 summary 概要更新。

## 2026-10-09（K8s 集群配置全折叠化）

- **集群角色分配 / 集群基础配置改为可折叠**: 与集群高级配置、Java 运行时同款 `<details>`
  折叠样式(▸ 旋转箭头 + 圆角边框), 重渲染后保持展开状态。
- **状态徽章**: 角色分配折叠头显示当前分配概要(如「1 控制面 · 2 工作节点」, 未分配时置灰
  「未分配」); 基础配置折叠头显示「K8s 版本 · CNI」概要。
- 高级配置的展开状态保持逻辑改按 `data-dtl` 标识区分(此前取首个 details, 多折叠块后失效)。
- Java 运行时块同步移至中间件底座卡片最底部(镜像加速器之后), 池联动保留。
- i18n: 新增「未分配」词条。

## 2026-10-09（目录命名区分 + 启用行对齐）

- **两个安装目录明确区分**: 「部署目录」标签改为「**中间件部署目录**」(提示:
  docker-compose 应用安装于此: compose / .env / 数据 / 日志 / 配置), Java 区块的「安装目录」
  改为「**Java 安装目录**」——中间件与 Java 各用各的目录, 不再混淆。
- **启用行对齐修复**: Java 启用行复选框标签补 `height:32px`(与右侧 32px 行高的行标签垂直
  居中对齐, 修复复选框文字偏高)。
- i18n: 新增 中间件部署目录/Java 安装目录/新提示 的英文词条; Java 提示词条同步最新文案。

## 2026-10-09（Java 运行时: 移入中间件底座卡片）

- **区块位置再调整**: Java 运行时折叠块从「部署服务器池」移入「中间件底座」卡片, 紧跟
  「目标服务器架构」选择之下——先选架构再配 Java 的顺序更自然(物料按架构准备); 卡片标题
  说明同步改为「Docker/Compose 与 Java 运行时离线安装」。
- 实现拆分: `renderPool` 只渲染服务器池, 新增 `renderJavaCard`(渲染 #javaBox)与
  `bindJavaEvents`(Java 事件); 服务器池增删/改名/改 IP 仍联动刷新 Java 部署目标标签;
  CSS 作用域 `#poolBox .jdetails/.jbadge/.kform` → `#javaBox`。
- 部署目标提示补充「从上方部署服务器池选择」; jsdom 端到端验证: 区块位置/启用表单/折叠
  状态保持/版本多选/池→Java 标签实时同步/localStorage 持久化全部通过, 无 JS 报错。

## 2026-10-09（Java 运行时: 第二轮反馈修正）

- **默认安装目录改为 `/data/java`**（原 `/usr/local/java`）: 前端默认值/兜底、validate 归一化、
  manifest 与多机节点脚本默认值、测试断言全链路同步; 已有自定义目录的配置不受影响。
- **Java 折叠框宽度自适应**: 去掉 `max-width:820px`, 灰框跟随卡片宽度铺满(与集群高级配置一致)。
- **架构切换可见反馈**: 切换 x86_64/ARM 后始终 toast 确认; 切到 ARM 时若自动取消了 K8s 集群
  (集群暂仅 x86_64)或 MySQL 5.7, 明确说明原因。经 jsdom 全页加载实测: arch 选择器绑定正常、
  切换生效、无 JS 报错——此前"不能选择"多为旧页面缓存/未感知 ARM 下集群自动取消所致, 请
  Ctrl+F5 强刷后重试。
- **物料仓库 Java 行按架构拆分**: 每个版本拆为 x86_64/aarch64 两行(原聚合为一行), 架构筛选
  可用, 状态逐架构准确; arm64 的 JDK 8/21 物料已下载补齐, 双架构 6 个包全部就绪。
- UI 提示补充: `/etc/profile 登录时自动加载 /etc/profile.d/*.sh`。

## 2026-10-09（新增: Java 运行时作为基础中间件）

- **Java 运行时(Temurin JDK 8/17/21, 双架构)作为基础中间件集成**, 非插件形式(不进 compose
  服务网格), 与 Docker 同层的宿主机运行时; 配置区块在「部署服务器池」卡片顶部: 启用开关 +
  版本多选 + 默认版本 + 部署目标(本机/每台服务器勾选), arm64 完整支持。
- **物料下载器 `prepare_java.py`**: Adoptium API 运行时解析各 major 最新 GA(文件名/sha256/
  链接), 下载走清华 TUNA → GitHub → GH 三镜像, 4 连接分段下载 + sha256 校验, 结果回填
  versions.json 的 `java.resolved` 供离线打包复用; `--check`/`--download` CLI; 打包时缺料
  自动补齐(与集群物料同模式)。
- **部署链路**: 包内顶层 `java/jdk<N>.tar.gz` 共享单份 + `jdk.sha256` 清单; deploy.sh 新增
  `install_java()`(【0.3/9】, 集群部署前执行): sha256 校验 → 解压 `/data/java/jdk<N>`
  (`--strip-components=1`, 幂等跳过已装) → 写 `/etc/profile.d/java.sh`(JAVA_HOME 指向默认
  版本) → `java -version` 实测; 多版本共存, 未勾选部署目标的主部署机自动跳过。
- **多机支持**: 服务器池按勾选分发(可勾纯 Java 节点——该节点无需 Docker); 纯 Java 节点生成
  精简 install-node.sh(不含容器段), 混合节点 Java 段置于 Docker 检查之前; distribute.sh 预检
  按节点类型条件化(docker 检查/仅 SSH), Java 物料随 tar 管道分发到节点。
- **校验与测试**: pytest 新增 5 个 Java 用例(归一化/多机节点计划/纯 Java 脚本/manifest 变量/
  目标豁免), 真实打包验证(java/ 产物结构 + manifest 变量 + 三版本 sha256 逐一致); 物料仓库
  页签新增「Java 运行时包」状态行(就绪/打包时下载); 演示站快照同步。
- **新依赖**: prepare_java.py 需打包机联网(仅首次), api.adoptium.net 获取元数据 + 清华/GitHub
  镜像下载; resolved 回填后离线打包不再需要外网。

## 2026-10-09（Java 运行时: 六点反馈修正）

- **区块位置**: Java 运行时折叠块移到「+ 添加服务器」按钮下方(不再在服务器池卡片顶部)。
- **自定义安装目录**: 新增 `install_dir` 配置(默认 `/data/java`), 表单可编辑, validate
  归一化(绝对路径/禁特殊字符), install 脚本与 manifest 全链路生效。
- **折叠样式对齐集群高级配置**: `<details>` 折叠 + ▸ 旋转箭头 + 状态徽章, 重渲染后保持展开
  状态; 表单字体统一为与集群/K8s 表单一致的 kform 规范。
- **部署目标标签实时同步**: 服务器输入框的 IP/别名变更时, Java 部署目标勾选行标签即时更新
  (oninput 直改文本, 不重渲染、不抢焦点), 与 K8s 角色标签同一事件链。
- **修复纯 Java 目标节点计划缺口**: 无任何多机拓扑时 `gen_nodes` 直接返回空, 勾选的服务器
  Java 目标被忽略; 现按 `targets` 是否含非 local 判断, 纯 Java 目标节点同样建计划并生成
  精简安装脚本(端到端验证: 计划/脚本变量/manifest 变量/bash -n 全通过)。
- UI 文案补充 Temurin 说明(Eclipse Temurin, Adoptium 发行版); pytest 59 passed 1 skipped,
  双冒烟 11+11 全绿, 演示站快照同步。

## 2026-10-09（代码复查: 六处小修复）

- **物料仓库「离线镜像包」行误显示就绪**：后端 catalog 接口把该键从布尔改为"已收集 CNI 名数组"
  后，物料仓库页签仍用 `!== false` 判定——空数组(尚未收集任何镜像包)会被当成"就绪"；现按数组
  长度判定，与集群卡片「缺料徽章」同口径（卡片侧 `clusterMaterialStatus()` 本就按 `includes(cni)`）。
- **部署形态切回单机时 kafka multihost 状态被错误覆盖**：重置块的 `else` 挂在了 redis 判断上，
  kafka 切回 single 会被重置成 mysql 形状（丢 count/brokers 结构，下游有兜底未产生坏产物）；
  改为 `else if` 三分支明确归位。
- **style.css 孤儿关键帧碎片清理**：`body::before` 环境光后残留的两个裸 `50%{...} 100%{...}`
  关键帧选择器（历史删除 @keyframes 只删了头尾），浏览器静默丢弃，已删除。
- **web.py CNI 段提取健壮化**：离线镜像包文件名 `k8s-<ver>-<cni>-amd64-images.tar` 的 CNI 段
  此前用 `split("-")[2]`（依赖 CNI 名无短横线，当前四个 CNI 均满足），改为剥固定前缀/后缀提取，
  未来新增含短横线的 CNI 名也不会切错；顺带修整该行被压缩成单行的书写异常。
- **nginx 整站反代 upstream 防御加固**：`mode=proxy` 的 `location /` 直接引用 `px_<n>` upstream，
  但 upstream 只在 `api_prefix` 非空时生成——现被 validate_config 兜住（proxy 模式强制
  `api_prefix="/"`），仅绕过校验直调才会生成坏配置；现 proxy 模式无条件生成 upstream。
- **打包 arcname 去版本硬编码**：docker/compose 安装包在包内的路径此前硬编码
  `docker-29.8.0.tgz`/`docker-compose-linux-<sub>`，与 versions.json 登记路径存在双份版本号；
  现从 catalog 路径取实际文件名（deploy.sh 本就按 `docker-*.tgz`/`docker-compose-linux-*`
  通配引用），未来升级版本只改 versions.json 一处。
- 测试：pytest 53 passed 1 skipped；真实打包冒烟 11 项 + 集群场景 11 项全绿；
  build_webui 同步校验通过。

## 2026-10-09（修复演示站主页截图 404）

- **docs/index.html 截图更新**：演示站（GitHub Pages，发布源为 `docs/` 目录）主页「界面一览」
  仍引用重制前的旧图 `images/ui-*.png`（已删除）导致全部 404；现更新为 11 张中文新截图
  `images/zh/ui-*.png`（与 README 界面导览/K8s 集群章节一一对应），并补充英文版 README_EN 链接。

## 2026-10-08（README 截图双语重制 + 补料分流 + 英文残留清零）

- **README 截图全面更新**：11 张界面截图按当前向导式界面重拍，中文 README 配中文界面截图
  (`docs/images/zh/`)、英文 README 配英文界面截图 (`docs/images/en/`)，移除旧版单页 UI 的过时截图。
- **补料入口分流**：「补料脚本」按钮的 K8s 提示改为只报当前所选集群版本(与集群卡片缺料徽章同源，
  复用 `clusterMaterialStatus()`)，不再罗列全部 11 个版本的下载命令；K8s 物料与中间件物料分开提示，
  `pull_missing_images.sh` 头部注明"仅中间件镜像物料, K8s 物料打包时自动补齐或用 prepare_cluster.py"。
- **英文模式残留清零**：反代表单(Static+API/Upload limit/Enable)、集群高级配置折叠区(kubelet 扩展、
  etcd 调优、Calico values 等占位符)、OS 依赖闭源提示、预览面板后端生成注释(compose/manifest/集群
  config 头部)、仓库弹窗、HA/连接账号等 disabled 输入值——共补 30+ 词条；两处硬编码中文输入值改为
  渲染时过词典；「清空选择」「完成」等遗漏词条补齐。
- **英文模式步骤胶囊错位修复**：词条替换会改变页签宽度，`gotoView` 延迟 350ms 按最终宽度重校胶囊，
  i18n MutationObserver 翻译落定后防抖重算，初始加载后同样重算——英文界面胶囊不再停留在错误页签上。

## 2026-10-08（英文翻译补全 + toast 堆叠淡出动画）

- **中英互译补全**：修复 `toast()`/`msg()` 只按 `zh|en` 管道分流、从不查英文词典的问题——
  英文模式下所有无 `|` 的动态提示(打包进度、读写分离开关、物料缺失、部署形态切换等)此前全是中文；
  `applyI18n` 对 title/placeholder 先切 `|` 半边再清残留中文(修「补料脚本」按钮 title 中英混排)；
  词典补齐 60+ 词条(K8s 物料三态行、服务器池表头、拓扑选项、步骤空态、镜像源说明、代理优化整句等)；
  英文模式全页扫描残留中文清零(仅语言切换按钮显示「中文」属预期)
- **多条 toast 堆叠淡出生硬**：此前上方提示消失瞬间，下方提示直接跳上来；
  现在淡出时高度/内边距同步收缩(300ms ease)并抵消 flex gap 残留，
  下方提示跟随平滑上移(rAF 采样 66 帧验证连续过渡)
- GitHub 仓库 About 补英文介绍(双语描述)并新增 topics(kubernetes/kubekey/air-gapped 等 15 个)

## 2026-10-08（物料仓库 K8s 独立页签）

- 「物料仓库」弹窗顶部新增两个页签：**中间件物料**（Docker/Compose 安装包 + 各中间件镜像，
  原「类型/架构」筛选只作用于这一页）与 **K8s 集群物料**（kk 二进制、各版本 artifact 产物/
  二进制缓存/离线镜像包、OS 依赖包，独立成页不再和中间件混在一张表里）；
  K8s 物料均为 amd64 且不分安装包/镜像，切到该页签时类型与架构筛选自动隐藏

## 2026-10-08（读写分离配置残留修复）

- 勾选过读写分离后，取消部署对应数据库（或把部署形态切回单机、切 arm64 架构）时，
  「端口与反代」里的读写分离端口组不再残留：开关与端口随服务取消一并清除，
  重新勾选也不带旧状态；恢复/导入一份带残留开关的旧配置同样自动对齐当前勾选。
  端口组渲染加了双保险——只有"该 MySQL 仍勾选且为主从形态"时才显示

## 2026-10-08（三条界面问题修复：端口标红/读写分离归属/待配置误报）

- 多机部署同端口不再标红：端口实时校验与打包校验同口径——多机的 MySQL 主从、Redis 哨兵
  各角色分布在不同机器，主/从/哨兵同端口合法（Kafka 多机节点组端口本就每台一致）；
  同机主从同端口仍会标红（那才是真冲突）
- 读写分离改为每个主从 MySQL 独立一个 ProxySQL 实例：同时部署 MySQL 5.7 与 8.0 时各自有
  独立开关并明确标注"只针对哪套主从"，独立入口/管理端口（8.0: 16033/16032，5.7: 16035/16034），
  配置不再混在同一对 hostgroup 里（旧版两套集群的读流量会互串）；顺带补上同机主从的
  读写分离支持（旧版会生成空 mysql_servers 的废配置）；旧配置（布尔开关/单实例端口键）
  自动迁移，单实例时容器名仍为 proxysql，部署布局不变
- 「账号与数据」tab 的"1 项待配置"误报修复：按实际勾选的应用逐项判断（旧版勾了 nacos/xxljob
  任一个就要求两个都初始化）；告警 Webhook 这类后端允许留空的项不再计入待配置
- 测试：pytest 53 passed 1 skipped（新增双实例隔离与同机主从读写分离用例、旧端口键迁移用例）；
  浏览器实测三场景通过

## 2026-10-08（重构后文档同步）

- 文档对齐 packer.py→packerlib/ 与 packer.html→webui/ 拆分：README 目录树把 packerlib
  14 个模块列全；README_EN.md 目录结构整段同步（packerlib/ / webui/ / build_webui.py +
  开发提示，顺带补上此前缺失的 prepare_cluster.py / deploy-cluster.sh / warehouse/cluster
  等 K8s 条目）；docs/k8s-README.md 与 docs/k8s-信创离线部署.md 中"逻辑都在 packer.py"
  的旧指向改为 packerlib/cluster.py 等。`python packer.py` 用法不变，无需改操作习惯

## 2026-10-08（集群高级配置折叠状态不再被联动重渲染重置）

- **修复**：勾选 K8s 集群后，展开「集群高级配置」折叠块，只要在别处切换选项（安装模式
  纯离线/在线、HA 类型、节点角色、一键填源、增删服务器等都会触发 `renderInfraTopology()`
  重建该区块），折叠块就被强制收起。现在重建前读取当前 `<details>` 的展开状态并原样
  继承，展开与否完全由用户控制（收起后重渲染也不会被强制展开）
- 浏览器实测：展开后切换安装模式/角色分配保持展开，手动收起后重渲染保持收起；
  pytest 52 passed 1 skipped

## 2026-10-08（packer 代码模块化拆分）

- **packer.py(4490 行)拆分为 packerlib/ 包(14 个功能模块)**：paths / util / catalog /
  validate / nacos_sql / compose / cluster / manifest / multinode / nginx_conf /
  progress / builder(打包主流程) / web / cli，按原分区注释按 AST 节点切分，跨模块依赖
  显式导入；packer.py 保留为**兼容薄壳**（`python packer.py` 各命令与
  `import packer` 用法不变，98 个顶层符号全量保留，`--dump-catalog` 输出与拆分前
  逐字节一致）
- 镜像 tar/补料脚本辅助函数（missing_materials / gen_pull_script / gen_retag_mirrors_sh
  等）从 web 段并入 builder 模块——本质是打包逻辑，同时消除 pack↔web 循环导入；
  模块改名 builder 避免与导出函数 `pack` 同名遮蔽；测试里 2 处 monkeypatch 相应
  改打实现模块（`packerlib.validate.check_warehouse` / `packerlib.builder.missing_materials`）
- **packer.html(2582 行)拆分为 webui/ 源文件 + build_webui.py 构建脚本**：
  template.html(结构, 231 行) / style.css(361 行) / app.js(1992 行)，构建产物与拆分前
  **逐字节一致**；packer.html 保持单文件交付形态（离线可直接打开），webui/ 是维护形态
  （改样式/脚本不用在 2500 行里翻找）
- **改 UI 流程**：改 `webui/` 源文件 → `python build_webui.py`（自动同步根目录
  packer.html 与 docs/ 副本）；新增防陈旧测试 `test_webui_sources_build_sync`
  （改了源忘构建会被 CI 拦下，已验证污染源文件时测试变红）
- 回归：pytest **52 passed**（新增 1 条）/ 冒烟打包 11 项 / 集群场景 11 项全绿；
  拆分前文件快照备份于 `backups/20261008-pre-split/`（本地留档，不入库）

## 2026-10-08（K8s 集群配置对齐 kk v4 + 多机同端口）

- **逐条对照 KubeKey v4 源码核查打包器生成的 config.yaml，修三处键位差异、补齐缺失能力**：
  - **etcd 数据目录键位修正（重要）**：v4 只读 `etcd.env.data_dir`（kk etcd 模板逐键消费
    `.etcd.env.*`），此前生成的顶层 `etcd.data_dir` 是旧版 kk 的键，v4 静默忽略 → 数据仍落
    `/var/lib/etcd` 而非自定义目录；已移入 `env` 并透出 etcd 调优 9 参数（心跳/选举/压缩保留/
    快照/后端配额/请求上限/max_snapshots/max_wals/log_level，白名单校验）
  - **kubelet 无需改**：`kubelet_args` 列表是 kk v1~v3 旧格式，v4 走 `kubelet.extra_args` map
    （kubeadm v1beta4 经 mapToNamedStringArgs 自动加 `--` 前缀），打包器现有生成即正确
  - **localpv 无需改**：v4 键就是 `storage_class.local.path`（`base_path` 是 kk v3 旧键）
- **控制面 HA 补 VIP 表单**（此前 kube-vip/haproxy 选了也无处填 VIP）：kube-vip 模式除
  `control_plane_endpoint.host` 外同步写入 `kube_vip.address`（空值会让静态 Pod 拿到空
  vip_address；kk 按该地址所在网段自动发现网卡并漂移 VIP）；haproxy 模式提示填域名或 127.0.0.2
- **证书与备份入口**：安装时续期开关（`kubernetes.certs.renew`）+ 续期 crontab（部署后写入
  主部署机 crontab 执行 `kk certs renew`，此链路原已存在仅缺 UI）+ kubeadm 配置带时间戳备份
  目录（`kubernetes.backup.kubeadm_config_dir`，kk post_install 每次部署自动备份，留空禁用）
- **CRI 运行时选择**：`cri.container_manager`（containerd 默认 / docker 仅在线安装——纯离线
  物料与预装流程只含 containerd，K8s ≥1.24 的 docker 由 kk 自动装 cri-dockerd）
- **CNI 扩展**：每节点 Pod 子网掩码 `cni.ipv4_mask_size`、Multi-CNI multus（含镜像 tag，
  离线镜像清单同步收集 `multus-cni`）、**Calico values 原样透传 helm**（`cni.calico.values`，
  v4 无独立 ipipMode/vxlanMode/mtu 键，靠 `-f` 自定义 values；YAML mapping 前置校验，非
  calico 自动忽略并告警）
- **DNS 覆盖**：CoreDNS/NodeLocalDNS 镜像 tag 与启停（`dns.*`；离线镜像清单同步用覆盖后的
  tag，关闭 nodelocaldns 时不再收集对应镜像）
- **多机主从允许主从同端口**：多机形态下从库/哨兵端口只存在于远端节点，放开与主库等端口的
  全局冲突校验（mysql8/mysql57/redis）；单机主从仍同机查重；kafka 多机节点组端口本就每台一致
- **kk-功能对照表重写**：NTP/存储/私有仓库/多CNI/kubelet/etcd 调优等十余项早已落地表单的
  能力从"暂未覆盖"移入"页面已有表单"，遗留项收敛为外部 etcd/双栈/HTTP 代理/BGP 等
- 回归：tests 51 passed 1 skipped（新增 etcd env/HA VIP/CRI+CNI+DNS/校验矩阵/多机同端口
  5 组用例）；docs/packer.html 已同步

## 2026-10-08（静态演示站同步机制 + 部署位置提醒）

- **修复多机部署从库端口输入框显示 undefined**：端口映射卡片追加的从库/哨兵端口定义
  缺 `default` 字段（mysql8_replica / mysql57_replica / redis_replica / redis_sentinel），
  首次渲染把 undefined 写进输入框；现与后端默认值对齐（13308/13309/16380/26379）。
  同时第一个端口标签由「MySQL 8.0 端口 / MySQL 5.7 端口 / Redis 端口」改为「主库端口」
  （中英双语），从库端口标签区分多机（每台从机）与同机语境；
  顺带补齐单机主从双从库的「从库2端口」（13310/13311）与单机哨兵的「从库端口」输入框
  （此前 UI 未渲染、只能用后端静默默认值）

- **修复 Pages demo 只显示最初 7 个应用**：docs/ 静态站此前是手工拷贝的陈旧副本
  （versions.json 只有内置 7 服务、packer.html 落后于纯离线/在线两模式产品化）。
  新增 `python packer.py --dump-catalog docs/versions.json` 一条命令导出合并服务目录
  （内置 + 13 个插件中间件共 20 个），docs/packer.html 直接同步根目录版本，
  静态退化逻辑补透传 cluster 字段（演示页恢复 K8s 集群卡片）；
  新增防陈旧测试 `test_docs_static_site_packer_html_sync` /
  `test_docs_versions_json_has_plugins`（新增插件忘记重新生成会被 CI 拦下）；
  plugins/README 上线步骤补第 4 步
- **部署位置提醒**：打包器界面（部署目录下方 + 部署服务器池）、README 中英版「服务器部署」
  章节、落地页 docs/index.html 均明确提示——**中间件默认部署在交付包所在服务器**
  （运行 deploy.sh 的机器），跨服务器部署需配置多机拓扑
- 清理 test_packer.py 尾部重复定义的 test_deploy_sh_retag_integration（heredoc 编辑遗留）

## 2026-10-01（多机部署真机首验 + 异常总账整合）

- **多机部署真机首验通过**：mysql8 + mysql57 双主从跨机（112 主部署机就地装双主库 +
  113 SSH 分发双从库），deploy.sh → distribute.sh → install-node.sh 全链路真机跑通；
  复制取证 IO/SQL 线程 Yes、延迟 0、主库写入端到端同步、从库 super_read_only 写保护、
  幂等重跑通过；kafka 集群(3/5 台)与 redis 哨兵(3 台)需第三台真机，产物级由单测覆盖
- **多机链路修复 9 个缺陷**（tests/test_packer 30 项回归）：
  节点 MySQL 容器名/服务键按服务名生成（双 MySQL 同节点不再撞名）· 节点数据目录按服务名
  隔离（同节点双实例不再共用 datadir）· distribute.sh 节点包解到 $DEPLOY_DIR/nodes/<名>/
  子目录（主库节点为主部署机自身时不再覆盖主包 compose/.env）· 本机节点 is_local_ip
  就地安装（免 tar 环回传输的 file changed 失败）· deploy.sh 6 处 cp 同文件 -ef 守卫
  （包解进部署目录不再 set -e 中断）· 多机导表客户端镜像检查移入实际导表分支 ·
  自定义拓扑端口(mysql*_replica 等)不再被静默丢弃 · 从库初始化 SQL 幂等化
  （STOP+RESET 前缀，重跑不再 ERROR 3081）· 从库补 super_read_only（与单机主从行为一致）
- **文档整理**：docs/问题修复全记录.md 成为全项目唯一异常总账（68 条，《测试环境问题记录》
  并入为第四部分）；新增 docs/参考文献.md（全部参考过的文献/官方文档/开源社区索引）；
  README 新增「多机部署」章节与「文档索引」

## 2026-09-29

- **备份计划按数据库拆分**：MySQL / PostgreSQL / MongoDB 各自独立的启用开关、备份日、备份时间与保留份数,
  部署时写入三条独立 crontab; 生成预览与《部署报告》分别列出每库的备份计划
- **备份区按所选数据库动态展示**：勾选了哪些库就显示哪些引擎的说明(mysqldump / pg_dump -Fc / mongodump),
  顶部列出"备份对象"清单, 多选时明确提示"N 种数据库将同时纳入备份计划"; 默认备份目录改为 /data/backup/db(每库独立子目录)
- **分区序号动态重排**：隐藏的配置分区不再占号, 消除"账号密码 1 → 数据库备份 3"跳号问题
- **修复**：backup.sh 轮转函数在备份目录为空时因 `set -o pipefail` 误退出(ls 无匹配), 改用 find 实现
- **修复**：打包器所有 HTTP 响应补 `Cache-Control: no-store`, 根治浏览器缓存旧页面导致"新功能看不到"
- README 中英文档全部 7 张 UI 截图更新为当前版本(含 Alertmanager / Kafka 双形态 / 三库独立备份计划)

## 2026-09-28（晚间）

- **备份恢复扩展到 PostgreSQL / MongoDB**：backup.sh / restore.sh 支持三种引擎——
  MySQL(mysqldump --single-transaction)、PostgreSQL(pg_dump -Fc)、MongoDB(mongodump --archive --gzip),
  全部在容器内执行零宿主机依赖, 按库分文件、保留 N 份自动轮转; 恢复前自动生成安全备份(pre_restore_*), 支持 --yes 免交互
- **一键卸载 uninstall.sh**：停容器 → 清理备份 crontab → 默认保留全部数据与物料, `--purge-data` 才删数据目录
  (仅限部署目录内挂载点, 需二次确认输入 DELETE); 删除失败时如实告警不中断
- **Alertmanager 告警链路闭环**：新插件 prom/alertmanager v0.28.1(双架构), 告警 Webhook 地址可空可校验;
  选配后 Prometheus 自动生成抓取配置与告警规则并挂载, "指标监控套件 / 可观测全家桶"同步纳入
- **物料补齐一键化**：物料仓库抽屉新增"补料脚本"按钮——盘点双架构全部缺失物料并生成
  pull_missing_images.sh(docker pull 多镜像源回退 → crane 兜底, tar 自动落位)
- **工程质量**：新增 tests/test_packer.py(18 用例)与 tools/local_e2e_test.py(真实容器端到端:
  起栈 → 三库备份 → 毁数据 → 恢复校验 → 告警注入 → 卸载); GitHub Actions CI(pytest + bash 语法 + shellcheck + 打包冒烟)
- Toast 提示改为平滑消失动画; Kafka 版本号/镜像体积按部署形态动态显示(单节点 4.3.1 / 集群 3.7.0)

## 2026-09-28

- **全项目中英双语**：打包器顶栏新增 中/EN 一键切换(选择记忆在浏览器, 切换即时生效)；
  物料目录(versions.json)与全部 13 个插件的标签/端口/密码文案改为 `中文|English` 双语约定，
  打包校验提示、部署摘要、`.env` 注释按界面语言输出；CLI 新增 `--lang zh|en`；
  服务器端《部署报告》按打包时选择的语言输出中文或英文(manifest.sh 新增 DEPLOY_LANG)
- **Kafka 3 节点集群形态**：勾选 Kafka 后可在"部署形态"中切换单节点 / 3 节点集群——
  KRaft 组合模式(免 ZooKeeper)、SASL_PLAINTEXT 多用户鉴权(参照生产参考环境, bitnami/kafka 3.7.0 双架构)、
  容器网络内免鉴权(kafka-ui 自动连接 kafka1), 宿主机广播地址部署时自动改写为服务器真实 IP;
  物料检查/镜像打包/端口布局/部署摘要/健康实测全部按形态自动切换
- **增量升级**：重跑 deploy.sh 自动对比新旧编排并输出"将重建/新增/移除/保持"清单,
  未变更服务不重建, 移除的服务自动清理(--remove-orphans)
- **Kafka UI 多形态适配**：集群下自动连接 kafka1:9092
- Kafka / Elasticsearch 容器健康检查确认覆盖(此前 README 表述滞后, 实际已在 HEALTH_WAIT 清单中)

## 2026-09-23

- **修复 MySQL 健康检查误报**：探活改为"鉴权探活优先"——老机器首次启动初始化可能超过原 180 秒窗口导致误判不健康；
  compose `start_period` 放宽到 300s
- `my.cnf` 落盘权限强制 644（MySQL 会忽略全局可写的配置文件）

## 2026-09-21

- **MySQL 配置文件挂载**：`conf/<svc>/my.cnf → /etc/mysql/conf.d`，内置 `sql_mode` 严格模式声明（主库/从库各一份），
  修改 MySQL 参数不再需要进容器

## 2026-09-14 ~ 09-20（前端体验第七轮打磨）

- 配色改为蓝青渐变；"物料仓库 / 历史产物"收进顶栏侧滑抽屉，主界面更聚焦
- 步骤条改为胶囊容器 + 滑动指示动画；标签页内编号从 1 开始
- 液态玻璃（liquid glass）视觉重做
- 修复：物料/历史页空白、生成预览按钮消失、"下一步"不能正确跳到预览等问题
- HTTPS 证书上传按钮提高辨识度

## 2026-09-14

- **前端整页改版**：五步向导流程 + 部署形态独立卡片 + 全局紧凑化

## 2026-09-11

- **集群形态**：MySQL 主从复制（8.0 与 5.7 均支持，GTID 自动同步）、Redis 哨兵高可用（1 主 1 从 + 3 哨兵）
- 新增 **Kafka（KRaft 单节点）+ Kafka UI**；Kafka / Elasticsearch / Kibana / Kafka-UI 支持鉴权配置
- **观测三件套闭环**：Node Exporter（主机指标）+ Prometheus + Loki/Promtail（容器日志）+ Grafana 数据源/仪表盘自动预配
- Grafana 与 Elasticsearch 数据源联动；服务列表增加分类筛选
- 集群部署脚本在 WSL2 systemd 环境逐包真机验证

## 2026-09-10

- 新增 6 个中间件插件：**RabbitMQ / MongoDB / Prometheus / Grafana / Elasticsearch / Kibana**（全部双架构）
- GitHub Pages 演示站上线：<https://litianyanaa-pixel.github.io/middleware-offline-deploy/packer.html>

## 2026-09-10（首个版本）

- 本地 Web 打包器（纯 Python 标准库，端口 8765）+ 服务器端零交互幂等部署脚本
- 内置服务：NGINX / MySQL 5.7 / MySQL 8.0 / Redis / Nacos / XXL-Job / MinIO
- 特性：按需打包、镜像 tag 归一化、`/proc/net/tcp` 端口预检、健康实测三级降级、
  Nacos/XXL-Job 自动导库、定时备份 + 一键恢复、反向代理向导（HTTPS 证书随包分发）、部署报告自动生成

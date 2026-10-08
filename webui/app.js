let CATALOG = null;
const selected = new Set();
const state = { ports: {}, secrets: {}, db: {}, extra: {}, backup: null, proxies: [], topology: { mysql8: 'single', mysql57: 'single', redis: 'single', kafka: 'single' }, svcFilter: 'all',
  servers: [],
  cluster: { enabled: false, kube_version: '', mode: 'cache', cni_type: 'calico', proxy_mode: 'iptables',
             pod_cidr: '10.233.64.0/18', service_cidr: '10.233.0.0/18', timezone: 'Asia/Shanghai', os_distros: [], roles: {},
             ha_type: 'local', ha_vip: '', upgrade_to: '', k8s_image_registry: '',
             container_manager: 'containerd', ipv4_mask_size: 24, multi_cni: 'none', multi_cni_tag: '',
             calico_values: '', dns: { coredns_tag: '', nodelocaldns_enabled: true, nodelocaldns_tag: '' },
             certs_renew_cron: '', kubeadm_config_dir: '/etc/kubekey/backup/kubernetes' },
  mh: { kafka: { enabled:false, count:3, brokers:[null,null,null] },
        mysql8: { enabled:false, master:null, replicas:[null] },
        mysql57: { enabled:false, master:null, replicas:[null] },
        redis: { enabled:false, master:null, replicas:[null,null] } },
  replicas: { mysql8: 1, mysql57: 1 },
  features: { proxysql: false } };
const WEEK = ["", "周一", "周二", "周三", "周四", "周五", "周六", "周日"];
let previewData = { compose: "", env: "", manifest: "" };
let curTab = "compose";

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const FALLBACK_COLORS = { nginx:"#009639", mysql57:"#00758f", mysql8:"#00758f", redis:"#d82c20", nacos:"#1b6dc1", xxljob:"#7c3aed", minio:"#c72e49" };
/* ================= i18n 中英切换 ================= */
const LANG_KEY = 'packer-lang';
let LANG = 'zh';
try { LANG = localStorage.getItem(LANG_KEY) === 'en' ? 'en' : 'zh'; } catch(e){}
// 界面英文词典: 键为渲染后的中文文本(按长度降序做整词替换; 未命中回退中文)
const EN_DICT = {
  "中间件离线包打包器": "Offline Delivery Packer", "离线交付打包器": "Offline Delivery Packer",
  "本地选型 → 生成编排 → 按需打包 → 服务器零交互部署": "Select locally → generate compose → pack on demand → zero-touch deploy",
  "保存配置": "Save Config", "导入配置": "Import Config", "物料仓库": "Warehouse", "历史产物": "Artifacts",
  "基础设施": "Infrastructure", "选择中间件": "Select Middleware", "端口与反代": "Ports & Proxy",
  "账号与数据": "Accounts & Data", "预览打包": "Preview & Pack", "关闭 (Esc)": "Close (Esc)",
  "重新检查": "Re-check", "类型": "Type", "架构": "Arch", "全部": "All", "安装包": "Packages", "镜像": "Images",
  "物料": "Item", "状态": "Status", "体积": "Size", "说明": "Notes", "文件": "File", "大小": "Size", "生成时间": "Generated at",
  "缺失项放入 wares/ 后重新检查": "Put missing items into wares/ then re-check",
  "补料脚本": "Pull script",
  "物料仓库无缺失, 无需补料": "No missing materials, nothing to pull",
  "补料脚本已下载: 放到仓库根目录执行 bash pull_missing_images.sh": "Pull script downloaded: run 'bash pull_missing_images.sh' at the repo root",
  "演示模式: 补料脚本需本地运行 packer.py 生成": "Demo: run packer.py locally to generate the pull script",
  "dist/ 目录 · 传输后可用 .sha256 校验完整性": "dist/ directory · verify with .sha256 after transfer",
  "暂无产物": "No artifacts yet",
  "compose 项目名与包名": "Compose project & bundle name", "项目名": "Project", "目标服务器架构": "Target server arch",
  "部署目录": "Deploy dir", "Docker 数据目录 data-root": "Docker data-root", "镜像加速器": "Registry mirrors",
  "https://... 每行一个": "https://... one per line", "一键填入实测可用源": "Fill verified mirrors",
  "compose / .env / 数据 / 日志 / 配置都在这里": "compose / .env / data / logs / config all live here",
  "一键勾选一组配套组件, 也可在下方单独增删": "One click for a curated suite; fine-tune below",
  "套件": "Suites", "可多选": "multi-select", "网关入口": "Gateway", "数据库": "Databases", "缓存与消息": "Cache & MQ",
  "微服务支撑": "Microservice", "对象存储": "Object storage", "可观测性": "Observability", "其他": "Other",
  "插件": "Plugins", "只打包勾选项": "Pack only selected",
  "端口映射": "Port Mapping", "外部访问端口(宿主机)": "Host port", "容器端口": "Container port",
  "箭头后为容器内端口; 支持追加自定义映射": "Right side is the container port; custom mappings supported",
  "添加自定义端口": "Add custom port", "自定义映射": "custom mapping", "默认映射": "default mapping", "未映射": "unmapped",
  "反向代理向导": "Reverse Proxy Wizard", "添加站点": "Add site", "删除站点": "Remove site",
  "域名": "Domain", "域名, 多个空格分隔; _ 为默认站点": "domains, space-separated; _ is the default site",
  "监听(容器端口)": "Listen (container port)", "静态": "Static", "整站反代": "Full proxy",
  "前端目录": "Web root", "容器内绝对路径": "absolute path in container",
  "容器内绝对路径, html 挂载于 /usr/share/nginx/html": "absolute path in container; html mounted at /usr/share/nginx/html",
  "留空则不托管静态页": "empty = no static hosting", "单页应用(SPA)": "SPA",
  "刷新子路径时回退 index.html": "fallback to index.html on subpath refresh",
  "接口前缀": "API prefix", "后端地址": "Backend", "http://服务名:容器端口": "http://service:container-port",
  "留空不代理": "empty = no proxy", "整站所有路径转发到该服务": "forward all paths to this service",
  "转发时去掉前缀": "Strip prefix", "后端收到的路径不含前缀": "backend receives paths without the prefix",
  "真实IP还原": "Real IP", "前面有 CDN/负载均衡时勾选": "enable when behind CDN/LB", "可信代理网段": "Trusted proxies",
  "逗号或换行分隔; 只有这些来源的 XFF 头会被采信": "comma/newline separated; only these sources' XFF headers are trusted",
  "HTTP 跳转": "HTTP redirect", "80 端口 301 到 https": "301 port 80 to https",
  "HTTPS 证书": "HTTPS cert", "上传证书": "Upload cert", "证书文件": "Certificate", "证书": "Certificate",
  "fullchain.pem / .crt(含中间证书)": "fullchain.pem / .crt (with intermediate)",
  "私钥文件": "Private key", "私钥": "Private key", ".key(暂不支持加密私钥)": ".key (encrypted keys unsupported)",
  "挂载于容器 /etc/nginx/ssl。": "mounted at /etc/nginx/ssl in the container",
  "部署即配好 SSL": "SSL configured on deploy", "证书随包分发到 nginx/ssl/": "certs ship to nginx/ssl/",
  "生成的配置自动包含代理优化: upstream 长连接复用(keepalive) · 连接/读写超时 · 响应缓冲 · X-Real-IP / X-Forwarded-": "Generated config includes proxy tuning: upstream keepalive · timeouts · response buffering · X-Real-IP / X-Forwarded-",
  "前缀改写示例: 随 输入实时更新": "prefix rewrite preview updates live", "示例: 浏览器请求": "Example: browser requests",
  "后端实际收到": "backend receives", "还原出的 IP 写入": "restored IP written to",
  "账号密码": "Accounts", "账号": "Username", "密码": "Password", "显示": "Show", "隐藏": "Hide", "随机": "Random",
  "弱": "Weak", "一般": "Fair", "良好": "Good", "强": "Strong",
  "数据库来源": "Database Source", "使用本次部署的 MySQL": "Use bundled MySQL", "使用外部数据库": "Use external database",
  "数据库地址": "DB host", "库名": "Database", "连接账号": "DB user",
  "建库建表 SQL 会在首次启动时自动导入": "schema SQL auto-imported on first start",
  "部署时先测连再建库建表(已存在则跳过)": "connectivity tested before import; existing schemas skipped",
  "Nacos / XXL-Job 的建库连库方式": "how Nacos / XXL-Job connect to their database",
  "MySQL 服务": "MySQL service", "使用哪个 MySQL": "Which MySQL", "root (自动使用 root 密码)": "root (uses root password)",
  "数据库备份": "Database Backup", "启用备份": "Enable backup", "部署时自动写入 crontab": "crontab written on deploy",
  "crontab + 容器内原生工具导出, 按库分文件压缩": "crontab + in-container native dump, per-DB compressed",
  "备份日": "Days", "工作日": "Weekdays", "每天": "Every day",
  "周一": "Mon", "周二": "Tue", "周三": "Wed", "周四": "Thu", "周五": "Fri", "周六": "Sat", "周日": "Sun",
  "备份时间": "Time", "每库保留份数": "Copies kept per DB", "备份目录(服务器)": "Backup dir (server)",
  "按库分文件 gzip": "one gzip per database",
  "每库一个 .sql.gz; 超出保留份数自动轮转删除": "one .sql.gz per DB; oldest rotated out beyond retention",
  "当前选择不需要账号密码 / 数据库配置。": "Current selection needs no accounts / database config.",
  "项待配置": " to configure", "已配置": "Configured",
  "Nacos 控制台密码为初始登记值": "Nacos console password is the initial value",
  "部署后请登录控制台改为自己要用的密码; .env 中该值仅作登记": "change it in the console after deploy; the .env value is for registration",
  "XXL-Job 控制台初始账号 admin": "XXL-Job console initial user admin",
  "初始密码即上面登记的 XXL-Job admin 密码(打包时按该值初始化 SQL)": "initial password is the XXL-Job admin password above (SQL initialized with it)",
  "预览 / 打包": "Preview / Pack", "生成预览": "Generate Preview", "开始打包": "Start Packing", "取消打包": "Cancel",
  "正在打包": "Packing", "复制当前内容": "Copy contents",
  "查看 docker-compose / .env / manifest 最终内容; 确认无误后": "inspect final docker-compose / .env / manifest; when confirmed",
  "服务器部署: 解压后进入目录执行  ./deploy.sh  (root)": "Server deploy: extract, enter the dir and run  ./deploy.sh  (root)",
  "服务器零手工配置": "zero manual config on the server", "幂等可重跑": "idempotent, safe to re-run",
  "产物大小": "Artifact size", "原始物料": "raw materials",
  "备份计划(crontab):": "backup plan (crontab):", "提示:": "Notes:", "将打包镜像:": "images to pack:",
  "预览已生成": "Preview ready", "打包完成": "Pack finished", "打包失败": "Pack failed",
  "打包已取消": "Pack cancelled", "已请求取消": "Cancellation requested",
  "已生成同名 .sha256 校验文件": "matching .sha256 generated",
  "与压缩包一起拷贝到服务器可在部署时自动校验完整性": "copy alongside the bundle; integrity auto-verified on deploy",
  "个镜像包": " image archive(s)",
  "Docker 静态二进制": "Docker static binary", "docker 安装包": "docker package",
  "docker-compose 插件": "docker-compose plugin", "compose v2 二进制": "compose v2 binary",
  "双架构": "dual-arch", "仅 x86": "x86 only", "缺": "missing", "缺失": "Missing", "就绪": "Ready",
  "物料状态已刷新": "Warehouse refreshed",
  "演示模式: 物料检查需本地运行 packer.py": "Demo: run packer.py locally for warehouse check",
  "静态演示版不支持该操作": "Not available in static demo — run python packer.py locally",
  "请在本地运行 python packer.py 体验完整功能": "run python packer.py locally for the full experience",
  "完整使用请克隆本仓库后本地运行": "clone this repo and run locally for full features",
  "静态演示模式: 物料状态需在本地运行 python packer.py 后查看": "Static demo: run python packer.py locally to see warehouse status",
  "静态演示模式: 打包功能需本地运行 packer.py": "Static demo: run packer.py locally to pack",
  "静态演示版": "Static demo",
  "已清空全部选择": "Selection cleared", "已移除 MySQL 5.7 (arm64 不支持)": "MySQL 5.7 removed (no arm64)",
  "基础平台": "Platform", "Kubernetes 集群": "Kubernetes cluster", "Kubernetes 集群参数": "Kubernetes cluster settings",
  "集群角色分配": "Cluster role assignment", "不参与": "Not used", "控制面": "Control plane",
  "集群基础配置": "Cluster basic settings", "集群高级配置": "Cluster advanced settings",
  "使用开源应用 kubekey 部署, 支持信创环境, 独立于中间件以外": "Deployed with open-source KubeKey; Xinchuang (domestic stack) ready; independent from the middleware",
  "⚠ 闭源/无公开镜像(麒麟/统信/阿里云)需自行提取放置, 详见 docs/k8s-信创离线部署.md": "⚠ closed-source (Kylin/UOS/Alibaba Cloud) packages must be extracted yourself, see docs/k8s-信创离线部署.md",
  "镜像仓库角色: 部署私有镜像仓库时必需; etcd 默认堆叠在控制面": "Registry role: required for a private registry; etcd is stacked on control planes by default",
  "至少 1 台控制面; 主部署机必须是集群节点之一": "at least 1 control plane; the primary deploy machine must join the cluster",
  "附带目标版本 kube 三件套与 upgrade-cluster.sh, 打包时自动下载目标版本二进制": "bundles target-version kube binaries and upgrade-cluster.sh, auto-downloaded at pack time",
  "与 Docker 加速器同一批实测源; K8s 组件镜像在离线/zone=cn 模式下自动走集群内仓库或国内源": "same verified mirrors as Docker; K8s images go through the in-cluster registry or CN mirrors in offline/zone=cn mode",
  "镜像源前缀用于在线/镜像下载的仓库地址; 节点 containerd 侧的加速器即下方「镜像加速」": "prefix for online/image download repos; node-side containerd mirrors are the 'Registry mirrors' below",
  "部署在分配了「镜像仓库」角色的节点; 多仓库节点自动 keepalived HA": "deployed on nodes with the 'registry' role; keepalived HA automatically with multiple registry nodes",
  "CRI 运行时": "Container runtime", "Pod 子网掩码": "Pod subnet mask",
  "docker 运行时仅支持在线安装(K8s ≥1.24 由 kk 自动装 cri-dockerd); 纯离线包只预装 containerd": "docker runtime is online-install only (kk auto-installs cri-dockerd for K8s ≥1.24); offline bundles preinstall containerd",
  "Multi-CNI": "Multi-CNI", "Multus 镜像 tag": "Multus image tag",
  "Calico values 覆盖": "Calico values override",
  "Calico 专属调优(ipipMode/vxlanMode/mtu 等)经此透传; 留空用 kk 默认 values": "Calico-specific tuning (ipipMode/vxlanMode/mtu etc.) passes through here; leave empty for kk defaults",
  "etcd 高级参数": "etcd advanced params", "kubeadm 备份目录": "kubeadm backup dir",
  "etcd 参数写入 config 的 etcd.env(data_dir 同); kubeadm-config 每次部署自动带时间戳备份到该目录": "etcd params go under etcd.env in the config (data_dir included); kubeadm-config is timestamp-backed-up there on every deploy",
  "CoreDNS 镜像 tag": "CoreDNS image tag",
  "NodeLocalDNS 镜像 tag, 可选": "NodeLocalDNS image tag (optional)",
  "证书续期 crontab": "Cert renewal crontab", "安装时证书续期": "Renew certs at install",
  "如 0 0 1 * * (每月 1 号零点续期); 留空=不启用": "e.g. 0 0 1 * * (renew at midnight on day 1 monthly); empty = disabled",
  "部署时执行一轮 kubeadm certs renew (默认开)": "run one round of kubeadm certs renew during deploy (on by default)",
  "crontab 在主部署机执行 kk certs renew 并自动重启控制面静态 Pod(日志 /var/log/kk-certs-renew.log); kk 侧安装续期由 kubernetes.certs.renew 控制": "the crontab runs kk certs renew on the primary deploy machine and restarts control-plane static pods (log: /var/log/kk-certs-renew.log); install-time renewal is controlled by kubernetes.certs.renew",
  "kube-vip VIP / 端点地址": "kube-vip VIP / endpoint address",
  "必填: 控制面同网段空闲 IP, 如 10.0.0.100": "required: a free IP in the control-plane subnet, e.g. 10.0.0.100",
  "必填: 如 lb.example.com 或 127.0.0.2": "required: e.g. lb.example.com or 127.0.0.2",
  "kube-vip 以静态 Pod 跑在控制面节点: VIP 须与节点同网段且未被占用, kk 自动选择网卡并漂移 VIP": "kube-vip runs as a static pod on control-plane nodes: the VIP must be free and in the same subnet; kk auto-picks the NIC and floats the VIP",
  "haproxy 静态 Pod 在每个节点本机转发 API(后端自动指向控制面节点); 端点地址写域名或 127.0.0.2 即可": "a haproxy static pod forwards the API on every node (backends auto-point to control planes); use a DNS name or 127.0.0.2 as the endpoint",
  "数据目录 / 运行时参数 / 镜像加速 / NTP / 存储 / 私有镜像仓库": "data dirs / runtime args / mirrors / NTP / storage / private registry",
  "containerd 2.x 依赖 glibc ≥ 2.35, 老系统请勾选静态构建或降级 1.7.x": "containerd 2.x needs glibc >= 2.35; use static build or 1.7.x on older distros",
  "开源发行版依赖包已内置": "packages for open-source distros are bundled",
  "静态构建 (glibc < 2.35 老系统必选)": "static build (required on glibc < 2.35)",
  "由 kk 设置节点 hostname (容器验证等特殊场景可关)": "let kk set node hostnames (disable for container-based validation)",
  "自定义时间同步 (默认关闭, 部署脚本只设时区)": "custom time sync (off by default; deploy script sets timezone only)",
  "安装方式": "Install mode", "控制面 HA": "Control-plane HA", "kube-proxy 模式": "kube-proxy mode",
  "CNI 插件": "CNI plugin", "升级包": "Upgrade bundle", "containerd 版本覆盖": "containerd version override",
  "containerd 运行时": "containerd runtime", "节点 hostname": "Node hostname", "K8s 镜像源前缀": "K8s image registry prefix",
  "镜像加速 (docker.io)": "Registry mirrors (docker.io)", "一键填入实测可用源": "Fill verified mirrors",
  "NTP 服务器": "NTP servers", "localpv 存储类": "localpv storage class", "NFS 存储类": "NFS storage class",
  "私有镜像仓库": "Private image registry", "不部署 (默认)": "None (default)", "启用 localpv 存储类": "Enable localpv storage class",
  "启用 NFS 存储类": "Enable NFS storage class", "设为默认": "Set as default",
  "containerd 数据目录": "containerd data dir", "docker 数据目录": "docker data dir",
  "etcd 数据目录": "etcd data dir", "kubelet 数据目录": "kubelet data dir", "kubelet max-pods": "kubelet max-pods",
  "每行一个: key=value (如 system-reserved=memory=300Mi)": "one per line: key=value (e.g. system-reserved=memory=300Mi)",
  "KubeletConfiguration 追加字段 (YAML 缩进), 如 maxOpenFiles: 1000000": "extra KubeletConfiguration fields (YAML indent), e.g. maxOpenFiles: 1000000",
  "每行一个 https://... , 写入节点 containerd": "one https://... per line, written to node containerd",
  "每行一台 NTP 服务器, 如 ntp.internal.corp": "one NTP server per line, e.g. ntp.internal.corp",
  "可选, 如 v1.7.27": "optional, e.g. v1.7.27", "如 hub.kubesphere.com.cn": "e.g. hub.kubesphere.com.cn",
  "NFS 服务器地址(必填)": "NFS server (required)", "导出路径": "export path", "HA VIP(可选)": "HA VIP (optional)",
  "artifact 产物 (离线含镜像, 推荐)": "artifact bundle (offline with images, recommended)",
  "二进制缓存 (离线)": "binary cache (offline)", "在线安装 (联网)": "online install (needs internet)",
  "纯离线 (二进制+镜像全本地)": "Fully offline (binaries + images local)",
  "镜像打包时按原生 tag 收集, 部署前导入节点本地 containerd, 全程不访问外网": "Images are collected with native tags at pack time and imported into node containerd before deploy; no internet access at all",
  "不生成 (默认)": "none (default)", "(推荐)": "(recommended)",
  "银河麒麟 V10": "Kylin V10", "统信 UOS 服务器版": "UOS Server", "Anolis OS (龙蜥)": "Anolis OS",
  "Alibaba Cloud Linux": "Alibaba Cloud Linux",
  "上一步": "Prev", "下一步": "Next",
  "基础设施与中间件 → 生成编排 → 按需打包 → 服务器零交互部署": "Infrastructure & middleware → orchestration → pack on demand → zero-touch server deploy",
  "中间件底座": "Middleware base", "Docker/Compose 离线安装, 为 Kubernetes 与中间件提供运行底座": "Docker/Compose offline install; the runtime base for Kubernetes and middleware",
  "K8s 集群与中间件多机部署的节点均从本池选择; K8s 密码登录即可(kk 自带 SSH 客户端)": "Both K8s and multi-host middleware nodes come from this pool; password SSH is fine (kk ships its own client)",
  "中间件默认部署在交付包所在服务器(运行 deploy.sh 的机器); 跨服务器部署请在中间件卡片配置多机拓扑": "Middleware deploys on the server hosting the bundle (the one running deploy.sh) by default; configure multi-host topology on the middleware cards to spread across servers",
  "已选 ": "Selected: ", "目标 ": "target ", "原始物料 ": "raw materials ", "打包后约为其": "packed ≈", "K8s 集群": "K8s cluster",
  "节点分发预览": "Node distribution", "集群 inventory": "Cluster inventory", "集群 config": "Cluster config",
  "工作节点": "Worker", "镜像仓库": "Image registry", "K8s 版本": "K8s version", "时区": "Timezone",
  "离线物料": "Offline materials", "OS 依赖包": "OS packages", "部署服务器池": "Server pool",
  "artifact 产物 (含镜像, 推荐)": "artifact bundle (with images, recommended)",
  "二进制缓存 (镜像走内网仓库)": "binary cache (images via intranet registry)",
  "iptables (旧内核安全)": "iptables (safe on old kernels)", "nftables (需内核≥5.13)": "nftables (kernel >= 5.13)",
  "containerd (默认)": "containerd (default)", "docker (仅在线安装)": "docker (online only)",
  "none (默认)": "none (default)", "multus (Pod 多网卡)": "multus (multi-NIC pods)",
  "启用 NodeLocalDNS (默认开)": "Enable NodeLocalDNS (on by default)",
  "MySQL 5.7 不支持 arm64": "MySQL 5.7 has no arm64 image",
  "已恢复上次填写的配置": "Restored your last configuration",
  "配置已导出, 可用 python packer.py --config 直接打包": "Config exported; pack with python packer.py --config",
  "配置已导入": "Config imported", "导入失败:": "Import failed:",
  "已复制到剪贴板": "Copied to clipboard", "复制失败, 请手动选择复制": "Copy failed; select & copy manually",
  "已填入实测可用的镜像加速器": "Verified mirrors filled in", "已读取": "Loaded",
  "证书文件过大(>200KB), 请确认选对了文件": "cert/key over 200KB; check the file",
  "已改为使用外部数据库": "switched to external database",
  "单机模式": "standalone", "主从模式 (一主一从)": "master-replica (1+1)", "哨兵高可用": "Sentinel HA",
  "Kafka 单节点模式": "Kafka single-node mode", "Redis 单机模式": "Redis standalone mode",
  "Kafka 3 节点集群模式 (KRaft": "Kafka 3-node cluster mode (KRaft",
  "SASL_PLAINTEXT 多用户鉴权": "SASL_PLAINTEXT multi-user auth",
  "体积约为单节点的 3 倍)": "about 3x the single-node size)",
  "无 ZooKeeper)": "no ZooKeeper)", "3 哨兵)": "3 sentinels)",
  "单机": "Standalone", "主从复制 (一主一从 · GTID)": "Master-replica (1+1 · GTID)",
  "单节点 (KRaft)": "Single node (KRaft)", "3 节点集群 (KRaft": "3-node cluster (KRaft",
  "哨兵高可用 (主": "Sentinel HA (master",
  "集群: 3 broker 互为副本与仲裁 (KRaft 组合模式": "cluster: 3 brokers with replication & quorum (KRaft combined mode",
  "主库服务名仍为 mysql8 (Nacos/XXL-Job 照常连它)": "master service stays mysql8 (Nacos/XXL-Job still connect to it)",
  "主库服务名仍为 mysql57": "master service stays mysql57",
  "从库只读端口 13308 自动同步; 部署脚本自动配置复制关系": "replica read-only port 13308 auto-syncs; deploy script configures replication",
  "从库只读端口 13309 自动同步; 部署脚本按 5.7 语法 (CHANGE MASTER) 自动配置复制关系": "replica read-only port 13309 auto-syncs; deploy script configures replication using 5.7 syntax (CHANGE MASTER)",
  "哨兵: 从库端口 16380 只读; 应用侧使用 sentinel 协议 (redis-sentinel:26379, master 名称 mymaster) 实现故障自动切换":
    "Sentinel: replica port 16380 read-only; clients use sentinel protocol (redis-sentinel:26379, master name mymaster) for failover",
  "Kafka 部署形态": "Kafka Topology", "MySQL 8.0 部署形态": "MySQL 8.0 Topology",
  "MySQL 5.7 部署形态": "MySQL 5.7 Topology", "Redis 部署形态": "Redis Topology",
  "3 节点集群 · SASL 端口": "3-node cluster · SASL port",
  "未选择组件": "No components selected", "端口不合法或冲突": "invalid or conflicting port",
  "也可接入 plugins/ 插件扩展": "extensible via plugins/",
  "部署时自动把广播地址改写为服务器 IP; 镜像改用 bitnami/kafka 3.7.0 (双架构": "advertised host rewritten to the server IP on deploy; image switched to bitnami/kafka 3.7.0 (dual-arch",
  "点击下方": "Click ", "步骤勾选组件": " to select components in the steps",
};
const EN_KEYS = Object.keys(EN_DICT).sort((a, b) => b.length - a.length);
const EN_RE = new RegExp(EN_KEYS.map(k => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'g');
// 目录数据(zh|en)取半边; 后端消息(带|)取半边
function trLabel(s){
  s = String(s == null ? '' : s);
  if (!s.includes('|')) return s;
  const i = s.indexOf('|');
  return (LANG === 'en' ? s.slice(i + 1) : s.slice(0, i)).trim();
}
function trMsg(s){
  s = String(s == null ? '' : s);
  if (!s.includes('|')) return s;
  const i = s.indexOf('|');
  return (LANG === 'en' ? s.slice(i + 1) : s.slice(0, i)).trim();
}
function trText(s){ return LANG === 'en' ? String(s).replace(EN_RE, m => EN_DICT[m] || m) : String(s); }
function applyI18n(root){
  if (LANG !== 'en') return;
  const scope = root || document;
  // 文本节点
  const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const n of nodes){
    if (!/[\u4e00-\u9fff]/.test(n.nodeValue)) continue;
    const v = trText(n.nodeValue);
    if (v !== n.nodeValue) n.nodeValue = v;
  }
  // 常用属性
  scope.querySelectorAll && scope.querySelectorAll('[title],[placeholder]').forEach(el => {
    for (const a of ['title', 'placeholder']){
      const v = el.getAttribute && el.getAttribute(a);
      if (v && /[\u4e00-\u9fff]/.test(v)){
        const t = trText(v);
        if (t !== v) el.setAttribute(a, t);
      }
    }
  });
}
// 动态渲染的内容自动翻译(仅在英文模式)
new MutationObserver(muts => {
  if (LANG !== 'en') return;
  for (const m of muts){
    m.addedNodes && m.addedNodes.forEach(n => {
      if (n.nodeType === 1) applyI18n(n);
      else if (n.nodeType === 3 && /[\u4e00-\u9fff]/.test(n.nodeValue)){
        const v = trText(n.nodeValue); if (v !== n.nodeValue) n.nodeValue = v;
      }
    });
    if (m.type === 'characterData' && /[\u4e00-\u9fff]/.test(m.target.nodeValue)){
      const v = trText(m.target.nodeValue); if (v !== m.target.nodeValue) m.target.nodeValue = v;
    }
  }
}).observe(document.documentElement, { childList: true, subtree: true, characterData: true });

function toast(text){
  const d = document.createElement('div');
  d.className = 'toast'; d.textContent = trMsg(text);
  $('#toast').appendChild(d);
  setTimeout(() => {
    d.classList.add('out');
    setTimeout(() => d.remove(), 320);
  }, 2600);
}
function fmtGB(mb){ return mb >= 1024 ? (mb/1024).toFixed(2) + ' GB' : mb + ' MB'; }
function fmtTime(sec){ sec = Math.max(0, Math.round(sec)); return String(Math.floor(sec/60)).padStart(2,'0') + ':' + String(sec%60).padStart(2,'0'); }
function msg(text, cls){ $('#msg').innerHTML = text ? '<div class="msg '+cls+'">'+esc(trMsg(text))+'</div>' : ''; }
function svcColor(key){ const m = CATALOG.services[key]; return (m && m.color) || FALLBACK_COLORS[key] || '#6e6e73'; }
function avatarHtml(key, cls){
  const m = CATALOG.services[key];
  if (typeof LOGOS !== 'undefined' && LOGOS[key]){
    return '<div class="avatar '+(cls||'')+'"><img src="'+LOGOS[key]+'" alt=""></div>';
  }
  const ab = (m.label||key).slice(0,2).toUpperCase();
  return '<div class="avatar '+(cls||'')+'"><span class="letter" style="background:'+svcColor(key)+'">'+esc(ab)+'</span></div>';
}
function logoSm(key){
  if (typeof LOGOS !== 'undefined' && LOGOS[key]) return '<span class="logo-sm"><img src="'+LOGOS[key]+'" alt=""></span> ';
  return '';
}
function randPwd(len){
  const cs = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789@#%*+=_.';
  const a = new Uint32Array(len); crypto.getRandomValues(a);
  return Array.from(a, n => cs[n % cs.length]).join('');
}
function randToken(){
  const a = new Uint8Array(48); crypto.getRandomValues(a);
  let bin=''; a.forEach(b=>bin+=String.fromCharCode(b));
  return 'SecretKey' + btoa(bin);
}
function pwStrength(v){
  let s = 0;
  if (v.length >= 8) s++;
  if (v.length >= 12) s++;
  if (/[a-z]/.test(v) && /[A-Z]/.test(v)) s++;
  if (/\d/.test(v) && /[^A-Za-z0-9]/.test(v)) s++;
  return Math.max(1, s);
}
function saveLocal(){
  clearTimeout(saveLocal._t);
  saveLocal._t = setTimeout(() => {
    try { localStorage.setItem('packer-cfg-v1', JSON.stringify(buildConfig())); } catch(e){}
  }, 400);
}
async function api(path, body){
  if (typeof STATIC_MODE !== 'undefined' && STATIC_MODE && path !== '/api/catalog')
    throw new Error('静态演示版不支持该操作 — 请在本地运行 python packer.py 体验完整功能');
  const opt = body !== undefined
    ? {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)}
    : {};
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({error:'响应解析失败'}));
  if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
  return data;
}

/* ================= 初始化 ================= */
let STATIC_MODE = false;   // 静态托管(GitHub Pages)演示模式: 无本地打包器后端
async function loadCatalog(){
  try {
    CATALOG = await api('/api/catalog');
  } catch(e) {
    // 静态托管场景: 退化为仓库内的 versions.json, 关闭需要后端的功能
    const v = await (await fetch('versions.json')).json();
    CATALOG = { services: v.services || {}, secrets: v.secrets || [],
                defaults: v.defaults || {}, cluster: v.cluster || {},
                files_present: {}, sizes: {docker:{},compose:{},images:{}} };
    STATIC_MODE = true;
  }
}
async function init(){
  await loadCatalog();
  renderSvcGrid(); renderClusterCard(); renderWarehouse(); setupWhFilter(); loadBundles(); restoreLocal();
  if (STATIC_MODE){
    document.querySelector('.content').insertAdjacentHTML('afterbegin',
      '<div class="msg warn" style="margin:0 0 18px">当前是 GitHub Pages <b>静态演示版</b>: 界面与配置流程可完整体验; 打包/预览/物料检查需要后端, 完整使用请克隆本仓库后本地运行 <b>python packer.py</b>。</div>');
    toast('静态演示模式: 打包功能需本地运行 packer.py');
  }
  $('#mirrorNote').textContent = CATALOG.defaults.registry_mirrors_note || '';
  $('#arch').onchange = onArchChange;
  $('#btnPreview').onclick = doPreview;
  $('#btnPack').onclick = doPack;
  $('#btnRecheck').onclick = async () => { if (STATIC_MODE){ toast('演示模式: 物料检查需本地运行 packer.py'); return; } CATALOG = await api('/api/catalog'); renderSvcGridSync(); renderWarehouse(); toast('物料状态已刷新'); };
  $('#btnPullScript').onclick = async () => {
    if (STATIC_MODE){ toast('演示模式: 补料脚本需本地运行 packer.py 生成'); return; }
    // K8s 二进制缺失与镜像补料分流: 二进制走 prepare_cluster.py / 打包时自动下载
    const fp0 = (CATALOG.files_present || {});
    const cc0 = (CATALOG.cluster || {});
    const k8sMissing = [];
    if (fp0['cluster_kk_amd64'] === false) k8sMissing.push('kk 二进制: 执行 python prepare_cluster.py --kk-build');
    for (const ver0 of Object.keys(cc0.versions || {})){
      if (fp0['cluster_cache_ready_'+ver0] === false)
        k8sMissing.push('K8s '+ver0+' 二进制缓存: 点「开始打包」时自动下载, 或执行 python prepare_cluster.py --download --kube-version '+ver0);
    }
    const miss = Object.entries(CATALOG.files_present || {}).filter(function(e){
      return !e[1] && e[0].indexOf('cluster_') !== 0;   // k8s 物料单独提示
    }).length;
    if (k8sMissing.length) toast('K8s 物料: ' + k8sMissing.join(' | '));
    if (!miss && k8sMissing.length) return;
    if (!miss){ toast('物料仓库无缺失, 无需补料'); return; }
    // 后端盘点缺失物料并返回补齐脚本(覆盖双架构), 保存到仓库根目录执行即可
    const resp = await fetch('/api/pull_script');
    const ct = resp.headers.get('Content-Type') || '';
    if (ct.includes('application/json')){
      const d = await resp.json();
      toast(d.message || '物料仓库无缺失, 无需补料');
      return;
    }
    const blob = await resp.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'pull_missing_images.sh';
    a.click();
    URL.revokeObjectURL(a.href);
    toast('补料脚本已下载: 放到仓库根目录执行 bash pull_missing_images.sh');
  };
  $('#btnFillMirrors').onclick = () => {
    $('#mirrors').value = (CATALOG.defaults.registry_mirrors || []).join('\n');
    toast('已填入实测可用的镜像加速器'); saveLocal();
  };
  $('#btnExport').onclick = doExport;
  $('#btnImport').onclick = () => $('#importFile').click();
  $('#importFile').onchange = doImport;
  $('#btnCopy').onclick = doCopy;
  $('#btnPrev').onclick = () => gotoStep(Math.max(1, +curStep - 1));
  $('#btnNext').onclick = () => gotoStep(Math.min(5, +curStep + 1));
  $('#btnPreview').onclick = () => { gotoStep(5); doPreview(); };
  $('#btnWh').onclick = () => openDrawer('wh');
  $('#btnOut').onclick = () => { openDrawer('out'); loadBundles(); };
  $$('[data-dwclose]').forEach(el => el.onclick = () => closeDrawers());
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawers(); });
  $$('#wizSteps .wtab').forEach(b => b.onclick = () => gotoView(b.dataset.v));
  window.addEventListener('resize', () => movePill(curStep));
  window.addEventListener('load', () => movePill(curStep));
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => movePill(curStep));
  $$('#previewCard .tabs button[data-t]').forEach(b => b.onclick = () => switchTab(b.dataset.t));
  $('#mCancel').onclick = async () => { try{ await api('/api/cancel', {}); }catch(e){} toast('已请求取消'); };
  $('#mClose').onclick = () => { $('#modal').classList.add('hidden'); loadBundles(); };
  // 中英切换: 记忆选择后整页重载(状态已在 localStorage 持久化)
  const bl = $('#btnLang');
  if (LANG === 'en'){ bl.textContent = '中文'; document.documentElement.lang = 'en'; }
  else { bl.textContent = 'EN'; }
  bl.onclick = () => {
    try { localStorage.setItem(LANG_KEY, LANG === 'en' ? 'zh' : 'en'); } catch(e){}
    location.reload();
  };
  document.addEventListener('input', saveLocal);
  gotoView('1');
  requestAnimationFrame(() => movePill('1'));
  checkRunning();
}

const CAT_ORDER = [['web','网关入口'],['db','数据库'],['cache','缓存与消息'],['svc','微服务支撑'],['storage','对象存储'],['obs','可观测性'],['other','其他']];

// 按部署形态取版本号/镜像体积 (kafka: 单节点 apache/kafka 4.3.1 / 集群 bitnami 3.7.0)
function isClusterForm(k){ return k === 'kafka' && (state.topology.kafka || 'single') === 'cluster'; }
function svcTag(k){
  const m = CATALOG.services[k]; if (!m) return '';
  if (isClusterForm(k) && m.cluster) return m.cluster.tag;
  return m.tag;
}
function svcSize(k){
  const arch = $('#arch').value;
  if (isClusterForm(k) && CATALOG.sizes.cluster && CATALOG.sizes.cluster[k])
    return CATALOG.sizes.cluster[k][arch] || 0;
  return (CATALOG.sizes.images[k] || {})[arch] || 0;
}

function svcCardHtml(key, meta){
  const size = svcSize(key);
  return '<span class="tick"></span>'+
    '<div class="head">'+avatarHtml(key)+
    '<div><b>'+esc(trLabel(meta.label))+'</b><span class="ver">'+esc(svcTag(key))+'</span></div></div>'+
    '<div class="foot"><span class="size">'+($('#arch').value==='amd64'?'x86':'ARM')+' '+fmtGB(size)+'</span>'+
    '<span>'+((meta.supported_arch||[]).length>1?'双架构':'仅 x86')+(meta.is_plugin?' · 插件':'')+'</span></div>'+
    '<div class="warnline">'+esc(meta.warn || '')+'</div>';
}

function renderSvcChips(){
  const bar = $('#catChips'); if (!bar) return;
  const counts = {};
  for (const [, m] of Object.entries(CATALOG.services)){ const c = (m.cat || 'other'); counts[c] = (counts[c] || 0) + 1; }
  let html = '<button class="chip-f'+(state.svcFilter==='all'?' on':'')+'" data-cat="all">全部 <i>'+Object.keys(CATALOG.services).length+'</i></button>';
  for (const [cat, label] of CAT_ORDER){
    if (!counts[cat]) continue;
    html += '<button class="chip-f'+(state.svcFilter===cat?' on':'')+'" data-cat="'+cat+'">'+label+' <i>'+counts[cat]+'</i></button>';
  }
  if (selected.size) html += '<button class="chip-f clear" data-clear="1">✕ 清空选择 <i>'+selected.size+'</i></button>';
  bar.innerHTML = html;
  bar.querySelectorAll('.chip-f[data-cat]').forEach(b => b.onclick = () => { state.svcFilter = b.dataset.cat; renderSvcGrid(); });
  bar.querySelectorAll('.chip-f[data-clear]').forEach(b => b.onclick = () => {
    selected.clear(); renderSvcGridSync(); toast('已清空全部选择');
  });
}

function renderSvcGrid(){
  renderSvcChips();
  const wrap = $('#svcCats'); wrap.innerHTML = '';
  for (const [cat, label] of CAT_ORDER){
    if (state.svcFilter !== 'all' && cat !== state.svcFilter) continue;
    const entries = Object.entries(CATALOG.services).filter(([k, m]) => (m.cat || 'other') === cat);
    if (!entries.length) continue;
    const sec = document.createElement('div');
    sec.className = 'cat';
    sec.innerHTML = '<h3 class="cat-h">'+label+' <i>'+entries.length+'</i></h3>'+
      '<div class="svc-grid">'+entries.map(([k, m]) =>
        '<div class="svc'+(k==='mysql57' && $('#arch').value==='arm64' ? ' dis' : '')+(selected.has(k) ? ' on' : '')+'" data-key="'+k+'">'+
        svcCardHtml(k, m)+'</div>').join('')+'</div>';
    wrap.appendChild(sec);
  }
  wrap.querySelectorAll('.svc').forEach(d => d.onclick = () => toggleSvc(d.dataset.key));
  renderSuites();
  updateSvcWarn();
  renderTopology();
  renderPool();
  renderInfraTopology();
}

function suiteAllOn(s){ return (s.members || []).every(m => selected.has(m)); }

function renderSuites(){
  const box = $('#suiteBox'); if (!box) return;
  const suites = CATALOG.suites || [];
  if (!suites.length){ box.innerHTML = ''; return; }
  let html = '<h3 class="cat-h">套件 <span class="hint" style="font-weight:400">一键勾选一组配套组件, 也可在下方单独增删</span></h3><div class="suite-row">';
  for (const s of suites){
    const names = (s.members || []).map(m => (CATALOG.services[m] || {}).label || m);
    const on = suiteAllOn(s);
    html += '<div class="suite'+(on?' on':'')+'" data-suite="'+esc(s.key)+'" title="包含: '+esc(names.join('、'))+'">'+
      '<div class="s-head"><span class="tick"></span><b>'+esc(trLabel(s.label))+'</b></div>'+
      '<div class="s-desc">'+esc(trLabel(s.desc || ''))+'</div>'+
      '<div class="s-members">'+(s.members || []).map(m => {
        const mm = CATALOG.services[m] || {};
        return '<span class="chip">'+(typeof LOGOS !== 'undefined' && LOGOS[m] ? '<img src="'+LOGOS[m]+'" alt="">' : '')+esc(trLabel(mm.label) || m)+'</span>';
      }).join('')+'</div></div>';
  }
  html += '</div>';
  box.innerHTML = html;
  box.querySelectorAll('.suite').forEach(el => el.onclick = () => {
    const s = suites.find(x => x.key === el.dataset.suite); if (!s) return;
    if (suiteAllOn(s)) (s.members || []).forEach(m => selected.delete(m));
    else (s.members || []).forEach(m => selected.add(m));
    renderSvcGridSync();
  });
}

function renderSvcGridSync(){
  $$('#svcCats .svc').forEach(d => {
    const k = d.dataset.key;
    d.classList.toggle('on', selected.has(k));
    d.classList.toggle('dis', k==='mysql57' && $('#arch').value==='arm64');
    const size = svcSize(k);
    d.querySelector('.size').textContent = ($('#arch').value==='amd64'?'x86':'ARM') + ' ' + fmtGB(size);
    const verEl = d.querySelector('.ver');
    if (verEl) verEl.textContent = svcTag(k);
  });
  renderSvcChips();
  renderSuites();
  updateSvcWarn(); renderDynamic();
}

/* Kubernetes 官方舵轮(简化矢量), 避免 emoji 字形在各平台被挤压变形 */
/* Kubernetes 官方 logo (github.com/kubernetes/kubernetes logo.svg, 矢量原样内联) */
const K8S_LOGO = "data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%2221.4%20178.5%20736%20714.2%22%3E%3Cpath%20fill%3D%22%23326CE5%22%20d%3D%22m%20386.93188%2C178.59794%20a%2048.929668%2C48.529248%200%200%200%20-18.75129%2C4.74509%20L%20112.30567%2C305.60274%20A%2048.929668%2C48.529248%200%200%200%2085.831333%2C338.52385%20L%2022.705266%2C613.15006%20a%2048.929668%2C48.529248%200%200%200%206.643127%2C37.20805%2048.929668%2C48.529248%200%200%200%202.781605%2C3.86153%20L%20209.23642%2C874.42456%20a%2048.929668%2C48.529248%200%200%200%2038.25525%2C18.26042%20l%20284.01821%2C-0.0654%20a%2048.929668%2C48.529248%200%200%200%2038.25525%2C-18.22769%20L%20746.8061%2C654.15419%20a%2048.929668%2C48.529248%200%200%200%209.45745%2C-41.06958%20L%20693.03931%2C338.4584%20A%2048.929668%2C48.529248%200%200%200%20666.56498%2C305.53729%20L%20410.65734%2C183.34303%20a%2048.929668%2C48.529248%200%200%200%20-23.72546%2C-4.74509%20z%22%2F%3E%3Cpath%20fill%3D%22%23fff%22%20d%3D%22m%20389.46729%2C272.05685%20c%20-8.45813%2C8.6e-4%20-15.31619%2C7.61928%20-15.31519%2C17.01687%2010e-6%2C0.14423%200.0295%2C0.28205%200.0327%2C0.42542%20-0.0125%2C1.27691%20-0.0741%2C2.81523%20-0.0327%2C3.92697%200.20171%2C5.42027%201.38324%2C9.56871%202.09439%2C14.56252%201.28834%2C10.68834%202.36788%2C19.54832%201.70169%2C27.78333%20-0.64789%2C3.10534%20-2.93516%2C5.94534%20-4.97417%2C7.91939%20l%20-0.35997%2C6.4795%20c%20-9.19102%2C0.76149%20-18.44352%2C2.1559%20-27.68515%2C4.25422%20-39.76672%2C9.02908%20-74.00517%2C29.5131%20-100.07232%2C57.17016%20-1.69145%2C-1.15393%20-4.65062%2C-3.27681%20-5.53054%2C-3.92697%20-2.7344%2C0.36926%20-5.49798%2C1.21295%20-9.09748%2C-0.88357%20-6.85378%2C-4.61354%20-13.09606%2C-10.98183%20-20.64933%2C-18.65311%20-3.46095%2C-3.66956%20-5.96724%2C-7.16386%20-10.07923%2C-10.701%20-0.9338%2C-0.80327%20-2.35888%2C-1.88971%20-3.40337%2C-2.71616%20-3.21476%2C-2.56307%20-7.00645%2C-3.89976%20-10.66827%2C-4.02514%20-4.70807%2C-0.16121%20-9.24037%2C1.67954%20-12.20634%2C5.39958%20-5.27283%2C6.61342%20-3.58466%2C16.72163%203.76335%2C22.58009%200.0746%2C0.0594%200.15396%2C0.10554%200.22907%2C0.16362%201.00973%2C0.81851%202.24619%2C1.86728%203.1743%2C2.55254%204.36352%2C3.22174%208.34948%2C4.87096%2012.69721%2C7.42852%209.15979%2C5.65673%2016.75337%2C10.34716%2022.77644%2C16.00241%202.35201%2C2.50715%202.7631%2C6.925%203.07612%2C8.83568%20l%204.90872%2C4.38512%20c%20-26.27764%2C39.54584%20-38.43915%2C88.39294%20-31.2521%2C138.16395%20l%20-6.41405%2C1.86531%20c%20-1.69048%2C2.18299%20-4.07925%2C5.61791%20-6.57773%2C6.64313%20-7.88026%2C2.48206%20-16.74905%2C3.39352%20-27.45608%2C4.51601%20-5.02684%2C0.41799%20-9.36418%2C0.16855%20-14.69342%2C1.17809%20-1.17293%2C0.2222%20-2.80722%2C0.64798%20-4.09059%2C0.94902%20-0.0446%2C0.009%20-0.0863%2C0.0226%20-0.1309%2C0.0327%20-0.07%2C0.0162%20-0.16185%2C0.0502%20-0.22907%2C0.0654%20-9.02695%2C2.18109%20-14.82588%2C10.47821%20-12.95901%2C18.65312%201.86731%2C8.17682%2010.68465%2C13.14935%2019.76576%2C11.19187%200.0656%2C-0.015%200.16074%2C-0.0175%200.22907%2C-0.0327%200.10252%2C-0.0235%200.19278%2C-0.0732%200.29452%2C-0.0981%201.2659%2C-0.27788%202.85232%2C-0.58705%203.9597%2C-0.88357%205.23946%2C-1.40285%209.03407%2C-3.46407%2013.7444%2C-5.26868%2010.13362%2C-3.63457%2018.52665%2C-6.67085%2026.70341%2C-7.85395%203.41508%2C-0.26747%207.01316%2C2.10712%208.80296%2C3.10886%20l%206.67585%2C-1.14537%20c%2015.3625%2C47.62926%2047.55736%2C86.12636%2088.32413%2C110.28245%20l%20-2.7816%2C6.67585%20c%201.0026%2C2.59224%202.10843%2C6.09958%201.36158%2C8.65957%20-2.97265%2C7.70859%20-8.0644%2C15.84504%20-13.86244%2C24.91604%20-2.80737%2C4.19078%20-5.68053%2C7.44303%20-8.21392%2C12.23906%20-0.60622%2C1.14761%20-1.37829%2C2.91048%20-1.96348%2C4.12332%20-3.93623%2C8.4219%20-1.04891%2C18.12187%206.51223%2C21.76197%207.60863%2C3.66295%2017.05297%2C-0.20037%2021.14019%2C-8.63934%200.006%2C-0.0119%200.0269%2C-0.0207%200.0327%2C-0.0327%200.004%2C-0.009%20-0.004%2C-0.0236%200%2C-0.0327%200.58217%2C-1.19647%201.40694%2C-2.76916%201.89804%2C-3.89424%202.16992%2C-4.97105%202.89194%2C-9.23107%204.41784%2C-14.03893%204.05224%2C-10.17885%206.27862%2C-20.85905%2011.85692%2C-27.51404%201.52752%2C-1.82236%204.01788%2C-2.52321%206.59985%2C-3.21451%20l%203.46882%2C-6.28315%20c%2035.53987%2C13.64156%2075.32106%2C17.30219%20115.06027%2C8.27936%209.06551%2C-2.05833%2017.81739%2C-4.72226%2026.27798%2C-7.91939%200.97492%2C1.72926%202.78672%2C5.05344%203.27248%2C5.89046%202.62384%2C0.85365%205.48775%2C1.29447%207.82122%2C4.74509%204.17347%2C7.13031%207.0276%2C15.56563%2010.50465%2C25.75439%201.52615%2C4.80777%202.28038%2C9.06798%204.45057%2C14.03892%200.49463%2C1.13301%201.31527%2C2.72779%201.89803%2C3.92697%204.07863%2C8.46638%2013.55289%2C12.34291%2021.17292%2C8.67206%207.56021%2C-3.64203%2010.45071%2C-13.34112%206.51223%2C-21.76196%20-0.58526%2C-1.2128%20-1.38994%2C-2.97575%20-1.99621%2C-4.12332%20-2.53364%2C-4.79589%20-5.40634%2C-8.01572%20-8.21392%2C-12.20634%20-5.79852%2C-9.0707%20-10.60772%2C-16.60606%20-13.58077%2C-24.3145%20-1.24313%2C-3.97574%200.20973%2C-6.44834%201.17809%2C-9.03203%20-0.57991%2C-0.66473%20-1.82087%2C-4.41925%20-2.55253%2C-6.18498%2042.36668%2C-25.0155%2073.61612%2C-64.94823%2088.29141%2C-111.06785%201.9817%2C0.31146%205.42607%2C0.92086%206.54495%2C1.14537%202.30334%2C-1.51916%204.42118%2C-3.50131%208.57389%2C-3.1743%208.17681%2C1.18266%2016.5696%2C4.2199%2026.7034%2C7.85394%204.71043%2C1.80438%208.50488%2C3.89883%2013.7444%2C5.30141%201.1074%2C0.29645%202.69378%2C0.57303%203.9597%2C0.85085%200.10179%2C0.0249%200.19195%2C0.0748%200.29452%2C0.0981%200.0684%2C0.0153%200.16352%2C0.0177%200.22908%2C0.0327%209.08163%2C1.95506%2017.90054%2C-3.01456%2019.76575%2C-11.19187%201.86478%2C-8.17539%20-3.93147%2C-16.47444%20-12.959%2C-18.65311%20-1.31311%2C-0.29859%20-3.17535%2C-0.80569%20-4.45057%2C-1.0472%20-5.32929%2C-1.00926%20-9.66655%2C-0.76036%20-14.69342%2C-1.17809%20-10.70708%2C-1.12194%20-19.57569%2C-2.03437%20-27.45607%2C-4.51601%20-3.21306%2C-1.24646%20-5.49884%2C-5.06971%20-6.61046%2C-6.64313%20l%20-6.18498%2C-1.79986%20c%203.20678%2C-23.19994%202.3421%2C-47.34497%20-3.20703%2C-71.50361%20-5.60079%2C-24.38357%20-15.49883%2C-46.68472%20-28.69961%2C-66.33309%201.58655%2C-1.44229%204.58271%2C-4.09548%205.43231%2C-4.87599%200.24835%2C-2.74801%200.035%2C-5.62922%202.87978%2C-8.67206%206.02276%2C-5.65557%2013.61694%2C-10.3452%2022.77643%2C-16.00241%204.3476%2C-2.55779%208.36659%2C-4.20655%2012.72993%2C-7.42852%200.98672%2C-0.7286%202.33409%2C-1.88243%203.37065%2C-2.71616%207.34646%2C-5.86043%209.03788%2C-15.96803%203.76335%2C-22.58009%20-5.27453%2C-6.61205%20-15.49543%2C-7.23487%20-22.84188%2C-1.37444%20-1.04569%2C0.82818%20-2.4646%2C1.90853%20-3.40338%2C2.71616%20-4.1118%2C3.53737%20-6.65119%2C7.03126%20-10.11195%2C10.701%20-7.55286%2C7.67168%20-13.79578%2C14.07193%20-20.64932%2C18.68584%20-2.96985%2C1.72897%20-7.31984%2C1.13073%20-9.29389%2C1.01446%20l%20-5.82501%2C4.15605%20C%20500.27311%2C376.86318%20455.0492%2C354.59475%20406.3533%2C350.26897%20c%20-0.1362%2C-2.04069%20-0.31463%2C-5.72937%20-0.35997%2C-6.83948%20-1.99355%2C-1.90762%20-4.40179%2C-3.53622%20-5.00689%2C-7.65759%20-0.66619%2C-8.23501%200.44607%2C-17.09499%201.73441%2C-27.78333%200.71115%2C-4.99381%201.89268%2C-9.14225%202.09439%2C-14.56252%200.0459%2C-1.23215%20-0.0277%2C-3.02011%20-0.0327%2C-4.35239%20-10e-4%2C-9.39759%20-6.85705%2C-17.01772%20-15.31518%2C-17.01687%20z%20m%20-19.17671%2C118.79088%20-4.54874%2C80.33929%20-0.32725%2C0.16363%20c%20-0.30509%2C7.18725%20-6.22028%2C12.92628%20-13.4826%2C12.92628%20-2.97487%2C0%20-5.72075%2C-0.95534%20-7.95212%2C-2.58526%20l%20-0.1309%2C0.0654%20-65.87494%2C-46.69823%20c%2020.24605%2C-19.90834%2046.14234%2C-34.62059%2075.9869%2C-41.39683%205.45167%2C-1.23781%2010.90091%2C-2.15627%2016.32965%2C-2.81433%20z%20m%2038.38615%2C0%20c%2034.84372%2C4.28545%2067.06745%2C20.06297%2091.76023%2C44.24388%20l%20-65.44952%2C46.40371%20-0.22908%2C-0.0981%20c%20-5.80924%2C4.2429%20-13.99408%2C3.19016%20-18.52221%2C-2.48708%20-1.85491%2C-2.32577%20-2.82817%2C-5.06044%20-2.94523%2C-7.82122%20l%20-0.0655%2C-0.0327%20z%20m%20-154.59178%2C74.21976%2060.14811%2C53.79951%20-0.0654%2C0.32725%20c%205.42904%2C4.71967%206.22963%2C12.90973%201.70169%2C18.58767%20-1.85478%2C2.32586%20-4.33755%2C3.88585%20-7.0031%2C4.61419%20l%20-0.0654%2C0.2618%20-77.09954%2C22.25283%20c%20-3.92412%2C-35.88222%204.53283%2C-70.7626%2022.38374%2C-99.84325%20z%20m%20270.33926%2C0.0327%20c%208.93685%2C14.48535%2015.70428%2C30.66403%2019.73304%2C48.20357%203.98044%2C17.32923%204.97939%2C34.62748%203.33792%2C51.34515%20l%20-77.49224%2C-22.31828%20-0.0654%2C-0.32725%20c%20-6.93922%2C-1.89651%20-11.20383%2C-8.95519%20-9.58835%2C-16.03514%200.66186%2C-2.90032%202.20143%2C-5.35391%204.28694%2C-7.16672%20l%20-0.0327%2C-0.16362%2059.82087%2C-53.53771%20z%20M%20377.13006%2C523.023%20h%2024.64174%20l%2015.31519%2C19.14398%20-5.49776%2C23.88908%20-22.12194%2C10.63555%20-22.18739%2C-10.66828%20-5.49776%2C-23.88907%20z%20m%2078.99757%2C65.51497%20c%201.04717%2C-0.0529%202.08976%2C0.0415%203.10886%2C0.22907%20l%200.1309%2C-0.16362%2079.75024%2C13.4826%20c%20-11.67148%2C32.79084%20-34.00528%2C61.19811%20-63.84601%2C80.20839%20l%20-30.95762%2C-74.77608%200.0981%2C-0.1309%20c%20-2.84377%2C-6.60777%200.002%2C-14.35654%206.54495%2C-17.50774%201.67514%2C-0.80677%203.42525%2C-1.2535%205.17051%2C-1.34172%20z%20m%20-133.94245%2C0.32725%20c%206.08601%2C0.0853%2011.5449%2C4.30946%2012.95901%2C10.50465%200.66202%2C2.90028%200.33981%2C5.77395%20-0.75267%2C8.31209%20l%200.22907%2C0.29452%20-30.63038%2C74.02341%20C%20275.35248%2C663.62313%20252.54242%2C636.1077%20240.34055%2C602.34787%20l%2079.06303%2C-13.41715%200.1309%2C0.16362%20c%200.88436%2C-0.16274%201.78127%2C-0.24127%202.6507%2C-0.22907%20z%20m%2066.79124%2C32.43029%20c%202.11999%2C-0.0779%204.27113%2C0.35707%206.31588%2C1.34172%202.6803%2C1.29069%204.75083%2C3.32294%206.05408%2C5.75955%20h%200.29452%20l%2038.9752%2C70.42369%20c%20-5.05825%2C1.69565%20-10.25839%2C3.1448%20-15.57699%2C4.3524%20-29.80784%2C6.7679%20-59.52097%2C4.71725%20-86.4261%2C-4.45057%20l%2038.87702%2C-70.29279%20h%200.0654%20c%202.3328%2C-4.36094%206.75698%2C-6.96265%2011.42094%2C-7.134%20z%22%2F%3E%3C%2Fsvg%3E";

/* ---- K8s 集群卡片(基础平台, 非中间件) ---- */
function clusterMaterialStatus(){
  const cc = CATALOG.cluster || {}; const arch = $('#arch').value;
  const fp = CATALOG.files_present || {};
  if (!fp['cluster_kk_' + arch]) return { ok: false, missing: 'kk 二进制' };
  const kv = state.cluster.kube_version;
  if (state.cluster.mode === 'artifact'){
    if (!fp['cluster_artifact_' + kv + '_' + arch]) return { ok: false, missing: 'artifact 产物(' + kv + ')' };
  } else if (fp['cluster_cache_ready_' + kv] === false){
    return { ok: false, missing: '部分二进制缓存' };
  } else if (state.cluster.mode !== 'online'){
    const imgs = fp['cluster_images_' + kv + '_' + arch] || [];
    if (!imgs.includes(state.cluster.cni_type))
      return { ok: false, missing: '离线镜像包(k8s-'+kv.replace(/^v/,'')+'-'+(state.cluster.cni_type||'')+', 打包时自动收集)' };
  }
  return { ok: true, missing: '' };
}
function renderClusterCard(){
  const box = $('#clusterBox'); if (!box || !CATALOG) return;
  const cc = CATALOG.cluster; if (!cc){ box.innerHTML = ''; return; }
  if (!state.cluster.kube_version){
    const rec = Object.keys(cc.versions || {}).find(v => cc.versions[v].recommended) || Object.keys(cc.versions || {})[0];
    state.cluster.kube_version = state.cluster.kube_version || rec || '';
  }
  const arch = $('#arch').value;
  const armBlock = arch === 'arm64';   // 集群暂仅 amd64, arm64 后续开放
  if (armBlock) state.cluster.enabled = false;
  const size = ((CATALOG.sizes.cluster_k8s || {})[arch]) || 0;
  const on = state.cluster.enabled && !armBlock;
  const mat = clusterMaterialStatus();
  const matBadge = armBlock ? '' :
    (mat.ok ? '<span style="display:inline-block;margin-left:8px;padding:1px 8px;border-radius:9px;background:#e6f7e9;color:#1a7f37;font-size:11px;vertical-align:middle">物料就绪</span>'
            : '<span style="display:inline-block;margin-left:8px;padding:1px 8px;border-radius:9px;background:#fde8e8;color:#c0392b;font-size:11px;vertical-align:middle" title="运行 python prepare_cluster.py --download 补齐">缺料: '+esc(mat.missing)+'</span>');
  box.innerHTML = '<h3 class="cat-h">基础平台 <i>1</i></h3><div class="svc-grid">'+
    '<div class="svc'+(on?' on':'')+(armBlock?' dis':'')+'" id="k8sSvcCard" style="min-height:96px">'+
    '<span class="tick"></span>'+
    '<div class="head"><div class="avatar" style="background:#326CE5;overflow:hidden">'+
    '<img src="'+K8S_LOGO+'" alt="" style="width:82%;height:82%;object-fit:contain"></div>'+
    '<div><b>Kubernetes 集群</b><span class="ver">'+esc(state.cluster.kube_version)+matBadge+'</span></div></div>'+
    '<div class="foot"><span class="size" style="white-space:nowrap;text-align:center;line-height:1.5">'+(arch==='amd64'?'x86':'ARM')+'<br>'+fmtGB(size)+'</span>'+
    '<span>使用开源应用 kubekey 部署, 支持信创环境, 独立于中间件以外</span></div>'+
    '<div class="warnline">'+(armBlock?'ARM64 暂未开放, 请切回 x86_64 打集群包':'')+'</div></div></div>'+
    '<div class="hint" style="margin-top:6px">物料缺失时打包自动补齐: 二进制走国内源下载, 离线镜像包用 Docker Desktop 收集(或 python prepare_cluster.py --images flannel), chart 走 GitHub 镜像源</div>';
  $('#k8sSvcCard').onclick = () => {
    if (armBlock){ toast('K8s 集群暂仅支持 x86_64, ARM64 后续开放|K8s cluster is x86_64-only for now'); return; }
    state.cluster.enabled = !state.cluster.enabled;
    saveLocal(); renderClusterCard(); renderPool(); renderInfraTopology(); updateStat(); updateStepBadges && updateStepBadges();
  };
}

/* ---- 基础设施拓扑(第 1 步: 部署服务器池独立成块 + 集群参数/角色) ---- */
function renderPool(){
  const box = $('#poolBox'); if (!box) return;
  let html = '<div class="map">'+
    '<div class="map-cols svcols"><span>别名</span><span>SSH 用户</span><span>SSH 密码 <em style="font-style:normal;opacity:.75">(留空=免密)</em></span><span>服务器 IP (IPv4)</span><span style="text-align:center">端口</span><span></span></div>';
  state.servers.forEach((sv, i) => {
    html += '<div class="map-row svrow">'+
      '<input type="text" data-sv="'+i+':name" value="'+esc(sv.name)+'" placeholder="node'+(i+1)+'">'+
      '<input type="text" data-sv="'+i+':user" value="'+esc(sv.user)+'" placeholder="root">'+
      '<input type="password" data-sv="'+i+':pass" value="'+esc(sv.pass||'')+'" placeholder="留空则用免密登录" autocomplete="new-password">'+
      '<input type="text" data-sv="'+i+':ip" value="'+esc(sv.ip)+'" placeholder="如 10.0.0.1'+i+'">'+
      '<input type="text" data-sv="'+i+':ssh" value="'+esc(sv.ssh)+'" placeholder="22" style="text-align:center">'+
      '<button type="button" class="delrow" data-svdel="'+i+'" title="移除">×</button></div>';
  });
  html += '</div><button type="button" class="addport wide" data-svadd="1">+ 添加服务器</button>'+
    '<div class="hint" style="margin-top:7px">K8s 集群与中间件多机部署的节点均从本池选择; K8s 密码登录即可(kk 自带 SSH 客户端)</div>'+
    '<div class="hint" style="margin-top:4px">中间件默认部署在交付包所在服务器(运行 deploy.sh 的机器); 跨服务器部署请在中间件卡片配置多机拓扑</div>';
  box.innerHTML = html;
  bindPoolEvents(box);
}

function renderInfraTopology(){
  const box = $('#topoInfraBox'); if (!box) return;
  if (!state.cluster.enabled){
    box.innerHTML = '<div class="hint">勾选上方 Kubernetes 集群卡片后, 这里会出现集群参数与角色分配(节点从上方「部署服务器池」选择)</div>';
    return;
  }
  const cc = CATALOG.cluster || {};
  const cl = state.cluster;

  // ---- 角色分配(前置定义, 区块渲染在最前) ----
  const roleSel = function(i, pick) {
    const sv = state.servers[i] || {};
    const lab = (sv.name || ('node'+(i+1))) + ' · ' + (sv.ip || 'IP未填');
    const rs = [['','不参与'],['control-plane','控制面'],['worker','工作节点'],['registry','镜像仓库']];
    return '<div class="krow"><label>'+esc(lab)+'</label><div class="kfield"><select data-crole="'+i+'">'+
      rs.map(function(t) { return '<option value="'+t[0]+'" '+(pick===t[0]?'selected':'')+'>'+t[1]+'</option>'; }).join('')+
      '</select></div></div>';
  };

  let html = '';

  // ---- 1. 集群角色分配(最先看到) ----
  html += '<h3 class="cat-h">集群角色分配 <span class="hint">(至少 1 台控制面; 主部署机必须是集群节点之一)</span></h3>'+
    '<div class="kform" style="max-width:420px">'+
    (state.servers.length ? state.servers.map(function(sv, i) { return roleSel(i, (cl.roles||{})[i]); }).join('') :
      '<div class="khint">服务器池为空, 请先在上方「部署服务器池」添加节点</div>')+
    '<div class="khint">镜像仓库角色: 部署私有镜像仓库时必需; etcd 默认堆叠在控制面</div></div>';

  // ---- 2. 基础参数 ----
  const verOpts = Object.keys(cc.versions || {}).map(function(v) {
    return '<option value="'+v+'" '+(cl.kube_version===v?'selected':'')+'>'+v+((cc.versions[v].recommended)?' (推荐)':'')+'</option>';
  }).join('');
  const cniOpts = ['calico','cilium','flannel','kubeovn'].map(function(c) {
    return '<option value="'+c+'" '+(cl.cni_type===c?'selected':'')+'>'+c+'</option>';
  }).join('');
  const haType = cl.ha_type || 'local';
  const cpCount = state.servers.filter(function(sv, i) { return (cl.roles||{})[i]==='control-plane'; }).length;
  const haField = cpCount > 1
    ? '<select data-cf="ha_type">'+
      '<option value="local" '+(haType==='local'?'selected':'')+'>local (仅单控制面可用)</option>'+
      '<option value="kube-vip" '+(haType==='kube-vip'?'selected':'')+'>kube-vip (推荐)</option>'+
      '<option value="haproxy" '+(haType==='haproxy'?'selected':'')+'>haproxy</option></select>'
    : '<input type="text" value="local (单控制面)" disabled>';
  // kube-vip/haproxy 必须提供端点地址(字段在下方 html 拼接区渲染, 依赖 kfTxt/kHint)
  const showVip = haType !== 'local';
  const modeShown = cl.mode === 'artifact' ? 'cache' : cl.mode;   // 旧配置的 artifact 模式并入纯离线展示
  const modeOpts = [
    ['cache', '纯离线 (二进制+镜像全本地)', '镜像打包时按原生 tag 收集, 部署前导入节点本地 containerd, 全程不访问外网'],
    ['online', '在线安装 (联网)', '节点直接联网下载; 国区自动 zone=cn 走国内源'],
  ].map(function(t) {
    return '<label class="kcheck" title="'+esc(t[2])+'"><input type="radio" name="cmode" value="'+t[0]+'" '+(modeShown===t[0]?'checked':'')+'><span>'+esc(t[1])+'</span></label>';
  }).join('');
  const distroChips = Object.entries(cc.distros || {}).map(function(e) {
    const d = e[0], m = e[1];
    const closed = m.open === false;
    return '<label '+(closed ? 'title="闭源/无公开容器镜像: 需自行在对应系统上提取依赖包并放入 warehouse/cluster/os/'+d+'/"' : '')+'>'+
      '<input type="checkbox" data-cos="'+d+'" '+((cl.os_distros||[]).includes(d)?'checked':'')+'><span>'+esc(m.label)+(closed?' ⚠':'')+'</span></label>';
  }).join('');
  const upOpts = '<option value="">不生成 (默认)</option>'+
    Object.keys(cc.versions||{}).filter(function(v){return v!==cl.kube_version;}).map(function(v) {
      return '<option value="'+v+'" '+(cl.upgrade_to===v?'selected':'')+'>'+v+'</option>';
    }).join('');

  const kfRow = function(l1, f1, l2, f2) {
    // 四参: 一行左右两组字段; 三参: 单字段行 + 缩进提示; 两参: 单字段行
    if (l2 && f2) return '<div class="krow"><label>'+l1+'</label><div class="kfield">'+f1+'</div><label>'+l2+'</label><div class="kfield">'+f2+'</div></div>';
    let h = '<div class="krow"><label>'+l1+'</label><div class="kfield">'+f1+'</div></div>';
    if (l2) h += kHint(l2);
    return h;
  };
  const kHint = function(text) {
    return '<div class="krow"><label></label><div class="kfield"><div class="khint">'+text+'</div></div></div>';
  };
  const kfTxt = function(cf, val, ph) {
    return '<input type="text" data-cf="'+cf+'" value="'+esc(val||'')+'" placeholder="'+esc(ph||'')+'">';
  };
  const kfChk = function(cf, val, label) {
    return '<label class="kcheck"><input type="checkbox" data-cfb="'+cf+'" '+(val?'checked':'')+'><span>'+esc(label)+'</span></label>';
  };
  const kfLines = function(cf, val, rows, ph) {
    return '<textarea data-cfl="'+cf+'" rows="'+rows+'" placeholder="'+esc(ph||'')+'">'+esc((val||[]).join('\n'))+'</textarea>';
  };

  html += '<h3 class="cat-h" style="margin-top:16px">集群基础配置</h3><div class="kform">'+
    kfRow('K8s 版本', '<select data-cf="kube_version">'+verOpts+'</select>',
        'CNI 插件', '<select data-cf="cni_type">'+cniOpts+'</select>')+
    kfRow('Pod CIDR', kfTxt('pod_cidr', cl.pod_cidr), 'Service CIDR', kfTxt('service_cidr', cl.service_cidr))+
    kfRow('控制面 HA', haField, 'kube-proxy 模式', '<select data-cf="proxy_mode">'+
      '<option value="iptables" '+(cl.proxy_mode==='iptables'?'selected':'')+'>iptables (旧内核安全)</option>'+
      '<option value="nftables" '+(cl.proxy_mode==='nftables'?'selected':'')+'>nftables (需内核≥5.13)</option></select>')+
    (showVip ? kfRow('kube-vip VIP / 端点地址', kfTxt('ha_vip', cl.ha_vip,
        haType==='kube-vip' ? '必填: 控制面同网段空闲 IP, 如 10.0.0.100' : '必填: 如 lb.example.com 或 127.0.0.2')) : '')+
    (showVip ? (haType==='kube-vip'
        ? kHint('kube-vip 以静态 Pod 跑在控制面节点: VIP 须与节点同网段且未被占用, kk 自动选择网卡并漂移 VIP')
        : kHint('haproxy 静态 Pod 在每个节点本机转发 API(后端自动指向控制面节点); 端点地址写域名或 127.0.0.2 即可')) : '')+
    kfRow('时区', kfTxt('timezone', cl.timezone), '升级包', '<select data-cf="upgrade_to">'+upOpts+'</select>')+
    kHint('附带目标版本 kube 三件套与 upgrade-cluster.sh, 打包时自动下载目标版本二进制')+
    kfRow('安装方式', '<div class="kinline">'+modeOpts+'</div>')+
    kfRow('OS 依赖包', '<div class="kchips">'+distroChips+'</div>')+
    kHint('开源发行版依赖包已内置')+
    kHint('⚠ 闭源/无公开镜像(麒麟/统信/阿里云)需自行提取放置, 详见 docs/k8s-信创离线部署.md')+
    '</div>';

  // ---- 3. 集群高级配置(折叠) ----
  const comp = cl.components || {};
  const kl = cl.kubelet || {};
  const st = cl.storage || {};
  const ir = cl.image_registry || {};
  const ntp = cl.ntp || {};
  const adv =
    kfRow('CRI 运行时', '<select data-cf="container_manager">'+
      '<option value="containerd" '+(cl.container_manager!=='docker'?'selected':'')+'>containerd (默认)</option>'+
      '<option value="docker" '+(cl.container_manager==='docker'?'selected':'')+'>docker (仅在线安装)</option></select>',
      'Pod 子网掩码', kfTxt('ipv4_mask_size', cl.ipv4_mask_size, '每节点 Pod 子网掩码, 默认 24'))+
    kHint('docker 运行时仅支持在线安装(K8s ≥1.24 由 kk 自动装 cri-dockerd); 纯离线包只预装 containerd')+
    kfRow('containerd 数据目录', kfTxt('components.containerd_root', comp.containerd_root, '/var/lib/containerd'),
        'docker 数据目录', kfTxt('components.docker_root', comp.docker_root, '/var/lib/docker'))+
    kfRow('etcd 数据目录', kfTxt('components.etcd_dir', comp.etcd_dir, '/var/lib/etcd'),
        'kubelet 数据目录', kfTxt('kubelet.root_dir', kl.root_dir, '/var/lib/kubelet'))+
    kfRow('kubelet max-pods', kfTxt('kubelet.max_pods', kl.max_pods, '110'),
        'containerd 版本覆盖', kfTxt('components.containerd_version_override', comp.containerd_version_override, '可选, 如 v1.7.27'))+
    kfRow('containerd 运行时', kfChk('components.static_binary', comp.static_binary, '静态构建 (glibc < 2.35 老系统必选)'))+
    kHint('containerd 2.x 依赖 glibc ≥ 2.35, 老系统请勾选静态构建或降级 1.7.x')+
    kfRow('kubelet 扩展参数', kfLines('kubelet.extra_args', kl.extra_args, 3, '每行一个: key=value (如 system-reserved=memory=300Mi)'))+
    kfRow('kubelet 扩展配置', '<textarea data-cft="kubelet.extra_config" rows="4" placeholder="KubeletConfiguration 追加字段 (YAML 缩进), 如 maxOpenFiles: 1000000">'+esc(kl.extra_config||'')+'</textarea>')+
    kfRow('etcd 高级参数', kfLines('components.etcd_env', comp.etcd_env, 3,
        '每行 key=value, 可选: heartbeat_interval / election_timeout / compaction_retention / snapshot_count / quota_backend_bytes / max_request_bytes / max_snapshots / max_wals / log_level'),
        'kubeadm 备份目录', kfTxt('kubeadm_config_dir', cl.kubeadm_config_dir, '/etc/kubekey/backup/kubernetes, 留空=禁用'))+
    kHint('etcd 参数写入 config 的 etcd.env(data_dir 同); kubeadm-config 每次部署自动带时间戳备份到该目录')+
    kfRow('Multi-CNI', '<select data-cf="multi_cni">'+
      '<option value="none" '+(cl.multi_cni!=='multus'?'selected':'')+'>none (默认)</option>'+
      '<option value="multus" '+(cl.multi_cni==='multus'?'selected':'')+'>multus (Pod 多网卡)</option></select>',
      'Multus 镜像 tag', kfTxt('multi_cni_tag', cl.multi_cni_tag, '可选, 默认 v4.3.0'))+
    kfRow('Calico values 覆盖', '<textarea data-cft="calico_values" rows="5" placeholder="仅 CNI=calico 生效; 原样透传 helm -f, 可配置 ipipMode / vxlanMode / mtu 等, 如:\ninstallation:\n  calicoNetwork:\n    ipipMode: Always\n    vxlanMode: Never\n    mtu: 1440">'+esc(cl.calico_values||'')+'</textarea>')+
    kHint('Calico 专属调优(ipipMode/vxlanMode/mtu 等)经此透传; 留空用 kk 默认 values')+
    kfRow('节点 hostname', kfChk('set_hostname', cl.set_hostname !== false, '由 kk 设置节点 hostname (容器验证等特殊场景可关)'),
        'K8s 镜像源前缀', kfTxt('k8s_image_registry', cl.k8s_image_registry, '如 hub.kubesphere.com.cn'))+
    kHint('镜像源前缀用于在线/镜像下载的仓库地址; 节点 containerd 侧的加速器即下方「镜像加速」')+
    kfRow('镜像加速 (docker.io)', kfLines('registry_mirrors', cl.registry_mirrors, 3, '每行一个 https://... , 写入节点 containerd')+
      '<button type="button" class="mini" data-cfill="1" style="margin-top:6px">一键填入实测可用源</button>',
      '与 Docker 加速器同一批实测源; K8s 组件镜像在离线/zone=cn 模式下自动走集群内仓库或国内源')+
    kfRow('NTP 服务器', kfChk('ntp.enabled', ntp.enabled, '自定义时间同步 (默认关闭, 部署脚本只设时区)')+
      '<div style="margin-top:6px">'+kfLines('ntp.servers', ntp.servers, 2, '每行一台 NTP 服务器, 如 ntp.internal.corp')+'</div>')+
    kfRow('CoreDNS 镜像 tag', kfTxt('dns.coredns_tag', (cl.dns||{}).coredns_tag, '可选, 默认跟随 kk 版本矩阵'),
        'NodeLocalDNS', kfChk('dns.nodelocaldns_enabled', (cl.dns||{}).nodelocaldns_enabled !== false, '启用 NodeLocalDNS (默认开)')+
      '<div style="margin-top:6px">'+kfTxt('dns.nodelocaldns_tag', (cl.dns||{}).nodelocaldns_tag, 'NodeLocalDNS 镜像 tag, 可选')+'</div>')+
    kfRow('证书续期 crontab', kfTxt('certs_renew_cron', cl.certs_renew_cron, '如 0 0 1 * * (每月 1 号零点续期); 留空=不启用'),
        '安装时证书续期', kfChk('certs_renew', cl.certs_renew !== false, '部署时执行一轮 kubeadm certs renew (默认开)'))+
    kHint('crontab 在主部署机执行 kk certs renew 并自动重启控制面静态 Pod(日志 /var/log/kk-certs-renew.log); kk 侧安装续期由 kubernetes.certs.renew 控制')+
    kfRow('localpv 存储类', kfChk('storage.localpv_enabled', st.localpv_enabled, '启用 localpv 存储类')+
      '<div style="margin-top:6px">'+kfTxt('storage.localpv_path', st.localpv_path, '/var/openebs/local')+'</div>')+
    kfRow('NFS 存储类', kfChk('storage.nfs_enabled', st.nfs_enabled, '启用 NFS 存储类')+
      '<div style="display:flex;gap:8px;margin-top:6px;flex-wrap:wrap">'+
      kfTxt('storage.nfs_server', st.nfs_server, 'NFS 服务器地址(必填)')+
      kfTxt('storage.nfs_path', st.nfs_path, '导出路径')+
      kfChk('storage.nfs_default', st.nfs_default, '设为默认')+'</div>')+
    kfRow('私有镜像仓库', '<select data-cf="image_registry.type">'+
      '<option value="" '+(!ir.type?'selected':'')+'>不部署 (默认)</option>'+
      '<option value="harbor" '+(ir.type==='harbor'?'selected':'')+'>Harbor</option>'+
      '<option value="docker-registry" '+(ir.type==='docker-registry'?'selected':'')+'>Docker Registry</option></select>'+
      '<div style="margin-top:6px">'+kfTxt('image_registry.vip', ir.vip, 'HA VIP(可选)')+'</div>',
      '部署在分配了「镜像仓库」角色的节点; 多仓库节点自动 keepalived HA');

  html += '<details style="margin-top:14px"><summary style="cursor:pointer"><h3 class="cat-h" style="margin:0">集群高级配置 <span class="hint">(运行时 / 数据目录 / etcd 调优 / Multi-CNI / Calico / DNS / 证书与备份 / 镜像加速 / NTP / 存储 / 私有镜像仓库)</span></h3></summary>'+
    '<div style="margin-top:10px"><div class="kform">'+adv+'</div></div></details>';

  box.innerHTML = html;
  bindInfraEvents(box);
}

function setPath(obj, path, val){
  const parts = path.split('.');
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i++){
    if (typeof cur[parts[i]] !== 'object' || cur[parts[i]] === null) cur[parts[i]] = {};
    cur = cur[parts[i]];
  }
  cur[parts[parts.length-1]] = val;
}

function bindPoolEvents(box){
  box.querySelectorAll('input[data-sv]').forEach(i => i.oninput = () => {
    const [idx, field] = i.dataset.sv.split(':');
    state.servers[+idx][field] = i.value;
    saveLocal();
    if (field === 'name' || field === 'ip'){
      // 同步下方 K8s 角色分配的节点标签(只改文字, 不重渲染, 避免丢失输入焦点)
      const sel = document.querySelector('#topoInfraBox select[data-crole="'+idx+'"]');
      if (sel){
        const sv = state.servers[+idx] || {};
        sel.closest('.krow').querySelector('label').textContent =
          (sv.name || ('node'+(+idx+1))) + ' · ' + (sv.ip || 'IP未填');
      }
    }
  });
  box.querySelectorAll('[data-svdel]').forEach(b => b.onclick = () => {
    state.servers.splice(+b.dataset.svdel, 1);
    for (const k of Object.keys(state.mh)){
      const m = state.mh[k];
      const fix = x => (typeof x === 'number' && x >= state.servers.length ? null : x);
      if (k === 'kafka'){ m.brokers = (m.brokers||[]).map(fix); }
      else { m.master = fix(m.master); m.replicas = (m.replicas||[]).map(fix); }
    }
    saveLocal(); renderTopology(); renderPool(); renderInfraTopology(); renderSvcGridSync();
  });
  box.querySelectorAll('[data-svadd]').forEach(b => b.onclick = () => {
    state.servers.push({ name: 'node'+(state.servers.length+1), user: 'root', ip: '', ssh: 22, pass: '' });
    saveLocal(); renderPool(); renderInfraTopology();
  });
}

function bindInfraEvents(box){
  // ---- 事件: K8s 集群参数 / 角色分配(服务器池事件在 bindPoolEvents) ----
  box.querySelectorAll('select[data-cf],input[data-cf]').forEach(i => i.oninput = () => {
    setPath(state.cluster, i.dataset.cf, i.value);
    if (i.dataset.cf === 'kube_version'){ renderClusterCard(); updateStat(); }
    if (i.dataset.cf === 'ha_type'){ saveLocal(); renderInfraTopology(); return; }   // 重渲染以显示/隐藏 VIP 行
    saveLocal();
  });
  box.querySelectorAll('input[data-cfb]').forEach(c => c.onchange = () => {
    setPath(state.cluster, c.dataset.cfb, c.checked);
    saveLocal();
  });
  box.querySelectorAll('textarea[data-cfl]').forEach(t => t.oninput = () => {
    setPath(state.cluster, t.dataset.cfl, t.value.split('\n').map(s => s.trim()).filter(Boolean));
    saveLocal();
  });
  box.querySelectorAll('textarea[data-cft]').forEach(t => t.oninput = () => {
    setPath(state.cluster, t.dataset.cft, t.value);
    saveLocal();
  });
  box.querySelectorAll('input[name="cmode"]').forEach(r => r.onchange = () => {
    state.cluster.mode = r.value;
    if (r.value === 'online' && !state.cluster.zone) state.cluster.zone = 'cn';
    saveLocal(); renderInfraTopology();
  });
  box.querySelectorAll('[data-cfill]').forEach(b => b.onclick = () => {
    state.cluster.registry_mirrors = [...(CATALOG.defaults.registry_mirrors || [])];
    saveLocal(); renderInfraTopology();
    toast('已填入实测可用的镜像加速源|Verified mirrors filled in');
  });
  box.querySelectorAll('input[data-cos]').forEach(c => c.onchange = () => {
    const d = c.dataset.cos;
    state.cluster.os_distros = c.checked
      ? [...new Set([...(state.cluster.os_distros||[]), d])]
      : (state.cluster.os_distros||[]).filter(x => x !== d);
    saveLocal();
  });
  box.querySelectorAll('select[data-crole]').forEach(s => s.onchange = () => {
    const i = s.dataset.crole;
    if (s.value) state.cluster.roles[i] = s.value;
    else delete state.cluster.roles[i];
    saveLocal(); renderInfraTopology();   // 控制面数量变化联动 HA 行显隐
  });
}

function renderTopology(){
  const box = $('#topoBox'); if (!box) return;
  const poolOpts = (pick) => {
    let o = '<option value="">选择服务器…</option>';
    state.servers.forEach((sv, i) => {
      const lab = (sv.name || ('node'+(i+1))) + ' · ' + (sv.ip || 'IP未填');
      o += '<option value="'+i+'" '+(pick===i?'selected':'')+'>'+esc(lab)+'</option>';
    });
    return o;
  };
  const selMh = (svc, field, idx, pick) =>
    '<select data-mh="'+svc+':'+field+':'+idx+'">'+poolOpts(pick)+'</select>';
  let html = '';

  // 部署服务器池与 K8s 集群参数已迁移至第 1 步「基础平台」; 此处仅中间件部署形态
  if (['mysql8','mysql57','redis','kafka'].some(s => selected.has(s))){
    html += '<div class="hint" style="margin-bottom:10px">多机部署的节点在「基础平台」的部署服务器池中选择</div>';
  }

  // ---- MySQL 8.0 / 5.7 ----
  for (const s of ['mysql8', 'mysql57']){
    if (!selected.has(s)) continue;
    const m = state.topology[s] || 'single';
    const nm = s === 'mysql8' ? 'MySQL 8.0' : 'MySQL 5.7';
    const defPort = s === 'mysql8' ? '13308' : '13309';
    const mh = state.mh[s] || { enabled:false, master:null, replicas:[null] };
    html += '<div class="grp"><h3>'+nm+' 部署形态</h3><div class="segs">'+
      '<label><input type="radio" name="topo_'+s+'" value="single" '+(m==='single'?'checked':'')+'><span>单机</span></label>'+
      '<label><input type="radio" name="topo_'+s+'" value="master-slave" '+(m==='master-slave'?'checked':'')+'><span>主从复制 (GTID)</span></label>'+
      '</div>';
    if (m === 'master-slave'){
      html += '<div class="segs" style="margin-top:10px">'+
        '<label><input type="radio" name="reps_'+s+'" value="1" '+((state.replicas[s]||1)===1?'checked':'')+'><span>1 从库</span></label>'+
        '<label><input type="radio" name="reps_'+s+'" value="2" '+((state.replicas[s]||1)===2?'checked':'')+'><span>2 从库</span></label>'+
        '<label><input type="radio" name="mhmode_'+s+'" value="single" '+(mh.enabled?'':'checked')+'><span>同机部署</span></label>'+
        '<label><input type="radio" name="mhmode_'+s+'" value="multi" '+(mh.enabled?'checked':'')+'><span>多机部署 (跨服务器)</span></label></div>';
      if (mh.enabled){
        const nrep = state.replicas[s] || 1;
        html += '<div class="pxsub" style="margin-top:12px"><div style="font-weight:600;font-size:12.5px;margin-bottom:9px">角色分配 <span class="hint">(从服务器池中选择, 每台一角色)</span></div>'+
          '<div class="map-row" style="grid-template-columns:80px 1fr;gap:10px;margin-bottom:8px"><span class="ctn">主库</span>'+selMh(s,'master',0,mh.master)+'</div>';
        for (let r = 0; r < nrep; r++){
          html += '<div class="map-row" style="grid-template-columns:80px 1fr;gap:10px;margin-bottom:8px"><span class="ctn">从库'+(r+1)+'</span>'+
            selMh(s,'replicas',r,(mh.replicas||[])[r])+'</div>';
        }
        html += '<div class="hint">从库只读 (GTID 自动同步), 复制关系由节点安装脚本自动建立; 主库 IP 自动写入 Nacos/XXL-Job 配置</div></div>';
      } else {
        html += '<div class="hint" style="margin-top:7px">主库服务名仍为 '+s+' (Nacos/XXL-Job 照常连它), 从库只读端口 '+defPort+' 自动同步; 部署脚本自动配置复制关系</div>';
      }
      html += '<div style="margin-top:10px"><label style="display:inline-flex;align-items:center;gap:7px;cursor:pointer">'+
        '<input type="checkbox" data-feat="proxysql" '+(state.features.proxysql?'checked':'')+'>'+
        '<span>读写分离 (ProxySQL: 写走主库, SELECT 自动分流到从库, 应用只连一个入口端口)</span></label></div>';
    }
    html += '</div>';
  }

  // ---- Redis ----
  if (selected.has('redis')){
    const r = state.topology.redis || 'single';
    const mh = state.mh.redis || { enabled:false, master:null, replicas:[null,null] };
    html += '<div class="grp"><h3>Redis 部署形态</h3><div class="segs">'+
      '<label><input type="radio" name="topo_redis" value="single" '+(r==='single'?'checked':'')+'><span>单机</span></label>'+
      '<label><input type="radio" name="topo_redis" value="sentinel" '+(r==='sentinel'?'checked':'')+'><span>哨兵高可用 (主 + 从 + 3 哨兵)</span></label>'+
      '</div>';
    if (r === 'sentinel'){
      html += '<div class="segs" style="margin-top:10px">'+
        '<label><input type="radio" name="mhmode_redis" value="single" '+(mh.enabled?'':'checked')+'><span>同机部署 (1 台服务器)</span></label>'+
        '<label><input type="radio" name="mhmode_redis" value="multi" '+(mh.enabled?'checked':'')+'><span>多机部署 (3 台服务器)</span></label></div>';
      if (mh.enabled){
        html += '<div class="pxsub" style="margin-top:12px"><div style="font-weight:600;font-size:12.5px;margin-bottom:9px">角色分配</div>'+
          '<div class="map-row" style="grid-template-columns:80px 1fr;gap:10px;margin-bottom:8px"><span class="ctn">主库</span>'+selMh('redis','master',0,mh.master)+'</div>';
        for (let i = 0; i < 2; i++){
          html += '<div class="map-row" style="grid-template-columns:80px 1fr;gap:10px;margin-bottom:8px"><span class="ctn">从库'+(i+1)+'</span>'+
            selMh('redis','replicas',i,(mh.replicas||[])[i])+'</div>';
        }
        html += '<div class="hint">每台节点运行 redis + sentinel, 哨兵监控指向主库节点 IP, 应用走 sentinel 协议</div></div>';
      } else {
        html += '<div class="hint" style="margin-top:7px">哨兵: 从库端口 16380 只读; 应用侧使用 sentinel 协议 (redis-sentinel:26379, master 名称 mymaster) 实现故障自动切换</div>';
      }
    }
    html += '</div>';
  }

  // ---- Kafka ----
  if (selected.has('kafka')){
    const k = state.topology.kafka || 'single';
    const mh = state.mh.kafka || { enabled:false, count:3, brokers:[null,null,null] };
    html += '<div class="grp"><h3>Kafka 部署形态</h3><div class="segs">'+
      '<label><input type="radio" name="topo_kafka" value="single" '+(k==='single'?'checked':'')+'><span>单节点 (KRaft)</span></label>'+
      '<label><input type="radio" name="topo_kafka" value="cluster" '+(k==='cluster'?'checked':'')+'><span>集群 (KRaft + SASL)</span></label>'+
      '</div>';
    if (k === 'cluster'){
      html += '<div class="segs" style="margin-top:10px">'+
        '<label><input type="radio" name="mhmode_kafka" value="single" '+(mh.enabled?'':'checked')+'><span>单机部署 (1 台服务器 · 3 容器)</span></label>'+
        '<label><input type="radio" name="mhmode_kafka" value="multi" '+(mh.enabled?'checked':'')+'><span>多机部署 (每台 1 broker)</span></label></div>';
      if (mh.enabled){
        const cnt = mh.count || 3;
        html += '<div class="segs" style="margin-top:10px">'+
          '<label><input type="radio" name="kafkacnt" value="3" '+(cnt===3?'checked':'')+'><span>3 节点</span></label>'+
          '<label><input type="radio" name="kafkacnt" value="5" '+(cnt===5?'checked':'')+'><span>5 节点</span></label></div>'+
          '<div class="pxsub" style="margin-top:12px"><div style="font-weight:600;font-size:12.5px;margin-bottom:9px">Broker 分配 <span class="hint">(broker 1..'+cnt+' 与节点一一对应)</span></div>';
        for (let i = 0; i < cnt; i++){
          html += '<div class="map-row" style="grid-template-columns:80px 1fr;gap:10px;margin-bottom:8px"><span class="ctn">broker'+(i+1)+'</span>'+
            selMh('kafka','brokers',i,(mh.brokers||[])[i])+'</div>';
        }
        html += '<div class="hint">KRaft 组合模式 (无 ZooKeeper), SASL_PLAINTEXT 鉴权; 镜像改用 bitnami/kafka 3.7.0 (双架构); 节点端口组在「端口与反代」中配置</div></div>';
      } else {
        html += '<div class="hint" style="margin-top:7px">单机集群: 3 broker 互为副本与仲裁 (KRaft 组合模式, 无 ZooKeeper), SASL 多用户鉴权; 镜像改用 bitnami/kafka 3.7.0 (双架构, 体积约为单节点的 3 倍)</div>';
      }
    } else {
      html += '<div class="hint" style="margin-top:7px">单节点 KRaft (无 ZooKeeper), SASL/SCRAM 鉴权, 密码在「账号与数据」中设置</div>';
    }
    html += '</div>';
  }
  box.innerHTML = html;

  // ---- 事件: 服务器池与集群参数已迁移至 bindInfraEvents(第 1 步基础设施区) ----

  // ---- 事件: 部署形态单选 ----
  box.querySelectorAll('input[name^="topo_"]').forEach(r => r.onchange = () => {
    const k = r.name.replace('topo_', '');
    state.topology[k] = r.value;
    if (r.value !== 'cluster' && r.value !== 'master-slave' && r.value !== 'sentinel'){
      state.mh[k] = { enabled:false, count:3, master:null, replicas:[null,null], brokers:[null,null,null] };
      if (k === 'kafka') state.mh[k] = { enabled:false, count:3, brokers:[null,null,null] };
      if (k === 'redis') state.mh[k] = { enabled:false, master:null, replicas:[null,null] };
      else state.mh[k] = { enabled:false, master:null, replicas:[null] };
    }
    saveLocal();
    const names = { mysql8: 'MySQL 8.0', mysql57: 'MySQL 5.7', redis: 'Redis', kafka: 'Kafka' };
    const modes = {
      mysql8: r.value === 'single' ? 'MySQL 8.0 单机模式' : 'MySQL 8.0 主从模式 (GTID)',
      mysql57: r.value === 'single' ? 'MySQL 5.7 单机模式' : 'MySQL 5.7 主从模式 (GTID)',
      redis: r.value === 'single' ? 'Redis 单机模式' : 'Redis 哨兵高可用',
      kafka: r.value === 'single' ? 'Kafka 单节点模式' : 'Kafka 集群模式 (KRaft + SASL)'
    };
    toast(modes[k] || (names[k] + ' ' + r.value));
    renderSvcGridSync();
  });

  // ---- 事件: 同机/多机切换 ----
  box.querySelectorAll('input[name^="mhmode_"]').forEach(r => r.onchange = () => {
    const k = r.name.replace('mhmode_', '');
    const en = r.value === 'multi';
    state.mh[k] = Object.assign(state.mh[k] || {}, { enabled: en });
    if (en && k === 'kafka' && (state.mh.kafka.brokers||[]).length !== (state.mh.kafka.count||3))
      state.mh.kafka.brokers = Array(state.mh.kafka.count||3).fill(null);
    if (en && k === 'redis' && (state.mh.redis.replicas||[]).length !== 2)
      state.mh.redis.replicas = [null, null];
    if (en && (k === 'mysql8' || k === 'mysql57') && (state.mh[k].replicas||[]).length !== (state.replicas[k]||1))
      state.mh[k].replicas = Array(state.replicas[k]||1).fill(null);
    saveLocal(); renderTopology(); renderDynamic();
    toast(en ? '多机部署: 请在服务器池中完成角色分配' : '同机部署: 所有容器运行在同一台服务器');
  });

  // ---- 事件: 从库数量 ----
  box.querySelectorAll('input[name^="reps_"]').forEach(r => r.onchange = () => {
    const k = r.name.replace('reps_', '');
    state.replicas[k] = +r.value;
    if (!state.mh[k].enabled) state.mh[k].replicas = Array(+r.value).fill(null);
    else state.mh[k].replicas = Array(+r.value).fill(null);
    saveLocal(); renderTopology();
    toast((k === 'mysql8' ? 'MySQL 8.0' : 'MySQL 5.7') + ' 从库数量: ' + r.value);
  });

  // ---- 事件: Kafka 节点数 ----
  box.querySelectorAll('input[name="kafkacnt"]').forEach(r => r.onchange = () => {
    state.mh.kafka.count = +r.value;
    state.mh.kafka.brokers = Array(+r.value).fill(null);
    saveLocal(); renderTopology();
    toast('Kafka 多机节点数: ' + r.value);
  });

  // ---- 事件: 角色分配下拉 ----
  box.querySelectorAll('select[data-mh]').forEach(sel => sel.onchange = () => {
    const [svc, field, idx] = sel.dataset.mh.split(':');
    const v = sel.value === '' ? null : +sel.value;
    if (field === 'master') state.mh[svc].master = v;
    else if (field === 'replicas') state.mh[svc].replicas[+idx] = v;
    else state.mh[svc].brokers[+idx] = v;
    saveLocal();
  });

  // ---- 事件: 读写分离开关 ----
  box.querySelectorAll('input[data-feat]').forEach(c => c.onchange = () => {
    state.features.proxysql = c.checked;
    saveLocal(); renderDynamic();
    toast(c.checked ? '已启用读写分离 (ProxySQL, 镜像需补料: proxysql/proxysql 2.6.6)' : '已关闭读写分离');
  });
}
function updateSvcWarn(){}
function toggleSvc(key){
  if (key === 'mysql57' && $('#arch').value === 'arm64'){ toast('MySQL 5.7 不支持 arm64'); return; }
  if (selected.has(key)) selected.delete(key); else selected.add(key);
  renderSvcGridSync();
}
function onArchChange(){
  if ($('#arch').value === 'arm64' && selected.has('mysql57')){
    selected.delete('mysql57'); toast('已移除 MySQL 5.7 (arm64 不支持)');
  }
  renderSvcGridSync(); renderClusterCard();
}

/* ================= 动态渲染 ================= */
/* ---- 五步向导引擎 (低频视图同导航) ---- */
let curStep = 1;
function movePill(v){
  const tab = document.querySelector('#wizSteps .wtab[data-v="'+v+'"]');
  const pill = document.getElementById('wizPill');
  if (!tab || !pill) return;
  pill.style.left = tab.offsetLeft + 'px';
  pill.style.width = tab.offsetWidth + 'px';
}
function openDrawer(id){
  $$('.dw-wrap').forEach(w => { w.classList.add('hidden'); w.classList.remove('closing'); });
  const el = document.getElementById('dw-' + id);
  if (el) el.classList.remove('hidden');
}
function closeDrawers(instant){
  $$('.dw-wrap').forEach(w => {
    if (w.classList.contains('hidden')) return;
    if (instant){ w.classList.add('hidden'); w.classList.remove('closing'); return; }
    if (w.classList.contains('closing')) return;   // 出场动画进行中, 不重复触发
    w.classList.add('closing');
    setTimeout(() => { w.classList.add('hidden'); w.classList.remove('closing'); }, 280);
  });
}
function gotoView(v){
  v = String(v);
  curStep = v;
  $$('.wstep').forEach(w => w.classList.toggle('on', w.dataset.step === v));
  $$('#wizSteps .wtab').forEach(b => b.classList.toggle('on', b.dataset.v === v));
  $('#btnPrev').disabled = (v === '1');
  $('#btnNext').disabled = (v === '5');
  $('#btnPreview').style.display = $('#btnPack').style.display = (v === '5') ? '' : 'none';
  movePill(v);
  window.scrollTo(0, 0);
}
const gotoStep = gotoView;
function updateStepBadges(){
  const svcs = [...selected];
  $('#badge2').textContent = svcs.length ? (svcs.length + ' 项') : '';
  // 步骤4: 需要填写的账号/数据库是否齐备
  let pending = 0;
  if (svcs.some(s => s==='nacos'||s==='xxljob') && (!state.db.nacos || !state.db.xxljob)) pending++;
  const hasSecret = CATALOG.secrets.some(it => it.services.some(s => selected.has(s)));
  if (hasSecret && Object.values(state.secrets).some(v => !String(v||'').trim())) pending++;
  const b4 = $('#badge4');
  if (pending){ b4.textContent = pending + ' 项待配置'; b4.className = 'wbadge warn'; }
  else { b4.textContent = hasSecret ? '已配置' : ''; b4.className = 'wbadge'; }
  // badge 文本变化会改变步骤 tab 宽度, 胶囊需同步(修复: 勾选组件后胶囊宽度不更新的问题)
  movePill(curStep);
}
// 分区序号动态重排: 隐藏的分区不占号, 避免 1→3 跳号
function renumberSections(){
  for (const step of document.querySelectorAll('.wstep')){
    let n = 0;
    for (const card of step.querySelectorAll('.card')){
      const num = card.querySelector('h2 .num');
      if (!num) continue;
      if (card.style.display === 'none'){ continue; }
      num.textContent = ++n;
    }
  }
}
function renderDynamic(){
  const svcs = [...selected];
  const has = svcs.length > 0;
  $('#sec-port').style.display = has ? '' : 'none';
  $('#sec-proxy').style.display = has && selected.has('nginx') ? '' : 'none';
  const hasSecret = CATALOG.secrets.some(it => it.services.some(s => selected.has(s)));
  $('#sec-pwd').style.display  = has && hasSecret ? '' : 'none';
  $('#sec-db').style.display   = has && svcs.some(s => s==='nacos'||s==='xxljob') ? '' : 'none';
  $('#sec-backup').style.display = has && svcs.some(s => ['mysql57','mysql8','postgres','mongodb'].includes(s)) ? '' : 'none';
  const vis = id => { const el = document.getElementById(id); return el && el.style.display !== 'none'; };
  $('#step3Empty').style.display = (vis('sec-port') || vis('sec-proxy')) ? 'none' : '';
  $('#step4Empty').style.display = (vis('sec-pwd') || vis('sec-db') || vis('sec-backup')) ? 'none' : '';
  renumberSections();
  $('#step5Empty').style.display = vis('previewCard') ? 'none' : '';
  updateStepBadges();
  renderTopology();
  renderPool();
  renderInfraTopology();

  // ---- 端口映射: host → container ----
  const KAFKA_CLUSTER_PORTS = [
    {key:'kafka_c1', label:'Kafka Broker 1 (SASL)', default:19092, container:9094},
    {key:'kafka_c2', label:'Kafka Broker 2 (SASL)', default:29092, container:9094},
    {key:'kafka_c3', label:'Kafka Broker 3 (SASL)', default:39092, container:9094}
  ];
  const KAFKA_MH_PORTS = [
    {key:'kafka_mh_inter', label:'Kafka 节点互联 (PLAINTEXT)', default:9092, container:9092},
    {key:'kafka_mh_ctrl', label:'Kafka 控制器 (KRaft)', default:9093, container:9093},
    {key:'kafka_mh_sasl', label:'Kafka SASL 对外', default:9094, container:9094}
  ];
  const mhOf = k => ((state.mh || {})[k] || {});
  const mhOn = k => mhOf(k).enabled && ['cluster','master-slave','sentinel'].includes(state.topology[k]);
  const pb = $('#portBox'); pb.innerHTML = '';
  for (const s of svcs){
    const meta = CATALOG.services[s];
    const kCluster = s === 'kafka' && state.topology.kafka === 'cluster';
    const kMulti = kCluster && mhOn('kafka');
    let portDefs = kCluster ? (kMulti ? KAFKA_MH_PORTS : KAFKA_CLUSTER_PORTS) : meta.ports;
    if (s === 'kafka' && kMulti){
      portDefs = portDefs.slice();
      const hint = '<div class="hint" style="margin:4px 0 2px">同一组端口应用到所有 Kafka 节点 (每台独立机器不冲突); 节点分配在「选择中间件」中维护</div>';
      pb.insertAdjacentHTML('beforeend', '');
      var _kh = hint;   // 注入到 grp 标题后(见下方 html 拼接)
    } else { var _kh = ''; }
    if ((s === 'mysql8' || s === 'mysql57') && (mhOn(s) || state.topology[s] === 'master-slave')){
      const defs = meta.ports.slice();
      defs[0] = Object.assign({}, defs[0], {label:'主库端口|Master port'});
      defs.push({key:s+'_replica',
                 label: mhOn(s) ? '从库端口 (每台从机)|Replica port (each replica host)' : '从库端口|Replica port',
                 default: s === 'mysql8' ? 13308 : 13309, container:3306});
      if (!mhOn(s) && (state.replicas[s]||1) === 2)
        defs.push({key:s+'_replica2', label:'从库2端口|Replica-2 port',
                   default: s === 'mysql8' ? 13310 : 13311, container:3306});
      portDefs = defs;
    }
    if (s === 'redis' && (mhOn('redis') || state.topology.redis === 'sentinel')){
      const defs = meta.ports.slice();
      defs[0] = Object.assign({}, defs[0], {label:'主库端口|Master port'});
      defs.push({key:'redis_replica',
                 label: mhOn('redis') ? '从库端口 (每台从机)|Replica port (each replica host)' : '从库端口|Replica port',
                 default:16380, container:6379});
      if (mhOn('redis'))
        defs.push({key:'redis_sentinel', label:'哨兵端口 (每节点)|Sentinel port (per node)', default:26379, container:26379});
      portDefs = defs;
    }
    if (!portDefs.length && !(state.extra[s]||[]).length) continue;
    let html = '<div class="grp"><h3>'+logoSm(s)+esc(trLabel(meta.label))+(kCluster?' <span class="hint">'+(kMulti?'多机节点端口组':'3 节点集群 · SASL 端口')+'</span>':((mhOn(s))?' <span class="hint">多机 · 角色节点端口</span>':''))+'</h3>'+(_kh||'')+'<div class="map">'+
      '<div class="map-cols"><span></span><span>外部访问端口(宿主机)</span><span></span><span>容器端口</span></div>';
    for (const p of portDefs){
      if (!(p.key in state.ports)) state.ports[p.key] = p.default;
      html += '<div class="map-row">'+
        '<span class="mlabel" title="'+esc(trLabel(p.label))+'">'+esc(trLabel(p.label))+'</span>'+
        '<input type="text" data-port="'+p.key+'" value="'+state.ports[p.key]+'">'+
        '<span class="arrow">→</span>'+
        '<span class="ctn" title="容器内端口">'+(p.container != null ? p.container : '?')+'</span></div>';
    }
    // 自定义映射
    const rows = state.extra[s] = state.extra[s] || [];
    rows.forEach((r, i) => {
      html += '<div class="map-row extra">'+
        '<span class="mlabel">自定义映射</span>'+
        '<input type="text" data-eh="'+s+':'+i+':host" value="'+esc(r.host)+'">'+
        '<span class="arrow">→</span>'+
        '<input type="text" data-eh="'+s+':'+i+':ctn" value="'+esc(r.container)+'">'+
        '<button type="button" class="delrow" data-edel="'+s+':'+i+'">×</button></div>';
    });
    html += '</div><button type="button" class="addport" title="添加自定义端口" aria-label="添加自定义端口" data-eadd="'+s+'">+</button></div>';
    pb.insertAdjacentHTML('beforeend', html);
  }
  // ---- ProxySQL 读写分离端口 ----
  if (state.features.proxysql){
    const pxDefs = [
      {key:'proxysql', label:'读写分离入口 (数据面)', default:16033, container:6033},
      {key:'proxysql_admin', label:'ProxySQL 管理面', default:16032, container:6032}];
    let ph = '<div class="grp"><h3>读写分离 (ProxySQL) <span class="hint">运行在主部署机</span></h3><div class="map">'+
      '<div class="map-cols"><span></span><span>外部访问端口(宿主机)</span><span></span><span>容器端口</span></div>';
    for (const p of pxDefs){
      if (!(p.key in state.ports)) state.ports[p.key] = p.default;
      ph += '<div class="map-row">'+
        '<span class="mlabel" title="'+esc(trLabel(p.label))+'">'+esc(trLabel(p.label))+'</span>'+
        '<input type="text" data-port="'+p.key+'" value="'+state.ports[p.key]+'">'+
        '<span class="arrow">→</span>'+
        '<span class="ctn">'+p.container+'</span></div>';
    }
    ph += '</div><div class="hint">应用统一连接 入口端口: 写自动路由到主库, SELECT 自动分流到从库; 管理面端口 (admin/admin) 供运维查看路由规则</div></div>';
    pb.insertAdjacentHTML('beforeend', ph);
  }
  pb.querySelectorAll('input[data-port]').forEach(i => i.oninput = () => { state.ports[i.dataset.port] = i.value; validatePorts(); saveLocal(); });
  const syncExtra = (sel) => {
    const [svc, idx, field] = sel.split(':');
    state.extra[svc][+idx][field === 'host' ? 'host' : 'container'] = pb.querySelector('[data-eh="'+sel+'"]').value;
    validatePorts(); saveLocal();
  };
  pb.querySelectorAll('input[data-eh]').forEach(i => i.oninput = () => syncExtra(i.dataset.eh));
  pb.querySelectorAll('[data-edel]').forEach(b => b.onclick = () => {
    const [svc, idx] = b.dataset.edel.split(':');
    state.extra[svc].splice(+idx, 1); renderDynamic();
  });
  pb.querySelectorAll('[data-eadd]').forEach(b => b.onclick = () => {
    const svc = b.dataset.eadd;
    state.extra[svc] = state.extra[svc] || [];
    state.extra[svc].push({host: '', container: ''});
    renderDynamic();
  });

  renderPwds();
  renderProxy();
  renderDb();
  renderBackup();
  validatePorts();
  updateStat();
}

/* ---- Nginx 反向代理向导 ---- */
function pxDefaultSite(){
  return { mode:'static', server_name:'', listen:80, root:'/usr/share/nginx/html/app',
           spa:true, api_prefix:'/api/', api_target:'', strip:true,
           ws:false, ws_path:'/ws/', body_size:'500m', realip:false,
           trusted:'10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16',
           ssl:false, redirect:true, cert_name:'', key_name:'', cert_pem:'', key_pem:'' };
}
function pxListenOptions(cur){
  let opts = CATALOG.services.nginx.ports.map(p => ({v: p.container, t: p.container + ' · 默认映射'}));
  (state.extra.nginx || []).forEach(r => {
    const c = parseInt(r.container, 10);
    if (c && !opts.some(o => +o.v === c)) opts.push({v: c, t: c + ' · 自定义映射'});
  });
  const curN = parseInt(cur, 10);
  if (curN && !opts.some(o => +o.v === curN)) opts.push({v: curN, t: curN + ' · 未映射!'});
  return opts;
}
function pxTargetOptions(){
  let opts = '';
  for (const s of selected){
    const m = CATALOG.services[s];
    const cn = s === 'xxljob' ? 'xxl-job' : s;
    for (const p of (m.ports||[])) opts += '<option value="http://'+cn+':'+(p.container != null ? p.container : '')+'">';
  }
  return opts;
}
/* 前缀改写示例: 随 输入实时更新 */
function pxExample(i){
  const p = state.proxies[i] || {};
  const el = document.querySelector('[data-pxex="'+i+'"]');
  if (!el) return;
  const pre = String(p.api_prefix || '').trim();
  if (p.mode === 'proxy' || !pre || pre === '/' || !p.api_target){ el.style.display = 'none'; return; }
  el.style.display = '';
  el.textContent = p.strip
    ? '示例: 浏览器请求 ' + pre + 'user/1?id=3   →   后端实际收到 /user/1?id=3'
    : '示例: 浏览器请求 ' + pre + 'user/1?id=3   →   后端实际收到 ' + pre + 'user/1?id=3';
}
function renderProxy(){
  const box = $('#proxyBox');
  if (!selected.has('nginx')){ box.innerHTML = ''; return; }
  let html = '';
  state.proxies.forEach((p, i) => {
    html += '<div class="dbbox" data-px="'+i+'">'+
      '<div class="dbhead"><span class="pxidx">'+(i+1)+'</span>'+
      '<input type="text" style="max-width:240px" placeholder="域名, 多个空格分隔; _ 为默认站点" data-pxf="'+i+':server_name" value="'+esc(p.server_name)+'">'+
      '<div class="segs">'+
      '<label><input type="radio" name="pxm_'+i+'" value="static" '+(p.mode!=='proxy'?'checked':'')+'>静态+接口</label>'+
      '<label><input type="radio" name="pxm_'+i+'" value="proxy" '+(p.mode==='proxy'?'checked':'')+'>整站反代</label>'+
      '</div><span style="flex:1"></span>'+
      '<div style="display:flex;align-items:center;gap:8px"><span class="hint">监听(容器端口)</span>'+
      '<select data-pxf="'+i+':listen" style="width:auto;max-width:160px">';
    for (const o of pxListenOptions(p.listen)) html += '<option value="'+o.v+'" '+(+p.listen===+o.v?'selected':'')+'>'+esc(o.t)+'</option>';
    html += '</select></div>'+
      '<button type="button" class="delrow" style="width:32px;height:32px;flex:none" title="删除站点" data-pxdel="'+i+'">×</button>'+
      '</div><div class="dbform" style="margin-top:11px">';
    if (p.mode !== 'proxy'){
      html += '<div class="row" style="gap:12px">'+
        '<div style="flex:2.4;min-width:240px"><label>前端目录 <span class="hint">容器内绝对路径, html 挂载于 /usr/share/nginx/html</span></label><input type="text" data-pxf="'+i+':root" value="'+esc(p.root)+'" placeholder="留空则不托管静态页"></div>'+
        '<div style="max-width:250px"><label>单页应用(SPA)</label><label style="margin:0;font-weight:500;cursor:pointer"><input type="checkbox" data-pxc="'+i+':spa" '+(p.spa?'checked':'')+'> 刷新子路径时回退 index.html</label></div>'+
        '</div>';
      html += '<div class="row" style="gap:12px;margin-top:10px;align-items:end">'+
        '<div style="max-width:150px"><label>接口前缀</label><input type="text" data-pxf="'+i+':api_prefix" value="'+esc(p.api_prefix)+'" placeholder="留空不代理"></div>'+
        '<div style="flex:2;min-width:220px"><label>后端地址 <span class="hint">http://服务名:容器端口</span></label><input type="text" list="pxTargets" data-pxf="'+i+':api_target" value="'+esc(p.api_target)+'" placeholder="http://xxl-job:8080"></div>'+
        '<div style="max-width:200px"><label>转发时去掉前缀</label><label style="margin:0;font-weight:500;cursor:pointer"><input type="checkbox" data-pxc="'+i+':strip" '+(p.strip?'checked':'')+'> 后端收到的路径不含前缀</label></div>'+
        '</div><div class="pxexample" data-pxex="'+i+'" style="display:none"></div>';
    } else {
      html += '<div class="row" style="gap:12px;align-items:end">'+
        '<div style="flex:2;min-width:260px"><label>后端地址 <span class="hint">整站所有路径转发到该服务</span></label><input type="text" list="pxTargets" data-pxf="'+i+':api_target" value="'+esc(p.api_target)+'" placeholder="http://nacos:8848"></div>'+
        '</div>';
    }
    html += '<div class="row" style="gap:12px;margin-top:10px;align-items:end">'+
      '<div style="display:flex;gap:10px;align-items:end"><div><label>WebSocket</label>'+
      '<label style="margin:0;font-weight:500;cursor:pointer;white-space:nowrap"><input type="checkbox" data-pxc="'+i+':ws" '+(p.ws?'checked':'')+'> 启用</label></div>'+
      '<input type="text" style="width:120px" data-pxf="'+i+':ws_path" value="'+esc(p.ws_path)+'"'+(p.ws?'':' disabled')+'></div>'+
      '<div style="max-width:130px"><label>上传限制</label><input type="text" data-pxf="'+i+':body_size" value="'+esc(p.body_size)+'"></div>'+
      '<div><label>HTTPS 证书</label><label style="margin:0;font-weight:500;cursor:pointer;white-space:nowrap"><input type="checkbox" data-pxc="'+i+':ssl" '+(p.ssl?'checked':'')+'> 上传证书, 部署即配好 SSL</label></div>'+
      '<div style="flex:1"></div>'+
      '<div><label>真实IP还原</label><label style="margin:0;font-weight:500;cursor:pointer;white-space:nowrap"><input type="checkbox" data-pxc="'+i+':realip" '+(p.realip?'checked':'')+'> 前面有 CDN/负载均衡时勾选</label></div>'+
      '</div>';
    if (p.ssl){
      html += '<div class="pxsub"><div class="row" style="gap:14px;align-items:end">'+
        '<div style="flex:1;min-width:230px"><label>证书文件 <span class="hint">fullchain.pem / .crt(含中间证书)</span></label>'+
        '<input type="file" accept=".pem,.crt,.cer" data-pxfile="'+i+':cert"></div>'+
        '<div style="flex:1;min-width:230px"><label>私钥文件 <span class="hint">.key(暂不支持加密私钥)</span></label>'+
        '<input type="file" accept=".key,.pem" data-pxfile="'+i+':key"></div>'+
        '<div style="max-width:190px"><label>HTTP 跳转</label><label style="margin:0;font-weight:500;cursor:pointer;white-space:nowrap"><input type="checkbox" data-pxc="'+i+':redirect" '+(p.redirect?'checked':'')+'> 80 端口 301 到 https</label></div>'+
        '</div><div class="hint" style="margin-top:8px" data-pxssl="'+i+'">'+pxSslStatus(p)+'</div></div>';
    }
    if (p.realip){
      html += '<div class="pxsub"><label>可信代理网段 <span class="hint">CIDR, 逗号或换行分隔; 只有这些来源的 XFF 头会被采信, 还原出的 IP 写入 $remote_addr 与日志</span></label>'+
        '<textarea rows="2" data-pxf="'+i+':trusted">'+esc(p.trusted)+'</textarea></div>';
    }
    html += '</div></div>';
  });
  html += '<datalist id="pxTargets">'+pxTargetOptions()+'</datalist>';
  html += '<button type="button" class="addport wide" data-pxadd>+ 添加站点</button>';
  html += '<div class="hint" style="margin-top:10px">生成的配置自动包含代理优化: upstream 长连接复用(keepalive) · 连接/读写超时 · 响应缓冲 · X-Real-IP / X-Forwarded-* 透传 · WebSocket 升级; gzip 与安全响应头已在 nginx.conf 全局开启。证书随包分发到 nginx/ssl/, 挂载于容器 /etc/nginx/ssl。</div>';
  box.innerHTML = html;
  state.proxies.forEach((_, i) => pxExample(i));

  const addSite = () => { state.proxies.push(pxDefaultSite()); saveLocal(); renderProxy(); };
  box.querySelectorAll('[data-pxadd]').forEach(b => b.onclick = addSite);
  box.querySelectorAll('[data-pxdel]').forEach(b => b.onclick = () => {
    state.proxies.splice(+b.dataset.pxdel, 1); saveLocal(); renderProxy();
  });
  box.querySelectorAll('input[name^=pxm]').forEach(r => r.onchange = () => {
    state.proxies[+r.name.slice(4)].mode = r.value; saveLocal(); renderProxy();
  });
  box.querySelectorAll('[data-pxf]').forEach(n => {
    const [i, f] = n.dataset.pxf.split(':');
    const apply = () => {
      let v = n.value;
      if (f === 'listen') v = parseInt(v, 10) || 80;
      state.proxies[+i][f] = v;
      if (f === 'api_prefix' || f === 'api_target') pxExample(+i);
      saveLocal();
    };
    n.oninput = apply;
    if (n.tagName === 'SELECT' || n.tagName === 'TEXTAREA') n.onchange = apply;
  });
  box.querySelectorAll('[data-pxc]').forEach(c => {
    const [i, f] = c.dataset.pxc.split(':');
    c.onchange = () => {
      state.proxies[+i][f] = c.checked;
      if (f === 'ws'){
        const w = box.querySelector('[data-pxf="'+i+':ws_path"]'); if (w) w.disabled = !c.checked;
      }
      if (f === 'api_prefix' || f === 'strip') pxExample(+i);
      saveLocal();
      if (f === 'realip' || f === 'ssl') renderProxy();   // 子面板按需出现
    };
  });
  // 证书文件读取(文本 PEM, 存进配置随包分发)
  box.querySelectorAll('[data-pxfile]').forEach(inp => inp.onchange = () => {
    const [i, kind] = inp.dataset.pxfile.split(':');
    const f = inp.files && inp.files[0];
    if (!f) return;
    if (f.size > 200 * 1024){ toast('证书文件过大(>200KB), 请确认选对了文件'); inp.value = ''; return; }
    const rd = new FileReader();
    rd.onload = () => {
      const p = state.proxies[+i];
      if (kind === 'cert'){ p.cert_pem = String(rd.result); p.cert_name = f.name; }
      else { p.key_pem = String(rd.result); p.key_name = f.name; }
      const st = box.querySelector('[data-pxssl="'+i+'"]');
      if (st) st.textContent = pxSslStatus(p);
      saveLocal();
      toast('已读取 ' + f.name);
    };
    rd.readAsText(f);
  });
}
function pxSslStatus(p){
  const c = p.cert_pem ? '证书 ' + (p.cert_name || '已加载') + ' ✓' : '证书未选择';
  const k = p.key_pem ? '私钥 ' + (p.key_name || '已加载') + ' ✓' : '私钥未选择';
  return '已加载: ' + c + ' · ' + k;
}

/* ---- 数据库备份策略(每种数据库独立 开关/星期/时间/保留份数) ---- */
const BK_ENGINES = {
  mysql: { name: 'MySQL', svcs: ['mysql57', 'mysql8'],
           hint: 'MySQL: mysqldump --single-transaction, 排除系统库, 每库一个 .sql.gz|MySQL: mysqldump --single-transaction, system DBs excluded, one .sql.gz per DB' },
  pg:    { name: 'PostgreSQL', svcs: ['postgres'],
           hint: 'PostgreSQL: pg_dump 按库导出, 每库一个 .sql.gz|PostgreSQL: pg_dump, one .sql.gz per DB' },
  mongo: { name: 'MongoDB', svcs: ['mongodb'],
           hint: 'MongoDB: mongodump --archive --gzip, 每库一个 .archive|MongoDB: mongodump --archive --gzip, one .archive per DB' }
};
// 兼容旧版单一计划({enabled,days,hour,keep,dir}) -> 新版 {dir, engines:{mysql,pg,mongo}}
function normBackup(sb){
  const b = sb || {};
  const leg = Array.isArray(b.days)
    ? { enabled: b.enabled !== false, days: b.days.map(Number), hour: b.hour != null ? +b.hour : 3, keep: b.keep || 7 }
    : null;
  const src = b.engines || {};
  const engines = {};
  for (const k of Object.keys(BK_ENGINES)){
    const e = src[k] || {};
    engines[k] = {
      enabled: e.enabled != null ? !!e.enabled : (leg ? leg.enabled : true),
      days: Array.isArray(e.days) ? e.days.map(Number) : (leg ? leg.days.slice() : [1,2,3,4,5,6,7]),
      hour: e.hour != null ? +e.hour : (leg ? leg.hour : 3),
      keep: e.keep || (leg ? leg.keep : 7)
    };
  }
  return { dir: b.dir || '/data/backup/db', engines: engines };
}
function renderBackup(){
  const box = $('#backupBox');
  state.backup = normBackup(state.backup);
  const b = state.backup;
  const sep = LANG === 'en' ? '; ' : '；';
  const active = Object.keys(BK_ENGINES).filter(k => BK_ENGINES[k].svcs.some(s => selected.has(s)));
  let html = '<div class="map-row" style="grid-template-columns:150px 1fr">'+
    '<span class="mlabel">'+trLabel('备份目录|Backup dir')+'</span><div><input type="text" id="bkDir" value="'+esc(b.dir)+'">'+
    '<div class="hint" style="margin-top:4px">'+trLabel('备份根目录, 每种数据库各自存子目录|Backup root; each DB engine stores its own sub-directory')+'</div></div></div>';
  for (const k of active){
    const e = b.engines[k];
    html += '<div class="dbbox" style="margin-top:13px"><div class="dbhead"><b>'+BK_ENGINES[k].name+'</b>'+
      '<label style="font-weight:500;cursor:pointer;margin-left:auto"><input type="checkbox" data-bken="'+k+'" '+(e.enabled?'checked':'')+'> '+trLabel('启用该库备份|Enable backup')+'</label></div>'+
      '<div data-bkdet="'+k+'" style="margin-top:10px;'+(e.enabled?'':'opacity:.45;pointer-events:none')+'">';
    html += '<label>'+trLabel('备份日|Backup days')+' <span class="hint">'+trLabel('可多选|multi-select')+'</span></label>'+
      '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap"><div class="segs">';
    for (let d=1; d<=7; d++) html += '<label><input type="checkbox" data-bkd="'+k+':'+d+'" '+(e.days.includes(d)?'checked':'')+'>'+WEEK[d]+'</label>';
    html += '</div>'+
      '<button type="button" class="mini" data-bkq="'+k+':every">'+trLabel('每天|Every day')+'</button>'+
      '<button type="button" class="mini" data-bkq="'+k+':week">'+trLabel('工作日|Weekdays')+'</button></div>';
    html += '<div class="row" style="margin-top:12px">'+
      '<div style="max-width:150px"><label>'+trLabel('备份时间|Backup time')+'</label><select data-bkhour="'+k+'">';
    for (let h=0; h<24; h++) html += '<option value="'+h+'" '+(e.hour===h?'selected':'')+'>'+String(h).padStart(2,'0')+':00</option>';
    html += '</select></div>'+
      '<div style="max-width:170px"><label>'+trLabel('每库保留份数|Copies kept per DB')+'</label><input type="text" data-bkkeep="'+k+'" value="'+esc(e.keep)+'"></div></div>';
    html += '</div></div>';
  }
  html += '<div class="hint" style="margin-top:10px">' + active.map(k => trLabel(BK_ENGINES[k].hint)).join(sep) + sep
        + trLabel('超出保留份数自动轮转删除|oldest files rotated out beyond retention') + '</div>';
  box.innerHTML = html;

  const dir = box.querySelector('#bkDir'); if (dir) dir.oninput = () => { b.dir = dir.value; saveLocal(); };
  for (const k of active){
    const e = b.engines[k];
    box.querySelector('[data-bken="'+k+'"]').onchange = ev => {
      e.enabled = ev.target.checked; saveLocal(); renderBackup();
    };
    box.querySelectorAll('[data-bkd^="'+k+':"]').forEach(c => c.onchange = () => {
      const d = +c.dataset.bkd.split(':')[1];
      if (c.checked){ if (!e.days.includes(d)) e.days.push(d); }
      else e.days = e.days.filter(x => x !== d);
      saveLocal();
    });
    box.querySelectorAll('[data-bkq^="'+k+':"]').forEach(btn => btn.onclick = () => {
      const mode = btn.dataset.bkq.split(':')[1];
      e.days = mode === 'every' ? [1,2,3,4,5,6,7] : [1,2,3,4,5];
      saveLocal(); renderBackup();
      toast(mode === 'every' ? trLabel('已选每天备份|Daily backup selected') : trLabel('已选工作日备份|Weekday backup selected'));
    });
    const h = box.querySelector('[data-bkhour="'+k+'"]'); if (h) h.onchange = () => { e.hour = +h.value; saveLocal(); };
    const kp = box.querySelector('[data-bkkeep="'+k+'"]'); if (kp) kp.oninput = () => { e.keep = kp.value; saveLocal(); };
  }
}

function renderPwds(){
  const wb = $('#pwdBox'); wb.innerHTML = '';
  const done = new Set();
  for (const item of CATALOG.secrets){
    if (!item.services.some(s => selected.has(s)) || done.has(item.key)) continue;
    done.add(item.key);
    if (!(item.key in state.secrets)) state.secrets[item.key] = item.default;
    const isToken = item.type === 'nacos_token';
    const isUser  = item.secret === false;
    const isPwd   = !isUser && !isToken;   // 真正的密码: 才有 显示/随机/强度
    const type = isPwd ? 'password' : 'text';
    const showBtn = isPwd ? '<button type="button" class="mini" data-show="'+item.key+'">显示</button>' : '';
    const genBtn  = isUser ? '' : '<button type="button" class="mini" data-gen="'+item.key+'" data-token="'+(isToken?1:0)+'">随机</button>';
    const st = isPwd ? '<span class="strength s'+pwStrength(state.secrets[item.key])+'" data-str="'+item.key+'"><i></i><i></i><i></i><i></i><em>'+strLabel(pwStrength(state.secrets[item.key]))+'</em></span>' : '<span></span>';
    let html = '<div class="grp"><h3>'+esc(trLabel(item.label))+'</h3><div class="pwline">'+
      '<div class="pwrow">'+
      '<input type="'+type+'" data-sec="'+item.key+'" value="'+esc(state.secrets[item.key])+'"'+(isUser?' autocomplete="off" spellcheck="false"':'')+'>'+
      showBtn + genBtn +
      '</div>'+
      st + '</div>';
    html += '</div>';
    wb.insertAdjacentHTML('beforeend', html);
  }
  const hints = [];
  if (selected.has('xxljob')) hints.push('* XXL-Job 控制台初始账号 admin, 初始密码即上面登记的 XXL-Job admin 密码(打包时按该值初始化 SQL), 登录后请修改');
  if (selected.has('nacos'))  hints.push('* Nacos 控制台密码为初始登记值, 部署后请登录控制台改为自己要用的密码; .env 中该值仅作登记');
  if (hints.length) wb.insertAdjacentHTML('beforeend', '<div class="hint" style="margin-top:10px">'+hints.join('<br>')+'</div>');
  wb.querySelectorAll('input').forEach(i => i.oninput = () => {
    state.secrets[i.dataset.sec] = i.value;
    if (i.type === 'password' || document.querySelector('.strength[data-str="'+i.dataset.sec+'"]')) syncStrength(i.dataset.sec);
    saveLocal();
  });
  wb.querySelectorAll('[data-show]').forEach(b => b.onclick = () => {
    const inp = wb.querySelector('input[data-sec='+b.dataset.show+']');
    const show = inp.type === 'password';
    inp.type = show ? 'text' : 'password'; b.textContent = show ? '隐藏' : '显示';
  });
  wb.querySelectorAll('[data-gen]').forEach(b => b.onclick = () => {
    const v = b.dataset.token === '1' ? randToken() : randPwd(16);
    state.secrets[b.dataset.gen] = v;
    const inp = wb.querySelector('input[data-sec='+b.dataset.gen+']');
    inp.value = v; inp.type = 'text';
    const sb = wb.querySelector('[data-show='+b.dataset.gen+']'); if (sb) sb.textContent = '隐藏';
    syncStrength(b.dataset.gen);
    saveLocal();
  });
}
function strLabel(s){
  return ['','弱','一般','良好','强'][s] || '';
}
function syncStrength(key){
  const bar = document.querySelector('.strength[data-str="'+key+'"]');
  if (!bar) return;
  const s = pwStrength(state.secrets[key] || '');
  bar.className = 'strength s' + s;
  bar.querySelector('em').textContent = strLabel(s);
}

/* ---- 数据库 ---- */
function renderDb(){
  const box = $('#dbBox'); box.innerHTML = '';
  const localMysqls = ['mysql57','mysql8'].filter(s => selected.has(s));
  for (const app of ['nacos','xxljob']){
    if (!selected.has(app)) continue;
    const schemaDefault = app === 'nacos' ? 'nacos' : 'xxl_job';
    const conf = state.db[app] || {};
    conf.mode = conf.mode || (localMysqls.length ? 'local' : 'external');
    if (conf.mode === 'local' && !localMysqls.length){
      conf.mode = 'external';
      toast(trLabel(CATALOG.services[app].label) + (LANG === 'en' ? ': MySQL not selected, switched to external database' : ': 本次未部署 MySQL, 已改为使用外部数据库'));
    }
    conf.local_svc = conf.local_svc || (localMysqls.includes('mysql8') ? 'mysql8' : (localMysqls[0] || ''));
    conf.schema = conf.schema || schemaDefault;
    conf.host = conf.host || '127.0.0.1'; conf.port = conf.port || 3306;
    conf.user = conf.user || 'root'; conf.password = conf.password || '';
    state.db[app] = conf;

    let html = '<div class="dbbox" data-app="'+app+'"><div class="dbhead"><b>'+esc(CATALOG.services[app].label)+'</b>'+
      '<div class="segs">'+
      '<label class="'+(localMysqls.length?'':'dis')+'"><input type="radio" name="dbm_'+app+'" value="local" '+(conf.mode==='local'?'checked':'')+(localMysqls.length?'':' disabled')+'>使用本次部署的 MySQL</label>'+
      '<label><input type="radio" name="dbm_'+app+'" value="external" '+(conf.mode==='external'?'checked':'')+'>使用外部数据库</label>'+
      '</div></div><div class="dbform">';
    if (conf.mode === 'local'){
      html += '<div class="row">';
      if (localMysqls.length > 1){
        html += '<div><label>使用哪个 MySQL</label><select data-dblsvc="'+app+'">';
        for (const m of localMysqls) html += '<option value="'+m+'" '+(conf.local_svc===m?'selected':'')+'>'+esc(CATALOG.services[m].label)+'</option>';
        html += '</select></div>';
      } else {
        html += '<div><label>MySQL 服务</label><input type="text" value="'+esc(CATALOG.services[localMysqls[0]].label)+'" disabled></div>';
      }
      html += '<div><label>库名</label><input type="text" data-dbschema="'+app+'" value="'+esc(conf.schema)+'"></div>'+
              '<div><label>连接账号</label><input type="text" value="root (自动使用 root 密码)" disabled></div>'+
              '</div><div class="hint" style="margin-top:8px">建库建表 SQL 会在首次启动时自动导入, 幂等可重跑</div>';
    } else {
      html += '<div class="row">'+
        '<div style="flex:2.2"><label>数据库地址 <span class="hint">127.0.0.1 自动改写 host-gateway</span></label><input type="text" data-dbhost="'+app+'" value="'+esc(conf.host)+'"></div>'+
        '<div style="max-width:120px;min-width:100px"><label>端口</label><input type="text" data-dbport="'+app+'" value="'+conf.port+'"></div>'+
        '</div><div class="row" style="margin-top:12px">'+
        '<div><label>库名</label><input type="text" data-dbschema="'+app+'" value="'+esc(conf.schema)+'"></div>'+
        '<div><label>账号</label><input type="text" data-dbuser="'+app+'" value="'+esc(conf.user)+'"></div>'+
        '<div><label>密码</label><input type="password" data-dbpass="'+app+'" value="'+esc(conf.password)+'"></div>'+
        '</div><div class="hint" style="margin-top:8px">部署时先测连再建库建表(已存在则跳过)</div>';
    }
    html += '</div></div>';
    box.insertAdjacentHTML('beforeend', html);
  }
  for (const app of Object.keys(state.db).filter(a => state.db[a] && selected.has(a))){
    const el = document.querySelector('.dbbox[data-app="'+app+'"]'); if (!el) continue;
    const conf = state.db[app];
    el.querySelectorAll('input[name=dbm_'+app+']').forEach(r => r.onchange = () => {
      conf.mode = r.value; saveLocal(); renderDb();
    });
    const sel = el.querySelector('[data-dblsvc]'); if (sel) sel.onchange = () => { conf.local_svc = sel.value; saveLocal(); };
    const bind = (attr, key) => { const n = el.querySelector(attr); if (n) n.oninput = () => { conf[key] = n.value; saveLocal(); }; };
    bind('[data-dbhost]','host'); bind('[data-dbport]','port'); bind('[data-dbschema]','schema');
    bind('[data-dbuser]','user'); bind('[data-dbpass]','password');
  }
}

/* ---- 端口冲突实时校验(仅宿主机端口查重; 容器端口只做合法性) ---- */
function validatePorts(){
  const seen = {};   // 宿主机端口 -> 代表元素
  const mark = (el, ok) => { el.classList.toggle('bad', !ok); el.title = ok ? '' : '端口不合法或冲突'; };
  const reg = (el, raw, isHost) => {
    const n = parseInt(raw, 10);
    const valid = /^\d+$/.test(String(raw).trim()) && n >= 1 && n <= 65535;
    if (!valid){ mark(el, false); return; }
    if (isHost){
      if (seen[n]){ mark(el, false); if (seen[n] !== el) mark(seen[n], false); }
      else { seen[n] = el; mark(el, true); }
    } else mark(el, true);
  };
  $$('#portBox input[data-port]').forEach(el => reg(el, state.ports[el.dataset.port], true));
  $$('#portBox input[data-eh]').forEach(el => {
    const [svc, idx, field] = el.dataset.eh.split(':');
    reg(el, state.extra[svc][+idx][field === 'host' ? 'host' : 'container'], field === 'host');
  });
}

/* ---- 底部统计 ---- */
function updateStat(){
  const svcs = [...selected];
  const clOn = state.cluster && state.cluster.enabled;
  if (!svcs.length && !clOn){ $('#statLine').innerHTML = '未选择组件'; $('#btnPack').disabled = $('#btnPreview').disabled = true; return; }
  $('#btnPack').disabled = $('#btnPreview').disabled = false;
  const arch = $('#arch').value;
  let mb = (CATALOG.sizes.docker[arch] || 0) + (CATALOG.sizes.compose[arch] || 0);
  for (const s of svcs) mb += (CATALOG.sizes.images[s] || {})[arch] || 0;
  const parts = [];
  if (svcs.length) parts.push('中间件 <b>'+svcs.length+'</b> 项');
  if (clOn) parts.push('K8s 集群 <b>'+state.cluster.kube_version+'</b>');
  if (clOn) mb += (CATALOG.sizes.cluster_k8s || {})[arch] || 0;
  $('#statLine').innerHTML = '已选 '+parts.join(' + ')+' · 目标 '+
    (arch==='amd64'?'x86_64':'ARM')+' · 原始物料 <b>'+fmtGB(mb)+'</b> · 打包后约为其 35%~45%';
}

/* ================= 配置 导入/导出/恢复 ================= */
function buildConfig(){
  return {
    project: $('#project').value.trim(),
    arch: $('#arch').value,
    lang: LANG,
    services: [...selected],
    ports: {...state.ports},
    secrets: {...state.secrets},
    deploy_dir: $('#deploy_dir').value.trim(),
    docker_data_root: $('#data_root').value.trim(),
    registry_mirrors: $('#mirrors').value.split('\n').map(s=>s.trim()).filter(Boolean),
    extra_ports: JSON.parse(JSON.stringify(state.extra)),
    backup: JSON.parse(JSON.stringify(normBackup(state.backup))),
    db: JSON.parse(JSON.stringify(state.db)),
    topology: JSON.parse(JSON.stringify(state.topology || {})),
    servers: JSON.parse(JSON.stringify(state.servers || [])),
    multihost: (() => {
      const out = {};
      for (const k of Object.keys(state.mh || {})){
        const m = state.mh[k] || {};
        if (!m.enabled) continue;
        if (k === 'kafka') out.kafka = { enabled:true, count: m.count || 3, brokers: m.brokers || [] };
        else out[k] = { enabled:true, master: m.master, replicas: m.replicas || [] };
      }
      return out;
    })(),
    replicas: JSON.parse(JSON.stringify(state.replicas || {})),
    features: JSON.parse(JSON.stringify(state.features || {})),
    cluster: JSON.parse(JSON.stringify(state.cluster || {enabled:false})),
    proxies: state.proxies.map(p => {
      const o = JSON.parse(JSON.stringify(p));
      o.trusted_proxies = p.realip ? String(p.trusted||'').split(/[\s,]+/).filter(Boolean) : [];
      return o;
    }),
  };
}
function applyConfig(cfg){
  $('#project').value = cfg.project || 'demo';
  $('#arch').value = cfg.arch === 'arm64' ? 'arm64' : 'amd64';
  $('#deploy_dir').value = cfg.deploy_dir || '/data/middleware';
  $('#data_root').value = cfg.docker_data_root || '/data/docker';
  $('#mirrors').value = ((cfg.registry_mirrors && cfg.registry_mirrors.length) ? cfg.registry_mirrors
    : (CATALOG.defaults.registry_mirrors || [])).join('\n');
  selected.clear(); (cfg.services || []).forEach(s => selected.add(s));
  Object.keys(state.ports).forEach(k => delete state.ports[k]); Object.assign(state.ports, cfg.ports || {});
  Object.keys(state.secrets).forEach(k => delete state.secrets[k]); Object.assign(state.secrets, cfg.secrets || {});
  Object.keys(state.db).forEach(k => delete state.db[k]); Object.assign(state.db, cfg.db || {});
  Object.keys(state.extra).forEach(k => delete state.extra[k]);
  const xp = cfg.extra_ports || {};
  for (const k of Object.keys(xp)) state.extra[k] = xp[k];
  state.topology = Object.assign({ mysql8: 'single', mysql57: 'single', redis: 'single', kafka: 'single' }, cfg.topology || {});
  state.servers = (Array.isArray(cfg.servers) ? cfg.servers : []).map(v =>
    Object.assign({ name: '', user: 'root', ip: '', ssh: 22, pass: '' }, v || {}));
  const mhIn = cfg.multihost || {};
  state.mh = {
    kafka: Object.assign({ enabled:false, count:3, brokers:[null,null,null] }, mhIn.kafka || {}),
    mysql8: Object.assign({ enabled:false, master:null, replicas:[null] }, mhIn.mysql8 || {}),
    mysql57: Object.assign({ enabled:false, master:null, replicas:[null] }, mhIn.mysql57 || {}),
    redis: Object.assign({ enabled:false, master:null, replicas:[null,null] }, mhIn.redis || {}),
  };
  state.replicas = Object.assign({ mysql8: 1, mysql57: 1 }, cfg.replicas || {});
  state.features = Object.assign({ proxysql: false }, cfg.features || {});
  state.cluster = Object.assign({ enabled: false, kube_version: '', mode: 'cache', cni_type: 'calico',
    proxy_mode: 'iptables', pod_cidr: '10.233.64.0/18', service_cidr: '10.233.0.0/18',
    timezone: 'Asia/Shanghai', os_distros: [], roles: {}, ha_type: 'local', ha_vip: '', upgrade_to: '',
    k8s_image_registry: '', container_manager: 'containerd', ipv4_mask_size: 24,
    multi_cni: 'none', multi_cni_tag: '', calico_values: '',
    dns: { coredns_tag: '', nodelocaldns_enabled: true, nodelocaldns_tag: '' },
    certs_renew_cron: '', kubeadm_config_dir: '/etc/kubekey/backup/kubernetes' }, cfg.cluster || {});
  state.cluster.dns = Object.assign({ coredns_tag: '', nodelocaldns_enabled: true, nodelocaldns_tag: '' },
    (state.cluster.dns) || {});
  // artifact 模式已下线(界面只留 纯离线/在线), 旧配置归一为纯离线, 避免页面勾选与实际打包不符
  if (state.cluster.mode === 'artifact') state.cluster.mode = 'cache';
  const bd = cfg.backup || {};
  state.backup = bd;   // normBackup() 在 renderBackup 时统一归一化(兼容旧版单一计划)
  state.proxies = (cfg.proxies || []).map(p => Object.assign(pxDefaultSite(), p, {
    trusted: Array.isArray(p.trusted_proxies) ? p.trusted_proxies.join(', ') : (p.trusted || '')
  }));
  if ($('#arch').value === 'arm64') selected.delete('mysql57');
  renderSvcGridSync();
  // 集群卡片与角色分配区跟随恢复的配置重渲染(renderSvcGridSync 不覆盖这两块)
  renderClusterCard(); renderTopology(); renderPool(); renderInfraTopology();
}
function restoreLocal(){
  try {
    const raw = localStorage.getItem('packer-cfg-v1');
    if (raw){ applyConfig(JSON.parse(raw)); toast('已恢复上次填写的配置'); return; }
  } catch(e){}
  // 首次使用: 默认勾选常用组合(除 MySQL 5.7) + 实测可用的加速器
  for (const k of Object.keys(CATALOG.services)){
    if (k !== 'mysql57'){ selected.add(k); document.querySelector('.svc[data-key="'+k+'"]').classList.add('on'); }
  }
  $('#mirrors').value = (CATALOG.defaults.registry_mirrors || []).join('\n');
  renderDynamic();
}
function doExport(){
  const blob = new Blob([JSON.stringify(buildConfig(), null, 2)], {type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = ($('#project').value.trim() || 'project') + '-packer.json';
  a.click(); URL.revokeObjectURL(a.href);
  toast('配置已导出, 可用 python packer.py --config 直接打包');
}
function doImport(e){
  const f = e.target.files[0]; if (!f) return;
  const rd = new FileReader();
  rd.onload = () => {
    try { applyConfig(JSON.parse(rd.result)); toast('配置已导入'); }
    catch(err){ msg('导入失败: ' + err.message, 'err'); }
  };
  rd.readAsText(f); e.target.value = '';
}

/* ================= 预览 / 打包 ================= */
function switchTab(t){
  curTab = t;
  $$('#previewCard .tabs button[data-t]').forEach(b => b.classList.toggle('on', b.dataset.t === t));
  $('#previewPre').textContent = previewData[t] || '';
}
async function doCopy(){
  try { await navigator.clipboard.writeText(previewData[curTab] || ''); toast('已复制到剪贴板'); }
  catch(e){ toast('复制失败, 请手动选择复制'); }
}
async function doPreview(){
  msg(''); $('#btnPreview').disabled = true;
  try {
    const d = await api('/api/preview', buildConfig());
    previewData = d;
    if (d.nodes && d.nodes.length){
      previewData.nodes = d.nodes.map(n =>
        '# ' + n.name + '  ' + n.user + '@' + n.ip + ' (SSH ' + n.ssh + ')' +
        '\n# 角色: ' + n.roles.join(', ') +
        '\n# 镜像: ' + (n.images.length ? n.images.join(', ') : '(无)')).join('\n\n') +
        (d.proxysql_conf ? '\n\n# ---- ProxySQL 读写分离配置 (conf/proxysql/proxysql.cnf) ----\n' + d.proxysql_conf : '');
    }
    $('#tabKafkaNodes').style.display = (d.nodes && d.nodes.length) ? '' : 'none';
    previewData.clusterInv = d.cluster_inventory || '';
    previewData.clusterCfg = d.cluster_config || '';
    $('#tabClusterInv').style.display = d.cluster_inventory ? '' : 'none';
    $('#tabClusterCfg').style.display = d.cluster_config ? '' : 'none';
    $('#previewCard').style.display = ''; switchTab(d.compose ? 'compose' : (d.cluster_inventory ? 'clusterInv' : 'compose'));
    let extra = '';
    if (d.backup_crons && d.backup_crons.length){
      extra += '\n' + d.backup_crons.map(c => trLabel('备份计划|backup plan') + '(crontab): ' + c).join('\n');
    }
    if (d.warnings.length) extra += '\n' + trLabel('提示|Notes') + ':\n- ' + d.warnings.map(w => trMsg(w)).join('\n- ');
    msg('预览已生成, 将打包镜像: ' + d.images.join(', ') + extra, d.warnings.length ? 'warn' : 'ok');
    $('#previewCard').scrollIntoView({behavior:'smooth'});
  } catch(e){ msg(e.message, 'err'); }
  $('#btnPreview').disabled = false;
}
async function doPack(){
  msg('');
  try { await api('/api/preview', buildConfig()); }
  catch(e){ msg(e.message, 'err'); return; }
  try { await api('/api/pack', buildConfig()); }
  catch(e){ msg(e.message, 'err'); return; }
  openProgress();
}
let pollTimer = null;
function openProgress(){
  $('#modal').classList.remove('hidden');
  $$('#modal .msg').forEach(x => x.remove());
  $('#mTitle').innerHTML = '<span class="spin"></span>正在打包';
  $('#mCancel').classList.remove('hidden'); $('#mClose').classList.add('hidden');
  clearInterval(pollTimer);
  pollTimer = setInterval(poll, 800);
  poll();
}
async function poll(){
  let s;
  try { s = await api('/api/progress'); } catch(e){ return; }
  $('#mBar').style.width = s.percent + '%';
  $('#mPct').textContent = s.percent + '% · ' + fmtGB(Math.round(s.done/1048576)) + ' / ' + fmtGB(Math.round(s.total/1048576));
  $('#mFile').textContent = s.current ? '正在打包: ' + s.current : (s.stage || '');
  $('#mStat').textContent = '速度 ' + s.speed + ' MB/s · 已用 ' + fmtTime(s.elapsed) + (s.eta ? ' · 预计剩余 ' + fmtTime(s.eta) : '');
  // 集群物料自动下载: 每组件一条进度
  const dlBox = $('#mDownloads');
  if (s.downloads && s.downloads.length){
    dlBox.style.display = '';
    dlBox.innerHTML = '<div class="hint" style="margin-bottom:6px">正在下载缺失集群物料 (国内可达源, 自动切换):</div>' +
      s.downloads.map(d => {
        const pct = d.total ? Math.min(100, Math.round(d.done * 100 / d.total)) : 0;
        return '<div style="display:flex;align-items:center;gap:8px;margin-bottom:5px;font-size:12px">'+
          '<span style="flex:0 0 240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(d.name)+'">'+esc(d.name)+'</span>'+
          '<span style="flex:1;height:8px;border-radius:99px;background:#e8e8ed;overflow:hidden;display:block">'+
          '<span style="display:block;height:100%;width:'+pct+'%;background:#3a7afe;border-radius:99px"></span></span>'+
          '<span style="flex:0 0 110px;text-align:right;color:#666">'+fmtGB(Math.round(d.done/1048576*10)/10)+' / '+fmtGB(Math.round(d.total/1048576*10)/10)+'</span></div>';
      }).join('');
  } else { dlBox.style.display = 'none'; dlBox.innerHTML = ''; }
  if (!s.running){
    clearInterval(pollTimer); pollTimer = null;
    $('#mBar').style.width = '100%';
    $('#mCancel').classList.add('hidden'); $('#mClose').classList.remove('hidden');
    if (s.error){
      $('#mTitle').textContent = s.error === '打包已取消' ? '已取消打包' : '打包失败';
      $('#mFile').textContent = ''; $('#mStat').textContent = '';
      const err = document.createElement('div');
      err.className = 'msg err'; err.style.marginTop = '12px'; err.textContent = s.error;
      $('#mStat').after(err);
    } else if (s.result){
      $('#mTitle').textContent = '打包完成';
      $('#mFile').textContent = s.result.path;
      $('#mStat').textContent = '产物大小 ' + s.result.size_mb + ' MB · 含 ' + s.result.images + ' 个镜像包' +
        (s.result.sha256 ? '\nSHA256: ' + s.result.sha256 : '') +
        (s.result.warnings.length ? '\n提示:\n- ' + s.result.warnings.join('\n- ') : '');
      msg('打包完成!\n' + s.result.path + ' (' + s.result.size_mb + ' MB)\n' +
          '已生成同名 .sha256 校验文件, 与压缩包一起拷贝到服务器可在部署时自动校验完整性\n\n' +
          '服务器部署: 解压后进入目录执行  ./deploy.sh  (root)', 'ok');
    }
  }
}
async function checkRunning(){
  try {
    const s = await api('/api/progress');
    if (s.running) openProgress();
  } catch(e){}
}

/* ================= 仓库状态 / 产物列表 ================= */
const whFilter = { kind: 'all', arch: 'all' };
function renderWarehouse(){
  if (typeof STATIC_MODE !== 'undefined' && STATIC_MODE){
    $('#whBody').innerHTML = '<tr><td colspan="5" class="hint">静态演示模式: 物料状态需在本地运行 python packer.py 后查看</td></tr>';
    const bd = $('#badgeWh'); if (bd){ bd.textContent = '演示'; bd.className = 'wbadge warn'; }
    return;
  }
  const p = CATALOG.files_present, sz = CATALOG.sizes;
  let rows = '', miss = 0;
  const row = (name, arch, ok, size, note, kind) => {
    if (!ok) miss++;
    rows += '<tr data-kind="'+kind+'" data-arch="'+arch+'"><td class="mono">'+esc(name)+'</td><td>'+(arch==='amd64'?'x86_64':'aarch64')+'</td>'+
      '<td><span class="dot '+(ok?'ok':'no')+'"></span>'+(ok?'就绪':'缺失')+'</td>'+
      '<td>'+(ok?fmtGB(size):'-')+'</td><td class="hint">'+esc(note||'')+'</td></tr>';
  };
  for (const arch of ['amd64','arm64']){
    row('docker 安装包', arch, p['docker_'+arch], sz.docker[arch], 'Docker 静态二进制', 'pkg');
    row('docker-compose 插件', arch, p['compose_'+arch], sz.compose[arch], 'compose v2 二进制', 'pkg');
  }
  // ---- K8s 集群物料(基础平台; 集群暂仅 amd64) ----
  const cc = CATALOG.cluster || {};
  if (cc.versions){
    // k8s 物料三态: ok=就绪 / na=可选(构建产物或自备) / dl=缺失(打包时自动下载)
    const k8sRow = (name, state3, note) => {
      const dot = state3 === 'ok' ? 'ok' : 'no';
      const label = state3 === 'ok' ? '就绪' : (state3 === 'na' ? '可选' : '打包时下载');
      rows += '<tr data-kind="k8s" data-arch="amd64"><td class="mono">'+esc(name)+'</td><td>x86_64</td>'+
        '<td><span class="dot '+dot+'"></span>'+label+'</td>'+
        '<td>-</td><td class="hint">'+esc(note||'')+'</td></tr>';
    };
    row('kk 二进制 (信创补丁构建)', 'amd64', p['cluster_kk_amd64'], (sz.cluster_k8s||{}).amd64 || 0, (cc.kk||{}).version || '', 'k8s');
    const cacheLabel = { kube:'kube 三件套', etcd:'etcd', cni_plugins:'CNI 插件', helm:'Helm', containerd:'containerd', crictl:'crictl', runc:'runc' };
    for (const [ver, vm] of Object.entries(cc.versions)){
      // artifact 是有网机器构建时的产物, 不计入缺失
      const compNames = Object.keys(vm.components || {}).map(k => cacheLabel[k] || k).join('/');
      k8sRow('K8s '+ver+' artifact 产物', p['cluster_artifact_'+ver+'_amd64'] ? 'ok' : 'na',
          '打包可选; 构建: python prepare_cluster.py --artifact-export 后执行生成的 .bat');
      k8sRow('K8s '+ver+' 二进制缓存', p['cluster_cache_ready_'+ver] !== false,
          '缺失不影响打包: 点「开始打包」时自动从国内源下载 ('+compNames+')');
      k8sRow('K8s '+ver+' 离线镜像包', p['cluster_images_'+ver+'_amd64'] !== false,
          '纯离线模式必需: 打包时按原生 tag 自动收集 (docker pull+save), 或 python prepare_cluster.py --images '+
          (vm.cni_versions ? 'flannel' : ''));
    }
    for (const [d, m] of Object.entries(cc.distros || {})){
      const closed = m.open === false;
      k8sRow('K8s OS 依赖包 · '+m.label, p['cluster_os_'+d] ? 'ok' : 'na',
          closed ? '闭源/无公开镜像: 需自行提取放置 (可选, 不影响其他发行版)'
                 : '缺失时「开始打包」自动下载或自备 (可选)');
    }
  }
  for (const [s, meta] of Object.entries(CATALOG.services)){
    for (const arch of (meta.supported_arch || ['amd64'])){
      const ok = p['img_'+s+'_'+arch];
      row(trLabel(meta.label) + (LANG === 'en' ? ' image' : ' 镜像'), arch, ok, (sz.images[s]||{})[arch] || 0,
          (meta.is_plugin ? '插件' : ''), 'img');
    }
    if (meta.cluster && meta.cluster.images){
      for (const arch of (meta.supported_arch || ['amd64'])){
        const ok = p['img_'+s+'_cluster_'+arch];
        row(trLabel(meta.label) + (LANG === 'en' ? ' cluster image ('+meta.cluster.tag+')' : ' 集群镜像 ('+meta.cluster.tag+')'), arch, ok,
            ((sz.cluster||{})[s]||{})[arch] || 0, LANG === 'en' ? 'for cluster topology' : '集群形态专用', 'img');
      }
    }
  }
  $('#whBody').innerHTML = rows;
  applyWhFilter();
  const bd = $('#badgeWh');
  if (bd){ bd.textContent = miss > 0 ? ('缺 ' + miss) : ''; bd.className = 'wbadge' + (miss > 0 ? ' warn' : ''); }
}
function applyWhFilter(){
  $$('#whBody tr').forEach(tr => {
    const okKind = whFilter.kind === 'all' || tr.dataset.kind === whFilter.kind;
    const okArch = whFilter.arch === 'all' || tr.dataset.arch === whFilter.arch;
    tr.classList.toggle('hide', !(okKind && okArch));
  });
}
function setupWhFilter(){
  const wire = (sel, key, attr) => {
    $$(sel + ' button').forEach(b => b.onclick = () => {
      whFilter[key] = b.dataset[attr];
      $$(sel + ' button').forEach(x => x.classList.toggle('on', x === b));
      applyWhFilter();
    });
  };
  wire('#fKind', 'kind', 'k');
  wire('#fArch', 'arch', 'a');
}
async function loadBundles(){
  try {
    const d = await api('/api/bundles');
    $('#outBody').innerHTML = d.bundles.length
      ? d.bundles.map(b => '<tr><td class="mono">'+esc(b.name)+'</td><td>'+b.size_mb+' MB</td><td>'+b.mtime+'</td>'+
          '<td class="mono hint" style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(b.sha256||'')+'">'+
          (b.sha256 ? esc(b.sha256.slice(0,16)) + '…' : '—')+'</td></tr>').join('')
      : '<tr><td colspan="4" class="hint">暂无产物</td></tr>';
  } catch(e){}
}

init().then(() => { applyI18n(document); });

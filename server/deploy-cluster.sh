#!/usr/bin/env bash
# =============================================================================
# K8s 集群离线部署脚本 (KubeKey · 信创)
# 由 packer.py 打入离线包根目录, 决策来自同目录 manifest.sh 的 CLUSTER_* 变量,
# 物料在 cluster/ 子目录 (kk / inventory.yaml / config.yaml / artifact|cache / os 包)。
#
# 可独立执行, 也可由 deploy.sh 在中间件部署前调用 (manifest CLUSTER_ENABLED=1 时)。
# 幂等可重跑; 可选参数: --skip-node-prep 跳过节点 OS 依赖预装 / --skip-verify 跳过集群校验
# =============================================================================
set -u
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for a in "$@"; do
  case "$a" in
    --skip-node-prep) SKIP_NODE_PREP=1 ;;
    --skip-verify)    SKIP_CLUSTER_VERIFY=1 ;;
    *) : ;;
  esac
done
SKIP_NODE_PREP="${SKIP_NODE_PREP:-0}"
SKIP_CLUSTER_VERIFY="${SKIP_CLUSTER_VERIFY:-0}"

log()  { echo -e "\033[32m[INFO]\033[0m  $*"; }
warn() { echo -e "\033[33m[WARN]\033[0m  $*" >&2; }
die()  { echo -e "\033[31m[FAIL]\033[0m  $*" >&2; exit 1; }
step() { echo; echo "[INFO] 【集群 $1】$2"; }

[ -f manifest.sh ] || die "缺少 manifest.sh, 请勿拆散离线包"
# shellcheck disable=SC1091
source ./manifest.sh
[ "${CLUSTER_ENABLED:-0}" = "1" ] || die "manifest.sh 未启用集群 (CLUSTER_ENABLED!=1)"
# kk v4 的 workdir 默认为 "当前目录/kubekey", binary_dir=workdir/kubekey。
# 固定 --workdir 到包目录下, 缓存铺放与 kk 查找路径严格一致。
KK_HOME="$PWD/kubekey"
KK_CACHE="$KK_HOME/kubekey"
KUBECONFIG_PATH=/etc/kubernetes/admin.conf

# ---------------------------------------------------------------- 【集群 0】环境
step 0 "部署机环境检查"
[ "$(id -u)" = "0" ] || die "请用 root 执行"
# kk/containerd/kubectl 等都装在 /usr/local/bin: cron/CI 等非登录 shell 的 PATH 不含它, 统一补上
export PATH="/usr/local/bin:/usr/local/sbin:$PATH"
UNAME_M="$(uname -m)"
case "$UNAME_M" in
  x86_64)  THIS_ARCH=amd64 ;;
  aarch64) THIS_ARCH=arm64 ;;
  *) die "不支持的部署机架构: $UNAME_M" ;;
esac
[ "$THIS_ARCH" = "$PKG_ARCH" ] || die "部署机架构 $THIS_ARCH 与打包架构 $PKG_ARCH 不一致"
[ -f cluster/kk ] || die "缺少 cluster/kk 二进制"
chmod +x cluster/kk 2>/dev/null || true   # 自愈: 个别解压环境不保留执行位
[ -x cluster/kk ] || die "cluster/kk 无法执行(权限或架构不符)"
OS_ID=""
[ -r /etc/os-release ] && OS_ID="$(. /etc/os-release && echo "${ID:-}" | tr -d '\"')"
log "部署机: arch=$THIS_ARCH os=${OS_ID:-unknown}  集群: ${CLUSTER_KUBE_VERSION:-?} (${CLUSTER_MODE:-cache} 模式)"

# kk 的连接器(本地+SSH)执行命令都前置 sudo, 精简系统常缺该命令
command -v sudo >/dev/null 2>&1 || {
  echo "deploy-cluster.sh: 部署机缺少 sudo(kk 依赖), 尝试自动安装..." >&2
  apt-get install -y sudo >/dev/null 2>&1 || yum install -y sudo >/dev/null 2>&1 || true
}
command -v sudo >/dev/null 2>&1 || die "部署机缺少 sudo 且自动安装失败(kk 连接器依赖 sudo, 请先安装)"

# 信创提示: 代理模式内核要求 / 目标版本 cgroup 要求
KVER="$(uname -r | cut -d- -f1)"
if [ "${CLUSTER_PROXY_MODE:-iptables}" = "nftables" ]; then
  if [ "$(printf '%s\n' 5.13 "$KVER" | sort -V | head -1)" = "5.13" ]; then
    warn "kube-proxy nftables 需内核>=5.13, 当前 $KVER, 部署失败请改回 iptables 重新打包"
  fi
fi
case "${CLUSTER_KUBE_VERSION:-}" in
  v1.2*|v1.3[0-4]|v1.3[0-4].*) CG="$(stat -fc %T /sys/fs/cgroup 2>/dev/null)"
                   [ "$CG" = "cgroup2fs" ] || log "cgroup: v1 — ${CLUSTER_KUBE_VERSION} 支持" ;;
  *) warn "${CLUSTER_KUBE_VERSION} 要求 cgroup v2, 请确认各节点已启用 (stat -fc %T /sys/fs/cgroup = cgroup2fs)" ;;
esac

# ---------------------------------------------------------------- 【集群 1】幂等
step 1 "已有集群检测"
if [ -f "$KUBECONFIG_PATH" ]; then
  log "本机已有 Kubernetes 集群, 跳过重复部署 (幂等)"
  log "扩容: 改打包配置增加节点后重新打包, 在此执行: ./cluster/kk add nodes -i cluster/inventory.yaml -c cluster/config.yaml"
  log "升级: ./cluster/kk upgrade cluster -i cluster/inventory.yaml -c cluster/config.yaml --with-kubernetes <版本>"
  log "卸载: ./uninstall-cluster.sh (kk delete cluster --all 一键卸载, 危险操作)"
  exit 0
fi

# ---------------------------------------------------------------- 【集群 2】OS 依赖
step 2 "节点 OS 依赖预装"
prep_one_node() {
  _ip="$1"; _user="$2"
  log "节点 $_ip: 探测系统信息"
  REMOTE="$(RUN_SSH "${_user}@$_ip" "uname -m; (. /etc/os-release 2>/dev/null && echo \$ID); command -v sudo >/dev/null && echo HAS_SUDO || echo NOSUDO" 2>/dev/null || true)"
  R_ARCH="$(echo "$REMOTE" | sed -n 1p | tr -d '\r')"
  R_OS="$(echo "$REMOTE" | sed -n 2p | tr -d '\r"')"
  R_SUDO="$(echo "$REMOTE" | sed -n 3p | tr -d '\r')"
  if [ -z "$R_ARCH" ] || [ -z "$R_OS" ]; then
    warn "节点 $_ip: SSH 探测失败, 跳过预装 (kk 建集群时会再连一次, 届时失败才是硬错误)"; return 1
  fi
  if [ "$R_SUDO" != "HAS_SUDO" ]; then
    log "节点 $_ip: 缺少 sudo(kk 依赖), 尝试远程安装"
    RUN_SSH "${_user}@$_ip" \
      "(apt-get update -qq 2>/dev/null; apt-get install -y sudo 2>/dev/null) || (yum install -y sudo 2>/dev/null) || dnf install -y sudo 2>/dev/null" \
      && RUN_SSH "${_user}@$_ip" "command -v sudo >/dev/null" \
      || { warn "节点 $_ip: sudo 自动安装失败, kk 将无法在该节点执行命令, 请手工安装后重跑"; return 1; }
    log "节点 $_ip: sudo 已就绪"
  fi
  [ "$R_ARCH" = "$UNAME_M" ] || { warn "节点 $_ip 架构 $R_ARCH 与部署机不一致, 跳过"; return 1; }
  pkg_dir=""
  for d in ${CLUSTER_OS_DISTROS:-}; do
    [ "$d" = "$R_OS" ] && pkg_dir="cluster/os/$d"
  done
  if [ -z "$pkg_dir" ] || [ ! -d "$pkg_dir" ]; then
    warn "节点 $_ip (os=$R_OS) 无对应 OS 依赖包, 假定已自备; 若 kk 报缺包请补 $pkg_dir 后重跑"
    return 0
  fi
  log "节点 $_ip: 分发并安装 OS 依赖包 ($pkg_dir)"
  RUN_SCP -q -r "$pkg_dir" "${_user}@$_ip:/tmp/.kk-os-pkgs" || return 1
  if ls "$pkg_dir"/*.deb >/dev/null 2>&1; then
    # 124+ 个 deb 一把梭可能因已装版本/依赖顺序报非零, 目的只是补齐缺失包: 失败降级为告警
    RUN_SSH "${_user}@$_ip" "dpkg -i /tmp/.kk-os-pkgs/*.deb" \
      || warn "节点 $_ip: OS 包 dpkg 安装部分报错(多为已装同版本), 继续以 kk 侧检测结果为准"
  else
    RUN_SSH "${_user}@$_ip" \
      "yum -y --disablerepo='*' localinstall /tmp/.kk-os-pkgs/*.rpm || rpm -Uvh --replacepkgs /tmp/.kk-os-pkgs/*.rpm" \
      || warn "节点 $_ip: OS 包 rpm 安装部分报错(多为已装同版本), 继续以 kk 侧检测结果为准"
  fi
  RUN_SSH "${_user}@$_ip" "rm -rf /tmp/.kk-os-pkgs" || true
  log "节点 $_ip: OS 依赖包完成"
}

if [ "$SKIP_NODE_PREP" = "1" ]; then
  warn "按参数跳过节点预检与 OS 依赖预装"
else
  # SSH 包装: 凭据取 cluster/inventory.yaml (kk 自带 SSH 不依赖这里, 这里只为预检/预装)
  # 用 sed 而非 grep -oP, 精简系统的 grep 可能不带 PCRE
  SSH_PORT="$(sed -n 's/^        port: \([0-9][0-9]*\).*/\1/p' cluster/inventory.yaml | head -1)"; SSH_PORT="${SSH_PORT:-22}"
  SSH_USER="$(sed -n 's/^        user: \(.*\)$/\1/p' cluster/inventory.yaml | head -1 | tr -d '\"')"; SSH_USER="${SSH_USER:-root}"
  PW="$(sed -n 's/^        password: \(.*\)$/\1/p' cluster/inventory.yaml | head -1 | tr -d '\"')"
  SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=8 -p $SSH_PORT"
  # 注意 scp 的端口参数是大写 -P(-p 是保留时间戳), 不能与 ssh 共用一份 OPTS
  SCP_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=8 -P $SSH_PORT"
  if [ -n "$PW" ] && command -v sshpass >/dev/null 2>&1; then
    RUN_SSH() { SSHPASS="$PW" sshpass -e ssh $SSH_OPTS "$@"; }
    RUN_SCP() { SSHPASS="$PW" sshpass -e scp $SCP_OPTS "$@"; }
  else
    RUN_SSH() { ssh $SSH_OPTS -o BatchMode=yes "$@"; }
    RUN_SCP() { scp $SCP_OPTS "$@"; }
    [ -n "$PW" ] && warn "部署机未装 sshpass 且集群用密码登录: 节点预检需免密密钥, 或装 sshpass, 或 --skip-node-prep"
  fi
  # 始终预检每个节点: 连通性 / 架构 / sudo(kk 连接器依赖); OS 包仅在包内携带且匹配发行版时预装
  # 注意 ${arr[@]}: ${arr} 只取数组首元素
  NODE_OK=0; NODE_FAIL=0
  for entry in ${CLUSTER_NODES[@]:-}; do
    IP="$(echo "$entry" | cut -d'|' -f2)"
    prep_one_node "$IP" "$SSH_USER" && NODE_OK=$((NODE_OK+1)) || NODE_FAIL=$((NODE_FAIL+1))
  done
  log "节点预检/预装: 成功 $NODE_OK, 失败/跳过 $NODE_FAIL"
fi

# ---------------------------------------------------------------- 【集群 3】物料
step 3 "kk 物料就位"
mkdir -p "$KK_CACHE"
EXTRA_SET=""
if [ "${CLUSTER_MODE:-cache}" = "online" ]; then
  # 在线安装: zone 由 config 决定(cn=国内源); 组件与镜像部署时联网下载
  EXTRA_SET="--set download.fetch=true"
  if [ "${CLUSTER_ZONE:-}" = "cn" ]; then
    log "在线安装模式: 国内源(zone=cn), 请确保节点可访问 hub.kubesphere.com.cn 与国内镜像源"
  else
    log "在线安装模式: 国际源, 请确保节点可访问 dl.k8s.io / registry.k8s.io"
  fi
elif [ "${CLUSTER_MODE:-cache}" = "cache" ] && [ -d cluster/cache ]; then
  # 二进制完整性校验(打包时生成 sha256 清单, 防止传输损坏带病上线)
  if [ -f cluster/cache.sha256 ]; then
    if command -v sha256sum >/dev/null 2>&1; then
      ( cd cluster && sha256sum -c cache.sha256 --quiet ) \
        || die "cluster/cache 二进制 sha256 校验失败, 包在传输中损坏, 请重新拷贝离线包"
      log "二进制缓存 sha256 校验通过 ($(wc -l < cluster/cache.sha256) 个文件)"
    else
      warn "缺少 sha256sum, 跳过二进制完整性校验"
    fi
  fi
  cp -a cluster/cache/. "$KK_CACHE/"
  log "二进制缓存已铺到 $KK_CACHE (download.fetch=false, 不访问网络)"
elif [ "${CLUSTER_MODE:-}" = "artifact" ]; then
  ART="$(ls cluster/*.tar.gz 2>/dev/null | grep -v -E 'deploy|os/' | head -1 || true)"
  [ -n "$ART" ] || die "artifact 模式但 cluster/ 内无 artifact tar.gz"
  [ -f cluster/artifact.md5 ] || die "缺少 cluster/artifact.md5"
  ACT_MD5="$(md5sum "$ART" | awk '{print $1}')"
  EXP_MD5="$(cat cluster/artifact.md5)"
  [ "$ACT_MD5" = "$EXP_MD5" ] || die "artifact md5 不一致: 实际 $ACT_MD5 期望 $EXP_MD5"
  EXTRA_SET="--set download.artifact_file=$PWD/$ART --set download.artifact_md5=$EXP_MD5"
  log "artifact 校验通过, kk 将从 $(basename "$ART") 解出全部二进制与镜像"
else
  warn "cache 模式但包内无 cluster/cache/, kk 将尝试联网下载(离线环境会失败)"
fi

# ---------------------------------------------------------------- 【集群 3.5】镜像导入(纯离线)
# 纯离线模式的镜像不在线拉取: 打包时已按原生 registry tag 收集为 tar, 这里在 kk create 之前
# 导入各节点本地 containerd。containerd 未安装的节点先用包内归档预装(精简配置, 数据目录与
# config.yaml 一致); kk 侧 config_policy=overwrite 会在建集群时重写配置并重启, 镜像落在
# 数据目录的磁盘存储里, 重启不丢。
if [ "${CLUSTER_MODE:-cache}" != "online" ] && ls cluster/images/*.tar >/dev/null 2>&1; then
  step "3.5" "节点离线镜像导入(纯离线, 不在线拉取)"
  # SKIP_NODE_PREP=1 时 RUN_SSH/RUN_SCP 未定义, 这里补默认(密钥免密)
  if [ "$(type -t RUN_SSH)" != "function" ]; then
    SSH_OPTS="${SSH_OPTS:--o StrictHostKeyChecking=no -o ConnectTimeout=8}"
    SCP_OPTS="${SCP_OPTS:--o StrictHostKeyChecking=no -o ConnectTimeout=8 -P 22}"
    RUN_SSH() { ssh $SSH_OPTS -o BatchMode=yes "$@"; }
    RUN_SCP() { scp $SCP_OPTS "$@"; }
  fi
  CT_TARBALL="$(ls cluster/cache/containerd/*/*/containerd*-linux-*.tar.gz 2>/dev/null | head -1 || true)"
  DATA_ROOT="${CLUSTER_CONTAINERD_DATA_ROOT:-/var/lib/containerd}"
  SANDBOX="${CLUSTER_SANDBOX_IMAGE:-registry.k8s.io/pause:3.9}"
  IMP_SH="$(mktemp /tmp/.kk-oi-import.XXXXXX.sh)"
  cat > "$IMP_SH" <<IMP_EOF
set -e
# 残留 kubelet unit 守卫: 上次失败部署遗留的 loaded+inactive unit 会让 kk precheck 卡死
if systemctl list-unit-files 2>/dev/null | awk '{print \$1}' | grep -qx 'kubelet.service' \\
   && [ "\$(systemctl is-active kubelet 2>/dev/null || true)" != "active" ] \\
   && [ ! -f /etc/kubernetes/admin.conf ]; then
  echo "[INFO] 清理残留 kubelet unit"
  systemctl stop kubelet 2>/dev/null || true
  rm -f /etc/systemd/system/kubelet.service /usr/lib/systemd/system/kubelet.service /lib/systemd/system/kubelet.service
  rm -rf /etc/systemd/system/kubelet.service.d
  systemctl daemon-reload
fi
mkdir -p /etc/containerd '$DATA_ROOT'
if ! command -v containerd >/dev/null 2>&1; then
  echo "[INFO] 预装 containerd (来自离线包)"
  [ -f /tmp/.kk-oi/@CT_TARBALL_NAME@ ] || { echo "[FAIL] 包内缺 containerd 归档且节点未装 containerd"; exit 1; }
  tar -xzf /tmp/.kk-oi/@CT_TARBALL_NAME@ --strip-components=1 -C /usr/local/bin/
fi
if [ ! -s /etc/containerd/config.toml ]; then
  containerd config default > /etc/containerd/config.toml
fi
# root/sandbox 与约定不一致时改写: 节点已有旧配置指向别处时, 镜像会导错数据目录,
# 随后 kk(config_policy=overwrite) 改 root 重启 containerd 会"丢"镜像
sed -i "s#^root = .*#root = \\"$DATA_ROOT\\"#" /etc/containerd/config.toml
sed -i "s#sandbox_image = \\"registry.k8s.io/pause:[^\"]*\\"#sandbox_image = \\"$SANDBOX\\"#" /etc/containerd/config.toml
cat > /etc/systemd/system/containerd.service <<'UNIT'
[Unit]
Description=containerd container runtime
Documentation=https://containerd.io
After=network.target local-fs.target

[Service]
ExecStartPre=-/sbin/modprobe overlay
ExecStart=/usr/local/bin/containerd

Type=notify
Delegate=yes
KillMode=process
Restart=always
RestartSec=5
LimitNPROC=infinity
LimitCORE=infinity
TasksMax=infinity
OOMScoreAdjust=-999

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now containerd >/dev/null 2>&1 || true
sleep 2
for t in /tmp/.kk-oi/*.tar; do
  [ -f "\$t" ] || continue
  ctr -n k8s.io images import --all-platforms "\$t" >/dev/null 2>&1 || echo "[WARN] \$(basename \$t) 导入部分报错(多为镜像已存在, 忽略)"
done
N=\$(ctr -n k8s.io images ls -q 2>/dev/null | wc -l)
echo "[INFO] 节点镜像数: \$N"
rm -rf /tmp/.kk-oi
[ "\$N" -ge 1 ] || { echo "[FAIL] 镜像导入后 containerd 仍为空"; exit 1; }
IMP_EOF
  sed -i "s|@CT_TARBALL_NAME@|$(basename "$CT_TARBALL")|" "$IMP_SH"
  LOCAL_IPS="$(hostname -I 2>/dev/null || true)"
  for entry in ${CLUSTER_NODES[@]:-}; do
    IP="$(echo "$entry" | cut -d'|' -f2)"
    if echo "$LOCAL_IPS" | grep -qw "$IP"; then
      mkdir -p /tmp/.kk-oi
      cp -a cluster/images/*.tar /tmp/.kk-oi/
      [ -n "$CT_TARBALL" ] && cp -a "$CT_TARBALL" /tmp/.kk-oi/
      bash "$IMP_SH" || die "本机($IP)离线镜像导入失败"
    else
      RUN_SSH "${SSH_USER:-root}@${IP}" "mkdir -p /tmp/.kk-oi" || die "节点 $IP SSH 不可达(镜像导入阶段)"
      RUN_SCP -q cluster/images/*.tar "${SSH_USER:-root}@${IP}:/tmp/.kk-oi/" || die "节点 $IP 镜像包分发失败"
      if [ -n "$CT_TARBALL" ]; then
        RUN_SCP -q "$CT_TARBALL" "${SSH_USER:-root}@${IP}:/tmp/.kk-oi/" || die "节点 $IP containerd 归档分发失败"
      fi
      RUN_SSH "${SSH_USER:-root}@${IP}" 'bash -s' < "$IMP_SH" || die "节点 $IP 离线镜像导入失败"
    fi
  done
  rm -f "$IMP_SH"
  log "全部节点离线镜像导入完成"
elif [ "${CLUSTER_MODE:-cache}" != "online" ] && [ "${CLUSTER_MODE:-}" != "artifact" ]; then
  # artifact 模式镜像内置于 artifact tar 由 kk 解出, 不适用本告警
  warn "离线模式但包内无 cluster/images/*.tar, 部署时会在线拉取镜像(非纯离线); 请用带 Docker Desktop 的打包机重新打包"
fi

# ---------------------------------------------------------------- 【集群 4】部署
step 4 "执行 kk create cluster (多节点 SSH, 耗时较长)"
# shellcheck disable=SC2086
./cluster/kk create cluster -i cluster/inventory.yaml -c cluster/config.yaml --workdir "$KK_HOME" $EXTRA_SET || {
  die "集群部署失败。排查: 1) 节点 SSH 连通性与密码 2) 重跑本脚本(幂等) 3) kk 日志上方第一个 FAIL"
}

# 证书自动续期(打包时配置了 cron 才启用): 每周期续期证书并重启控制面静态 Pod 所在服务
if [ -n "${CLUSTER_CERTS_RENEW_CRON:-}" ] && [ -f "$KUBECONFIG_PATH" ]; then
  RENEW_SH=/usr/local/bin/kk-certs-renew.sh
  cat > "$RENEW_SH" <<RENEW_EOF
#!/usr/bin/env bash
# K8s 证书自动续期 (由 deploy-cluster.sh 安装; 谨慎: 续期后控制面组件会重启)
cd "$PWD"
./cluster/kk certs renew -i cluster/inventory.yaml -c cluster/config.yaml >> /var/log/kk-certs-renew.log 2>&1
systemctl restart kubelet
RENEW_EOF
  chmod +x "$RENEW_SH"
  ( crontab -l 2>/dev/null | grep -v kk-certs-renew.sh ; \
    echo "${CLUSTER_CERTS_RENEW_CRON} $RENEW_SH" ) | crontab - \
    && log "证书自动续期已安装: crontab '${CLUSTER_CERTS_RENEW_CRON}' (日志 /var/log/kk-certs-renew.log)"
fi

# ---------------------------------------------------------------- 【集群 5】验证+报告
step 5 "集群验证与部署报告"
export PATH="/usr/local/bin:$PATH"
KUBECTL="$(command -v kubectl || echo /usr/local/bin/kubectl)"
# 运维 shell 持久化: 登录即用 kubectl(PATH + KUBECONFIG)
printf 'export PATH=/usr/local/bin:$PATH
export KUBECONFIG=%s
' "$KUBECONFIG_PATH" > /etc/profile.d/k8s.sh
if [ "$SKIP_CLUSTER_VERIFY" != "1" ] && [ -x "$KUBECTL" ] && [ -f "$KUBECONFIG_PATH" ]; then
  KUBECONFIG="$KUBECONFIG_PATH" "$KUBECTL" get nodes -o wide || warn "kubectl 查询失败, 请手工确认"
fi
{
  echo "K8s 集群部署信息  $(date '+%F %T')"
  echo "=================================================="
  echo "集群版本:   ${CLUSTER_KUBE_VERSION:-?} (kube-proxy ${CLUSTER_PROXY_MODE:-iptables})"
  echo "CNI:        ${CLUSTER_CNI:-?}    HA 端点: ${CLUSTER_HA_TYPE:-local}${CLUSTER_HA_VIP:+ ($CLUSTER_HA_VIP)}"
  echo "架构:       $PKG_ARCH"
  echo "部署机:     $(hostname) (${OS_ID:-unknown}, 内核 $(uname -r))"
  echo "节点清单:"
  for entry in ${CLUSTER_NODES[@]:-}; do
    echo "  $(echo "$entry" | tr '|' ' ')"
  done
  echo
  echo "日常运维(部署机即控制面之一):"
  echo "  export KUBECONFIG=$KUBECONFIG_PATH"
  echo "  kubectl get nodes -o wide          # 节点就绪状态"
  echo "  kubectl get pods -A                # 全部组件 Pod"
  echo
  echo "把 kubectl 用到运维本机(任选其一):"
  echo "  a) scp 本机 /etc/kubernetes/admin.conf 到运维机 ~/.kube/config"
  echo "  b) 直接在本机执行以上 kubectl 命令"
  echo
  echo "kk 命令速查(在本目录执行, 均带 -i cluster/inventory.yaml -c cluster/config.yaml):"
  echo "  新增节点  页面加节点重新打包后: ./cluster/kk add nodes"
  echo "  删除节点  ./cluster/kk delete nodes -i cluster/inventory.yaml -c cluster/config.yaml <节点名>"
  echo "  升级集群  ./cluster/kk upgrade cluster --with-kubernetes <版本> [--all] ${CLUSTER_UPGRADE_TO:+(本包已带 $CLUSTER_UPGRADE_TO 物料: ./upgrade-cluster.sh)}"
  echo "  续期证书  ./cluster/kk certs renew (${CLUSTER_CERTS_RENEW_CRON:+已自动续期: $CLUSTER_CERTS_RENEW_CRON}${CLUSTER_CERTS_RENEW_CRON:-建议每年手动一次})"
  echo "  备份etcd  ./cluster/kk etcd backup -i cluster/inventory.yaml -c cluster/config.yaml"
  echo "  卸载集群  ./uninstall-cluster.sh (危险)"
  echo
  echo "配置文件: cluster/inventory.yaml / cluster/config.yaml (改动需重新打包)"
} > cluster-k8s-报告.txt 2>/dev/null || true
log "部署完成! 集群 ${CLUSTER_KUBE_VERSION:-} 已就绪 (详情: cluster-k8s-报告.txt)"

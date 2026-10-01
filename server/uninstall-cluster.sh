#!/usr/bin/env bash
# =============================================================================
# K8s 集群一键卸载脚本 (由 packer.py 打入离线包)
# 卸载主体是 kk 自带能力, 比手工清理干净:
#   ./cluster/kk delete cluster --all
#     - kubeadm reset + 停删 kubelet/控制面静态 Pod 进程 + 删 kubeadm/kubelet/kubectl
#     - 删 /etc/kubernetes /var/lib/kubelet /var/log/pods /root/.kube/config
#     - 清 iptables/ipvs 与 CNI 网络接口及状态 (/etc/cni /opt/cni /var/lib/cni)
#     - 删 /etc/hosts 中 kubekey 注入段
#     - --all 连带: containerd 本体(服务/二进制/数据目录含离线导入的镜像)、crictl、
#       外置 etcd、镜像仓库
# 本脚本只兜底 kk 删除剧本不感知的部分: 证书续期 cron、/etc/profile.d/k8s.sh、
# kubelet 卷孤挂载(kk 对 shim 是 kill -9, 挂载不卸载则数据目录删不掉)、kk 留下的
# 空父目录与 /etc/crictl.yaml、自定义 etcd/kubelet 数据目录(kk 只清默认路径)。
# 危险操作: 清空集群所有节点的 Kubernetes 数据(不碰业务数据盘其他内容)。
# 用法: ./uninstall-cluster.sh [--yes] [--keep-cri] [--with-data]
#   --yes       跳过交互确认(自动化场景)
#   --keep-cri  保留节点 containerd 与镜像数据(重装更快; 默认连 CRI 一起删)
#   --with-data 连存储盘数据(storage_disks)一起清(默认不动)
# =============================================================================
set -u
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ASSUME_YES=0 KEEP_CRI=0 WITH_DATA=0
for a in "$@"; do
  case "$a" in
    --yes) ASSUME_YES=1 ;;
    --keep-cri) KEEP_CRI=1 ;;
    --with-data) WITH_DATA=1 ;;
    *) echo "[FAIL] 未知参数: $a (支持: --yes --keep-cri --with-data)"; exit 1 ;;
  esac
done

log()  { echo -e "\033[32m[INFO]\033[0m  $*"; }
warn() { echo -e "\033[33m[WARN]\033[0m  $*" >&2; }
die()  { echo -e "\033[31m[FAIL]\033[0m  $*" >&2; exit 1; }

[ -f manifest.sh ] || die "缺少 manifest.sh, 请勿拆散离线包"
# shellcheck disable=SC1091
source ./manifest.sh
[ "${CLUSTER_ENABLED:-0}" = "1" ] || die "本包未启用集群"
[ "$(id -u)" = "0" ] || die "请用 root 执行"
[ -f cluster/kk ] || die "缺少 cluster/kk"
[ -f cluster/inventory.yaml ] || die "缺少 cluster/inventory.yaml"
[ -f cluster/config.yaml ] || die "缺少 cluster/config.yaml"
chmod +x cluster/kk 2>/dev/null || true

echo "即将卸载集群 ${CLUSTER_KUBE_VERSION:-}, 以下节点的 Kubernetes 数据将被清空${KEEP_CRI:+ (保留 containerd)}:"
for entry in ${CLUSTER_NODES[@]:-}; do
  echo "  $(echo "$entry" | tr '|' ' ')"
done
if [ "$ASSUME_YES" != "1" ]; then
  read -r -p "确认卸载? 输入 yes 继续: " ans
  [ "$ans" = "yes" ] || { echo "已取消"; exit 0; }
fi

KK_HOME="$PWD/kubekey"
KK_ARGS=(-i cluster/inventory.yaml -c cluster/config.yaml --workdir "$KK_HOME")
if [ "$KEEP_CRI" = "1" ]; then
  # 保留 CRI: 其余删除开关用 --set 逐个打开(与 --all 的差异仅 delete.cri=false)
  KK_ARGS+=(--set delete.dns=true --set delete.etcd=true --set delete.image_registry=true)
else
  KK_ARGS+=(--all)
fi
[ "$WITH_DATA" = "1" ] && KK_ARGS+=(--with-data)

log "执行 kk 一键卸载 (多节点 SSH, 耗时较长): ./cluster/kk delete cluster ${KK_ARGS[*]}"
./cluster/kk delete cluster "${KK_ARGS[@]}" || \
  die "kk 卸载失败。可原样重跑本脚本重试; 节点 SSH 不可达时先修复再重跑"

# ---------------------------------------------------------------- 残留兜底
# kk 只删它自己装的; 以下是本离线包部署的附件与 kk 删除剧本的边角:
#   - 证书续期 cron + 运维 shell 配置 (deploy-cluster.sh 装的)
#   - kubelet pod 卷孤挂载(kk 对 shim 是 kill -9, 挂载残留导致数据目录删不掉, 须先 umount)
#   - kk 只清子目录留下的空父目录(/etc/cni /opt/cni)、/var/lib/etcd、/etc/crictl.yaml
#   - 自定义 etcd/kubelet 数据目录(kk 只清默认路径)
log "kk 卸载完成, 清理部署附件与挂载残留..."

if crontab -l 2>/dev/null | grep -q kk-certs-renew.sh; then
  ( crontab -l 2>/dev/null | grep -v kk-certs-renew.sh ) | crontab - \
    && log "已移除证书自动续期 cron"
fi
rm -f /usr/local/bin/kk-certs-renew.sh /var/log/kk-certs-renew.log
rm -f /etc/profile.d/k8s.sh

safe_dir() {  # 只放行无 ".." 的绝对路径, 防误删
  case "$1" in
    /*) [ "$1" != "/" ] || return 1 ;;
    *) return 1 ;;
  esac
  case "$1" in *..*) return 1 ;; esac
  return 0
}
KUBELET_DIR="/var/lib/kubelet"
if [ -n "${CLUSTER_KUBELET_ROOT_DIR:-}" ] && [ "${CLUSTER_KUBELET_ROOT_DIR%/}" != "/var/lib/kubelet" ] \
   && safe_dir "$CLUSTER_KUBELET_ROOT_DIR"; then
  KUBELET_DIR="${CLUSTER_KUBELET_ROOT_DIR%/}"
fi
ETCD_DIR=""
if [ -n "${CLUSTER_ETCD_DATA_DIR:-}" ] && [ "${CLUSTER_ETCD_DATA_DIR%/}" != "/var/lib/etcd" ] \
   && safe_dir "$CLUSTER_ETCD_DATA_DIR"; then
  ETCD_DIR="${CLUSTER_ETCD_DATA_DIR%/}"
fi

# 各节点同一份清理逻辑(本机直接跑, 远端 bash -s), 参数:
#   $1=kubelet 数据目录  $2=etcd 数据目录(worker 传空)  $3=keep_cri(1/0)
RES_SH="$(mktemp /tmp/.kk-res.XXXXXX.sh)"
cat > "$RES_SH" <<'RES_EOF'
set -u
umount_tree() {  # 深度优先卸载目录下的孤挂载点, 不 umount 则 rm -rf 报 Device busy
  local root="$1" m
  [ -n "$root" ] && [ -d "$root" ] || return 0
  while :; do
    m="$(awk -v r="$root/" 'index($2, r) == 1 {print $2}' /proc/mounts | sort -r | head -1)"
    [ -n "$m" ] || break
    umount -f "$m" 2>/dev/null || umount -l "$m" 2>/dev/null || break
  done
}
umount_tree "$1"
umount_tree /var/lib/kubelet
rm -rf /root/kubekey "$1" /var/lib/kubelet /var/lib/etcd /etc/cni /opt/cni /var/lib/cni
if [ -n "$2" ]; then umount_tree "$2"; rm -rf "$2" /var/lib/etcd; fi
[ "${3:-0}" = "1" ] || rm -f /etc/crictl.yaml
RES_EOF

SSH_PORT="$(sed -n 's/^        port: \([0-9][0-9]*\).*/\1/p' cluster/inventory.yaml | head -1)"; SSH_PORT="${SSH_PORT:-22}"
SSH_USER="$(sed -n 's/^        user: \(.*\)$/\1/p' cluster/inventory.yaml | head -1 | tr -d '\"')"; SSH_USER="${SSH_USER:-root}"
PW="$(sed -n 's/^        password: \(.*\)$/\1/p' cluster/inventory.yaml | head -1 | tr -d '\"')"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=8 -p $SSH_PORT"
if [ -n "$PW" ] && command -v sshpass >/dev/null 2>&1; then
  RUN_SSH() { SSHPASS="$PW" sshpass -e ssh $SSH_OPTS "$@"; }
else
  RUN_SSH() { ssh $SSH_OPTS -o BatchMode=yes "$@"; }
fi

for entry in ${CLUSTER_NODES[@]:-}; do
  _ip="$(echo "$entry" | cut -d'|' -f2)"
  _role="$(echo "$entry" | cut -d'|' -f3)"
  _etcd=""; [ "$_role" != "worker" ] && _etcd="$ETCD_DIR"
  if hostname -I 2>/dev/null | grep -qw "$_ip"; then
    bash "$RES_SH" "$KUBELET_DIR" "$_etcd" "$KEEP_CRI" \
      || warn "本机($_ip): 残留清理部分失败, 可手工检查 $KUBELET_DIR"
  else
    RUN_SSH "${SSH_USER}@${_ip}" "bash -s -- '$KUBELET_DIR' '$_etcd' '$KEEP_CRI'" < "$RES_SH" \
      || warn "节点 $_ip: 残留清理部分失败(SSH 或目录占用), 可手工检查 $KUBELET_DIR"
  fi
done
rm -f "$RES_SH"
log "部署附件与挂载残留清理完成"

if [ "$KEEP_CRI" = "1" ]; then
  log "集群已卸载 (--keep-cri: containerd 与镜像数据保留于 ${CLUSTER_CONTAINERD_DATA_ROOT:-/var/lib/containerd})"
  log "彻底清理: 重跑本脚本去掉 --keep-cri, 或手工删除上述目录"
else
  log "集群已卸载干净: k8s 组件/containerd/镜像/网络规则/DNS 段均已清除"
fi
log "重新部署: 重跑 ./deploy-cluster.sh 即可 (幂等)"

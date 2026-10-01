#!/usr/bin/env bash
# =============================================================================
# K8s 集群一键卸载脚本 (由 packer.py 打入离线包)
# 危险操作: 会清空该集群所有节点的 Kubernetes 数据(不含各自业务数据盘)。
# 用法: ./uninstall-cluster.sh [--yes]
# =============================================================================
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ASSUME_YES=0
[ "${1:-}" = "--yes" ] && ASSUME_YES=1

[ -f manifest.sh ] || { echo "[FAIL] 缺少 manifest.sh"; exit 1; }
source ./manifest.sh
[ "${CLUSTER_ENABLED:-0}" = "1" ] || { echo "[FAIL] 本包未启用集群"; exit 1; }

echo "即将卸载集群 ${CLUSTER_KUBE_VERSION:-}, 节点清单:"
for entry in ${CLUSTER_NODES[@]:-}; do
  echo "  $(echo "$entry" | tr '|' ' ')"
done
if [ "$ASSUME_YES" != "1" ]; then
  read -r -p "确认卸载? 输入 yes 继续: " ans
  [ "$ans" = "yes" ] || { echo "已取消"; exit 0; }
fi

./cluster/kk delete cluster -f cluster/inventory.yaml -c cluster/config.yaml --yes || \
  ./cluster/kk delete cluster -i cluster/inventory.yaml -c cluster/config.yaml --yes || {
    echo "[FAIL] 卸载失败, 可重跑本脚本重试"; exit 1
  }
echo "[INFO] 集群已卸载。残留数据目录(如需彻底清理请手工删除):"
echo "  /etc/kubernetes /var/lib/kubelet /var/lib/etcd /root/kubekey /etc/cni /opt/cni /etc/containerd"
[ -n "${CLUSTER_CONTAINERD_DATA_ROOT:-}" ] && echo "  $CLUSTER_CONTAINERD_DATA_ROOT (containerd 数据目录, 含离线导入的集群镜像; 确认无用后可删)"
echo "  各节点 kubelet/etcd 数据若自定义过 root-dir/data_dir, 也在对应数据盘目录下"

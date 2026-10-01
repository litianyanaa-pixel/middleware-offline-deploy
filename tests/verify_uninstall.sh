#!/bin/bash
# 真机全链路验证: kk 一键卸载(对在跑集群) → 双节点干净校验 → 产品化包重新部署 → 双 Ready
# 在控制面节点(112)执行: bash verify_uninstall.sh <离线包tar>
# 前置: 112→113 免密 SSH; 包为带 cluster 的产品化离线包
exec > /data1/bundle/verify_uninstall.log 2>&1
set -x
date
# cron/CI 等非登录 shell 的 PATH 常缺 /usr/local/bin, kk 系二进制都装在那
export PATH="/usr/local/bin:/usr/local/sbin:$PATH"
die() { echo "[FAIL] $*"; date; exit 1; }

TAR="$1"; [ -n "$TAR" ] || die "用法: $0 <离线包tar>"
[ -f "$TAR" ] || die "包不存在: $TAR"
W=10.100.11.113
sshw() { ssh -o StrictHostKeyChecking=no -o BatchMode=yes "root@$1" "$2"; }

# ===== 1. 解包与包完整性 =====
cd /data1/bundle
rm -rf demo2 && mkdir demo2
tar -xzf "$TAR" -C demo2 --strip-components=1 || die "解压失败"
[ -x demo2/uninstall-cluster.sh ] || die "包内无 uninstall-cluster.sh"
grep -q "CLUSTER_ETCD_DATA_DIR='/data1/etcd'" demo2/manifest.sh || die "manifest 缺 CLUSTER_ETCD_DATA_DIR"
grep -q "CLUSTER_KUBELET_ROOT_DIR='/data1/kubelet'" demo2/manifest.sh || die "manifest 缺 CLUSTER_KUBELET_ROOT_DIR"
echo '== 1. 解包与 manifest 校验通过'

# ===== 2. kk 一键卸载在跑集群 (kk delete cluster --all) =====
cd /data1/bundle/demo2
bash uninstall-cluster.sh --yes || die "kk 一键卸载失败"
echo '== 2. kk 一键卸载完成'

# ===== 3. 双节点干净校验(任何残留即失败) =====
CLEAN_SH='
export PATH="/usr/local/bin:/usr/local/sbin:$PATH"
fail=0
chk() { eval "$2" >/dev/null 2>&1 && { echo "RESIDUAL: $1"; fail=1; }; }
# 二进制按绝对路径查(command -v 依赖 PATH, 非登录 shell 会漏判)
bin_left() { [ -e "/usr/local/bin/$1" ] || [ -e "/usr/bin/$1" ] || [ -e "/bin/$1" ]; }
chk "/etc/kubernetes 仍在"                "[ -e /etc/kubernetes ]"
chk "kubelet unit 仍在"                   "[ -e /etc/systemd/system/kubelet.service ]"
chk "kubelet 二进制仍在"                  "bin_left kubelet"
chk "kubeadm 二进制仍在"                  "bin_left kubeadm"
chk "kubectl 二进制仍在"                  "bin_left kubectl"
chk "crictl 仍在"                         "bin_left crictl"
chk "containerd unit 仍在"                "systemctl list-unit-files | grep -q \"^containerd.service\""
chk "containerd 二进制仍在"               "bin_left containerd"
chk "/etc/containerd 仍在"                "[ -e /etc/containerd ]"
chk "/etc/crictl.yaml 仍在"               "[ -e /etc/crictl.yaml ]"
for d in /data1/etcd /data1/kubelet /data1/containerd /var/lib/kubelet /var/lib/etcd /etc/cni /opt/cni /var/lib/cni; do
  chk "$d 仍在" "[ -e $d ]"
done
chk "cni0 网卡仍在"                       "ip link show cni0"
chk "flannel.1 网卡仍在"                  "ip link show flannel.1"
chk "nodelocaldns 网卡仍在"               "ip link show nodelocaldns"
chk "kube-ipvs0 网卡仍在"                 "ip link show kube-ipvs0"
chk "iptables 仍有 KUBE 规则"             "iptables-save | grep -q \"^-A KUBE\""
chk "控制面静态 Pod 进程仍在"             "pgrep -x kube-apiserver"
chk "kubelet 进程仍在"                    "pgrep -x kubelet"
chk "/etc/hosts 仍有 kubekey 段"          "grep -q \"kubekey hosts BEGIN\" /etc/hosts"
chk "证书续期 cron 仍在"                  "crontab -l 2>/dev/null | grep -q kk-certs-renew"
[ "$fail" = 0 ] && echo CLEAN || echo DIRTY
'
R112="$(bash -c "$CLEAN_SH")";  echo "-- 112:"; echo "$R112"
R113="$(sshw "$W" "bash -s" <<< "$CLEAN_SH")"; echo "-- 113:"; echo "$R113"
echo "$R112" | grep -q "^CLEAN$" || die "112 卸载后仍有残留(见上)"
echo "$R113" | grep -q "^CLEAN$" || die "113 卸载后仍有残留(见上)"
echo '== 3. 双节点卸载干净校验通过'

# ===== 4. 产品化包重新部署 (deploy-cluster.sh 端到端) =====
cd /data1/bundle/demo2
bash deploy-cluster.sh || die "deploy-cluster.sh 失败"

# ===== 5. 集群就绪校验 =====
export KUBECONFIG=/etc/kubernetes/admin.conf
KUBECTL="$(command -v kubectl || echo /usr/local/bin/kubectl)"
"$KUBECTL" wait --for=condition=Ready nodes --all --timeout=300s || die "节点未 Ready"
# CNI 等尾随 Pod 比节点 Ready 晚几秒, 就绪判定带重试
NOT_READY=""
for _ in $(seq 1 36); do
  NOT_READY="$("$KUBECTL" get pods -A --no-headers 2>/dev/null | grep -vE 'Running|Completed' || true)"
  [ -z "$NOT_READY" ] && break
  sleep 5
done
[ -z "$NOT_READY" ] || die "存在非 Running Pod: $NOT_READY"
"$KUBECTL" get nodes -o wide
"$KUBECTL" get pods -A
date
echo '== 全链路验证通过: kk 一键卸载干净 + 产品化包重新部署双 Ready'

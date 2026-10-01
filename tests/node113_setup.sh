#!/bin/bash
# 113 节点纯离线初始化: 净场 → 预装 containerd(root=/data1/containerd, sandbox=pause:3.9) → 导入全量镜像
# 由 112 通过 ssh 分发执行; 镜像 tar 与 containerd 归档需先 scp 到本机 /data1/ (CTTAR 由主脚本 sed 注入)
exec > /data1/node113_setup.log 2>&1
set -x
date

die() { echo "[FAIL] $*"; date; exit 1; }
CTTAR=containerd-static-1.7.28-linux-amd64.tar.gz

# ===== 净场 =====
systemctl stop kubelet containerd etcd 2>/dev/null || true
systemctl disable kubelet containerd etcd 2>/dev/null || true
pkill -9 kubelet 2>/dev/null || true
pkill -9 etcd 2>/dev/null || true
rm -rf /usr/local/bin/{calicoctl,containerd,containerd-shim,containerd-shim-runc-v1,containerd-shim-runc-v2,containerd-stress,ctr,etcd,etcdctl,etcdutl,kubeadm,kubectl,kubelet,kube-scripts,runc,crictl,helm}
rm -f /usr/bin/{kubeadm,kubelet,kubectl,containerd,containerd-shim-runc-v2,ctr,runc}
rm -f /etc/systemd/system/{kubelet,containerd,etcd}.service
rm -f /usr/lib/systemd/system/{kubelet,containerd,etcd}.service
rm -f /lib/systemd/system/{kubelet,containerd,etcd}.service
rm -rf /etc/systemd/system/{kubelet,containerd,etcd}.service.d
rm -rf /etc/kubernetes /var/lib/kubelet /var/lib/etcd /etc/cni /opt/cni /var/lib/cni /var/lib/containerd /etc/containerd /etc/crictl.yaml /data1/etcd /data1/kubelet /data1/containerd
ip link delete cni0 2>/dev/null || true
ip link delete flannel.1 2>/dev/null || true
iptables -F
iptables -t nat -F
iptables -t mangle -F
systemctl daemon-reload
echo '== 净场完成'

# ===== 预装 containerd =====
[ -f "/data1/$CTTAR" ] || die "缺 /data1/$CTTAR (主脚本 scp)"
tar -xzf "/data1/$CTTAR" --strip-components=1 -C /usr/local/bin/ || die "containerd 解压失败"
mkdir -p /etc/containerd
/usr/local/bin/containerd config default > /etc/containerd/config.toml
sed -i 's#root = "/var/lib/containerd"#root = "/data1/containerd"#' /etc/containerd/config.toml
sed -i 's#sandbox_image = "registry.k8s.io/pause:3.8"#sandbox_image = "registry.k8s.io/pause:3.9"#' /etc/containerd/config.toml
grep -E '^root|sandbox_image' /etc/containerd/config.toml
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
systemctl enable --now containerd
sleep 3
systemctl is-active containerd || die "containerd 未起来"

# ===== 导入全量镜像(纯离线; worker 不跑 etcd/控制面, 导入亦无害) =====
for t in /data1/k8s-v1.28.15-images.tar /data1/flannel-images.tar; do
  [ -f "$t" ] || die "缺镜像包 $t"
  echo "== import $t"
  ctr -n k8s.io images import --all-platforms "$t" 2>&1 | tail -1
done
ctr -n k8s.io images tag flannel/flannel:v0.27.4 docker.io/flannel/flannel:v0.27.4 2>/dev/null || true
ctr -n k8s.io images tag flannel/flannel-cni-plugin:v1.7.1-flannel1 docker.io/flannel/flannel-cni-plugin:v1.7.1-flannel1 2>/dev/null || true
N_IMG=$(ctr -n k8s.io images ls | grep -cE 'kube-proxy|pause|coredns|flannel')
[ "$N_IMG" -ge 4 ] || die "导入镜像数量不足($N_IMG)"
echo '== 113 镜像清单:'
ctr -n k8s.io images ls | grep -E 'kube-proxy|pause|coredns|flannel'
date
echo '== 113 初始化完成'

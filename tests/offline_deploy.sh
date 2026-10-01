#!/bin/bash
# 纯离线重建 v1.28.15+flannel: 净场 → 解包 → 修正config → 预装containerd → 导入全量镜像 → 113同样 → deploy-cluster.sh
# 原则: 全程零在线镜像拉取; 镜像以 registry.k8s.io 等原生 tag 导入节点本地 containerd
exec > /data1/bundle/offline_deploy.log 2>&1
set -x
date

die() { echo "[FAIL] $*"; date; exit 1; }

# ===== 净场 =====
kubeadm reset -f 2>/dev/null || true
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
rm -rf /etc/kubernetes /var/lib/kubelet /var/lib/etcd /etc/cni /opt/cni /var/lib/cni /var/lib/containerd /etc/containerd /etc/crictl.yaml /data1/etcd /data1/kubelet /data1/containerd /root/kubekey
ip link delete cni0 2>/dev/null || true
ip link delete nodelocaldns 2>/dev/null || true
ip link delete kube-ipvs0 2>/dev/null || true
iptables -F
iptables -t nat -F
iptables -t mangle -F
systemctl daemon-reload
echo '== 净场完成'

# ===== 解包 =====
cd /data1/bundle
rm -rf demo
mkdir demo
tar -xzf demo-offline.tar.gz -C demo --strip-components=1 || die "bundle 解压失败"

# ===== config.yaml 纯离线修正 =====
# 1) CNI 切 flannel; 2) 去掉 hub 在线加速段(纯离线用本地镜像); 3) config.root → data_root(kk 模板读的键)
sed -i 's/type: "calico"/type: "flannel"/' demo/cluster/config.yaml
sed -i '/^  registry:$/,+1d' demo/cluster/config.yaml
sed -i '/^      config:$/d' demo/cluster/config.yaml
sed -i 's|^        root: "/data1/containerd"|      data_root: "/data1/containerd"|' demo/cluster/config.yaml
grep -q 'type: "flannel"' demo/cluster/config.yaml || die "config CNI 未切到 flannel"
grep -q 'data_root: "/data1/containerd"' demo/cluster/config.yaml || die "config data_root 修正失败"
grep -q 'imageRepository' demo/cluster/config.yaml && die "config 仍残留在线镜像源"
# 显式关闭 localpv 存储类(kk 默认启用, 离线包未带 localpv chart 时会折在存储类步骤)
grep -q '^  storage_class:' demo/cluster/config.yaml || printf '  storage_class:\n    local:\n      enabled: false\n      default: false\n' >> demo/cluster/config.yaml
grep -q 'enabled: false' demo/cluster/config.yaml || die "storage_class 显式关闭失败"
echo '== config.yaml 纯离线修正通过'

# ===== 预装 containerd (root=/data1/containerd, sandbox=pause:3.9) =====
CTARCH=$(ls /data1/bundle/demo/cluster/cache/containerd/*/amd64/containerd-static-*.tar.gz 2>/dev/null | head -1)
[ -n "$CTARCH" ] || die "包内无 containerd 静态归档(cluster/cache/containerd/)"
tar -xzf "$CTARCH" --strip-components=1 -C /usr/local/bin/ || die "containerd 解压失败"
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
command -v ctr >/dev/null || die "ctr 不可用"

# ===== 导入全量镜像(纯离线) =====
for t in /data1/k8s-v1.28.15-images.tar /data1/flannel-images.tar /data1/etcd3515.tar; do
  [ -f "$t" ] || die "缺镜像包 $t"
  echo "== import $t"
  ctr -n k8s.io images import --all-platforms "$t" 2>&1 | tail -1
done
# docker.io 单路径镜像 containerd 会规范化为 library/ 前缀, tag 必须写全称
ctr -n k8s.io images tag hub.kubesphere.com.cn/etcd:v3.5.15 docker.io/library/etcd:v3.5.15 2>/dev/null || true
ctr -n k8s.io images tag flannel/flannel:v0.27.4 docker.io/flannel/flannel:v0.27.4 2>/dev/null || true
ctr -n k8s.io images tag flannel/flannel-cni-plugin:v1.7.1-flannel1 docker.io/flannel/flannel-cni-plugin:v1.7.1-flannel1 2>/dev/null || true
N_IMG=$(ctr -n k8s.io images ls | grep -cE 'kube-apiserver|kube-proxy|pause|coredns|etcd|flannel')
[ "$N_IMG" -ge 7 ] || die "导入镜像数量不足($N_IMG), 拒绝继续"
echo '== 112 镜像清单:'
ctr -n k8s.io images ls | grep -E 'kube-apiserver|kube-controller|kube-scheduler|kube-proxy|pause|coredns|etcd|flannel'

# ===== flannel chart 放入 kk binary_dir(解包后需重新放置) =====
mkdir -p /data1/bundle/demo/kubekey/kubekey/cni/flannel
cp /data1/flannel-chart.tgz /data1/bundle/demo/kubekey/kubekey/cni/flannel/flannel-v0.27.4.tgz
[ -f /data1/bundle/demo/kubekey/kubekey/cni/flannel/flannel-v0.27.4.tgz ] || die "flannel chart 放置失败"

# ===== 113 节点: 分发物料并初始化 =====
CTBASE=$(basename "$CTARCH")
scp -o StrictHostKeyChecking=no /data1/k8s-v1.28.15-images.tar /data1/flannel-images.tar root@10.100.11.113:/data1/ || die "113 镜像分发失败"
scp -o StrictHostKeyChecking=no "$CTARCH" "root@10.100.11.113:/data1/$CTBASE" || die "113 containerd 分发失败"
scp -o StrictHostKeyChecking=no /data1/node113_setup.sh root@10.100.11.113:/data1/ || die "113 脚本分发失败"
ssh -o StrictHostKeyChecking=no root@10.100.11.113 "sed -i 's/^CTTAR=.*/CTTAR=$CTBASE/' /data1/node113_setup.sh; bash /data1/node113_setup.sh" || die "113 初始化失败"
echo '== 113 初始化完成'

# ===== 产品部署脚本端到端(物料铺设 + kk create + 证书 + 报告) =====
cd /data1/bundle/demo
bash deploy-cluster.sh
RC=$?
echo "== deploy-cluster.sh 退出码: $RC"
date
exit $RC

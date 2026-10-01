#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯离线镜像链路单测(不依赖 Docker/网络, 只测纯函数与产物约定)

覆盖:
  - k8s_image_specs: 控制面/DNS/etcd(library 规范化)/CNI(flannel=ghcr, calico=quay)/HA/存储
  - cluster_images_tar_name: 包名约定(warehouse 与 bundle 内一致)
  - _pull_candidates: 国内源候选与 docker.io 特例(hub 兜底/etcd quay 兜底, 路径不带 library)
  - gen_cluster_config: 离线无 imageRepository + sandbox 三元组 + data_root + config_policy
                       + storage_class 显式输出; 在线(cn)有 imageRepository
  - gen_manifest_sh: CLUSTER_SANDBOX_IMAGE 与 versions.json sandbox_image_tag 一致
用法: python tests/test_offline_images.py  (或 pytest tests/test_offline_images.py)
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "tests"))
import packer  # noqa: E402
import prepare_cluster as prep  # noqa: E402

CATALOG = packer.load_catalog()
CCAT = CATALOG["cluster"]
CHECKS = []


def check(name, cond):
    CHECKS.append((name, bool(cond)))
    print(("  ✓ " if cond else "  ✗ FAIL ") + name)


def test_image_specs_flannel():
    specs = prep.k8s_image_specs(CCAT, "v1.28.15", "flannel")
    must = [
        "registry.k8s.io/kube-apiserver:v1.28.15",
        "registry.k8s.io/kube-proxy:v1.28.15",
        "registry.k8s.io/pause:3.9",
        "registry.k8s.io/coredns/coredns:v1.10.1",
        "registry.k8s.io/dns/k8s-dns-node-cache:1.22.23",
        # docker.io 单路径镜像必须带 library 前缀(containerd 规范化行为)
        "docker.io/library/etcd:v3.5.15",
        # flannel chart 镜像在 ghcr.io, tag 跟随 chart
        "ghcr.io/flannel-io/flannel:v0.27.4",
        "ghcr.io/flannel-io/flannel-cni-plugin:v1.8.0-flannel1",
    ]
    for m in must:
        check("specs 含 %s" % m, m in specs)
    check("specs 无空 tag 条目", all(not s.endswith(":") for s in specs))
    check("specs 无 docker.io/etcd 旧写法", "docker.io/etcd:" not in " ".join(specs))


def test_image_specs_calico_ha_storage():
    specs = prep.k8s_image_specs(CCAT, "v1.28.15", "calico",
                                 ha_type="kube-vip",
                                 storage={"localpv_enabled": True, "nfs_enabled": True})
    check("calico 镜像走 quay.io", "quay.io/calico/node:v3.28.5" in specs)
    check("kube-vip 随 HA 出现", "docker.io/plndr/kube-vip:v0.7.2" in specs)
    check("localpv 镜像随存储出现", "docker.io/openebs/dynamic-localpv-provisioner:4.4.0" in specs)
    check("nfs provisioner 随存储出现",
          "registry.k8s.io/sig-storage/nfs-subdir-external-provisioner:v4.0.18" in specs)
    check("flannel 镜像不在 calico specs", not any("flannel" in s for s in specs))


def test_tar_name():
    check("包名约定", prep.cluster_images_tar_name("v1.28.15", "flannel") == "k8s-1.28.15-flannel-amd64-images.tar")


def test_pull_candidates():
    c = prep._pull_candidates("docker.io/library/etcd:v3.5.15")
    check("etcd 有 hub 兜底且不带 library", "hub.kubesphere.com.cn/etcd:v3.5.15" in c)
    check("etcd 有 quay 兜底", "quay.io/coreos/etcd:v3.5.15" in c)
    check("etcd 官方源在最后", c[-1] == "docker.io/library/etcd:v3.5.15")
    c2 = prep._pull_candidates("registry.k8s.io/pause:3.9")
    check("k8s 镜像首选 daocloud 前缀", c2[0] == "m.daocloud.io/registry.k8s.io/pause:3.9")
    check("k8s 镜像官方源兜底", c2[-1] == "registry.k8s.io/pause:3.9")
    check("k8s 镜像不含 hub 兜底", not any("hub.kubesphere" in x for x in c2))
    check("候选无双前缀", not any("k8s.io/registry.k8s.io/" in x or "docker.io/docker.io/" in x for x in c + c2))


def test_gen_cluster_config_offline():
    cfg = {"project": "t", "arch": "amd64",
           "cluster": {"enabled": True, "kube_version": "v1.28.15", "mode": "cache",
                       "cni_type": "flannel", "proxy_mode": "iptables", "ha_type": "local",
                       "pod_cidr": "10.233.64.0/18", "service_cidr": "10.233.0.0/18",
                       "timezone": "Asia/Shanghai", "os_distros": [], "roles": {},
                       "components": {"containerd_root": "/data1/containerd", "static_binary": True},
                       "storage": {}, "kubelet": {}, "ntp": {}}}
    y = packer.gen_cluster_config(cfg, CATALOG)
    check("离线无 imageRepository", "imageRepository" not in y)
    check("sandbox 三元组 registry", 'registry: "registry.k8s.io"' in y)
    check("sandbox 三元组 tag", 'tag: "3.9"' in y)
    check("data_root 直写(非 config.root)", 'data_root: "/data1/containerd"' in y and "config:" not in y)
    check("config_policy=overwrite", "config_policy: overwrite" in y)
    check("storage_class 显式关闭", "  storage_class:\n    local:\n      enabled: false" in y)


def test_gen_cluster_config_online_cn():
    cfg = {"project": "t", "arch": "amd64",
           "cluster": {"enabled": True, "kube_version": "v1.28.15", "mode": "online", "zone": "cn",
                       "cni_type": "flannel", "proxy_mode": "iptables", "ha_type": "local",
                       "pod_cidr": "10.233.64.0/18", "service_cidr": "10.233.0.0/18",
                       "timezone": "Asia/Shanghai", "os_distros": [], "roles": {},
                       "components": {}, "storage": {}, "kubelet": {}, "ntp": {}}}
    y = packer.gen_cluster_config(cfg, CATALOG)
    check("在线cn有 imageRepository=hub", 'imageRepository: "hub.kubesphere.com.cn"' in y)
    check("在线 zone=cn", "zone: \"cn\"" in y)
    check("在线 fetch=true", "fetch: true" in y)
    check("在线无 config_policy(不覆盖节点配置)", "config_policy" not in y)


def test_manifest_sandbox_matches_catalog():
    ver = CATALOG["cluster"]["versions"]["v1.28.15"]
    expect = "CLUSTER_SANDBOX_IMAGE='registry.k8s.io/pause:%s'" % ver.get("sandbox_image_tag")
    cfg = {"project": "t", "arch": "amd64", "services": [],
           "deploy_dir": "/data/middleware", "docker_data_root": "/data/docker",
           "registry_mirrors": [],
           "ports": {}, "secrets": {}, "db": {}, "servers": [],
           "cluster": {"enabled": True, "kube_version": "v1.28.15", "mode": "cache",
                       "cni_type": "flannel", "proxy_mode": "iptables", "ha_type": "local",
                       "pod_cidr": "", "service_cidr": "", "timezone": "Asia/Shanghai",
                       "os_distros": [], "nodes": [{"name": "m", "ip": "1.2.3.4", "role": "control-plane"}],
                       "components": {"etcd_dir": "/data1/etcd"},
                       "kubelet": {"root_dir": "/data1/kubelet"},
                       "storage": {}, "upgrade_to": ""}}
    m = packer.gen_manifest_sh(cfg, CATALOG, [], [])
    check("manifest CLUSTER_SANDBOX_IMAGE 与目录一致", expect in m)
    check("manifest 导出 CLUSTER_ETCD_DATA_DIR(卸载兜底)", "CLUSTER_ETCD_DATA_DIR='/data1/etcd'" in m)
    check("manifest 导出 CLUSTER_KUBELET_ROOT_DIR(卸载兜底)", "CLUSTER_KUBELET_ROOT_DIR='/data1/kubelet'" in m)
    u = (BASE / "server" / "uninstall-cluster.sh").read_text(encoding="utf-8")
    check("卸载脚本走 kk delete cluster --all", "delete cluster" in u and "--all" in u)
    check("卸载脚本无 kk v4 不存在的 --yes 传参", 'kk delete cluster "${KK_ARGS[@]}"' in u)
    check("卸载脚本清理证书续期 cron", "kk-certs-renew.sh" in u)
    check("卸载脚本清理孤挂载(umount_tree)", "umount_tree" in u)
    d = (BASE / "server" / "deploy-cluster.sh").read_text(encoding="utf-8")
    check("deploy scp 端口用大写 -P(scp 语义)", "scp $SCP_OPTS" in d and "scp $SSH_OPTS" not in d)
    check("deploy 补非登录 shell PATH", 'export PATH="/usr/local/bin:/usr/local/sbin:$PATH"' in d)
    up = packer.gen_upgrade_sh(cfg)
    check("upgrade scp 端口用大写 -P", "scp $SCP_OPTS" in up and "scp $SSH_OPTS" not in up)
    check("upgrade 补非登录 shell PATH", 'export PATH="/usr/local/bin:/usr/local/sbin:$PATH"' in up)


def main():
    for fn in (test_image_specs_flannel, test_image_specs_calico_ha_storage, test_tar_name,
               test_pull_candidates, test_gen_cluster_config_offline,
               test_gen_cluster_config_online_cn, test_manifest_sandbox_matches_catalog):
        print(fn.__name__ + ":")
        fn()
    failed = [n for n, ok in CHECKS if not ok]
    print(("== 单测通过 (%d 项) ==" if not failed else "== 单测失败: %s ==") % (len(CHECKS) if not failed else failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

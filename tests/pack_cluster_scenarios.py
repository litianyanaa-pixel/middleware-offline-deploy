#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集群打包场景补充测试(冒烟之外的三种形态)

复用 tests/pack_cluster_smoke.py 的假物料机制, 额外覆盖:
  场景A 混合包: 集群 + nginx 同包(断言 compose 与 cluster/ 共存, deploy.sh 非纯集群薄壳)
  场景B 升级包: upgrade_to 目标版本三件套 + upgrade-cluster.sh 随包
  场景C 多节点HA: 2 控制面 + 1 worker + 1 registry(kube-vip VIP / registry 入 inventory)
跑完自动清理假物料与产物。用法: python tests/pack_cluster_scenarios.py
"""
import json
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "tests"))
import packer  # noqa: E402
from pack_cluster_smoke import make_fake_materials, clean_fake_materials, FAKE_ROOT  # noqa: E402

K8S_VER = "v1.34.11"
UP_VER = "v1.33.13"
ARCH = "amd64"


def put_upgrade_fake():
    for b in ("kubeadm", "kubelet", "kubectl"):
        p = FAKE_ROOT / f"kube/{UP_VER}/{ARCH}/{b}"
        if not (p.is_file() and p.stat().st_size > 0):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"fake-up-" + b.encode())
    # 升级目标版本的离线镜像包(cluster_materials 会校验)
    up_img = FAKE_ROOT / ("images/k8s-%s-flannel-%s-images.tar" % (UP_VER.lstrip('v'), ARCH))
    if not up_img.is_file():
        up_img.parent.mkdir(parents=True, exist_ok=True)
        up_img.write_bytes(b"fake-up-images")


def base_cfg(services=None, upgrade_to="", roles=None, servers=None, ha="local"):
    return {
        "project": "scen", "arch": ARCH, "lang": "zh",
        "services": services or [],
        "cluster": {"enabled": True, "kube_version": K8S_VER, "mode": "cache",
                    "cni_type": "flannel", "proxy_mode": "iptables",
                    "pod_cidr": "10.233.64.0/18", "service_cidr": "10.233.0.0/18",
                    "timezone": "Asia/Shanghai", "os_distros": ["ubuntu"],
                    "ha_type": ha, "ha_vip": "10.233.255.100" if ha != "local" else "",
                    "upgrade_to": upgrade_to, "roles": roles or {}},
        "servers": servers or [],
        "deploy_dir": "/data/middleware", "docker_data_root": "/data/docker",
        "registry_mirrors": [], "ports": ({"nginx": "18080"} if services else {}),
        "secrets": {}, "db": {},
    }


def pack_bundle(cfg):
    """打包并返回 (bundle_path, result); 调用方负责清理返回的 out_dir(通过 bundle.parent)"""
    out_dir = Path(tempfile.mkdtemp(prefix="cluster-scen-"))
    result = packer.pack(json.loads(json.dumps(cfg)), packer.load_catalog(), out_dir=out_dir)
    bundle = Path(result["path"])

    def read(n):
        with tarfile.open(bundle) as tf:
            return tf.extractfile(
                next(m for m in tf.getmembers() if m.name.split("/", 1)[1] == n)).read().decode("utf-8")

    def names():
        with tarfile.open(bundle) as tf:
            return {m.name.split("/", 1)[1] for m in tf.getmembers() if "/" in m.name}
    return bundle, names, read, result


def main():
    make_fake_materials()
    put_upgrade_fake()
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(("  ✓ " if cond else "  ✗ FAIL ") + name)

    # ---- 场景A 混合包: 集群 + nginx ----
    print("场景A 混合包(集群+nginx):")
    bundle, names, read, result = pack_bundle(base_cfg(services=["nginx"],
        roles={"0": "control-plane"},
        servers=[{"name": "m1", "user": "root", "ip": "192.168.10.11", "ssh": 22, "pass": "p"}]))
    try:
        ns = names()
        check("cluster/ 与 compose 同包", "cluster/inventory.yaml" in ns and "docker-compose.yml" in ns)
        check("deploy.sh 非纯集群薄壳(先集群后中间件)",
              "deploy-cluster.sh" in read("deploy.sh") and "docker" in read("deploy.sh").lower())
        check("nginx 镜像随包", any(n.startswith("images/") for n in ns))
    finally:
        shutil.rmtree(bundle.parent, ignore_errors=True)

    # ---- 场景B 升级包 ----
    print("场景B 升级包(upgrade_to=%s):" % UP_VER)
    bundle, names, read, _ = pack_bundle(base_cfg(upgrade_to=UP_VER,
        roles={"0": "control-plane"},
        servers=[{"name": "m1", "user": "root", "ip": "192.168.10.11", "ssh": 22, "pass": "p"}]))
    try:
        ns = names()
        check("目标版本三件套随包",
              all("cluster/upgrade/%s/%s/%s" % (UP_VER, ARCH, b) in ns for b in ("kubeadm", "kubelet", "kubectl")))
        check("upgrade-cluster.sh 随包且指向 KK_HOME", "upgrade-cluster.sh" in ns
              and "KK_HOME" in read("upgrade-cluster.sh"))
    finally:
        shutil.rmtree(bundle.parent, ignore_errors=True)

    # ---- 场景C 多节点HA(2控制面+1worker+1registry) ----
    print("场景C 多节点HA(2控制面+1worker+1registry):")
    bundle, names, read, _ = pack_bundle(base_cfg(ha="kube-vip",
        roles={"0": "control-plane", "1": "control-plane", "2": "worker", "3": "registry"},
        servers=[{"name": "cp%d" % i, "user": "root", "ip": "192.168.10.1%d" % i, "ssh": 22, "pass": "p"}
                 for i in (1, 2, 3, 4)]))
    try:
        inv = read("cluster/inventory.yaml")
        check("inventory 含两控制面", inv.count("        - cp1") and inv.count("        - cp2"))
        check("inventory 含 registry 组", "registry:\n      hosts:" in inv)
        cfg_yaml = read("cluster/config.yaml")
        check("config 为 kube-vip HA", "kube-vip" in cfg_yaml and "10.233.255.100" in cfg_yaml)
        check("manifest 四节点数组", "cp4" in read("manifest.sh"))
    finally:
        shutil.rmtree(bundle.parent, ignore_errors=True)

    clean_fake_materials()
    failed = [n for n, ok in checks if not ok]
    print(("== 场景测试通过 (%d 项) ==" if not failed else "== 场景测试失败: %s ==")
          % (len(checks) if not failed else failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

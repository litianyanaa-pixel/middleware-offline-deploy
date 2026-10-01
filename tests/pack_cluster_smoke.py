#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集群离线包打包冒烟回归测试

自备假物料 -> 调 packer.pack() 打纯集群包(cache 模式) -> 断言产物结构:
  - deploy.sh 为纯集群薄壳
  - cluster/kk 具备可执行权限(tar 内 mode 755)
  - cluster/inventory.yaml 含 etcd 组
  - manifest.sh 含 CLUSTER_ENABLED/CLUSTER_NODES
  - cluster/cache.sha256 清单存在且覆盖 kube 三件套
  - HA/升级产物按配置出现
跑完自动清理假物料与产物。用法: python tests/pack_cluster_smoke.py
"""
import io
import json
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import packer  # noqa: E402

K8S_VER = "v1.34.11"
ARCH = "amd64"
FAKE_ROOT = BASE / "warehouse" / "cluster"


def make_fake_materials():
    """生成最小假物料(不影响真实物料; 真实物料存在时跳过覆盖)"""
    def put(rel, content, binary=False):
        p = FAKE_ROOT / rel
        if p.is_file() and p.stat().st_size > 0:
            return   # 保留真实物料
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if binary else content.encode("utf-8"))
    put("kk/v4.0.7-xc1/amd64/kk", "#!/bin/sh\necho fake-kk\n", binary=True)
    for b in ("kubeadm", "kubelet", "kubectl"):
        put(f"kube/{K8S_VER}/{ARCH}/{b}", f"fake-{b}", binary=True)
    put(f"etcd/v3.6.5/{ARCH}/etcd-v3.6.5-linux-{ARCH}.tar.gz", "fake", binary=True)
    put(f"cni/v1.9.1/{ARCH}/cni-plugins-linux-{ARCH}-v1.9.1.tgz", "fake", binary=True)
    put(f"helm/v3.18.5/{ARCH}/helm-v3.18.5-linux-{ARCH}.tar.gz", "fake", binary=True)
    put(f"crictl/v1.34.0/{ARCH}/crictl-v1.34.0-linux-{ARCH}.tar.gz", "fake", binary=True)
    put(f"containerd/v2.2.6/{ARCH}/containerd-2.2.6-linux-{ARCH}.tar.gz", "fake", binary=True)
    put(f"runc/v1.3.6/{ARCH}/runc.{ARCH}", "fake", binary=True)
    put("os/ubuntu/amd64/socat_1.7.4.1-3ubuntu4_amd64.deb", "fake", binary=True)
    # 纯离线新物料: 镜像包 + CNI chart(版本跟随 versions.json); cni_type 与冒烟配置同为 flannel
    cni_ver = ((packer.load_catalog()["cluster"]["versions"][K8S_VER].get("cni_versions") or {})
               .get("flannel", ""))
    put(f"images/k8s-{K8S_VER.lstrip('v')}-flannel-{ARCH}-images.tar", b"fake", binary=True)
    if cni_ver:
        put(f"charts/flannel/flannel-{cni_ver}.tgz", b"fake", binary=True)


def clean_fake_materials():
    """仅删除冒烟测试创建的假物料(内容以 fake 开头的文件), 真实物料不动"""
    for p in FAKE_ROOT.rglob("*"):
        if p.is_file() and p.name != "README.md":
            try:
                if p.read_bytes().startswith(b"fake"):
                    p.unlink()
            except OSError:
                pass
    for p in sorted(FAKE_ROOT.rglob("*"), reverse=True):
        if p.is_dir() and not any(p.iterdir()) and p.name != "cluster":
            p.rmdir()


def main():
    make_fake_materials()
    catalog = packer.load_catalog()
    cfg = {
        "project": "smokecluster", "arch": ARCH, "lang": "zh", "services": [],
        "cluster": {"enabled": True, "kube_version": K8S_VER, "mode": "cache",
                    "cni_type": "flannel", "proxy_mode": "iptables",
                    "pod_cidr": "10.233.64.0/18", "service_cidr": "10.233.0.0/18",
                    "timezone": "Asia/Shanghai", "os_distros": ["ubuntu"],
                    "ha_type": "local", "ha_vip": "", "upgrade_to": "",
                    "roles": {"0": "control-plane", "1": "worker"}},
        "servers": [
            {"name": "master1", "user": "root", "ip": "192.168.10.11", "ssh": 22, "pass": "pw1"},
            {"name": "worker1", "user": "root", "ip": "192.168.10.12", "ssh": 22, "pass": "pw2"},
        ],
        "deploy_dir": "/data/middleware", "docker_data_root": "/data/docker",
        "registry_mirrors": [], "ports": {}, "secrets": {}, "db": {},
    }
    out_dir = Path(tempfile.mkdtemp(prefix="cluster-smoke-"))
    result = packer.pack(json.loads(json.dumps(cfg)), catalog, out_dir=out_dir)
    bundle = Path(result["path"])
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(("  ✓ " if cond else "  ✗ FAIL ") + name)

    with tarfile.open(bundle) as tf:
        names = {m.name.split("/", 1)[1] for m in tf.getmembers() if "/" in m.name}
        modes = {m.name.split("/", 1)[1]: format(m.mode, "o") for m in tf.getmembers() if "/" in m.name}
        read = lambda n: tf.extractfile(
            next(m for m in tf.getmembers() if m.name.split("/", 1)[1] == n)).read().decode("utf-8")

        check("产物标注 cluster=true", result.get("cluster") is True)
        check("deploy.sh 为纯集群薄壳", "deploy-cluster.sh" in read("deploy.sh"))
        check("cluster/kk 权限位 755", modes.get("cluster/kk") == "755")
        inv = read("cluster/inventory.yaml")
        check("inventory 含 etcd 组", "    etcd:\n      hosts:" in inv)
        check("inventory 含控制面与工作节点", "        - master1" in inv and "        - worker1" in inv)
        check("manifest 含 CLUSTER_ENABLED", "CLUSTER_ENABLED=1" in read("manifest.sh"))
        check("manifest 含双节点数组", "CLUSTER_NODES=(" in read("manifest.sh"))
        check("cache.sha256 覆盖 kube 三件套",
              all(("cache/kube/%s/%s/%s" % (K8S_VER, ARCH, b)) in read("cluster/cache.sha256")
                  for b in ("kubeadm", "kubelet", "kubectl")))
        cfg_yaml = read("cluster/config.yaml")
        check("config 含 HA 端点(local)", "type: \"local\"" in cfg_yaml)
        check("uninstall-cluster.sh 随包", "uninstall-cluster.sh" in names)
        check("OS 依赖包在包内", any(n.startswith("cluster/os/ubuntu/") for n in names))

    shutil.rmtree(out_dir, ignore_errors=True)
    clean_fake_materials()
    failed = [n for n, ok in checks if not ok]
    print(("== 冒烟通过 (%d 项) ==" if not failed else "== 冒烟失败: %s ==") % (len(checks) if not failed else failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

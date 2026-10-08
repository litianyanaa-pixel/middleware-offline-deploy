#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集群全生命周期回归测试(本地可跑, 不依赖 Docker/网络/真机)

补 test_offline_images.py 之外的生命周期分支:
  - gen_cluster_config: 自定义 etcd/kubelet 数据目录直写(卸载兜底的前提)
  - gen_manifest_sh:    CLUSTER_UPGRADE_TO / CLUSTER_CERTS_RENEW_CRON 导出
  - uninstall-cluster.sh: --keep-cri 分支(--set 三连) / --with-data / safe_dir 防误删 / 未知参数拒绝
  - gen_upgrade_sh:     升级脚本关键内容(kk upgrade --with-kubernetes / 镜像导入 / 铺缓存 / sandbox 改写)
  - deploy-cluster.sh:  artifact 分支(md5 校验) / 幂等指引 / deb+rpm 双分支
  - 三个 shell 脚本 bash -n 语法门禁
用法: python tests/test_cluster_lifecycle.py  (或 pytest tests/test_cluster_lifecycle.py)
"""
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "tests"))
import packer  # noqa: E402

CATALOG = packer.load_catalog()
CCAT = CATALOG["cluster"]
CHECKS = []


def check(name, cond):
    CHECKS.append((name, bool(cond)))
    print(("  ✓ " if cond else "  ✗ FAIL ") + name)


def _base_cluster(**over):
    cl = {"enabled": True, "kube_version": "v1.28.15", "mode": "cache", "zone": "",
          "cni_type": "flannel", "proxy_mode": "iptables", "ha_type": "local",
          "pod_cidr": "10.233.64.0/18", "service_cidr": "10.233.0.0/18",
          "timezone": "Asia/Shanghai", "os_distros": [],
          "roles": {"0": "control-plane"},
          "components": {}, "storage": {}, "kubelet": {}, "ntp": {}}
    cl.update(over)
    return {"project": "t-online", "arch": "amd64", "services": [], "cluster": cl,
            "servers": [{"name": "m1", "user": "root", "ip": "10.0.0.1", "ssh": 22}],
            "ports": {}}


def test_online_cn_static_fallback():
    """online+zone=cn 必须自动回退非 static containerd(qingstor 镜像无 static-*, 404 实测),
    offline/cache 与国际源保留 static"""
    cfg = _base_cluster(mode="online", zone="cn", components={"static_binary": True})
    out, _ = packer.validate_config(cfg, CATALOG)
    check("online+cn 回退非 static", out["cluster"]["components"]["static_binary"] is False)
    cfg2 = _base_cluster(mode="cache", components={"static_binary": True})
    out2, _ = packer.validate_config(cfg2, CATALOG)
    check("cache 模式保留 static", out2["cluster"]["components"]["static_binary"] is True)
    cfg3 = _base_cluster(mode="online", zone="intl", components={"static_binary": True})
    out3, _ = packer.validate_config(cfg3, CATALOG)
    check("online 国际源保留 static", out3["cluster"]["components"]["static_binary"] is True)

    y = packer.gen_cluster_config(_base_cluster(mode="online", zone="cn"), CATALOG)
    check("online+cn 不写裸域名 imageRepository", "imageRepository" not in y)
    check("online+cn 不覆盖 sandbox(交 kk 内置 CN 映射)", "sandbox_image:" not in y)
    y2 = packer.gen_cluster_config(_base_cluster(mode="cache"), CATALOG)
    check("offline 模式保留 sandbox 覆盖", 'registry: "registry.k8s.io"' in y2)


def test_config_custom_dirs():
    """自定义 etcd/kubelet 目录必须直写进 config.yaml, uninstall 兜底依赖这两个值"""
    cfg = _base_cluster(components={"etcd_dir": "/data1/etcd", "containerd_root": "/data1/containerd"},
                        kubelet={"root_dir": "/data1/kubelet"})
    y = packer.gen_cluster_config(cfg, CATALOG)
    check("etcd 自定义目录写入 env.data_dir", '  etcd:' in y and '    env:' in y and '      data_dir: "/data1/etcd"' in y)
    check("kubelet 自定义目录写入 root-dir", '"root-dir": "/data1/kubelet"' in y)
    check("containerd data_root 直写", 'data_root: "/data1/containerd"' in y)
    # 目录为空时不产生空值行(kk 会用默认路径)
    y2 = packer.gen_cluster_config(_base_cluster(), CATALOG)
    check("未配置时不残留 data_dir/root-dir", "data_dir:" not in y2 and "root-dir:" not in y2)


def test_config_etcd_env_and_advance():
    """etcd 数据目录/高级参数必须挂在 etcd.env 下(v4 只读 .etcd.env.*, 顶层 data_dir 是旧版键会被忽略)"""
    cfg = _base_cluster(components={"etcd_dir": "/data1/etcd",
                                    "etcd_env": ["heartbeat_interval=250", "election_timeout=5000",
                                                 "compaction_retention=8", "snapshot_count=10000",
                                                 "quota_backend_bytes=8589934592",
                                                 "max_request_bytes=1572864", "max_snapshots=5",
                                                 "max_wals=5", "log_level=info"]})
    y = packer.gen_cluster_config(cfg, CATALOG)
    check("env 块包含 data_dir", "    env:\n      data_dir: \"/data1/etcd\"" in y)
    for k, v in (("heartbeat_interval", 250), ("election_timeout", 5000), ("compaction_retention", 8),
                 ("snapshot_count", 10000), ("quota_backend_bytes", 8589934592),
                 ("max_request_bytes", 1572864), ("max_snapshots", 5), ("max_wals", 5)):
        check("env.%s=%s" % (k, v), ("      %s: %d" % (k, v)) in y)
    check("env.log_level", '      log_level: "info"' in y)
    check("顶层无 etcd.data_dir 残留", "\n    data_dir:" not in y)
    _, warns = packer.validate_config(cfg, CATALOG)
    check("validate 不告警", not warns)


def test_config_ha_vip_and_backup():
    """kube-vip 必须写 kube_vip.address(网卡自动发现依据); kubeadm 备份目录/证书续期显式落 config"""
    y = packer.gen_cluster_config(_base_cluster(ha_type="kube-vip", ha_vip="10.0.0.100"), CATALOG)
    check("kube-vip 写 host", '      host: "10.0.0.100"' in y)
    check("kube-vip 写 kube_vip.address", "      kube_vip:\n        address: \"10.0.0.100\"" in y)
    y2 = packer.gen_cluster_config(_base_cluster(ha_type="haproxy", ha_vip="127.0.0.2"), CATALOG)
    check("haproxy 写 host 不写 kube_vip", 'host: "127.0.0.2"' in y2 and "kube_vip:" not in y2)
    y3 = packer.gen_cluster_config(_base_cluster(), CATALOG)
    check("默认 backup 目录(/etc/kubekey/backup/kubernetes)", 'kubeadm_config_dir: "/etc/kubekey/backup/kubernetes"' in y3)
    y4 = packer.gen_cluster_config(_base_cluster(kubeadm_config_dir="/data1/kk-backup"), CATALOG)
    check("自定义备份目录", 'kubeadm_config_dir: "/data1/kk-backup"' in y4)
    y5 = packer.gen_cluster_config(_base_cluster(kubeadm_config_dir="", certs_renew=False), CATALOG)
    check('空串禁用备份 + certs.renew: false', 'kubeadm_config_dir: ""' in y5 and "renew: false" in y5)


def test_config_cri_cni_dns():
    """container_manager 选择 / CNI 高级(掩码+multus+calico values) / DNS 覆盖必须落到对应键"""
    y = packer.gen_cluster_config(_base_cluster(container_manager="docker"), CATALOG)
    check("docker 运行时写 container_manager", 'container_manager: "docker"' in y)
    check("docker 不写 containerd 子块", "    containerd:" not in y)
    y2 = packer.gen_cluster_config(_base_cluster(ipv4_mask_size=25, multi_cni="multus", multi_cni_tag="v4.3.0",
                                                 calico_values="installation:\n  calicoNetwork:\n    mtu: 1440",
                                                 dns={"coredns_tag": "v1.12.1", "nodelocaldns_enabled": True,
                                                      "nodelocaldns_tag": "1.26.4"}), CATALOG)
    check("ipv4_mask_size 直写", "    ipv4_mask_size: 25" in y2)
    check("multi_cni: multus + multus tag", "    multi_cni: multus" in y2 and 'tag: "v4.3.0"' in y2)
    check("calico values 块透传(helm -f)", "    calico:\n      values: |\n        installation:" in y2)
    check("coredns/nodelocaldns tag 覆盖", 'tag: "v1.12.1"' in y2 and 'tag: "1.26.4"' in y2)
    y3 = packer.gen_cluster_config(_base_cluster(dns={"nodelocaldns_enabled": False}), CATALOG)
    check("nodelocaldns 显式关闭", "    nodelocaldns:\n      enabled: false" in y3)
    check("关闭时不写 nodelocaldns tag", "k8s-dns-node-cache" not in y3)
    # 离线镜像清单跟随 DNS 覆盖/multus(导入镜像必须与 config 生成的 tag 一致)
    prep = packer._prepare_module()
    specs = prep.k8s_image_specs(CCAT, "v1.28.15", "flannel",
                                 dns={"coredns_tag": "v1.12.1", "nodelocaldns_tag": "1.26.4"},
                                 multi_cni="multus", multi_cni_tag="v4.4.0")
    check("镜像清单用覆盖后的 coredns tag", "registry.k8s.io/coredns/coredns:v1.12.1" in specs)
    check("镜像清单用覆盖后的 nodelocaldns tag", "k8s-dns-node-cache:1.26.4" in specs)
    check("multus 镜像入清单", "ghcr.io/k8snetworkplumbingwg/multus-cni:v4.4.0" in specs)
    specs_off = prep.k8s_image_specs(CCAT, "v1.28.15", "flannel", dns={"nodelocaldns_enabled": False})
    check("nodelocaldns 关闭不出镜像", not any("k8s-dns-node-cache" in s for s in specs_off))


def test_config_validation_matrix():
    """新键非法值必须前置拦截(免得到部署时才被 kk 拦下)"""
    def expect_err(name, over):
        try:
            packer.validate_config(_base_cluster(**over), CATALOG)
            check(name, False)
        except packer.PackError:
            check(name, True)
    expect_err("offline+docker 拒绝", {"container_manager": "docker"})
    expect_err("运行时非法值", {"container_manager": "podman"})
    expect_err("etcd 未知键", {"components": {"etcd_env": ["foo=1"]}})
    expect_err("etcd 非整数", {"components": {"etcd_env": ["heartbeat_interval=abc"]}})
    expect_err("mask 越界", {"ipv4_mask_size": 40})
    expect_err("multi_cni 非法", {"multi_cni": "spiderpool"})
    expect_err("calico values 非 YAML", {"calico_values": ": : :"})
    expect_err("备份目录相对路径", {"kubeadm_config_dir": "rel/path"})
    # 在线模式 docker 放行
    out, _ = packer.validate_config(_base_cluster(mode="online", container_manager="docker"), CATALOG)
    check("online+docker 放行", out["cluster"]["container_manager"] == "docker")
    # calico values 仅 calico 生效(其他 CNI 静默忽略+告警)
    out2, w2 = packer.validate_config(_base_cluster(cni_type="flannel", calico_values="a: 1"), CATALOG)
    check("非 calico 忽略 values 并告警", out2["cluster"]["calico_values"] == "" and len(w2) == 1)


def test_manifest_lifecycle_vars():
    """升级目标版本与证书续期 cron 必须导出到 manifest.sh(deploy/upgrade/uninstall 三脚本都读)"""
    cfg = _base_cluster(upgrade_to="v1.29.15", certs_renew_cron="0 0 1 * *",
                        nodes=[{"name": "m", "ip": "1.2.3.4", "role": "control-plane"},
                               {"name": "w", "ip": "5.6.7.8", "role": "worker"}])
    cfg["deploy_dir"] = "/data/middleware"
    cfg["docker_data_root"] = "/data/docker"
    cfg["ports"] = {}
    cfg["secrets"] = {}
    cfg["db"] = {}
    cfg["servers"] = []
    cfg["registry_mirrors"] = []
    m = packer.gen_manifest_sh(cfg, CATALOG, [], [])
    check("manifest 导出 CLUSTER_UPGRADE_TO", "CLUSTER_UPGRADE_TO='v1.29.15'" in m)
    check("manifest 导出 CLUSTER_CERTS_RENEW_CRON", "CLUSTER_CERTS_RENEW_CRON='0 0 1 * *'" in m)
    # 未配置时导出空串(脚本端 ${VAR:-} 判空, 不能缺变量)
    cfg2 = _base_cluster(nodes=[{"name": "m", "ip": "1.2.3.4", "role": "control-plane"}])
    cfg2.update({"deploy_dir": "/data/m", "docker_data_root": "/data/docker",
                 "ports": {}, "secrets": {}, "db": {}, "servers": [],
                 "registry_mirrors": []})
    m2 = packer.gen_manifest_sh(cfg2, CATALOG, [], [])
    check("未配置时导出空 CLUSTER_UPGRADE_TO", "CLUSTER_UPGRADE_TO=''" in m2)
    check("未配置时导出空 CLUSTER_CERTS_RENEW_CRON", "CLUSTER_CERTS_RENEW_CRON=''" in m2)


def test_uninstall_flag_matrix():
    u = (BASE / "server" / "uninstall-cluster.sh").read_text(encoding="utf-8")
    check("--keep-cri 打开 dns/etcd/registry 三删开关",
          "--set delete.dns=true --set delete.etcd=true --set delete.image_registry=true" in u)
    check("--with-data 透传 kk", 'KK_ARGS+=(--with-data)' in u)
    check("safe_dir 防误删(拒绝相对路径/根/..)", "safe_dir()" in u and '*..*' in u)
    check("未知参数拒绝", "未知参数" in u)
    check("自定义 kubelet 目录走 CLUSTER_KUBELET_ROOT_DIR", "CLUSTER_KUBELET_ROOT_DIR" in u)
    check("自定义 etcd 目录走 CLUSTER_ETCD_DATA_DIR", "CLUSTER_ETCD_DATA_DIR" in u)
    check("worker 不清 etcd 自定义目录", '$_role' in u and '"$2"' in u)


def test_upgrade_sh_content():
    up_to = "v1.29.15"
    cfg = _base_cluster(upgrade_to=up_to)
    up = packer.gen_upgrade_sh(cfg)
    check("升级走 kk upgrade cluster --with-kubernetes", "kk upgrade cluster" in up and "--with-kubernetes %s" % up_to in up)
    check("导入目标版本离线镜像(cluster/upgrade)", "cluster/upgrade/images-*.tar" in up)
    check("目标版本三件套铺到 kk 缓存", "kube/%s/amd64" % up_to in up)
    tag = ((CCAT["versions"].get(up_to) or {}).get("sandbox_image_tag") or "")
    check("sandbox 镜像随目标版本改写", tag and ("pause:%s" % tag) in up)
    check("升级后 kubectl get nodes 验证", "kubectl get nodes" in up)
    check("升级脚本带 --all(连带组件)", "--all" in up)
    # 回归: _script 变量 + bash -c 嵌套引号在 sed \\" 转义上两层都炸(真机升级首测发现),
    # 必须走临时文件 bash -s 下发(与 deploy-cluster.sh 同套路); 单引号块能骗过整文件 bash -n,
    # 所以要把内嵌脚本体抠出来单独做语法门禁
    check("升级脚本无 bash -c 嵌套引号块", "_script=" not in up)
    check("远端导入走 bash -s 临时文件", "RUN_SSH \"$1\" 'bash -s' < \"$IMP_SH\"" in up)
    check("crictl 铺缓存连目录层(不带 /.)", 'cp -a cluster/upgrade/cache/. "$KK_CACHE/"' in up)
    bash = _find_working_bash()
    if bash and 'IMP_SH="$(mktemp' in up:
        # 回归: 节点侧脚本体经 heredoc 反转义后才合法, 必须在同一 shell 层落盘后再做语法门禁
        # (真机升级首测炸点: _script 变量 + bash -c 的 sed \\" 转义, 整文件 bash -n 拦不住)
        import os
        seg0 = up.find('IMP_SH="$(mktemp')
        seg1 = up.find('\nUP_EOF', seg0) + len('\nUP_EOF')
        r = subprocess.run([bash, "-c", up[seg0:seg1] + '\ncat "$IMP_SH"'],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            fd, tp = tempfile.mkstemp(suffix=".sh")
            try:
                with os.fdopen(fd, "w", newline="\n") as f:
                    f.write(r.stdout)
                r2 = subprocess.run([bash, "-n", tp], capture_output=True, text=True)
                check("导入脚本体(heredoc 展开后) bash -n 通过", r2.returncode == 0)
            finally:
                os.unlink(tp)
        else:
            check("导入脚本 heredoc 落盘失败", False)


def test_deploy_sh_branches():
    d = (BASE / "server" / "deploy-cluster.sh").read_text(encoding="utf-8")
    check("artifact 分支校验 md5 后传给 kk", "download.artifact_file" in d and "artifact.md5" in d)
    check("artifact md5 不一致即终止", "artifact md5 不一致" in d)
    check("deb 分支(dpkg)", "dpkg -i" in d)
    check("rpm 分支(localinstall)", "localinstall" in d)
    check("幂等指引: 扩容", "kk add nodes" in d)
    check("幂等指引: 升级", "kk upgrade cluster" in d)
    check("幂等指引: 卸载", "./uninstall-cluster.sh" in d)
    check("cache sha256 校验门禁", "cache.sha256" in d)
    check("kubelet 残留 unit 守卫(失败重部署卡死)", "清理残留 kubelet unit" in d)


def _find_working_bash():
    """Windows 下 System32\\bash.exe(WSL) 会遮蔽 Git Bash: 逐候选做 bash -n 冒烟, 首个可用者胜"""
    import os
    import shutil
    import tempfile
    cands = []
    git = shutil.which("git")
    if git:
        cands.append(str(Path(git).resolve().parents[1] / "bin" / "bash.exe"))
    cands.append(r"C:\Program Files\Git\bin\bash.exe")
    w = shutil.which("bash")
    if w:
        cands.append(w)
    for c in cands:
        if not Path(c).is_file():
            continue
        fd, tp = tempfile.mkstemp(suffix=".sh")
        try:
            with os.fdopen(fd, "w", newline="\n") as f:
                f.write("exit 0\n")
            if subprocess.run([c, "-n", tp], capture_output=True).returncode == 0:
                return c
        except OSError:
            continue
        finally:
            try:
                os.unlink(tp)
            except OSError:
                pass
    return None


def test_scripts_bash_syntax():
    bash = _find_working_bash()
    if not bash:
        print("  (跳过: 本机无可用 bash)")
        return
    up = packer.gen_upgrade_sh(_base_cluster(upgrade_to="v1.29.15"))
    with tempfile.TemporaryDirectory() as td:
        gen = Path(td) / "upgrade-cluster.sh"
        gen.write_text(up, encoding="utf-8", newline="\n")
        for f in (BASE / "server" / "deploy-cluster.sh", BASE / "server" / "uninstall-cluster.sh", gen):
            r = subprocess.run([bash, "-n", str(f)], capture_output=True, text=True)
            check("bash -n 语法通过: %s" % f.name, r.returncode == 0)


def main():
    for fn in (test_online_cn_static_fallback, test_config_custom_dirs, test_config_etcd_env_and_advance,
               test_config_ha_vip_and_backup, test_config_cri_cni_dns, test_config_validation_matrix,
               test_manifest_lifecycle_vars, test_uninstall_flag_matrix,
               test_upgrade_sh_content, test_deploy_sh_branches, test_scripts_bash_syntax):
        print(fn.__name__ + ":")
        fn()
    failed = [n for n, ok in CHECKS if not ok]
    print(("== 单测通过 (%d 项) ==" if not failed else "== 单测失败: %s ==") % (len(CHECKS) if not failed else failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

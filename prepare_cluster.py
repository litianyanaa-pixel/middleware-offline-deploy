#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""K8s 集群物料准备脚本 (Windows 原生 · 纯标准库)

打包器(packer.py)跑在 Windows 上, 物料准备同样不需要离开 Windows:
    python k8s/prepare_materials.py --check                 # 物料盘点
    python k8s/prepare_materials.py --download              # 下载缺失二进制(多源多连接)
    python k8s/prepare_materials.py --download --kube-version v1.33.13   # 指定版本
    python k8s/prepare_materials.py --download --upgrade-to v1.34.12     # 连升级目标版本一起下
    python k8s/prepare_materials.py --os-packages ubuntu    # 容器提取 OS 依赖包(需 Docker Desktop)
    python k8s/prepare_materials.py --artifact-export       # 生成 kk artifact 导出物料 + 一键 .bat
    python k8s/prepare_materials.py --kk-build              # 生成 kk 构建 .bat(调本机 Go 交叉编译)

下载源均为国内可达(逐源自动切换, 大文件 4 连接分段并逐段校验):
  kube 二进制   dl.k8s.io (微软国内 CDN, 实测 MB/s 级)
  etcd          mirrors.huaweicloud.com -> GitHub 镜像
  helm          get.helm.sh -> mirrors.huaweicloud.com
  containerd    mirrors.huaweicloud.com -> GitHub 镜像
  crictl        mirrors.huaweicloud.com -> GitHub 镜像
  cni-plugins   GitHub 镜像(gh-proxy.com 等)
也可被 packer.py 动态导入: ensure_materials(...) 在打包时自动补齐缺失物料。
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CATALOG_PATH = BASE_DIR / "versions.json"
ARCH = "amd64"          # 集群当前仅开放 amd64

# GitHub 直连不可达时的镜像候选(依次轮询, 单源分段失败自动换源重试)。
# 可在 versions.json cluster.download_mirrors.github_mirrors 自定义覆盖。
GH_MIRRORS_DEFAULT = [
    "https://gh-proxy.com/https://github.com",
    "https://ghfast.top/https://github.com",
    "https://ghproxy.net/https://github.com",
]
GH_MIRRORS = list(GH_MIRRORS_DEFAULT)


def _load_github_mirrors():
    """versions.json cluster.download_mirrors.github_mirrors 覆盖默认镜像列表"""
    try:
        ccat = (load_catalog() or {}).get("cluster") or {}
        mirrors = ((ccat.get("download_mirrors") or {}).get("github_mirrors")) or []
        if mirrors:
            GH_MIRRORS[:] = [str(m).rstrip("/") for m in mirrors]
    except Exception:
        pass


_load_github_mirrors()

# 每组件: 主源(国内可达)在前, 备源在后。url 模板变量 {ver}/{ver_nov}/{arch}
HUAWEI = "https://mirrors.huaweicloud.com"
COMPONENT_SOURCES = {
    "etcd": [
        HUAWEI + "/etcd/{ver}/etcd-{ver}-linux-{arch}.tar.gz",
        "GH/etcd-io/etcd/releases/download/{ver}/etcd-{ver}-linux-{arch}.tar.gz",
    ],
    "cni_plugins": [
        "GH/containernetworking/plugins/releases/download/{ver}/cni-plugins-linux-{arch}-{ver}.tgz",
    ],
    "helm": [
        "https://get.helm.sh/helm-{ver}-linux-{arch}.tar.gz",
        HUAWEI + "/helm/{ver}/helm-{ver}-linux-{arch}.tar.gz",
    ],
    "crictl": [
        HUAWEI + "/cri-tools/{ver}/crictl-{ver}-linux-{arch}.tar.gz",
        "GH/kubernetes-sigs/cri-tools/releases/download/{ver}/crictl-{ver}-linux-{arch}.tar.gz",
    ],
    "containerd": [
        HUAWEI + "/containerd/{ver}/containerd-{ver_nov}-linux-{arch}.tar.gz",
        "GH/containerd/containerd/releases/download/{ver}/containerd-{ver_nov}-linux-{arch}.tar.gz",
    ],
    "runc": [
        "GH/opencontainers/runc/releases/download/{ver}/runc.{arch}",
    ],
}


def info(msg):  print(f"[INFO] {msg}")
def warn(msg):  print(f"[WARN] {msg}", file=sys.stderr)
def die(msg):   print(f"[FAIL] {msg}", file=sys.stderr); sys.exit(1)


def load_catalog():
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def cluster_kube_version(ccat, kube_version=""):
    vers = ccat.get("versions") or {}
    if kube_version and kube_version in vers:
        return kube_version
    for v, m in vers.items():
        if m.get("recommended"):
            return v
    return sorted(vers)[0]


def has_curl():
    return shutil.which("curl") is not None


def content_length(url):
    try:
        out = subprocess.run(["curl", "-sIL", "-m", "20", url],
                             capture_output=True, text=True, timeout=30).stdout
        for line in out.splitlines():
            if line.lower().startswith("content-length:"):
                return int(line.split(":", 1)[1].strip())
    except Exception:
        pass
    return 0


def fetch_simple(url, out: Path, timeout=1800):
    out.parent.mkdir(parents=True, exist_ok=True)
    if has_curl():
        r = subprocess.run(["curl", "-sL", "-m", str(timeout), "-o", str(out), url])
        if r.returncode == 0 and out.is_file() and out.stat().st_size > 0:
            return True
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp, open(out, "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        return out.stat().st_size > 0
    except Exception as e:
        warn(f"urllib 下载失败 {url}: {e}")
        return False


def fetch_parallel(url, out: Path, size: int, n=4, tick=None):
    """分段并行下载(curl -r), 逐段校验大小; tick(done_bytes) 汇报累计进度"""
    out.parent.mkdir(parents=True, exist_ok=True)
    parts = []
    chunk = (size + n - 1) / n
    for i in range(n):
        s = int(i * chunk); e = int(min((i + 1) * chunk - 1, size - 1))
        parts.append([out.with_name(out.name + f".p{i}"), s, e, e - s + 1, 0])  # [path, s, e, want, got]
    lock = threading.Lock()

    def report():
        if tick:
            tick(sum(p[4] for p in parts))

    for rnd in range(3):
        fails = []
        def work(p):
            path, s, e, want, _ = p
            for _ in range(3):
                r = subprocess.run(["curl", "-sL", "-m", "900", "-r", f"{s}-{e}", "-o", str(path), url])
                got = path.stat().st_size if path.is_file() else 0
                if r.returncode == 0 and got == want:
                    p[4] = got
                    with lock:
                        report()
                    return
            fails.append(path)
        ts = [threading.Thread(target=work, args=(p,)) for p in parts]
        for t in ts: t.start()
        for t in ts: t.join()
        if not fails:
            total = sum(p[4] for p in parts)
            if total == size:
                with open(out, "wb") as fo:
                    for p in parts:
                        fo.write(p[0].read_bytes())
                for p in parts:
                    p[0].unlink()
                if tick:
                    tick(size)
                return True
        for p in parts:
            if p[0].exists():
                p[0].unlink()
            p[4] = 0
        warn(f"分段下载不完整, 第 {rnd+2} 轮重试…")
    return False


def is_tar_valid(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    if path.suffix in (".tgz", ".gz"):
        import tarfile
        try:
            with tarfile.open(path) as tf:
                tf.getmembers()
            return True
        except Exception:
            return False
    return True


def expand_urls(tpl):
    """GH/ 前缀表示 github 仓库路径, 展开为全部镜像候选; 其余为主源直连"""
    urls = []
    if tpl.startswith("GH/"):
        for m in GH_MIRRORS:
            urls.append(m + "/" + tpl[3:])
    urls.append(tpl)
    return urls


def component_jobs(ccat, kube_ver, arch, upgrade_to=""):
    """按 versions.json 推导下载任务: [(out_path, url模板列表, 校验函数, 显示名)]"""
    jobs = []
    comps = ((ccat.get("versions") or {}).get(kube_ver) or {}).get("components") or {}
    kube_dir = BASE_DIR / ("warehouse/cluster/kube/%s/%s" % (kube_ver, arch))
    for b in ("kubeadm", "kubelet", "kubectl"):
        jobs.append((kube_dir / b, [f"https://dl.k8s.io/release/{kube_ver}/bin/linux/{arch}/{b}"],
                     lambda p: p.is_file() and p.stat().st_size > 1_000_000, b))
    for key in ("etcd", "cni_plugins", "helm", "crictl", "containerd", "runc"):
        comp = comps.get(key) or {}
        cv = comp.get("version", "")
        tpls = COMPONENT_SOURCES.get(key) or []
        wdir = BASE_DIR / comp.get("warehouse_dir", "").format(arch=arch)
        name = tpls[0].split("/")[-1].replace("{ver}", cv).replace("{ver_nov}", cv.lstrip("v")).replace("{arch}", arch)
        tpls_e = [t.replace("{ver}", cv).replace("{ver_nov}", cv.lstrip("v")).replace("{arch}", arch) for t in tpls]
        urls = []
        for t in tpls_e:
            urls.extend(expand_urls(t))
        jobs.append((wdir / name, urls, is_tar_valid if out_suffix_is_tgz(name) else (lambda p: p.is_file() and p.stat().st_size > 100_000), key + "/" + name))
    if upgrade_to and upgrade_to != kube_ver:
        up_dir = BASE_DIR / ("warehouse/cluster/kube/%s/%s" % (upgrade_to, arch))
        for b in ("kubeadm", "kubelet", "kubectl"):
            jobs.append((up_dir / b, [f"https://dl.k8s.io/release/{upgrade_to}/bin/linux/{arch}/{b}"],
                         lambda p: p.is_file() and p.stat().st_size > 1_000_000, "upgrade/" + b))
    return jobs


def out_suffix_is_tgz(name):
    return name.endswith((".tgz", ".gz"))


def ensure_materials(ccat, kube_ver, arch=ARCH, upgrade_to="", progress_cb=None, only_missing=True):
    """补齐缺失物料(可被 packer 导入)。progress_cb(event, name, done, total)
    event: 'start'(总览) / 'dl'(组件字节进度) / 'done'(单组件完成) / 'skip'
    返回 (downloaded_mb, failed_names)"""
    jobs = component_jobs(ccat, kube_ver, arch, upgrade_to)
    to_do = []
    for out, urls, check, disp in jobs:
        if only_missing and out.is_file() and check(out):
            if progress_cb:
                progress_cb("skip", disp, 1, 1)
            continue
        to_do.append((out, urls, check, disp))
    total = 0
    for out, urls, check, disp in to_do:
        size = 0
        for u in urls:
            size = content_length(u)
            if size:
                break
        total += size or 0
    if progress_cb:
        progress_cb("start", "download", 0, total)
    downloaded = 0
    failed = []
    for out, urls, check, disp in to_do:
        size = 0
        for u in urls:
            size = content_length(u)
            if size:
                break
        ok = False
        for url in urls:
            if size > 8_000_000 and has_curl():
                def tick(done, _n=size):
                    if progress_cb:
                        progress_cb("dl", disp, min(done, _n), _n)
                ok = fetch_parallel(url, out, size, tick=tick)
            else:
                ok = fetch_simple(url, out)
                if progress_cb:
                    progress_cb("dl", disp, size if ok else 0, size)
            if ok and check(out):
                break
            if out.exists():
                out.unlink()
            ok = False
        if ok:
            downloaded += out.stat().st_size
            if progress_cb:
                progress_cb("done", disp, 1, 1)
        else:
            failed.append(disp)
    return downloaded, failed


# ------------------------------------------------------------------ 离线镜像收集(docker)

# 打包机经国内镜像源拉取各 registry 的候选前缀(依次尝试, 命中即用; 空串=直连)
IMAGE_MIRROR_PREFIXES = {
    "registry.k8s.io": ["m.daocloud.io/registry.k8s.io/"],
    "quay.io":         ["m.daocloud.io/quay.io/"],
    "ghcr.io":         ["m.daocloud.io/ghcr.io/"],
    "docker.io":       ["docker.m.daocloud.io/", "m.daocloud.io/docker.io/"],
}

# CNI 附加镜像: flannel chart 只覆写 repository(kk values: ghcrio_registry), tag 跟随 chart 版本
FLANNEL_CNI_PLUGIN = {"v0.27.4": "v1.8.0-flannel1"}
CALICO_IMAGES = ("node", "cni", "kube-controllers", "typha", "pod2daemon-flexvol")


def cluster_images_tar_name(kube_version, cni_type, arch=ARCH):
    """纯离线镜像包在 warehouse 与 bundle 内的文件名(kube版本-cni-架构)"""
    return "k8s-%s-%s-%s-images.tar" % (kube_version.lstrip("v"), cni_type, arch)


def k8s_image_specs(ccat, kube_version, cni_type, ha_type="local", storage=None, arch=ARCH,
                    dns=None, multi_cni="none", multi_cni_tag=""):
    """纯离线模式所需镜像清单(原生 registry tag, 部署前导入节点 containerd)。
    覆盖: 控制面+CoreDNS+NodeLocalDNS+etcd+CNI(+HA 端点+存储类+Multi-CNI)。
    与 kk 各角色模板实际引用的镜像严格一致; 版本号取自 versions.json(对齐 kk per-minor vars),
    dns 覆盖 tag 时以覆盖值为准(离线导入的镜像必须与 config.yaml 生成的 tag 一致)。"""
    ver = ccat["versions"][kube_version]
    comps = ver.get("components") or {}
    dns = dns or {}
    images = [
        "registry.k8s.io/kube-apiserver:%s" % kube_version,
        "registry.k8s.io/kube-controller-manager:%s" % kube_version,
        "registry.k8s.io/kube-scheduler:%s" % kube_version,
        "registry.k8s.io/kube-proxy:%s" % kube_version,
        "registry.k8s.io/pause:%s" % (ver.get("sandbox_image_tag") or "3.9"),
        "registry.k8s.io/coredns/coredns:%s" % (dns.get("coredns_tag") or ver.get("coredns_tag") or ""),
    ]
    if dns.get("nodelocaldns_enabled") is not False:
        # NodeLocalDNS 默认启用(kk 05-dns defaults), kubelet clusterDNS 指向 169.254.25.10
        images.append("registry.k8s.io/dns/k8s-dns-node-cache:%s"
                      % (dns.get("nodelocaldns_tag") or ver.get("nodelocaldns_tag") or ""))
    if multi_cni == "multus":
        images.append("ghcr.io/k8snetworkplumbingwg/multus-cni:%s" % (multi_cni_tag or "v4.3.0"))
    # docker.io 单路径镜像必须写 library 全称: containerd 会把 docker.io/etcd 规范化成
    # docker.io/library/etcd 再查本地, tag 少了 library 会 miss 后转在线拉取
    images.append("docker.io/library/etcd:%s" % ((comps.get("etcd") or {}).get("version") or ""))
    cni_ver = (ver.get("cni_versions") or {}).get(cni_type) or ver.get("cni_plugin", {}).get("version", "")
    if cni_type == "flannel":
        # flannel chart 的镜像在 ghcr.io(kk values 仅覆写 repository 前缀, tag 跟随 chart)
        images += ["ghcr.io/flannel-io/flannel:%s" % cni_ver,
                   "ghcr.io/flannel-io/flannel-cni-plugin:%s" % FLANNEL_CNI_PLUGIN.get(cni_ver, "v1.8.0-flannel1")]
    elif cni_type == "calico":
        images += ["quay.io/calico/%s:%s" % (n, cni_ver) for n in CALICO_IMAGES]
    if ha_type == "kube-vip":
        images.append("docker.io/plndr/kube-vip:v0.7.2")
    elif ha_type == "haproxy":
        images.append("docker.io/library/haproxy:2.9.6-alpine")
    storage = storage or {}
    if storage.get("localpv_enabled"):
        lp = (comps.get("localpv") or {}).get("version") or "4.4.0"
        images += ["docker.io/openebs/dynamic-localpv-provisioner:%s" % lp,
                   "docker.io/openebs/linux-utils:%s" % lp]
    if storage.get("nfs_enabled"):
        nf = (comps.get("nfs") or {}).get("version") or "v4.0.18"
        images.append("registry.k8s.io/sig-storage/nfs-subdir-external-provisioner:%s" % nf)
    # 剔除缺版本号的条目(目录数据缺失时宁可打包报缺, 也不生成非法 tag)
    return [i for i in images if not i.endswith(":")]


def _pull_candidates(ref):
    """一个原生镜像引用的拉取源候选(完整引用, 依次尝试, 官方源兜底)。
    约定: 镜像源前缀 + "去掉默认 registry 的引用" = 完整引用
    (registry.k8s.io/pause:3.9 -> m.daocloud.io/registry.k8s.io/pause:3.9)"""
    registry, _, path = ref.partition("/")
    if registry not in IMAGE_MIRROR_PREFIXES:   # 不带 registry 前缀 = docker hub
        registry, path = "docker.io", ref
    srcs = [p + path for p in IMAGE_MIRROR_PREFIXES[registry]]
    if registry == "docker.io":
        # daocloud 对 docker.io 有白名单限制(如 etcd 不在列), hub.kubesphere 实测可达, 作兜底;
        # hub 路径不带 docker.io 与 library/ 前缀: docker.io/library/etcd -> hub.kubesphere.com.cn/etcd
        srcs.append("hub.kubesphere.com.cn/" + path.replace("library/", "", 1))
        if ref.startswith("docker.io/library/etcd:"):
            # etcd 官方在 quay.io 同源发布(coreos/etcd), 是 docker.io/etcd 拉不到时的最后兜底
            srcs.append("quay.io/coreos/etcd:" + ref.split(":", 1)[1])
    srcs.append(ref)   # 官方源兜底(离线/被墙环境会超时, 放最后)
    return srcs


def _docker(*args, timeout=3600):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def ensure_images(ccat, kube_ver, arch, image_specs, cni_type="", progress_cb=None):
    """收集纯离线镜像包: docker pull(国内源逐个尝试) -> retag 原生 -> docker save 单 tar。
    可被 packer 导入调用。返回 (downloaded_mb, failed_refs)"""
    out = BASE_DIR / "warehouse" / "cluster" / "images" / cluster_images_tar_name(kube_ver, cni_type, arch)
    if out.is_file() and out.stat().st_size > 1_000_000:
        if progress_cb:
            progress_cb("skip", out.name, 1, 1)
        return 0.0, []
    if shutil.which("docker") is None:
        return 0.0, ["docker 不可用(需 Docker Desktop 运行中)"]
    # 已存在的本地镜像不重复拉
    to_pull = []
    for ref in image_specs:
        if _docker("image", "inspect", ref).returncode != 0:
            to_pull.append(ref)
    if progress_cb:
        progress_cb("start", "images", 0, len(image_specs))
    failed = []
    for i, ref in enumerate(to_pull):
        ok = False
        for src in _pull_candidates(ref):
            r = _docker("pull", src, timeout=1800)
            if r.returncode == 0:
                _docker("tag", src, ref)
                ok = True
                break
            warn(f"拉取 {src} 失败, 换下一源")
        if not ok:
            failed.append(ref)
            continue
        if progress_cb:
            progress_cb("dl", ref, i + 1, len(to_pull))
    if progress_cb:
        progress_cb("done", "images", 1, 1)
    if failed:
        return 0.0, failed
    out.parent.mkdir(parents=True, exist_ok=True)
    r = _docker("save", "-o", str(out), *image_specs, timeout=3600)
    if r.returncode != 0:
        return 0.0, ["docker save 失败: %s" % (r.stderr or "").strip()[-200:]]
    return out.stat().st_size / 1048576, []


def chart_jobs(ccat, kube_ver, cni_type, arch=ARCH, storage=None):
    """按 CNI/存储选择推导 chart(+calicoctl) 下载任务, 布局与 kk binary_dir 约定一致:
    charts/<cni>/<cni>-<ver>.tgz; calico 另含 tigera-operator chart 与 calicoctl 二进制"""
    ver = (ccat.get("versions") or {}).get(kube_ver) or {}
    cni_ver = (ver.get("cni_versions") or {}).get(cni_type) or ver.get("cni_plugin", {}).get("version", "")
    cdir = BASE_DIR / "warehouse" / "cluster" / "charts" / cni_type
    jobs = []
    if cni_type == "flannel" and cni_ver:
        jobs.append((cdir / ("flannel-%s.tgz" % cni_ver),
                     expand_urls("GH/flannel-io/flannel/releases/download/%s/flannel.tgz" % cni_ver),
                     is_tar_valid, "flannel chart"))
    elif cni_type == "calico" and cni_ver:
        jobs.append((cdir / ("tigera-operator-%s.tgz" % cni_ver),
                     expand_urls("GH/projectcalico/calico/releases/download/%s/tigera-operator-%s.tgz" % (cni_ver, cni_ver)),
                     is_tar_valid, "calico tigera-operator chart"))
        ctl = cdir / cni_ver / arch / ("calicoctl-linux-%s" % arch)
        jobs.append((ctl,
                     expand_urls("GH/projectcalico/calico/releases/download/%s/calicoctl-linux-%s" % (cni_ver, arch)),
                     lambda p: p.is_file() and p.stat().st_size > 1_000_000, "calicoctl"))
        # v3.32+ 把 CRD 拆成独立 chart(部署时先装)
        try:
            minor = tuple(int(x) for x in cni_ver.lstrip("v").split(".")[:2])
        except ValueError:
            minor = (0, 0)
        if minor >= (3, 32):
            jobs.append((cdir / ("crd.projectcalico.org.v1-%s.tgz" % cni_ver),
                         expand_urls("GH/projectcalico/calico/releases/download/%s/crd.projectcalico.org.v1-%s.tgz" % (cni_ver, cni_ver)),
                         is_tar_valid, "calico crd chart"))
    elif cni_type == "cilium" and cni_ver:
        jobs.append((cdir / ("cilium-%s.tgz" % cni_ver),
                     ["https://helm.cilium.io/cilium-%s.tgz" % cni_ver], is_tar_valid, "cilium chart"))
    elif cni_type == "kubeovn" and cni_ver:
        jobs.append((cdir / ("kube-ovn-%s.tgz" % cni_ver),
                     ["https://kubeovn.github.io/kube-ovn/kube-ovn-%s.tgz" % cni_ver], is_tar_valid, "kube-ovn chart"))
    storage = storage or {}
    if storage.get("localpv_enabled"):
        lp = ((ver.get("components") or {}).get("localpv") or {}).get("version") or "4.4.0"
        jobs.append((BASE_DIR / "warehouse/cluster/charts/localpv" / ("localpv-provisioner-%s.tgz" % lp),
                     ["https://openebs.github.io/dynamic-localpv-provisioner/localpv-provisioner-%s.tgz" % lp],
                     is_tar_valid, "localpv chart"))
    if storage.get("nfs_enabled"):
        nf = ((ver.get("components") or {}).get("nfs") or {}).get("version") or "v4.0.18"
        jobs.append((BASE_DIR / "warehouse/cluster/charts/nfs" / ("nfs-subdir-external-provisioner-%s.tgz" % nf),
                     expand_urls("GH/kubernetes-sigs/nfs-subdir-external-provisioner/releases/download/nfs-subdir-external-provisioner-%s/nfs-subdir-external-provisioner-%s.tgz" % (nf, nf)),
                     is_tar_valid, "nfs provisioner chart"))
    return jobs


def ensure_charts(ccat, kube_ver, cni_type, arch=ARCH, storage=None, progress_cb=None):
    """补齐 CNI/存储 chart 物料(可被 packer 导入)。返回 (downloaded_mb, failed_names)"""
    jobs = chart_jobs(ccat, kube_ver, cni_type, arch, storage)
    to_do = [(o, u, c, d) for o, u, c, d in jobs if not (o.is_file() and c(o))]
    if not to_do:
        for _, _, _, d in jobs:
            if progress_cb:
                progress_cb("skip", d, 1, 1)
        return 0.0, []
    downloaded = 0
    failed = []
    for out, urls, check, disp in to_do:
        ok = False
        for url in urls:
            ok = fetch_simple(url, out)
            if ok and check(out):
                break
            if out.exists():
                out.unlink()
            ok = False
        if ok:
            downloaded += out.stat().st_size
            if progress_cb:
                progress_cb("done", disp, 1, 1)
        else:
            failed.append(disp)
    return downloaded / 1048576, failed


# ------------------------------------------------------------------ CLI commands

def cmd_images(ccat, kube_version="", cni_type="flannel"):
    """收集纯离线镜像包(docker; 规格=控制面+DNS+etcd+CNI, 原生 tag 单 tar)"""
    ver = cluster_kube_version(ccat, kube_version)
    out = BASE_DIR / "warehouse" / "cluster" / "images" / cluster_images_tar_name(ver, cni_type, ARCH)
    if out.is_file() and out.stat().st_size > 1_000_000:
        info(f"镜像包已存在: {out} ({out.stat().st_size / 1048576:.0f} MB)")
        return
    specs = k8s_image_specs(ccat, ver, cni_type, arch=ARCH)
    info(f"收集离线镜像 {len(specs)} 个 -> {out}")
    for s in specs:
        print(f"  - {s}")
    mb, failed = ensure_images(ccat, ver, ARCH, specs, cni_type)
    if failed:
        die("镜像收集失败: " + ", ".join(failed))
    info(f"完成 ({mb:.0f} MB): {out}")


def cmd_check(ccat, kube_version=""):
    ver = cluster_kube_version(ccat, kube_version)
    arch = ARCH
    print(f"== 集群物料盘点 (K8s {ver}, {arch}) ==")
    ok = True
    def mark(cond, label, detail=""):
        nonlocal ok
        print(("  [就绪] " if cond else "  [缺失] ") + label + (f"  {detail}" if detail else ""))
        ok = ok and cond
    kk = (ccat.get("kk") or {}).get("binaries", {}).get(arch)
    mark(kk and (BASE_DIR / kk).is_file(), f"kk 二进制 ({arch}, 打过信创补丁)", kk or "")
    for out, urls, check, disp in component_jobs(ccat, ver, arch):
        mark(out.is_file() and check(out), disp, str(out.parent))
    art = ccat.get("artifact") or {}
    art_name = art.get("filename_pattern", "").format(kube_version=ver, arch=arch)
    art_path = BASE_DIR / art["warehouse_dir"] / art_name
    print(f"  [可选] artifact 产物 (mode=artifact 才需要): {'存在' if art_path.is_file() else '未生成'}  {art_path}")
    for distro in (ccat.get("distros") or {}):
        d_dir = BASE_DIR / ccat["os_packages_dir"].format(distro=distro, arch=arch)
        n = len(list(d_dir.glob("*.deb"))) + len(list(d_dir.glob("*.rpm"))) if d_dir.is_dir() else 0
        print(f"  [{'就绪' if n else '可选'}] OS 依赖包 {distro}: {n} 个包" + ("" if n else f"  (按需, {d_dir})"))
    return ok


def cmd_download(ccat, kube_version="", upgrade_to=""):
    ver = cluster_kube_version(ccat, kube_version)
    downloaded, failed = ensure_materials(ccat, ver, ARCH, upgrade_to,
                                          progress_cb=lambda ev, n, d, t: None)
    if failed:
        die(f"{len(failed)} 个组件下载失败: {failed} (网络恢复后重跑 --download)")
    print(f"== 二进制物料齐备 (本次下载 {downloaded/1048576:.0f} MB) ==")


def cmd_os_packages(ccat, distros):
    if not shutil.which("docker"):
        die("未检测到 docker, 请先启动 Docker Desktop (OS 依赖包在容器内提取)")
    subprocess.run(["docker", "pull", "-q", "docker.m.daocloud.io/library/ubuntu:22.04"], capture_output=True)
    for distro in distros:
        fam = (ccat.get("distros") or {}).get(distro, {}).get("family", "rpm")
        base_img = "docker.m.daocloud.io/library/ubuntu:22.04" if fam == "deb" else "docker.m.daocloud.io/library/rockylinux:9"
        out_dir = BASE_DIR / ccat["os_packages_dir"].format(distro=distro, arch=ARCH)
        out_dir.mkdir(parents=True, exist_ok=True)
        win_out = str(out_dir).replace("/", "\\")
        print(f"[OS ] 提取 {distro} ({fam}) 依赖包 -> {out_dir}")
        if fam == "deb":
            script = ("apt-get update -qq >/dev/null 2>&1; cd /out && "
                      "apt-get download socat conntrack ipset ebtables chrony ipvsadm nfs-common >/dev/null 2>&1; "
                      "apt-get download $(apt-cache depends --recurse --no-recommends --no-suggests "
                      "--no-conflicts --no-breaks --no-replaces --no-enhances "
                      "socat conntrack ipset ebtables chrony ipvsadm nfs-common 2>/dev/null | grep '^\\w' | sort -u) >/dev/null 2>&1; "
                      "echo debs=$(ls /out/*.deb 2>/dev/null | wc -l)")
        else:
            script = ("mkdir -p /out && cd /out && "
                      "dnf download --resolve socat conntrack-tools ipset ebtables chrony ipvsadm nfs-utils >/dev/null 2>&1; "
                      "echo rpms=$(ls /out/*.rpm 2>/dev/null | wc -l)")
        r = subprocess.run(["docker", "run", "--rm", "-v", win_out + ":/out", base_img, "bash", "-c", script],
                           capture_output=True, text=True)
        print("  " + (r.stdout.strip().splitlines()[-1] if r.stdout.strip() else f"失败: {r.stderr[:200]}"))


def cmd_artifact_export(ccat, kube_version=""):
    """生成 kk artifact 导出专用 Config + Windows 一键 .bat (Docker Desktop 内跑 linux kk)"""
    ver = cluster_kube_version(ccat, kube_version)
    vmeta = ccat["versions"][ver]
    comps = vmeta.get("components") or {}
    arch = ARCH
    art_dir = BASE_DIR / ccat["artifact"]["warehouse_dir"]
    art_dir.mkdir(parents=True, exist_ok=True)
    cni_versions = vmeta.get("cni_versions") or {}
    cni_type = vmeta.get("cni_plugin", {}).get("type", "calico")
    def g(k): return (comps.get(k) or {}).get("version", "")
    cfg_name = f"artifact-config-{ver}-{arch}.yaml"
    lines = [
        "# kk artifact 导出专用配置 (由 prepare_cluster.py 生成; 镜像清单由 kk 按 kube_version+CNI 自动聚合)",
        "apiVersion: kubekey.kubesphere.io/v1",
        "kind: Config",
        "spec:",
        "  zone: cn",
        "  download:",
        "    os: linux",
        "    fetch: true",
        f"    arch: [{arch}]",
        f"    kube_version: [{ver}]",
    ]
    for key in ("etcd", "helm", "crictl", "containerd", "runc", "cni_plugins"):
        v = g(key)
        if v:
            lines.append(f"    {key}_version: [{v}]")
    lines += [
        "    cni:",
        f"      type: [{cni_type}]",
    ]
    if cni_versions.get(cni_type):
        lines.append(f"      {cni_type}_version: [{cni_versions[cni_type]}]")
    (art_dir / cfg_name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[OK ] 导出配置: {art_dir / cfg_name}")

    out_name = ccat["artifact"]["filename_pattern"].format(kube_version=ver, arch=arch)
    kk_ver = (ccat.get("kk") or {}).get("version", "v4.0.7-xc1")
    bat = f"""@echo off
REM 一键导出 kk 离线镜像产物 (需 Docker Desktop; 在有网的 Windows 打包机上执行)
REM 产物: {out_name}  -> 放回本目录后打包器 mode=artifact 即可使用
cd /d %~dp0
docker run --rm -v "%~dp0.":/work -v "%~dp0..\\kk":/kk:ro -w /work ubuntu:22.04 bash -c ^
  "sed -i 's|http://archive.ubuntu.com|http://mirrors.huaweicloud.com|g; s|http://security.ubuntu.com|http://mirrors.huaweicloud.com|g' /etc/apt/sources.list && apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq ca-certificates >/dev/null 2>&1 && printf '#!/bin/bash\\n[ \"$1\" = \"-SE\" ] && shift\\nexec \"$@\"\\n' > /usr/local/bin/sudo && chmod +x /usr/local/bin/sudo && touch /etc/sudoers && printf '#!/bin/bash\\nexit 0\\n' > /usr/sbin/visudo && chmod +x /usr/sbin/visudo && chmod +x /kk/{kk_ver}/{arch}/kk && /kk/{kk_ver}/{arch}/kk artifact export -c /work/{cfg_name} --workdir /work/kkstage"
if exist "%~dp0..\\..\\..\\kkstage\\artifact\\kubekey-artifact.tgz" (
  move /y "%~dp0..\\..\\..\\kkstage\\artifact\\kubekey-artifact.tgz" "%~dp0{out_name}" >nul
  rd /s /q "%~dp0..\\..\\..\\kkstage"
)
if errorlevel 1 (
  echo [FAIL] 导出失败, 见上方日志
  exit /b 1
)
echo [OK ] 已生成 {out_name}
"""
    bat_path = art_dir / "export-artifact.bat"
    bat_path.write_text(bat, encoding="gbk", errors="replace")
    print(f"[OK ] 一键脚本: {bat_path}  (双击执行, 需 Docker Desktop)")
    print("提示: 镜像拉取走 registry.k8s.io/docker.io, 国内网络慢属正常; 产物约数 GB")


def cmd_kk_build():
    """生成 kk 构建 .bat (调本机 Go 直接交叉编译 linux 二进制, 与在 Linux 上 make kk 等价)"""
    root = BASE_DIR
    bat = f"""@echo off
REM 一键构建打过信创补丁的 kk (需本机安装 Go 1.22+ 并加入 PATH)
REM 产物放置: warehouse/cluster/kk/v4.0.7-xc1/{{amd64,arm64}}/kk
setlocal
set KK_SRC={root.parent}\\kubekey
if not exist "%KK_SRC%\\cmd\\kk\\kubekey.go" (
  echo [FAIL] 未找到 KubeKey 源码: %KK_SRC%
  echo        请将打补丁的 kubekey 源码仓库放到 E:\\project\\kubekey 或修改本脚本 KK_SRC
  exit /b 1
)
where go >nul 2>nul || (echo [FAIL] 未找到 go, 请安装 Go 1.22+ 并加入 PATH & exit /b 1)
cd /d %KK_SRC%
set CGO_ENABLED=0
set GOPROXY=https://goproxy.cn,direct
set GOOS=linux
set GOARCH=amd64
go build -trimpath -tags builtin -o "{root}\\warehouse\\cluster\\kk\\v4.0.7-xc1\\amd64\\kk" cmd/kk/kubekey.go || exit /b 1
echo [OK ] amd64 构建完成
set GOARCH=arm64
go build -trimpath -tags builtin -o "{root}\\warehouse\\cluster\\kk\\v4.0.7-xc1\\arm64\\kk" cmd/kk/kubekey.go || exit /b 1
echo [OK ] arm64 构建完成
"""
    p = BASE_DIR / "build-kk.bat"
    p.write_text(bat, encoding="gbk", errors="replace")
    print(f"[OK ] 构建脚本: {p}  (双击执行, 需本机 Go; 源码需含信创补丁)")
    print("提示: 补丁内容清单见 docs/k8s-信创离线部署.md 第 2.1 节")


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description="K8s 集群物料准备 (Windows 原生)")
    ap.add_argument("--check", action="store_true", help="物料盘点")
    ap.add_argument("--download", action="store_true", help="下载缺失二进制(多源多连接)")
    ap.add_argument("--kube-version", metavar="VER", default="", help="指定 K8s 版本(默认推荐版本)")
    ap.add_argument("--upgrade-to", metavar="VER", default="", help="同时下载升级目标版本的 kube 三件套")
    ap.add_argument("--os-packages", nargs="*", metavar="DISTRO",
                    help="容器提取 OS 依赖包(如: ubuntu kylin; 需 Docker Desktop)")
    ap.add_argument("--images", nargs="?", const="flannel", default="", metavar="CNI",
                    help="收集纯离线镜像包(控制面+DNS+etcd+CNI; 需 Docker Desktop; 默认 CNI=flannel)")
    ap.add_argument("--artifact-export", action="store_true", help="生成 kk artifact 导出配置 + 一键 .bat")
    ap.add_argument("--kk-build", action="store_true", help="生成 kk 构建 .bat")
    ap.add_argument("--all", action="store_true", help="check + download + artifact-export + kk-build")
    args = ap.parse_args()
    if not any(v for k, v in vars(args).items() if k != "kube_version"):
        ap.print_help()
        return

    ccat = (load_catalog().get("cluster") or {})
    if not ccat:
        die("versions.json 缺少 cluster 节")
    if args.all:
        args.check = args.download = args.artifact_export = args.kk_build = True
        args.os_packages = []
    if args.check:
        cmd_check(ccat, args.kube_version)
    if args.download:
        cmd_download(ccat, args.kube_version, args.upgrade_to)
    if args.os_packages:
        cmd_os_packages(ccat, args.os_packages or ["ubuntu"])
    if args.images:
        cmd_images(ccat, args.kube_version, args.images)
    if args.artifact_export:
        cmd_artifact_export(ccat, args.kube_version)
    if args.kk_build:
        cmd_kk_build()


if __name__ == "__main__":
    main()

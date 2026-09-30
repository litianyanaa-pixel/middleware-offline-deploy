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


# ------------------------------------------------------------------ CLI commands

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
    if args.artifact_export:
        cmd_artifact_export(ccat, args.kube_version)
    if args.kk_build:
        cmd_kk_build()


if __name__ == "__main__":
    main()

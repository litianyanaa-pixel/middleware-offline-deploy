# -*- coding: utf-8 -*-
"""Java 运行时物料准备 (Temurin JDK): 版本解析 -> 多源下载 -> sha256 校验 -> 回填 versions.json

设计:
- 运行时 resolve: Adoptium API 实时获取各 major 最新 GA 的文件名/sha256/下载链接,
  结果回填 versions.json 的 java.resolved, 之后离线打包机可直接复用, 无需再联网
- 下载源: 清华 TUNA -> GitHub 原始 -> GH 三镜像 (与集群物料同一套镜像策略)
- 分段下载复用 prepare_cluster.fetch_parallel (4 连接, 逐段校验)
- Windows 原生运行, 纯标准库 (与 prepare_cluster.py 同约束)

用法:
  python prepare_java.py --check                 查看各版本/架构物料就绪状态
  python prepare_java.py --download              下载全部 major 双架构 (可在后追加 --majors/--arch)
  python prepare_java.py --download --majors 17 --arch amd64
"""
import argparse
import hashlib
import json
import subprocess
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
VERSIONS_FILE = BASE_DIR / "versions.json"
JAVA_WAREHOUSE = BASE_DIR / "warehouse" / "java"

sys_path_added = False

API_TMPL = ("https://api.adoptium.net/v3/assets/latest/%s/hotspot"
            "?os=linux&architecture=%s&image_type=jdk")
TUNA_TMPL = "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/%s/jdk/%s/linux/%s"
GH_RAW = "https://github.com"
# Adoptium API 架构名 -> 包内子目录名 (与 docker/compose 包的 x86_64/aarch64 对齐)
PKG_SUB = {"x64": "x86_64", "aarch64": "aarch64"}
MAJORS_DEFAULT = ["8", "17", "21"]


def _log(msg):
    print(msg, flush=True)


def _gh_mirrors():
    """GH 三镜像 (延迟导入 prepare_cluster, 保持本模块可独立运行)"""
    global sys_path_added
    if not sys_path_added:
        import sys
        if str(BASE_DIR) not in sys.path:
            sys.path.insert(0, str(BASE_DIR))
        sys_path_added = True
    try:
        from prepare_cluster import GH_MIRRORS_DEFAULT
        return list(GH_MIRRORS_DEFAULT)
    except Exception:
        return [
            "https://gh-proxy.com/https://github.com",
            "https://ghfast.top/https://github.com",
            "https://ghproxy.net/https://github.com",
        ]


def _load_versions():
    with open(VERSIONS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_versions(data):
    with open(VERSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _sha256(path: Path, tick=None):
    h = hashlib.sha256()
    done = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
            done += len(b)
            if tick:
                tick(len(b))
    return h.hexdigest()


# ---------------------------------------------------------------- resolve

def resolve(major, api_arch):
    """经 Adoptium API 获取指定 major/架构 最新 GA 的元数据。
    返回 {version, filename, checksum, size, gh_link}; 失败抛 RuntimeError"""
    url = API_TMPL % (major, api_arch)
    try:
        r = subprocess.run(["curl", "-sL", "-m", "40", url],
                           capture_output=True, timeout=60)
        assets = json.loads(r.stdout.decode("utf-8", "replace") or "null")
    except Exception as e:
        raise RuntimeError("Adoptium API 请求失败 (%s): %s" % (major, e))
    if not isinstance(assets, list) or not assets:
        raise RuntimeError("Adoptium API 无返回 (major=%s arch=%s)" % (major, api_arch))
    it = assets[0]
    ver = it.get("version") or {}
    pkg = (it.get("binary") or {}).get("package") or {}
    if not pkg.get("name") or not pkg.get("link"):
        raise RuntimeError("Adoptium API 返回缺 package 字段 (major=%s arch=%s)" % (major, api_arch))
    return {
        "version": ver.get("openjdk_version") or ver.get("semver") or it.get("release_name", ""),
        "filename": pkg["name"],
        "checksum": (pkg.get("checksum") or "").lower(),
        "size": int(pkg.get("size") or 0),
        "gh_link": pkg["link"],
    }


# ---------------------------------------------------------------- 下载

def _head_size(url):
    try:
        r = subprocess.run(["curl", "-sIL", "-m", "30", url], capture_output=True, timeout=40)
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            if line.lower().startswith("content-length:"):
                n = int(line.split(":", 1)[1].strip())
                if n > 1024 * 1024:   # 忽略重定向页的短 body
                    return n
    except Exception:
        pass
    return 0


def _download_whole(url, out: Path):
    r = subprocess.run(["curl", "-sL", "-m", "1800", "--retry", "3", "-o", str(out), url])
    return r.returncode == 0 and out.is_file() and out.stat().st_size > 1024 * 1024


def download(major, api_arch, meta, out: Path, tick=None):
    """按多源顺序下载单个 JDK 包; 成功返回实际使用的源描述"""
    gh = meta["gh_link"]
    urls = [TUNA_TMPL % (major, api_arch, meta["filename"]), gh]
    urls += [m + gh[len(GH_RAW):] for m in _gh_mirrors() if gh.startswith(GH_RAW)]
    last_err = ""
    for u in urls:
        src = u.split("/https://")[0] if "/https://" in u else u
        _log("  尝试: %s" % (src if src != gh else "GitHub"))
        try:
            out.unlink(missing_ok=True)
            size = meta.get("size") or _head_size(u)
            if size > 1024 * 1024:
                from prepare_cluster import fetch_parallel
                if fetch_parallel(u, out, size, n=4, tick=tick):
                    return src, size
                last_err = "分段下载校验失败"
            elif _download_whole(u, out):
                return src, out.stat().st_size
            else:
                last_err = "整文件下载失败"
        except Exception as e:
            last_err = str(e)
        out.unlink(missing_ok=True)
    raise RuntimeError("全部下载源失败 (最后错误: %s)" % last_err)


# ---------------------------------------------------------------- ensure / check

def java_dir(arch):
    """arch: amd64/arm64 -> warehouse/java/x86_64|aarch64"""
    return JAVA_WAREHOUSE / ("x86_64" if arch == "amd64" else "aarch64")


def resolved_of(versions, major, arch):
    r = ((versions.get("java") or {}).get("resolved") or {}).get(str(major)) or {}
    return r.get("x86_64" if arch == "amd64" else "aarch64") or None


def ensure(majors, arch, progress_cb=None):
    """确保 JDK tar 就绪 (缺则 resolve+下载+校验+回填 versions.json)。
    progress_cb(event: dict) 可选; 返回 (missing: list[str], warnings: list[str])"""
    from prepare_cluster import fetch_parallel   # noqa: F401  (提前失败优于下载到一半)
    versions = _load_versions()
    jmeta = versions.get("java") or {}
    majors_cfg = [str(m) for m in (jmeta.get("majors") or MAJORS_DEFAULT)]
    majors = [str(m) for m in majors] if majors else majors_cfg
    api_arch = "x64" if arch == "amd64" else "aarch64"
    sub = PKG_SUB[api_arch]
    ddir = java_dir(arch)
    ddir.mkdir(parents=True, exist_ok=True)
    resolved = {m: dict(((jmeta.get("resolved") or {}).get(m)) or {}) for m in majors}   # major -> {sub: meta}
    resolved_all = dict((jmeta.get("resolved") or {}))
    missing, warns = [], []

    def put_res(major, meta):
        resolved.setdefault(major, {})[sub] = meta
        resolved_all.setdefault(major, {})[sub] = meta
        jmeta["resolved"] = resolved_all
        versions["java"] = jmeta
        _save_versions(versions)

    for major in majors:
        done_ev = {"stage": "java", "major": major, "arch": arch, "done": 0}
        meta = resolved.get(major, {}).get(sub)
        cached = meta and (ddir / meta["filename"]).is_file()
        if cached:
            real = _sha256(ddir / meta["filename"], lambda n: done_ev.__setitem__("done", done_ev["done"] + n))
            if not meta.get("checksum") or real == meta["checksum"]:
                if progress_cb:
                    progress_cb(dict(done_ev, event="skip", filename=meta["filename"]))
                continue
            warns.append("Java %s/%s 缓存 sha256 不符, 重新下载" % (major, arch))
        # 需要 resolve (无缓存元数据或需要重下)
        try:
            meta = resolve(major, api_arch)
        except RuntimeError as e:
            missing.append("Java %s (%s): %s" % (major, arch, e))
            continue
        out = ddir / meta["filename"]
        if progress_cb:
            progress_cb(dict(done_ev, event="download", filename=meta["filename"], total=meta["size"]))
        try:
            src, size = download(major, api_arch, meta, out,
                                 tick=lambda n: (done_ev.__setitem__("done", done_ev["done"] + n),
                                                 progress_cb(dict(done_ev, event="progress")))[1] if progress_cb else None)
        except (RuntimeError, ImportError) as e:
            missing.append("Java %s (%s): 下载失败 - %s" % (major, arch, e))
            out.unlink(missing_ok=True)
            continue
        real = _sha256(out)
        if meta["checksum"] and real != meta["checksum"]:
            out.unlink(missing_ok=True)
            missing.append("Java %s (%s): sha256 校验失败" % (major, arch))
            continue
        if not meta["checksum"]:
            warns.append("Java %s (%s): API 未提供 checksum, 已按下载实测值回填" % (major, arch))
        meta["size"] = size
        meta["sha256"] = real
        meta["source"] = src
        meta.pop("checksum", None)
        put_res(str(major), meta)
        _log("  Java %s %s -> %s (%.0f MB, %s)"
             % (major, arch, meta["filename"], size / 1048576, meta["version"]))

    # 顺手把 API 解析到但未下载的架构元数据也回填, 供离线打包提示
    return missing, warns


def check(arch_list):
    versions = _load_versions()
    majors_cfg = [str(m) for m in ((versions.get("java") or {}).get("majors") or MAJORS_DEFAULT)]
    _log("Java majors: %s" % ", ".join(majors_cfg))
    for arch in arch_list:
        api_arch = "x64" if arch == "amd64" else "aarch64"
        sub = PKG_SUB[api_arch]
        _log("[%s / %s]" % (arch, sub))
        for major in majors_cfg:
            meta = resolved_of(versions, major, arch)
            if meta:
                path = java_dir(arch) / meta["filename"]
                ok = "OK " if path.is_file() else "缺文件"
                _log("  JDK %s: %s %s (%s)" % (major, ok, meta["filename"], meta.get("version", "?")))
            else:
                _log("  JDK %s: 未 resolve (需联网运行 --download)" % major)


def main():
    ap = argparse.ArgumentParser(description="Java 运行时物料准备 (Temurin JDK)")
    ap.add_argument("--check", action="store_true", help="查看物料就绪状态")
    ap.add_argument("--download", action="store_true", help="下载缺失物料 (resolve 最新 GA)")
    ap.add_argument("--majors", default="", help="版本列表, 逗号分隔 (默认取 versions.json java.majors)")
    ap.add_argument("--arch", default="amd64,arm64", help="架构: amd64,arm64")
    args = ap.parse_args()

    archs = [a.strip() for a in args.arch.split(",") if a.strip() in ("amd64", "arm64")] or ["amd64"]
    if args.check:
        check(archs)
        return
    if not args.download:
        ap.print_help()
        return
    majors = [m.strip() for m in args.majors.split(",") if m.strip()] if args.majors else []
    all_missing, all_warns = [], []
    for arch in archs:
        _log("== 架构 %s ==" % arch)
        missing, warns = ensure(majors, arch)
        all_missing += missing
        all_warns += warns
    for w in all_warns:
        _log("[WARN] %s" % w)
    if all_missing:
        _log("[FAIL] 以下物料未就绪:")
        for m in all_missing:
            _log("  - %s" % m)
        raise SystemExit(1)
    _log("Java 物料全部就绪")


if __name__ == "__main__":
    main()

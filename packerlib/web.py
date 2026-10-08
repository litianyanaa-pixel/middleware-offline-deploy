# -*- coding: utf-8 -*-
"""本地 Web 界面: HTTP 服务 / 打包任务管理 / 配置预览接口"""
import json
import os
import re
import subprocess
import tarfile
import tempfile
import threading
import time
import webbrowser
from datetime import datetime

from .catalog import PLUGINS, load_catalog
from .cluster import cluster_materials, gen_cluster_config, gen_cluster_inventory
from .compose import (backup_crons, gen_compose, gen_env, resolve_db,
                      validate_compose_with_docker)
from .manifest import build_summary_lines, gen_images_txt, gen_manifest_sh
from .multinode import gen_nodes, gen_proxysql_conf
from .builder import (gen_pull_script, gen_retag_mirrors_sh, missing_materials, pack)
from .paths import BASE_DIR, DIST_DIR, HTML_FILE, LOGOS_JS, log
from .progress import PackProgress
from .util import PackError, bash_quote, tr
from .validate import validate_config


# ---------------------------------------------------------------- Web 界面

PACK_STATE = {"prog": None}   # 当前打包进度对象


def start_pack_async(cfg, catalog):
    """后台线程打包, 避免大包时 HTTP 请求挂住; 返回是否成功启动"""
    prog = PACK_STATE["prog"]
    if prog is not None and prog.running:
        return False
    prog = PackProgress()
    PACK_STATE["prog"] = prog

    def worker():
        try:
            pack(cfg, catalog, progress=prog)
        except PackError as e:
            prog.fail(str(e))
        except Exception as e:
            log.exception("打包线程异常")
            prog.fail("内部错误: %s" % e)

    threading.Thread(target=worker, daemon=True).start()
    return True


def list_bundles(limit=20):
    """dist/ 下已有产物, 按时间倒序"""
    items = []
    if DIST_DIR.is_dir():
        for p in sorted(DIST_DIR.glob("*.tar.gz"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
            sha_file = p.with_suffix(p.suffix + ".sha256")
            sha = ""
            if sha_file.is_file():
                first = sha_file.read_text(encoding="utf-8").split()
                if first:
                    sha = first[0]
            items.append({
                "name": p.name,
                "size_mb": round(p.stat().st_size / 1048576),
                "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                "sha256": sha,
            })
    return items


def _fsize_mb(rel):
    try:
        return round((BASE_DIR / rel).stat().st_size / 1048576)
    except OSError:
        return 0


def catalog_response(catalog):
    """目录信息 + 缺料检查 + 物料体积 (含插件集群形态物料)"""
    present = {}
    sizes = {"docker": {}, "compose": {}, "images": {}, "cluster": {}}
    for arch in ("amd64", "arm64"):
        present["docker_" + arch] = (BASE_DIR / catalog["docker"]["packages"][arch]).is_file()
        present["compose_" + arch] = (BASE_DIR / catalog["compose"]["packages"][arch]).is_file()
        sizes["docker"][arch] = _fsize_mb(catalog["docker"]["packages"][arch])
        sizes["compose"][arch] = _fsize_mb(catalog["compose"]["packages"][arch])
        for s, meta in catalog["services"].items():
            for a, rel in meta["images"].items():
                present["img_%s_%s" % (s, a)] = (BASE_DIR / rel).is_file()
                sizes["images"].setdefault(s, {})[a] = _fsize_mb(rel)
    services = {}
    for s, meta in catalog["services"].items():
        mod = PLUGINS.get(s)
        cl = getattr(mod, "CLUSTER", None) if mod else None
        if cl and cl.get("images"):
            meta = dict(meta)
            meta["cluster"] = cl
            services[s] = meta
            for a, rel in cl["images"].items():
                present["img_%s_cluster_%s" % (s, a)] = (BASE_DIR / rel).is_file()
                sizes["cluster"].setdefault(s, {})[a] = _fsize_mb(rel)
        else:
            services[s] = meta
    # ---- K8s 集群物料状态(kk / artifact / cache 组件 / OS 包) ----
    ccat = catalog.get("cluster") or {}
    if ccat:
        sizes["cluster_k8s"] = {}
        for arch in ("amd64", "arm64"):
            kk_rel = (ccat.get("kk") or {}).get("binaries", {}).get(arch)
            present["cluster_kk_" + arch] = bool(kk_rel) and (BASE_DIR / kk_rel).is_file()
            art_size = 0
            for ver in (ccat.get("versions") or {}):
                fname = ccat["artifact"]["filename_pattern"].format(kube_version=ver, arch=arch)
                rel = "%s/%s" % (ccat["artifact"]["warehouse_dir"], fname)
                present["cluster_artifact_%s_%s" % (ver, arch)] = (BASE_DIR / rel).is_file()
                if ver == max(ccat.get("versions") or {}):
                    art_size = _fsize_mb(rel)
            sizes["cluster_k8s"][arch] = art_size if art_size else _fsize_mb(kk_rel) if kk_rel else 0
        # cache 组件就绪状态(按版本聚合, 页面卡片状态点用); 集群当前仅开放 amd64
        for ver, vmeta in (ccat.get("versions") or {}).items():
            comps = vmeta.get("components") or {}
            all_ok = True
            for _key, comp in comps.items():
                wdir_rel = comp.get("warehouse_dir")
                if not wdir_rel:
                    continue
                wdir = BASE_DIR / wdir_rel.format(arch="amd64")
                if not (wdir.is_dir() and any(
                        f for f in wdir.glob("**/*")
                        if f.is_file() and not re.search(r"\.(p\d+|part\d*|tmp)$", f.name))):
                    all_ok = False
            present["cluster_cache_ready_%s" % ver] = all_ok
            # 纯离线镜像包就绪状态(值为已收集的 CNI 列表, 页面按选中 CNI 精确校验)
            img_dir = BASE_DIR / "warehouse" / "cluster" / "images"
            cnis = sorted({t.name.split("-")[2] for t in img_dir.glob("k8s-%s-*-amd64-images.tar" % ver.lstrip("v"))})                 if img_dir.is_dir() else []
            present["cluster_images_%s_amd64" % ver] = cnis
        # OS 依赖包就绪状态(按发行版×amd64; arm64 暂不开放集群)
        for distro in (ccat.get("distros") or {}):
            d_dir = BASE_DIR / ccat["os_packages_dir"].format(distro=distro, arch="amd64")
            present["cluster_os_%s" % distro] = d_dir.is_dir() and any(d_dir.iterdir())
    return {"services": services, "secrets": catalog["secrets"],
            "cluster": ccat,
            "defaults": catalog["defaults"], "files_present": present, "sizes": sizes,
            "suites": catalog.get("suites", [])}


def make_handler(catalog):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send_json(self, obj, code=200):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                html = HTML_FILE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
            elif self.path == "/api/catalog":
                self._send_json(catalog_response(catalog))
            elif self.path == "/logos.js":
                data = LOGOS_JS.read_bytes() if LOGOS_JS.is_file() else b"const LOGOS={};"
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif self.path == "/api/progress":
                prog = PACK_STATE["prog"]
                self._send_json(prog.snapshot() if prog else {"running": False})
            elif self.path == "/api/bundles":
                self._send_json({"bundles": list_bundles()})
            elif self.path.startswith("/api/pull_script"):
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                a = (q.get("arch") or ["both"])[0]
                archs = (a,) if a in ("amd64", "arm64") else ("amd64", "arm64")
                script, items = gen_pull_script(catalog, archs)
                if not items:
                    self._send_json({"ok": True, "missing": 0,
                                     "message": "物料仓库无缺失, 无需补料|No missing materials"})
                else:
                    data = script.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/x-shellscript; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="pull_missing_images.sh"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
            else:
                self._send_json({"error": "not found"}, 404)

        def do_POST(self):
            try:
                cfg = self._read_body()
                if self.path == "/api/preview":
                    cfg2, warns = validate_config(dict(cfg), catalog)
                    compose = gen_compose(cfg2, catalog) if cfg2["services"] else ""
                    env = gen_env(cfg2, catalog) if cfg2["services"] else ""
                    _nodes = gen_nodes(cfg2, catalog)
                    _cl_inv = gen_cluster_inventory(cfg2) if (cfg2.get("cluster") or {}).get("enabled") else ""
                    _cl_cfg = gen_cluster_config(cfg2, catalog) if (cfg2.get("cluster") or {}).get("enabled") else ""
                    _cl_warns = []
                    if (cfg2.get("cluster") or {}).get("enabled"):
                        _miss, _items = cluster_materials(cfg2, catalog)
                        if _miss:
                            auto_ok = any(("二进制缓存" in m) or ("缺 kubeadm" in m) or ("缺 kubelet" in m) or ("缺 kubectl" in m) for m in _miss)
                            for m in _miss:
                                _cl_warns.append("集群物料缺失: " + m)
                            if auto_ok:
                                _cl_warns.append("缺失的二进制组件将在点击「开始打包」时自动从国内可达源下载"
                                                 "(dl.k8s.io / 华为云镜像 / GitHub 镜像), 请保持打包机网络可用"
                                                 "|Missing binaries will be auto-downloaded when packing starts")
                    self._send_json({
                        "compose": compose,
                        "env": env,
                        "cluster_inventory": _cl_inv,
                        "cluster_config": _cl_cfg,
                        "manifest": gen_manifest_sh(cfg2, catalog,
                                                    resolve_db(cfg2)[1],
                                                    build_summary_lines(cfg2, catalog)),
                        "summary_lines": build_summary_lines(cfg2, catalog),
                        "images": [i[0] for i in gen_images_txt(cfg2, catalog)],
                        "nodes": [{"name": n["name"], "ip": n["server"]["ip"],
                                   "user": n["server"]["user"], "ssh": n["server"]["ssh"],
                                   "auth": "password" if n["server"].get("password") else "key",
                                   "roles": n["roles"],
                                   "images": [i[0] for i in n["images"]]} for n in _nodes],
                        "proxysql_conf": gen_proxysql_conf(cfg2) if (cfg2.get("features") or {}).get("proxysql") else "",
                        "backup_crons": ["%s %s" % (cron, eng) for eng, cron in backup_crons(cfg2.get("backup") or {})],
                        "warnings": warns + _cl_warns + (validate_compose_with_docker(compose, env) if compose else []),
                    })
                elif self.path == "/api/pack":
                    # 先做纯校验, 通过后再交给后台线程
                    validate_config(dict(cfg), catalog)
                    if not start_pack_async(cfg, catalog):
                        self._send_json({"error": "已有打包任务正在进行中"}, 409)
                    else:
                        self._send_json({"started": True})
                elif self.path == "/api/cancel":
                    prog = PACK_STATE["prog"]
                    if prog is not None and prog.running:
                        prog.cancel()
                        self._send_json({"cancelled": True})
                    else:
                        self._send_json({"error": "当前没有进行中的打包任务"}, 400)
                else:
                    self._send_json({"error": "not found"}, 404)
            except PackError as e:
                self._send_json({"error": str(e)}, 400)
            except Exception as e:
                log.exception("接口异常")
                self._send_json({"error": "服务器内部错误: %s" % e}, 500)

    return Handler, ThreadingHTTPServer


def run_web(port, no_browser=False):
    catalog = load_catalog()
    handler_cls, server_cls = make_handler(catalog)
    httpd = server_cls(("127.0.0.1", port), handler_cls)
    url = "http://127.0.0.1:%d" % port
    log.info("打包器已启动: %s  (Ctrl+C 退出)", url)
    if not no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("已退出")

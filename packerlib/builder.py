# -*- coding: utf-8 -*-
"""打包主流程: 校验 -> 生成配置与脚本 -> 物料收集 -> tar.gz 产物(含镜像 tar/仓库辅助)"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path

from .catalog import PLUGINS, load_catalog
from .cluster import (_auto_build_artifact, _auto_download_cluster_materials,
                      cluster_materials, gen_cluster_config, gen_cluster_inventory,
                      gen_upgrade_sh)
from .compose import (gen_backup_conf, gen_compose, gen_env, plugin_ctx,
                      resolve_db, validate_compose_with_docker)
from .manifest import build_summary_lines, gen_images_txt, gen_manifest_sh
from .multinode import (gen_distribute_sh, gen_node_install_sh, gen_nodes,
                        gen_proxysql_conf)
from .nginx_conf import gen_nginx_confs, px_ssl_rel
from .nacos_sql import build_nacos_sql
from .paths import (BASE_DIR, CLUSTER_SH, DIST_DIR, HTML_FILE, LOGOS_JS, SERVER_SH,
                    SQL_DIR, TPL_DIR, WAREHOUSE, log)
from .progress import PackProgress
from .util import PackError, bash_quote, has_multihost, is_multihost, proxysql_name, tr
from .validate import check_warehouse, validate_config


CLUSTER_ONLY_SH = """#!/usr/bin/env bash
# 本包仅含 K8s 集群(未勾选中间件): 直接执行集群部署脚本
set -euo pipefail
cd "$(dirname "$0")"
exec bash ./deploy-cluster.sh "$@"
"""


# ---------------------------------------------------------------- 打包

BUNDLE_README = """\
# 中间件离线部署包 (由 packer.py 生成)

## 部署(服务器上, root 执行)
  tar -xzf {bundle}.tar.gz
  cd {bundle}
  ./deploy.sh

## 完整性校验(可选, 建议传输后先校验)
  把 {bundle}.tar.gz 与 {bundle}.tar.gz.sha256 放同一目录, 执行:
  sha256sum -c {bundle}.tar.gz.sha256
  (deploy.sh 启动时若在同目录找到压缩包与 .sha256 也会自动校验)

## 说明
- 全部部署参数(中间件/端口/密码/数据库/目录)在 manifest.sh, 服务器上零交互
- 脚本幂等, 可重复执行(已导入的库表会自动跳过)
- docker-compose.yml 与 .env 会复制到部署目录 {deploy_dir}; 数据/日志/配置全部持久化在该目录
- 重复部署时旧的 docker-compose.yml/.env 会自动备份到包目录 backup/<时间戳>/
- 账号密码见 .env (部署后位于 {deploy_dir}/.env)
- 目标架构: {arch}; 项目: {project}; 生成时间: {ts}
"""


def pack(cfg, catalog, out_dir=None, progress=None):
    prog = progress if progress is not None else PackProgress()
    cfg, warns = validate_config(cfg, catalog)

    project = cfg["project"]
    arch = cfg["arch"]
    needs_client, client_img = resolve_db(cfg)
    nodes = gen_nodes(cfg, catalog)   # 多机节点安装计划(主节点优先)

    bundle_name = "%s-%s-%s-offline" % (project, "x86" if arch == "amd64" else "arm",
                                        datetime.now().strftime("%Y%m%d-%H%M"))
    out_dir = Path(out_dir) if out_dir else DIST_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = out_dir / (bundle_name + ".tar.gz")

    # 大文件清单(docker/compose 安装包 + 镜像), 先算总量供进度条使用; 纯集群包不带 docker 静态包
    pkg_sub = "x86_64" if arch == "amd64" else "aarch64"
    big_files = []
    if cfg["services"]:
        # 包内文件名取 catalog 登记的实际文件名(deploy.sh 按 docker-*.tgz / docker-compose-linux-* 通配引用,
        # 版本升级只改 versions.json 即可, 不再有第二处版本号要同步)
        big_files += [
            ("%s/packages/%s/%s" % (bundle_name, pkg_sub, Path(catalog["docker"]["packages"][arch]).name),
             BASE_DIR / catalog["docker"]["packages"][arch]),
            ("%s/packages/%s/%s" % (bundle_name, pkg_sub, Path(catalog["compose"]["packages"][arch]).name),
             BASE_DIR / catalog["compose"]["packages"][arch]),
        ]
    _mirror_tars = []
    for fname, rel, _ in gen_images_txt(cfg, catalog):
        big_files.append(("%s/images/%s" % (bundle_name, fname), BASE_DIR / rel))
        if _tar_has_mirror_prefix(BASE_DIR / rel):
            _mirror_tars.append(fname)
    if _mirror_tars:
        warns.append("镜像 tar 仓库名带国内镜像源前缀(补料时 crane 兜底产物): %s; 部署脚本会自动重打标为原生短名, "
                     "建议空闲时重跑补料脚本让 tar 内为原生短名|"
                     "Image tars carry mirror-prefixed refs: %s; deploy scripts retag automatically"
                     % (", ".join(_mirror_tars), ", ".join(_mirror_tars)))
    for nd in nodes:   # 多机节点镜像(随节点分发)
        for fname, rel, _ in nd["images"]:
            big_files.append(("%s/nodes/%s/images/%s" % (bundle_name, nd["name"], fname), BASE_DIR / rel))

    # ---- K8s 集群物料(kk / artifact 或二进制缓存 / OS 依赖包), 逐文件登记 ----
    cluster_on = bool((cfg.get("cluster") or {}).get("enabled"))
    cluster_small = {}      # bundle内路径 -> 内容字符串
    cluster_md5 = ""
    if cluster_on:
        prog.stage("收集 K8s 集群物料")
        cl_missing, cl_items = cluster_materials(cfg, catalog)
        if cl_missing:
            # 缺失物料若属于可下载组件(二进制缓存), 自动从国内可达源补齐(带每组件进度);
            # kk 二进制/artifact 产物无法自动生成, 明确提示备料途径。
            auto = _auto_download_cluster_materials(cfg, catalog, cl_missing, prog)
            warns += auto["warnings"]
            if auto["downloaded_mb"]:
                warns.append("已自动下载缺失集群物料 %.0f MB (打包机需可达 dl.k8s.io / 华为云镜像 / GitHub 镜像)"
                             "|Auto-downloaded %.0f MB of missing cluster materials" % (auto["downloaded_mb"], auto["downloaded_mb"]))
            cl_missing, cl_items = cluster_materials(cfg, catalog)
            art_detail = ""   # artifact 构建失败详情(供最终报错引用)
            if cl_missing and cfg["cluster"]["mode"] == "artifact" and any("artifact 产物" in m for m in cl_missing):
                # artifact 产物可在打包机构建(需 Docker Desktop): 自动执行 kk artifact export
                art_ok, art_detail = _auto_build_artifact(cfg, catalog, prog)
                if art_ok:
                    warns.append("已自动构建 kk artifact 产物(打包机 Docker 拉取全量镜像)"
                                 "|kk artifact bundle built automatically at pack time")
                    cl_missing, cl_items = cluster_materials(cfg, catalog)
                elif art_detail:
                    warns.append("artifact 自动构建失败: %s|artifact auto-build failed: %s" % (art_detail, art_detail))
                    print("[WARN] artifact 自动构建失败: %s" % art_detail)
            if cl_missing:
                hints = [m for m in cl_missing]
                raise PackError("集群物料不全(自动补齐后仍缺):\n" + "\n".join("  - %s" % m for m in hints) +
                                ("\nartifact 自动构建失败详情: %s" % art_detail if art_detail else "") +
                                "\n二进制组件可用 python prepare_cluster.py --download 自动下载;"
                                "\nartifact 产物已尝试自动构建(需 Docker Desktop), 也可手动: python prepare_cluster.py --artifact-export 后执行生成的 .bat"
                                "|Incomplete cluster materials after auto-download; see docs")
        cache_hash_lines = []   # cache 模式: 二进制完整性清单(sha256sum -c 兼容)
        for rel, src in cl_items:
            if src.is_file():
                big_files.append(("%s/%s" % (bundle_name, rel), src))
                if rel.startswith("cluster/") and rel.endswith(".tar.gz") and cfg["cluster"]["mode"] == "artifact":
                    cluster_md5 = hashlib.md5(src.read_bytes()).hexdigest()
            else:   # 缓存/OS包目录: 展开为单文件
                for f in sorted(src.rglob("*")):
                    if f.is_file():
                        arc = "%s/%s/%s" % (bundle_name, rel, f.relative_to(src))
                        big_files.append((arc, f))
                        if rel.startswith("cluster/cache"):
                            # 清单路径 = bundle 内 cluster/ 之后的相对路径(sha256sum -c 于 cluster/ 下执行)
                            inner = "/".join([rel[len("cluster/"):],
                                              str(f.relative_to(src)).replace("\\", "/")])
                            cache_hash_lines.append("%s  %s" % (
                                hashlib.sha256(f.read_bytes()).hexdigest(), inner))
        if cache_hash_lines:
            cluster_small["cluster/cache.sha256"] = "\n".join(cache_hash_lines) + "\n"
        cluster_small["cluster/inventory.yaml"] = gen_cluster_inventory(cfg)
        cluster_small["cluster/config.yaml"] = gen_cluster_config(cfg, catalog)
        if cluster_md5:
            cluster_small["cluster/artifact.md5"] = cluster_md5 + "\n"
        cluster_small["deploy-cluster.sh"] = CLUSTER_SH.read_text(encoding="utf-8")
        if (TPL_DIR / "uninstall-cluster.sh").is_file():
            cluster_small["uninstall-cluster.sh"] = (TPL_DIR / "uninstall-cluster.sh").read_text(encoding="utf-8")
        else:
            uc = BASE_DIR / "server" / "uninstall-cluster.sh"
            if uc.is_file():
                cluster_small["uninstall-cluster.sh"] = uc.read_text(encoding="utf-8")
        # 集群升级包: 目标版本 kube 三件套 + 一键升级脚本
        up_to = (cfg["cluster"].get("upgrade_to") or "").strip()
        if up_to:
            kube_src = BASE_DIR / ("warehouse/cluster/kube/%s/%s" % (up_to, arch))
            for b in ("kubeadm", "kubelet", "kubectl"):
                big_files.append(("%s/cluster/upgrade/%s/%s/%s" % (bundle_name, up_to, arch, b), kube_src / b))
            cluster_small["upgrade-cluster.sh"] = gen_upgrade_sh(cfg)

    # ---- Java 运行时物料(Temurin JDK): 缺料时打包机自动下载, 包内顶层 java/ 共享单份 ----
    java_on = bool((cfg.get("java") or {}).get("enabled"))
    java_sha_lines = []
    if java_on:
        prog.stage("收集 Java 运行时物料")
        import prepare_java
        jwarns, jmissing = [], []
        try:
            jmissing, jwarns = prepare_java.ensure(cfg["java"]["versions"], arch)
        except ImportError:
            jmissing = ["prepare_java.py 模块加载失败"]
        warns += ["%s|%s" % (w, w) for w in jwarns]
        if jmissing:
            raise PackError("Java 运行时物料不全(自动下载失败):\n" + "\n".join("  - %s" % m for m in jmissing) +
                            "\n可手动执行: python prepare_java.py --download 后重新打包"
                            "|Incomplete Java runtime materials; run: python prepare_java.py --download then repack")
        vers_json = prepare_java._load_versions()
        for v in cfg["java"]["versions"]:
            meta = prepare_java.resolved_of(vers_json, v, arch)
            if not meta or not (prepare_java.java_dir(arch) / meta["filename"]).is_file():
                raise PackError("Java %s 物料元数据缺失(resolve 失败): %s|Missing Java %s material metadata"
                                % (v, meta, v))
            big_files.append(("%s/java/jdk%s.tar.gz" % (bundle_name, v),
                              prepare_java.java_dir(arch) / meta["filename"]))
            java_sha_lines.append("%s  jdk%s.tar.gz" % (meta.get("sha256", ""), v))
        if not cfg["services"]:
            warns.append("纯集群包不安装 Java 运行时(deploy.sh 链路未包含): JDK 包已置于包内 java/ 目录可手动解压, "
                         "或勾选至少一个中间件|Cluster-only bundle does not auto-install Java; JDK tars are under "
                         "java/ for manual install, or select at least one middleware")

    total_bytes = sum(p.stat().st_size for _, p in big_files)
    prog.begin(total_bytes)

    # ---- SQL ----
    prog.stage("准备 SQL")
    sql_files = {}   # bundle内路径 -> 内容
    if "nacos" in cfg["services"]:
        sql_files["sql/nacos.sql"] = build_nacos_sql(cfg, catalog, prog)
    if "xxljob" in cfg["services"]:
        xxl = SQL_DIR / "xxl-job.sql"
        if not xxl.is_file():
            raise PackError("缺少 sql/xxl-job.sql")
        xxl_sql = xxl.read_text(encoding="utf-8")
        # 控制台 admin 初始密码 = 登记的 XXL_JOB_ADMIN_PASSWORD: 打包时按该值重算 SQL 内的 sha256
        admin_pw = str(cfg.get("secrets", {}).get("XXL_JOB_ADMIN_PASSWORD", "")).strip()
        if admin_pw:
            xxl_sql = re.sub(
                r"(VALUES \(1, 'admin', ')[0-9a-f]{64}(')",
                lambda m: m.group(1) + hashlib.sha256(admin_pw.encode("utf-8")).hexdigest() + m.group(2),
                xxl_sql, count=1)
        sql_files["sql/xxl-job.sql"] = xxl_sql

    # ---- 配置模板 ----
    # bundle 内 conf/ 目录结构 = 部署目录结构(deploy.sh 整体拷贝到 $DEPLOY_DIR 下)
    conf_files = {}  # bundle内路径 -> 源文件Path 或 内容字符串
    if "nginx" in cfg["services"]:
        conf_files["conf/nginx/nginx.conf"] = TPL_DIR / "nginx.conf"
        wizard_conf = gen_nginx_confs(cfg, catalog)
        conf_files["conf/nginx/conf/default.conf"] = wizard_conf if wizard_conf is not None \
            else (TPL_DIR / "default.conf").read_text(encoding="utf-8")
        conf_files["conf/nginx/html/index.html"] = TPL_DIR / "index.html"
        # 向导上传的 HTTPS 证书随包分发(容器内挂载于 /etc/nginx/ssl/wizard/...)
        for i, s in enumerate(cfg.get("proxies") or []):
            if s.get("ssl"):
                rel = px_ssl_rel(i, s)
                conf_files["conf/nginx/ssl/%s/fullchain.pem" % rel] = s["cert_pem"]
                conf_files["conf/nginx/ssl/%s/privkey.pem" % rel] = s["key_pem"]
    if "redis" in cfg["services"]:
        conf_files["conf/redis/redis.conf"] = TPL_DIR / "redis.conf"
        if (cfg.get("topology") or {}).get("redis") == "sentinel" and not is_multihost(cfg, "redis"):
            _rp = cfg["secrets"].get("REDIS_PASSWORD", "")
            conf_files["conf/redis/sentinel.conf"] = (
                "# 由打包器生成 (Redis 哨兵配置, 重新部署时自动覆盖)\n"
                "port 26379\n"
                # 监控目标用 compose 服务名(主机名), Redis 6.2+ 需显式开启域名解析
                "sentinel resolve-hostnames yes\n"
                "sentinel announce-hostnames yes\n"
                "sentinel monitor mymaster redis 6379 2\n"
                "sentinel auth-pass mymaster %s\n"
                "sentinel down-after-milliseconds mymaster 5000\n"
                "sentinel failover-timeout mymaster 60000\n"
                "sentinel parallel-syncs mymaster 1\n" % _rp)

    # MySQL 配置文件挂载出来(容器内 /etc/mysql/conf.d/my.cnf), 主库/从库各一份; 多机时在节点包内
    if "mysql8" in cfg["services"] or "mysql57" in cfg["services"]:
        for _ms in ("mysql8", "mysql57"):
            if _ms in cfg["services"] and not is_multihost(cfg, _ms):
                conf_files["conf/%s/my.cnf" % _ms] = TPL_DIR / "mysql-my.cnf"
                if (cfg.get("topology") or {}).get(_ms) == "master-slave":
                    conf_files["conf/%s-replica/my.cnf" % _ms] = TPL_DIR / "mysql-my.cnf"
                    if (cfg.get("replicas") or {}).get(_ms, 1) == 2:
                        conf_files["conf/%s-replica2/my.cnf" % _ms] = TPL_DIR / "mysql-my.cnf"

    # ProxySQL 读写分离配置(可选组件, 每个主从 MySQL 独立实例)
    for _px in ("mysql8", "mysql57"):
        if (cfg.get("features") or {}).get("proxysql_" + _px):
            conf_files["conf/%s/proxysql.cnf" % proxysql_name(cfg, _px)] = gen_proxysql_conf(cfg, _px)

    # ---- 插件附带的配置文件(可选 conf_files 钩子) ----
    for s in cfg["services"]:
        if s in PLUGINS and hasattr(PLUGINS[s], "conf_files"):
            conf_files.update(PLUGINS[s].conf_files(cfg, cfg["ports"], plugin_ctx(cfg)))

    # ---- 生成 ----
    prog.stage("生成配置文件")
    summary_lines = build_summary_lines(cfg, catalog)
    if cfg["services"]:
        compose_text = gen_compose(cfg, catalog)
        env_text = gen_env(cfg, catalog)
        images = gen_images_txt(cfg, catalog)
        images_txt = "# tar文件|统一短名\n" + "".join("%s|%s\n" % (f, short) for f, _, short in images)
        warns += validate_compose_with_docker(compose_text, env_text)
    else:
        compose_text, env_text, images, images_txt = "", "", [], ""
    manifest_text = gen_manifest_sh(cfg, catalog, client_img, summary_lines, bundle_name=bundle_name)
    for nd in nodes:   # 节点 compose 同样过一遍 docker 校验
        warns += validate_compose_with_docker(nd["compose"], nd["env"])

    cl_json = dict(cfg.get("cluster") or {})
    if cl_json:
        cl_json.pop("nodes", None)
        cl_json["nodes"] = [{"name": n["name"], "ip": n["ip"], "role": n["role"]}
                            for n in (cfg.get("cluster") or {}).get("nodes", [])]
    manifest_json = {
        "project": project, "arch": arch, "generated_at": datetime.now().isoformat(timespec="seconds"),
        "services": cfg["services"], "ports": cfg["ports"], "secrets": cfg["secrets"],
        "deploy_dir": cfg["deploy_dir"], "docker_data_root": cfg["docker_data_root"],
        "registry_mirrors": cfg["registry_mirrors"],
        "db": cfg["db"], "extra_ports": cfg.get("extra_ports") or {},
        "proxies": cfg.get("proxies") or [],
        "backup": cfg.get("backup") or {},
        "mysql_client_image": client_img,
        "servers": [{"name": s.get("name"), "user": s.get("user"), "ip": s.get("ip"),
                     "ssh": s.get("ssh"), "auth": "password" if s.get("password") else "key"}
                    for s in (cfg.get("servers") or [])],
        "multihost": cfg.get("multihost") or {},
        "replicas": cfg.get("replicas") or {},
        "features": cfg.get("features") or {},
        "nodes": [{"name": n["name"], "ip": n["server"]["ip"], "roles": n["roles"]} for n in nodes],
        "cluster": cl_json,
    }

    readme = BUNDLE_README.format(bundle=bundle_name, deploy_dir=cfg["deploy_dir"],
                                  arch=arch, project=project,
                                  ts=datetime.now().strftime("%Y-%m-%d %H:%M"))

    small_files = [
        ("%s/deploy.sh" % bundle_name,
         CLUSTER_ONLY_SH.encode("utf-8") if (cluster_on and not cfg["services"]) else SERVER_SH.read_bytes(), 0o755),
        ("%s/manifest.sh" % bundle_name, manifest_text.encode("utf-8"), 0o600),
        ("%s/manifest.json" % bundle_name,
         json.dumps(manifest_json, ensure_ascii=False, indent=2).encode("utf-8"), 0o600),
        ("%s/README.txt" % bundle_name, readme.encode("utf-8"), 0o644),
    ]
    if cfg["services"]:   # 纯集群包不带中间件编排
        small_files += [
            ("%s/docker-compose.yml" % bundle_name, compose_text.encode("utf-8"), 0o644),
            ("%s/.env" % bundle_name, env_text.encode("utf-8"), 0o600),
            ("%s/images.txt" % bundle_name, images_txt.encode("utf-8"), 0o644),
            ("%s/images/retag-mirrors.sh" % bundle_name, gen_retag_mirrors_sh().encode("utf-8"), 0o755),
        ]
    for rel, content in cluster_small.items():
        mode = 0o755 if rel.endswith(".sh") else 0o644
        small_files.append(("%s/%s" % (bundle_name, rel), content.encode("utf-8"), mode))
    for rel, content in sql_files.items():
        small_files.append(("%s/%s" % (bundle_name, rel), content.encode("utf-8"), 0o644))
    if java_on:
        small_files.append(("%s/java/jdk.sha256" % bundle_name,
                            ("\n".join(java_sha_lines) + "\n").encode("utf-8"), 0o644))
    for rel, src in conf_files.items():
        data = src.read_text(encoding="utf-8") if isinstance(src, Path) else src
        small_files.append(("%s/%s" % (bundle_name, rel), data.encode("utf-8"), 0o644))
    if any(s in ("mysql57", "mysql8", "postgres", "mongodb") for s in cfg["services"]):
        small_files.append(("%s/restore.sh" % bundle_name,
                            (TPL_DIR / "restore.sh").read_text(encoding="utf-8").encode("utf-8"), 0o755))
    if any(e.get("enabled") for e in (cfg["backup"].get("engines") or {}).values()):
        small_files.append(("%s/backup.sh" % bundle_name,
                            (TPL_DIR / "backup.sh").read_text(encoding="utf-8").encode("utf-8"), 0o755))
        small_files.append(("%s/backup.conf" % bundle_name,
                            gen_backup_conf(cfg).encode("utf-8"), 0o600))
    # 一键卸载脚本: 中间件包随包提供 (停容器→可选删数据卷→清 crontab→保留物料)
    if cfg["services"]:
        small_files.append(("%s/uninstall.sh" % bundle_name,
                            (TPL_DIR / "uninstall.sh").read_text(encoding="utf-8").encode("utf-8"), 0o755))

    # ---- 多机节点产物 ----
    if nodes:
        for nd in nodes:
            base = "%s/nodes/%s" % (bundle_name, nd["name"])
            node_compose = nd["compose"]
            small_files.append(("%s/install-node.sh" % base,
                                gen_node_install_sh(cfg, nd).encode("utf-8"), 0o755))
            if nd["compose_parts"]:   # 纯 Java 节点无容器服务, 不写 compose/.env/镜像清单
                small_files.append(("%s/docker-compose.yml" % base, node_compose.encode("utf-8"), 0o644))
                small_files.append(("%s/.env" % base, nd["env"].encode("utf-8"), 0o600))
                small_files.append(("%s/images.txt" % base,
                                    ("# tar文件|统一短名\n" + "".join("%s|%s\n" % (f, s) for f, _, s in nd["images"])).encode("utf-8"),
                                    0o644))
                small_files.append(("%s/images/retag-mirrors.sh" % base,
                                    gen_retag_mirrors_sh().encode("utf-8"), 0o755))
            small_files.append(("%s/uninstall.sh" % base,
                                (TPL_DIR / "uninstall.sh").read_text(encoding="utf-8").encode("utf-8"), 0o755))
            for rel, src in nd["conf_files"].items():
                data = src.read_text(encoding="utf-8") if isinstance(src, Path) else src
                small_files.append(("%s/%s" % (base, rel), data.encode("utf-8"), 0o644))
            if nd.get("backup"):
                small_files.append(("%s/backup.sh" % base,
                                    (TPL_DIR / "backup.sh").read_text(encoding="utf-8").encode("utf-8"), 0o755))
                small_files.append(("%s/backup.conf" % base,
                                    ("\n".join(["# 由 packer.py 自动生成 (节点备份配置)",
                                                "BACKUP_DIR=%s" % nd["backup"]["BACKUP_DIR"],
                                                "MYSQL_ENABLED=%s" % nd["backup"]["MYSQL_ENABLED"],
                                                "MYSQL_SERVICES=%s" % nd["backup"]["MYSQL_SERVICES"],
                                                "MYSQL_KEEP=%s" % nd["backup"]["MYSQL_KEEP"],
                                                "MYSQL_ROOT_PASSWORD=%s" % nd["backup"]["MYSQL_ROOT_PASSWORD"]]) + "\n").encode("utf-8"),
                                    0o600))
        small_files.append(("%s/distribute.sh" % bundle_name,
                            gen_distribute_sh(cfg, nodes, catalog).encode("utf-8"), 0o755))
        warns += ["多机部署: 打包完成后请检查 nodes/ 各节点配置, 在主部署机执行 ./distribute.sh 分发安装"
                  "|Multi-host: review nodes/ configs, then run ./distribute.sh on the main host"]
    if cluster_on:
        warns += ["包内含 K8s 集群: 服务器上 ./deploy.sh 会先部署集群(多节点 SSH, 耗时较长)再继续中间件部分"
                  "|Bundle includes a K8s cluster: ./deploy.sh provisions the cluster first (multi-node SSH, slow), then middleware"]
    total_bytes += sum(len(d) for _, d, _ in small_files)
    prog.set_total(total_bytes)

    def write_bytes(tf, arcname, data, mode=0o644):
        info = tarfile.TarInfo(name=arcname)
        info.size = len(data)
        info.mode = mode
        info.mtime = int(datetime.now().timestamp())
        tf.addfile(info, io.BytesIO(data))

    class _CountingReader:
        """包装文件对象, 边压缩边上报进度"""
        def __init__(self, fobj):
            self._f = fobj

        def read(self, n=-1):
            prog.check()
            b = self._f.read(n)
            if b:
                prog.add(len(b))
            return b

    def add_file(tf, arcname, src, mode=0o644):
        info = tf.gettarinfo(name=str(src), arcname=arcname)
        info.mode = mode
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        with open(src, "rb") as f:
            tf.addfile(info, _CountingReader(f))

    log.info("开始打包: %s", bundle_path)
    try:
        with tarfile.open(bundle_path, "w:gz", compresslevel=6) as tf:
            for arcname, data, mode in small_files:
                prog.check()
                write_bytes(tf, arcname, data, mode=mode)
                prog.add(len(data))
            for arcname, src in big_files:
                prog.check()
                prog.file(Path(arcname).name, src.stat().st_size)
                # kk 是部署脚本要直接执行其二进制, 权位必须保留(Windows 侧源文件常无 x 位)
                add_file(tf, arcname, src, mode=0o755 if arcname.endswith("/kk") else 0o644)
    except Exception:
        # 半成品直接删掉, 避免留下损坏的包
        try:
            bundle_path.unlink()
        except OSError:
            pass
        raise

    size_mb = bundle_path.stat().st_size / 1048576
    # SHA256 校验文件, 格式兼容 sha256sum -c; 供服务器传输后校验完整性
    prog.stage("计算 SHA256")
    sha = hashlib.sha256()
    with open(bundle_path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            sha.update(chunk)
    digest = sha.hexdigest()
    sha_path = bundle_path.with_suffix(bundle_path.suffix + ".sha256")
    # 强制 LF: sha256sum -c 在 Linux 上会把 \r 当作文件名的一部分
    sha_path.write_text("%s  %s\n" % (digest, bundle_path.name), encoding="utf-8", newline="\n")
    log.info("打包完成: %s (%.0f MB, sha256=%s...)", bundle_path, size_mb, digest[:16])
    result = {
        "path": str(bundle_path),
        "name": bundle_name,
        "size_mb": round(size_mb),
        "sha256": digest,
        "images": len(images),
        "cluster": cluster_on,
        "warnings": warns,
    }
    prog.finish(result)
    return result


def missing_materials(catalog, archs=("amd64", "arm64")):
    """盘点仓库缺失的镜像物料: [(镜像引用, 输出相对路径, 平台)]"""
    items = []
    for arch in archs:
        for s, meta in catalog["services"].items():
            rel = meta["images"].get(arch)
            if rel and not (BASE_DIR / rel).is_file():
                ref = "%s:%s" % (meta["image"], meta["tag"])
                items.append((ref, rel, "linux/%s" % arch))
        mod = PLUGINS.get("kafka")
        cl = getattr(mod, "CLUSTER", None) if mod else None
        if cl and arch in cl.get("images", {}) and not (BASE_DIR / cl["images"][arch]).is_file():
            items.append(("%s:%s" % (cl["image"], cl["tag"]), cl["images"][arch], "linux/%s" % arch))
    return items


def _tar_has_mirror_prefix(path):
    """检查 docker save tar 的 manifest.json RepoTags 是否含镜像源前缀仓库"""
    try:
        with tarfile.open(path, "r:") as tf:
            return _tar_repo_tags(tf)
    except Exception:
        pass
    try:
        with tarfile.open(path, "r:gz") as tf:
            return _tar_repo_tags(tf)
    except Exception:
        pass
    return False


def _tar_repo_tags(tf):
    """读取 docker save tar(旧式 manifest.json 或 OCI index.json)的仓库引用"""
    names = set(tf.getnames())
    tags = []
    if "manifest.json" in names:
        mf = tf.extractfile("manifest.json")
        if mf:
            for item in json.loads(mf.read().decode("utf-8", "replace")):
                tags += (item.get("RepoTags") or [])
    elif "index.json" in names:
        idx = tf.extractfile("index.json")
        if idx:
            for e in json.loads(idx.read().decode("utf-8", "replace")).get("manifests") or []:
                tags.append((e.get("annotations") or {}).get("org.opencontainers.image.ref.name", ""))
    for t in tags:
        if t and t.split("/")[0] in MIRROR_PREFIXES:
            return True
    return False


MIRROR_PREFIXES = ("docker.1ms.run", "docker.1panel.live", "docker.xuanyuan.me")


def gen_retag_mirrors_sh():
    """生成 images/retag-mirrors.sh: 镜像仓库名归一化。

    仓库镜像 tar 的仓库名有多种形态(补料走国内源/官方多级命名空间所致):
      nginx:1.31.5-amd64(架构后缀) / docker.1ms.run/library/rabbitmq:4.3.5(镜像源+library)
      / docker.1ms.run/apache/kafka:4.3.1(镜像源前缀) / docker.elastic.co/elasticsearch/elasticsearch:9.3.0(多级命名空间)
    离线机 compose 按原生短名取镜像, 找不到会触发联网拉取失败。docker load 全部 tar
    后执行本脚本, 与 deploy.sh normalize_image 同规则: 仓库名最后一段与短名一致且
    tag 匹配(含 -amd64/-arm64 后缀)即重打标回原生短名。
    """
    lines = [
        "#!/usr/bin/env bash",
        "# 镜像重打标 (由打包器生成): 兼容镜像源前缀(docker.1ms.run/xxx)、library/ 前缀、",
        "# 架构后缀(-amd64/-arm64)、多级命名空间(docker.elastic.co/ns/xxx) 四类仓库名,",
        "# 统一 tag 回 images.txt 里的原生短名(与 deploy.sh normalize_image 同规则)。",
        "# 用法: docker load 全部 tar 后执行  bash images/retag-mirrors.sh [images.txt] [arch]",
        "set -u",
        'cd "$(dirname "$0")/.."',
        'TXT="${1:-images.txt}"',
        'ARCH="${2:-}"; [ -n "$ARCH" ] || { case "$(uname -m)" in aarch64|arm64) ARCH=arm64 ;; *) ARCH=amd64 ;; esac; }',
        '[ -f "$TXT" ] || exit 0',
        'IMAGES="$(docker images --format \'{{.Repository}}:{{.Tag}}\' 2>/dev/null)"',
        'while IFS="|" read -r file short; do',
        '  case "$file" in ""|"#"*) continue ;; esac',
        '  [ -n "$short" ] || continue',
        '  case "$short" in *:*) ;; *) continue ;; esac',
        '  docker image inspect "$short" >/dev/null 2>&1 && continue',
        '  base="${short%%:*}"; tag="${short#*:}"; tail="${base##*/}"; found=""',
        '  for rt in $IMAGES; do',
        '    case "$rt" in *:*) ;; *) continue ;; esac',
        '    case "${rt##*:}" in "$tag"|"$tag-$ARCH") ;; *) continue ;; esac',
        '    [ "${rt%:*}" = "$base" ] && { found="$rt"; break; }',
        '    [ "${rt%:*}" = "library/$base" ] && { found="$rt"; break; }',
        '    case "${rt%:*}" in */"$tail") found="$rt"; break ;; esac',
        '  done',
        '  if [ -n "$found" ]; then',
        '    docker tag "$found" "$short" && echo "[retag] $found -> $short"',
        '  else',
        '    echo "[WARN] $short 镜像缺失(tar 内无可归一化副本), 请重跑补料脚本"',
        '  fi',
        'done < "$TXT"',
        "",
    ]
    return "\n".join(lines)


def gen_pull_script(catalog, archs=("amd64", "arm64")):
    """生成缺失物料一键补齐脚本 (docker pull 多源回退 -> crane 兜底)"""
    from datetime import datetime as _dt
    items = missing_materials(catalog, archs)
    lines = [
        "#!/usr/bin/env bash",
        "# 缺失物料一键补齐脚本 (由 Local Bundle Studio 生成于 %s)" % _dt.now().strftime("%Y-%m-%d %H:%M"),
        "# 用法: 在仓库根目录(含 warehouse/)执行:  bash pull_missing_images.sh",
        "# 范围: 仅中间件镜像物料; K8s 集群物料(kk/二进制缓存/离线镜像包)打包时自动补齐,",
        "#       或用 python prepare_cluster.py 单独准备。",
        "# 逻辑: 依次尝试国内镜像源 docker pull -> docker tag -> docker save 落盘到规范路径;",
        "#       全部失败且有 tools/bin/crane 时回退 crane 拉取(load 后重存, 保证 tar 仓库名规范)。",
        "set -uo pipefail",
        'cd "$(dirname "$0")"',
        "",
        'MIRRORS=(docker.1ms.run docker.1panel.live docker.xuanyuan.me)',
        'CRANE="tools/bin/crane"; [ -x "$CRANE.exe" ] && CRANE="$CRANE.exe"',
        "",
        "pull_tar() { # $1=image:tag $2=platform $3=out.tar",
        '  local img="$1" plat="$2" out="$3" m full',
        '  if [ -s "$out" ]; then echo "SKIP $out (已存在)"; return 0; fi',
        '  mkdir -p "$(dirname "$out")"',
        '  case "$img" in */*) ;; *) img="library/$img" ;; esac   # 官方库镜像走 library/ 前缀',
        '  for m in "${MIRRORS[@]}"; do',
        '    full="$m/$img"',
        '    if command -v docker >/dev/null 2>&1 && docker pull --platform "$plat" "$full"; then',
        '      local short="${img#library/}"',
        '      docker tag "$full" "$short"',
        '      docker save "$short" -o "$out"',
        '      docker rmi "$full" "$short" >/dev/null 2>&1 || true',
        '      echo "[ok] $out"',
        '      return 0',
        "    fi",
        "  done",
        '  if [ -x "$CRANE" ]; then',
        '    for m in "${MIRRORS[@]}"; do',
        '      if "$CRANE" pull --platform="$plat" "$m/$img" "$out.tmp"; then',
        '        if command -v docker >/dev/null 2>&1 && docker load -i "$out.tmp" 2>/dev/null; then',
        '          local short="${img#library/}"',
        '          docker tag "$m/$img" "$short" 2>/dev/null || true',
        '          docker save "$short" -o "$out"',
        '          docker rmi "$m/$img" "$short" >/dev/null 2>&1 || true',
        "        else",
        '          mv "$out.tmp" "$out"   # 无 docker: crane 产物直接落位(仓库名含镜像源前缀, docker load 后可正常使用)',
        "        fi",
        '        rm -f "$out.tmp"',
        '        echo "[ok] $out"',
        '        return 0',
        "      fi",
        "    done",
        "  fi",
        '  echo "[FAIL] $img ($plat) 全部镜像源失败, 请检查网络或手工放置到: $out"',
        "  return 1",
        "}",
        "",
        "FAIL=0",
    ]
    for ref, rel, plat in items:
        lines.append('pull_tar %s %s %s || FAIL=1' % (bash_quote(ref), bash_quote(plat), bash_quote(rel)))
    lines += [
        "",
        'if [ "$FAIL" -eq 0 ]; then echo "==== 全部缺失物料已补齐 ===="; else echo "==== 部分物料失败, 见上方 [FAIL] ===="; exit 1; fi',
        "",
    ]
    return "\n".join(lines), items

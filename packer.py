#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中间件离线包本地打包器

用法:
  python packer.py                        # 启动本地 Web 界面(推荐)
  python packer.py --port 8765            # 指定端口
  python packer.py --config proj.json     # 命令行模式: 按配置文件直接打包
  python packer.py --example              # 生成配置文件样例 example-config.json

功能:
  - 读取 versions.json 物料目录, 校验离线包是否齐全
  - 根据页面选择生成 docker-compose.yml / .env / manifest.sh / images.txt
  - 自动从 nacos 镜像 tar 中提取建表 SQL 并附加建库语句(纯 python, 不依赖本机 docker)
  - 只把本次勾选的中间件镜像 + 对应架构的 docker/compose 安装包打进离线包
  - 产物: dist/<项目名>-<架构>-<日期>-offline.tar.gz
"""
import argparse
import base64
import importlib.util
import io
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CATALOG_FILE = BASE_DIR / "versions.json"
PLUGINS_DIR = BASE_DIR / "plugins"
LOGOS_JS = BASE_DIR / "packer_logos.js"
SERVER_SH = BASE_DIR / "server" / "deploy.sh"
CLUSTER_SH = BASE_DIR / "server" / "deploy-cluster.sh"
CLUSTER_ONLY_SH = """#!/usr/bin/env bash
# 本包仅含 K8s 集群(未勾选中间件): 直接执行集群部署脚本
set -euo pipefail
cd "$(dirname "$0")"
exec bash ./deploy-cluster.sh "$@"
"""
HTML_FILE = BASE_DIR / "packer.html"
DIST_DIR = BASE_DIR / "dist"
SQL_DIR = BASE_DIR / "sql"
TPL_DIR = BASE_DIR / "templates"
WAREHOUSE = BASE_DIR / "warehouse"

log = logging.getLogger("packer")

# ---------------------------------------------------------------- 基础工具

class PackError(Exception):
    """打包失败(用户可理解的错误)"""


# 可插拔中间件插件表: {service_key: module}, 由 load_catalog() 填充
PLUGINS = {}


def _load_plugins():
    """加载 plugins/ 目录下的中间件插件; 下划线开头的文件视为模板不加载"""
    plugins = {}
    if not PLUGINS_DIR.is_dir():
        return plugins
    for f in sorted(PLUGINS_DIR.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location("mw_plugin_%s" % f.stem, f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:
            log.error("插件加载失败 %s: %s", f.name, e)
            continue
        missing = [a for a in ("SERVICE_KEY", "META", "compose_block") if not hasattr(mod, a)]
        if missing:
            log.error("插件 %s 缺少 %s, 已跳过", f.name, "/".join(missing))
            continue
        if mod.SERVICE_KEY in plugins:
            log.error("插件 %s 的 SERVICE_KEY %s 重名, 已跳过", f.name, mod.SERVICE_KEY)
            continue
        plugins[mod.SERVICE_KEY] = mod
        log.info("已加载中间件插件: %s (%s)", mod.SERVICE_KEY, mod.META.get("label", ""))
    return plugins


def load_catalog():
    global PLUGINS
    with open(CATALOG_FILE, "r", encoding="utf-8") as f:
        catalog = json.load(f)
    # 合并插件(可插拔中间件): 服务/密钥字段与内置中间件完全同构
    PLUGINS = _load_plugins()
    for key, mod in PLUGINS.items():
        meta = dict(mod.META)
        meta["is_plugin"] = True
        catalog["services"][key] = meta
        for s in meta.get("secrets", []):
            catalog["secrets"].append(s)
    return catalog


def bash_quote(v):
    """单引号安全转义, 用于生成 manifest.sh"""
    return "'" + str(v).replace("'", "'\\''") + "'"


# ---------------------------------------------------------------- 多机部署(服务器池 + 角色分配)

IP_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")
KRAFT_CLUSTER_ID = "iZWRiSqjZAlYwlKEqHFQWI"

# ProxySQL 读写分离代理(可选组件, 不进中间件网格)
PROXYSQL_META = {
    "image": "proxysql/proxysql", "tag": "2.6.6",
    "images": {"amd64": "warehouse/images/proxysql/2.6.6/amd64.tar",
               "arm64": "warehouse/images/proxysql/2.6.6/arm64.tar"},
}


def valid_ipv4(ip):
    m = IP_RE.match(str(ip or "").strip())
    if not m:
        return False
    return all(0 <= int(g) <= 255 for g in m.groups())


def is_multihost(cfg, svc):
    """某服务的集群形态是否为多机部署"""
    mh = (cfg.get("multihost") or {}).get(svc) or {}
    return bool(mh.get("enabled")) and cfg.get("topology", {}).get(svc) in ("cluster", "master-slave", "sentinel")


def has_multihost(cfg):
    return any(is_multihost(cfg, s) for s in ("kafka", "mysql8", "mysql57", "redis"))


def master_server_ip(cfg, svc):
    """多机 MySQL 主库节点 IP"""
    mh = (cfg.get("multihost") or {}).get(svc) or {}
    idx = mh.get("master")
    servers = cfg.get("servers") or []
    return servers[idx]["ip"] if isinstance(idx, int) and 0 <= idx < len(servers) else ""


def tr(text, lang="zh"):
    """双语约定: 文案写作 "中文|English", 按 lang 取半边; 无竖线时原样返回"""
    s = str(text)
    if "|" in s:
        zh, _, en = s.partition("|")
        return (en if lang == "en" else zh).strip()
    return s


def env_quote(v):
    """生成 .env 值: 含 # 时加双引号(值字符集已禁止双引号/反斜杠/$)"""
    v = str(v)
    if "#" in v or " " in v:
        return '"%s"' % v
    return v


PASSWORD_RE = re.compile(r"^[A-Za-z0-9@#%*+=_.:/~-]+$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
# Webhook 地址: 允许常规 URL 字符(含 & ? = : /), 禁止空格与 shell 危险字符
WEBHOOK_RE = re.compile(r"^[A-Za-z0-9@#%*+=_./:?!&(),;~\[\]-]+$")
PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
LINUX_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]*$")
MIRROR_RE = re.compile(r"^https?://[A-Za-z0-9_.:/-]+$")


def gen_random_password(length=16):
    import secrets
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789@#%*+=_."
    return "".join(secrets.choice(alphabet) for _ in range(length))


def gen_nacos_token():
    import secrets
    return "SecretKey" + base64.b64encode(secrets.token_bytes(48)).decode()


# ---------------------------------------------------------------- 配置校验

def validate_config(cfg, catalog):
    """校验+规范化配置, 返回 (norm_cfg, warnings)"""
    warns = []

    project = str(cfg.get("project", "")).strip().lower()
    if not PROJECT_RE.match(project):
        raise PackError("项目名不合法: 只能小写字母开头, 小写字母/数字/中划线, 2-41 位|Invalid project name: must start with a lowercase letter, 2-41 chars of lowercase letters/digits/hyphens")
    arch = cfg.get("arch")
    if arch not in ("amd64", "arm64"):
        raise PackError("必须选择目标架构 (amd64 / arm64)|Target architecture must be selected (amd64 / arm64)")
    lang = cfg.get("lang") or "zh"
    if lang not in ("zh", "en"):
        lang = "zh"
    cfg["lang"] = lang

    services = cfg.get("services") or []
    cluster_raw = cfg.get("cluster") or {}
    cluster_on = bool(cluster_raw.get("enabled"))
    if not services and not cluster_on:
        raise PackError("至少选择一个中间件或启用 K8s 集群|Select at least one middleware or enable the K8s cluster")
    known = list(catalog["services"].keys())
    for s in services:
        if s not in known:
            raise PackError("未知中间件: %s|Unknown middleware: %s" % (s, s))
        meta = catalog["services"][s]
        if arch not in meta["supported_arch"]:
            raise PackError("%s 不支持 %s 架构 (%s)|%s does not support %s architecture (%s)"
                            % (tr(meta["label"], "zh"), arch, meta.get("note", "").partition("|")[0],
                               tr(meta["label"], "en"), arch, meta.get("note", "").partition("|")[2]))

    # ---- 端口 ----
    ports = cfg.get("ports") or {}
    norm_ports = {}
    seen = {}
    for s in services:
        for p in catalog["services"][s]["ports"]:
            key = p["key"]
            raw = ports.get(key, p["default"])
            try:
                v = int(raw)
            except (TypeError, ValueError):
                raise PackError("端口 %s 不合法: %s|Invalid port for %s: %s" % (tr(p["label"], "zh"), raw, tr(p["label"], "en"), raw))
            if not (1 <= v <= 65535):
                raise PackError("端口 %s 超出范围 1-65535: %s|Port %s out of range 1-65535: %s" % (tr(p["label"], "zh"), v, tr(p["label"], "en"), v))
            # 多机部署的服务跑在不同服务器上, 同端口合法(如多机主从同端口), 不参与查重
            mh_on = str((cfg.get("topology") or {}).get(s, "single")) in ("master-slave", "sentinel", "cluster")                     and ((cfg.get("multihost") or {}).get(s) or {}).get("enabled")
            if v in seen and not mh_on:
                raise PackError("端口冲突: %s 和 %s 都用了 %d|Port conflict: %s and %s both use %d"
                                % (tr(seen[v], "zh"), tr(p["label"], "zh"), v, tr(seen[v], "en"), tr(p["label"], "en"), v))
            if not mh_on:
                seen[v] = p["label"]
            norm_ports[key] = v
    cfg["ports"] = norm_ports

    # ---- 部署形态(可选): mysql8 主从 / redis 哨兵 ----
    topo_raw = cfg.get("topology") or {}
    topology = {}
    if "mysql8" in services:
        m = str(topo_raw.get("mysql8", "single"))
        if m not in ("single", "master-slave"):
            raise PackError("MySQL 部署形态不合法: %s (single / master-slave)|Invalid MySQL topology: %s (single / master-slave)" % (m, m))
        topology["mysql8"] = m
    if "mysql57" in services:
        m = str(topo_raw.get("mysql57", "single"))
        if m not in ("single", "master-slave"):
            raise PackError("MySQL 5.7 部署形态不合法: %s (single / master-slave)|Invalid MySQL 5.7 topology: %s (single / master-slave)" % (m, m))
        topology["mysql57"] = m
    if "redis" in services:
        r = str(topo_raw.get("redis", "single"))
        if r not in ("single", "sentinel"):
            raise PackError("Redis 部署形态不合法: %s (single / sentinel)|Invalid Redis topology: %s (single / sentinel)" % (r, r))
        topology["redis"] = r
    if "kafka" in services:
        k = str(topo_raw.get("kafka", "single"))
        if k not in ("single", "cluster"):
            raise PackError("Kafka 部署形态不合法: %s (single / cluster)|Invalid Kafka topology: %s (single / cluster)" % (k, k))
        topology["kafka"] = k
    cfg["topology"] = topology

    # ---- 多机部署: 服务器池 + 角色分配(可选) ----
    raw_servers = cfg.get("servers") or []
    servers = []
    seen_ips = set()
    for i, sv in enumerate(raw_servers):
        ip = str((sv or {}).get("ip", "")).strip()
        if not ip:
            continue
        if not valid_ipv4(ip):
            raise PackError("服务器 IP 不合法: %s (第 %d 台)|Invalid server IP: %s (#%d)" % (ip, i + 1, ip, i + 1))
        if ip in seen_ips:
            raise PackError("服务器 IP 重复: %s|Duplicate server IP: %s" % (ip, ip))
        seen_ips.add(ip)
        ssh_raw = sv.get("ssh", 22)
        try:
            ssh = int(ssh_raw)
        except (TypeError, ValueError):
            raise PackError("SSH 端口不合法: %s (服务器 %s)|Invalid SSH port: %s (server %s)" % (ssh_raw, ip, ssh_raw, ip))
        if not (1 <= ssh <= 65535):
            raise PackError("SSH 端口超出范围: %d (服务器 %s)|SSH port out of range: %d (server %s)" % (ssh, ip, ssh, ip))
        name = re.sub(r"[^A-Za-z0-9_-]", "", str(sv.get("name") or ("node%d" % (i + 1)))) or ("node%d" % (i + 1))
        password = str(sv.get("password") or sv.get("pass") or "")
        if "|" in password or "\n" in password:
            raise PackError("SSH 密码不能包含 | 或换行符 (服务器 %s)|SSH password must not contain | or newline (server %s)" % (ip, ip))
        servers.append({"name": name, "user": str(sv.get("user") or "root").strip() or "root",
                        "ip": ip, "ssh": ssh, "password": password})
    cfg["servers"] = servers

    # ---- K8s 集群(可选): 角色分配自服务器池, 物料按 catalog["cluster"] ----
    cluster = {"enabled": False}
    if cluster_on:
        if arch != "amd64":
            raise PackError("K8s 集群暂仅支持 amd64, arm64(鲲鹏/飞腾)后续开放|K8s cluster is amd64-only for now; arm64 support coming later")
        # 集群节点 SSH 密码留空 = 按免密连接, 提醒运维预分发密钥
        srv_raw = cfg.get("servers") or []
        role_map = cluster_raw.get("roles") or {}
        empty_pw = [str((srv_raw[int(i)] or {}).get("name") or ("node%s" % (int(i) + 1)))
                    for i in role_map if int(i) < len(srv_raw) and not ((srv_raw[int(i)] or {}).get("password") or (srv_raw[int(i)] or {}).get("pass") or "").strip()]
        if empty_pw:
            warns.append("集群节点未填 SSH 密码: %s — 将按免密 SSH 连接, 部署前需在节点间预分发密钥; 否则请回填密码重新打包"
                         "|Cluster nodes without SSH password: %s — passwordless SSH assumed; distribute keys first or fill passwords and repack"
                         % (", ".join(empty_pw), ", ".join(empty_pw)))
        ccat = catalog.get("cluster") or {}
        if not ccat:
            raise PackError("versions.json 缺少 cluster 物料目录|cluster catalog missing in versions.json")
        kube_ver = str(cluster_raw.get("kube_version") or "")
        if kube_ver not in (ccat.get("versions") or {}):
            raise PackError("K8s 版本不在物料目录: %s (可选 %s)|K8s version not in catalog: %s"
                            % (kube_ver, sorted((ccat.get("versions") or {})), kube_ver))
        mode = str(cluster_raw.get("mode") or "artifact")
        if mode not in ("artifact", "cache", "online"):
            raise PackError("集群离线模式不合法: %s (artifact / cache / online)|Invalid cluster mode: %s" % (mode, mode))
        if mode == "online":
            pass   # 联网安装: 无需 artifact/cache 物料, zone 由配置生成
        # 数据目录(可选覆盖 kk 默认)
        comps_raw = cluster_raw.get("components") or {}
        def _abspath(v, what):
            v = str(v or "").strip()
            if v and not v.startswith("/"):
                raise PackError("%s 需为绝对路径: %s|%s must be an absolute path: %s" % (what, v, what, v))
            return v
        components = {
            "containerd_root": _abspath(comps_raw.get("containerd_root"), "containerd 数据目录"),
            "docker_root": _abspath(comps_raw.get("docker_root"), "docker 数据目录"),
            "etcd_dir": _abspath(comps_raw.get("etcd_dir"), "etcd 数据目录"),
            "static_binary": bool(comps_raw.get("static_binary")),
            "containerd_version_override": str(comps_raw.get("containerd_version_override") or "").strip(),
        }
        if components["containerd_version_override"]:
            # 统一补 v 前缀: 仓库目录/包内 cache 布局均按 vX.Y.Z 命名, 1.7.27 会生成不存在的目录
            if not components["containerd_version_override"].startswith("v"):
                components["containerd_version_override"] = "v" + components["containerd_version_override"]
        if components["containerd_version_override"] and not re.match(r"^v\d+\.\d+", components["containerd_version_override"]):
            raise PackError("containerd 版本覆盖不合法: %s|Invalid containerd version override: %s"
                            % (components["containerd_version_override"], components["containerd_version_override"]))
        if mode == "online" and str(cluster_raw.get("zone") or "cn") == "cn" and components["static_binary"]:
            # 在线+国内源: qingstor 镜像只同步非 static 版 containerd(static-*.tar.gz 404),
            # 静态构建仅离线包/国际源(用户直连 GitHub)可信, 这里自动回退并提示
            components["static_binary"] = False
            warns.append("在线+国内源: 国内镜像未同步 static 版 containerd, 已自动改用非 static 构建"
                         "(老系统兼容性由 containerd 官方发布包保证)|"
                         "Online+cn: domestic mirror lacks static containerd builds, falling back to non-static")
        # kubelet 参数
        kubelet_raw = cluster_raw.get("kubelet") or {}
        kubelet = {}
        if kubelet_raw.get("max_pods"):
            try:
                kubelet["max_pods"] = int(kubelet_raw["max_pods"])
                if not (1 <= kubelet["max_pods"] <= 1000):
                    raise ValueError
            except (TypeError, ValueError):
                raise PackError("max-pods 不合法: %s|Invalid max-pods: %s" % (kubelet_raw["max_pods"], kubelet_raw["max_pods"]))
        extra_args = []
        for kv in (kubelet_raw.get("extra_args") or []):
            kv = str(kv).strip()
            if kv:
                if "=" not in kv:
                    raise PackError("kubelet extra_args 需为 key=value 形式: %s|kubelet extra_args must be key=value: %s" % (kv, kv))
                extra_args.append(kv)
        kubelet["extra_args"] = extra_args
        kubelet["extra_config"] = str(kubelet_raw.get("extra_config") or "")
        # kubelet 数据目录(root-dir 启动参数): 数据盘场景常用
        root_dir = str(kubelet_raw.get("root_dir") or "").strip()
        if root_dir and not root_dir.startswith("/"):
            raise PackError("kubelet 数据目录需为绝对路径: %s|kubelet root-dir must be an absolute path: %s" % (root_dir, root_dir))
        kubelet["root_dir"] = root_dir
        # 镜像加速(docker.io)
        mirrors = [str(m).strip().rstrip("/") for m in (cluster_raw.get("registry_mirrors") or []) if str(m).strip()]
        for m in mirrors:
            if not m.startswith(("http://", "https://")):
                raise PackError("镜像加速地址需以 http(s):// 开头: %s|Registry mirror must start with http(s)://: %s" % (m, m))
        # K8s 组件镜像源前缀(在线/镜像下载走该仓库)
        k8s_image_registry = str(cluster_raw.get("k8s_image_registry") or "").strip().rstrip("/")
        if k8s_image_registry and not re.match(r"^[a-z0-9][a-z0-9._/-]*(:[0-9]+)?$", k8s_image_registry, re.I):
            raise PackError("K8s 镜像源前缀不合法: %s (如 hub.kubesphere.com.cn 或 host:port/namespace)|Invalid K8s image registry prefix: %s"
                            % (k8s_image_registry, k8s_image_registry))
        # NTP
        ntp_raw = cluster_raw.get("ntp") or {}
        ntp = {"enabled": bool(ntp_raw.get("enabled")),
               "servers": [str(s).strip() for s in (ntp_raw.get("servers") or []) if str(s).strip()]}
        if ntp["enabled"] and not ntp["servers"]:
            raise PackError("启用 NTP 时至少填写一台 NTP 服务器|NTP enabled requires at least one server")
        # 存储
        storage_raw = cluster_raw.get("storage") or {}
        storage = {
            "localpv_enabled": bool(storage_raw.get("localpv_enabled")),
            "localpv_path": _abspath(storage_raw.get("localpv_path"), "localpv 路径") or "/var/openebs/local",
            "nfs_enabled": bool(storage_raw.get("nfs_enabled")),
            "nfs_default": bool(storage_raw.get("nfs_default")),
            "nfs_server": str(storage_raw.get("nfs_server") or "").strip(),
            "nfs_path": str(storage_raw.get("nfs_path") or "").strip() or "/share/kubernetes",
        }
        if storage["nfs_enabled"] and not storage["nfs_server"]:
            raise PackError("启用 NFS 存储类必须填写 NFS 服务器地址|NFS storage class requires an NFS server address")
        # 私有镜像仓库部署
        imgreg_raw = cluster_raw.get("image_registry") or {}
        imgreg = {"type": str(imgreg_raw.get("type") or "").strip(),
                  "vip": str(imgreg_raw.get("vip") or "").strip()}
        if imgreg["type"] and imgreg["type"] not in ("harbor", "docker-registry"):
            raise PackError("镜像仓库类型不合法: %s (harbor / docker-registry)|Invalid registry type: %s" % (imgreg["type"], imgreg["type"]))
        if imgreg["type"]:
            has_registry_node = any(n["role"] == "registry" for n in nodes)
            if not has_registry_node:
                raise PackError("部署私有镜像仓库需要给至少一台节点分配 registry 角色|Deploying a private registry requires a node with the registry role")
            if imgreg["vip"] and not valid_ipv4(imgreg["vip"]):
                raise PackError("镜像仓库 VIP 不合法: %s|Invalid registry VIP: %s" % (imgreg["vip"], imgreg["vip"]))
        # 是否由 kk 设置节点 hostname(容器等特殊环境需关闭; 默认开)
        set_hostname = cluster_raw.get("set_hostname")
        set_hostname = True if set_hostname is None else bool(set_hostname)
        # 证书自动续期 crontab(部署后写入; 空=不启用)
        certs_cron = str(cluster_raw.get("certs_renew_cron") or "").strip()
        cni_type = str(cluster_raw.get("cni_type") or (ccat["versions"][kube_ver]["cni_plugin"]["type"]))
        if cni_type not in ("calico", "cilium", "flannel", "kubeovn"):
            raise PackError("CNI 类型不合法: %s|Invalid CNI type: %s" % (cni_type, cni_type))
        # CNI×K8s 兼容矩阵前置校验(免得到服务器部署时才被 kk precheck 拦下)
        minor = ".".join(kube_ver.split(".")[:2])   # v1.34.11 -> v1.34
        matrix = ccat.get("cni_matrix") or {}
        if cni_type in matrix and minor:
            ok_minors = set()
            for _ver, minors in matrix[cni_type].items():
                ok_minors.update(minors)
            if ok_minors and minor not in ok_minors:
                raise PackError(
                    "CNI %s 不支持 Kubernetes %s (可选: %s)|CNI %s does not support Kubernetes %s (supported: %s)"
                    % (cni_type, kube_ver, sorted(ok_minors), cni_type, kube_ver, sorted(ok_minors)))
        proxy_mode = str(cluster_raw.get("proxy_mode") or "iptables")
        if proxy_mode not in ("iptables", "nftables"):
            raise PackError("kube-proxy 模式不合法: %s (iptables / nftables)|Invalid kube-proxy mode: %s" % (proxy_mode, proxy_mode))
        for cidr_key, v in (("pod_cidr", cluster_raw.get("pod_cidr") or "10.233.64.0/18"),
                            ("service_cidr", cluster_raw.get("service_cidr") or "10.233.0.0/18")):
            if "/" not in v or v.count("/") > 1:
                raise PackError("集群 %s 需为 CIDR 形式: %s|Cluster %s must be CIDR format: %s" % (cidr_key, v, cidr_key, v))
        roles_raw = cluster_raw.get("roles") or {}
        nodes, seen_names = [], set()
        for idx, role in roles_raw.items():
            if role in ("", None, "none"):
                continue
            if role not in ("control-plane", "worker", "registry"):
                raise PackError("集群角色不合法: %s (control-plane / worker / registry)|Invalid cluster role: %s" % (role, role))
            try:
                sv = servers[int(idx)]
            except (ValueError, IndexError):
                raise PackError("集群角色分配指向不存在的服务器: %s|Cluster role points to a missing server: %s" % (idx, idx))
            if sv["name"] in seen_names:
                raise PackError("集群节点名重复: %s|Duplicate cluster node name: %s" % (sv["name"], sv["name"]))
            seen_names.add(sv["name"])
            nodes.append({"name": sv["name"], "ip": sv["ip"], "user": sv["user"],
                          "ssh": sv["ssh"], "password": sv["password"], "role": role})
        if not any(n["role"] == "control-plane" for n in nodes):
            raise PackError("K8s 集群至少需要一台控制面节点(在角色分配中选择)|The K8s cluster needs at least one control-plane node (assign it in role mapping)")
        os_distros = [d for d in (cluster_raw.get("os_distros") or []) if d]
        for d in os_distros:
            if d not in (ccat.get("distros") or {}):
                raise PackError("OS 依赖包发行版不在支持矩阵: %s|Distro not in cluster matrix: %s" % (d, d))
        # 控制面高可用: 多控制面不允许 local(无 VIP, 其余节点无法加入同一端点)
        ha_type = str(cluster_raw.get("ha_type") or "local")
        if ha_type not in ("local", "kube-vip", "haproxy"):
            raise PackError("控制面 HA 类型不合法: %s (local / kube-vip / haproxy)|Invalid control-plane HA type: %s" % (ha_type, ha_type))
        ha_vip = str(cluster_raw.get("ha_vip") or "").strip()
        cp_count = sum(1 for n in nodes if n["role"] == "control-plane")
        if ha_type == "local" and cp_count > 1:
            warns.append("多控制面 + local 端点仅单点可用, 生产请选 kube-vip/haproxy 并填 VIP"
                         "|Multiple control-plane nodes with a local endpoint expose a single VIP; prefer kube-vip/haproxy + VIP")
        if ha_type in ("kube-vip", "haproxy"):
            if not ha_vip:
                raise PackError("HA 类型为 %s 时必须填写 VIP|HA type %s requires a VIP" % (ha_type, ha_type))
            if not valid_ipv4(ha_vip):
                raise PackError("VIP 不是合法 IPv4: %s|Invalid VIP: %s" % (ha_vip, ha_vip))
        # 集群升级包(可选): 目标版本的 kube 三件套须已备料
        upgrade_to = str(cluster_raw.get("upgrade_to") or "").strip()
        if upgrade_to:
            kube_dir = BASE_DIR / ("warehouse/cluster/kube/%s/%s" % (upgrade_to, arch))
            missing_up = [b for b in ("kubeadm", "kubelet", "kubectl") if not (kube_dir / b).is_file()]
            if missing_up:
                raise PackError(
                    "升级包缺目标版本二进制(%s): 应位于 %s|Upgrade bundle missing %s under %s"
                    % ("/".join(missing_up), kube_dir, "/".join(missing_up), kube_dir))
        cluster = {"enabled": True, "kube_version": kube_ver, "mode": mode, "cni_type": cni_type,
                   "proxy_mode": proxy_mode, "zone": str(cluster_raw.get("zone") or "cn"),
                   "components": components, "kubelet": kubelet, "registry_mirrors": mirrors,
                   "k8s_image_registry": k8s_image_registry, "set_hostname": set_hostname,
                   "ntp": ntp, "storage": storage, "image_registry": imgreg,
                   "certs_renew_cron": certs_cron,
                   "ha_type": ha_type, "ha_vip": ha_vip, "upgrade_to": upgrade_to,
                   "pod_cidr": cluster_raw.get("pod_cidr") or "10.233.64.0/18",
                   "service_cidr": cluster_raw.get("service_cidr") or "10.233.0.0/18",
                   "timezone": cluster_raw.get("timezone") or "Asia/Shanghai",
                   "os_distros": os_distros, "nodes": nodes}
        vminor = int(kube_ver.split(".")[1]) if kube_ver.startswith("v") else 0
        if vminor >= 35:
            warns.append("K8s %s 要求 cgroup v2, 麒麟/龙蜥等默认 cgroup v1 的系统需先启用并重启节点|K8s %s requires cgroup v2; enable it on cgroup-v1 distros (kylin/anolis...) before deploying" % (kube_ver, kube_ver))
        if proxy_mode == "nftables":
            warns.append("kube-proxy nftables 模式要求节点内核 >= 5.13, 旧内核(麒麟 4.19/阿里云 5.10)请用 iptables|nftables proxy mode needs kernel >= 5.13; use iptables on old kernels")
    cfg["cluster"] = cluster

    mh_raw = cfg.get("multihost") or {}
    mh = {}

    def _pick_idx(v, what):
        try:
            idx = int(v)
        except (TypeError, ValueError):
            raise PackError("%s 节点选择不合法|Invalid node assignment for %s" % (what, what))
        if not (0 <= idx < len(servers)):
            raise PackError("%s 指向了不存在的服务器(请先完善服务器池)|%s points to a missing server (fill the server pool first)" % (what, what))
        return idx

    if "kafka" in services and topology.get("kafka") == "cluster" and (mh_raw.get("kafka") or {}).get("enabled"):
        k = mh_raw.get("kafka") or {}
        try:
            count = int(k.get("count", 3))
        except (TypeError, ValueError):
            count = 0
        if count not in (3, 5):
            raise PackError("Kafka 多机节点数只能是 3 或 5|Kafka multi-host node count must be 3 or 5")
        brokers = [_pick_idx(x, "Kafka broker%d" % (i + 1)) for i, x in enumerate(k.get("brokers") or [])]
        if len(brokers) != count or len(set(brokers)) != count:
            raise PackError("Kafka 多机需为 %d 个 broker 分配 %d 台不同服务器|Kafka multi-host needs %d distinct servers for %d brokers" % (count, count, count, count))
        mh["kafka"] = {"enabled": True, "count": count, "brokers": brokers}

    for ms in ("mysql8", "mysql57"):
        if ms in services and topology.get(ms) == "master-slave" and (mh_raw.get(ms) or {}).get("enabled"):
            m = mh_raw.get(ms) or {}
            master = _pick_idx(m.get("master"), "%s 主库" % ms)
            replicas = [_pick_idx(x, "%s 从库" % ms) for x in (m.get("replicas") or [])]
            if not (1 <= len(replicas) <= 2) or master in replicas or len(set(replicas)) != len(replicas):
                raise PackError("%s 多机主从需 1-2 台不同的从库服务器且不与主库重复|%s multi-host needs 1-2 distinct replica servers != master" % (ms, ms))
            mh[ms] = {"enabled": True, "master": master, "replicas": replicas}

    if "redis" in services and topology.get("redis") == "sentinel" and (mh_raw.get("redis") or {}).get("enabled"):
        r = mh_raw.get("redis") or {}
        master = _pick_idx(r.get("master"), "Redis 主库")
        replicas = [_pick_idx(x, "Redis 从库") for x in (r.get("replicas") or [])]
        if len(replicas) != 2 or master in replicas or len(set(replicas)) != 2:
            raise PackError("Redis 多机哨兵需 1 主 + 2 从共 3 台不同服务器|Redis multi-host sentinel needs 1 master + 2 replicas on 3 distinct servers")
        mh["redis"] = {"enabled": True, "master": master, "replicas": replicas}
    cfg["multihost"] = mh

    # 单机主从从库数量(1-2); 多机时以分配的服务器数为准
    reps_raw = cfg.get("replicas") or {}
    reps = {}
    for ms in ("mysql8", "mysql57"):
        if ms not in services or topology.get(ms) != "master-slave":
            continue
        if ms in mh:
            reps[ms] = len(mh[ms]["replicas"])
            continue
        try:
            n = int(reps_raw.get(ms, 1))
        except (TypeError, ValueError):
            n = 1
        if n not in (1, 2):
            raise PackError("%s 从库数量只能是 1 或 2|%s replica count must be 1 or 2" % (ms, ms))
        reps[ms] = n
    cfg["replicas"] = reps

    # 读写分离(ProxySQL): 存在 MySQL 主从形态时可选
    features_raw = cfg.get("features") or {}
    features = {"proxysql": bool(features_raw.get("proxysql")) and bool(reps)}
    if features["proxysql"] and "proxysql" in services:
        raise PackError("读写分离为可选组件, 无需在中间件中单独选择|Read/write splitting is an optional component, not a middleware")
    cfg["features"] = features

    # 集群形态附加端口键(并入全局查重)
    topo_port_defs = []
    if topology.get("mysql8") == "master-slave":
        topo_port_defs.append(("mysql8_replica", "MySQL 从库端口|MySQL replica port", 13308))
        if reps.get("mysql8", 1) == 2 and "mysql8" not in mh:
            topo_port_defs.append(("mysql8_replica2", "MySQL 从库2端口|MySQL replica2 port", 13310))
    if topology.get("mysql57") == "master-slave":
        topo_port_defs.append(("mysql57_replica", "MySQL 5.7 从库端口|MySQL 5.7 replica port", 13309))
        if reps.get("mysql57", 1) == 2 and "mysql57" not in mh:
            topo_port_defs.append(("mysql57_replica2", "MySQL 5.7 从库2端口|MySQL 5.7 replica2 port", 13311))
    if topology.get("redis") == "sentinel":
        topo_port_defs.append(("redis_replica", "Redis 从库端口|Redis replica port", 16380))
        if "redis" in mh:
            topo_port_defs.append(("redis_sentinel", "Redis 哨兵端口(每节点)|Redis sentinel port (per node)", 26379))
    if topology.get("kafka") == "cluster":
        if "kafka" in mh:
            # 多机: 每节点同一组宿主端口(内网互联/控制器/SASL 对外), 各台机器独立不冲突
            topo_port_defs.append(("kafka_mh_inter", "Kafka 节点互联端口|Kafka inter-broker port", 9092))
            topo_port_defs.append(("kafka_mh_ctrl", "Kafka 控制器端口|Kafka controller port", 9093))
            topo_port_defs.append(("kafka_mh_sasl", "Kafka SASL 对外端口|Kafka SASL port", 9094))
        else:
            topo_port_defs.append(("kafka_c1", "Kafka broker1 端口(SASL)|Kafka broker1 port (SASL)", 19092))
            topo_port_defs.append(("kafka_c2", "Kafka broker2 端口(SASL)|Kafka broker2 port (SASL)", 29092))
            topo_port_defs.append(("kafka_c3", "Kafka broker3 端口(SASL)|Kafka broker3 port (SASL)", 39092))
    if features.get("proxysql"):
        topo_port_defs.append(("proxysql", "读写分离入口端口|Read/write split entry port", 16033))
        topo_port_defs.append(("proxysql_admin", "ProxySQL 管理端口|ProxySQL admin port", 16032))
    if "kafka" in mh:
        # 多机时主部署机不运行 Kafka 容器, 单机端口键让位给节点端口组
        norm_ports.pop("kafka", None)
        norm_ports.pop("kafka_host", None)
    seen_topo = set(norm_ports.values())
    for key, label, default in topo_port_defs:
        raw = (cfg.get("ports") or {}).get(key, default)
        try:
            v = int(raw)
        except (TypeError, ValueError):
            raise PackError("端口 %s 不合法: %s|Invalid port for %s: %s" % (tr(label, "zh"), raw, tr(label, "en"), raw))
        if not (1 <= v <= 65535):
            raise PackError("端口 %s 超出范围 1-65535: %d|Port %s out of range 1-65535: %d" % (tr(label, "zh"), v, tr(label, "en"), v))
        if v in seen_topo:
            raise PackError("端口冲突: %s 与其他端口都用了 %d|Port conflict: %s conflicts on %d" % (tr(label, "zh"), v, tr(label, "en"), v))
        seen_topo.add(v)
        norm_ports[key] = v

    # ---- 密码/账号 ----
    secrets_cfg = cfg.get("secrets") or {}
    need_keys = [x["key"] for x in catalog["secrets"]
                 if set(x["services"]) & set(services)]
    for item in catalog["secrets"]:
        key = item["key"]
        if key not in need_keys:
            continue
        val = str(secrets_cfg.get(key, item["default"])).strip()
        typ = item.get("type")
        if typ == "nacos_token":
            if not val.startswith("SecretKey"):
                raise PackError("Nacos Auth Token 必须以 SecretKey 开头|Nacos Auth Token must start with SecretKey")
            b64part = val[len("SecretKey"):]
            try:
                raw = base64.b64decode(b64part, validate=True)
            except Exception:
                raise PackError("Nacos Auth Token 的 SecretKey 后面必须是合法 Base64|Nacos Auth Token must be valid Base64 after SecretKey")
            if len(raw) < 32:
                raise PackError("Nacos Auth Token 解码后长度必须 >= 32 字节|Decoded Nacos Auth Token must be >= 32 bytes")
        elif item.get("type") == "webhook":
            # 告警 Webhook 地址: 可留空(仅界面展示), 非空时须是合法 URL 字符
            if val and not WEBHOOK_RE.match(val):
                raise PackError("%s 格式不合法(应为 http/https 开头的 URL): %s|%s is invalid (must be a http/https URL): %s"
                                % (tr(item["label"], "zh"), key, tr(item["label"], "en"), key))
        elif item.get("secret", True):
            if not PASSWORD_RE.match(val):
                raise PackError("%s 含不合法字符(禁止 空格 和 $ ` \" ' \\ ; | 字符): %s|%s contains illegal characters (space and $ ` \" ' \\ ; | are forbidden): %s"
                                % (tr(item["label"], "zh"), key, tr(item["label"], "en"), key))
            if len(val) < item.get("min_len", 6):
                raise PackError("%s 长度至少 %d 位|%s must be at least %d characters"
                                % (tr(item["label"], "zh"), item.get("min_len", 6), tr(item["label"], "en"), item.get("min_len", 6)))
        else:
            if not USERNAME_RE.match(val):
                raise PackError("%s 含不合法字符: %s|%s contains illegal characters: %s"
                                % (tr(item["label"], "zh"), key, tr(item["label"], "en"), key))
        secrets_cfg[key] = val
    # 只保留本次所选服务的密钥: 未选组件的账号密码不写入 .env / manifest.sh
    cfg["secrets"] = {k: secrets_cfg[k] for k in need_keys}

    # ---- 目录/镜像源 ----
    deploy_dir = str(cfg.get("deploy_dir") or catalog["defaults"]["deploy_dir"]).strip()
    data_root = str(cfg.get("docker_data_root") or catalog["defaults"]["docker_data_root"]).strip()
    if not LINUX_PATH_RE.match(deploy_dir) or deploy_dir == "/":
        raise PackError("部署目录必须是 Linux 绝对路径, 如 /data/middleware|Deploy dir must be a Linux absolute path, e.g. /data/middleware")
    if not LINUX_PATH_RE.match(data_root) or data_root == "/":
        raise PackError("Docker 数据目录必须是 Linux 绝对路径, 如 /data/docker|Docker data-root must be a Linux absolute path, e.g. /data/docker")
    cfg["deploy_dir"] = deploy_dir.rstrip("/")
    cfg["docker_data_root"] = data_root.rstrip("/")

    mirrors = cfg.get("registry_mirrors") or []
    norm_mirrors = []
    for m in mirrors:
        m = str(m).strip()
        if not m:
            continue
        if not MIRROR_RE.match(m):
            raise PackError("镜像加速器地址不合法: %s|Invalid registry mirror URL: %s" % (m, m))
        norm_mirrors.append(m)
    cfg["registry_mirrors"] = norm_mirrors

    # ---- 额外端口映射(可选, 每个中间件都能追加自定义 host:container) ----
    extra = cfg.get("extra_ports") or {}
    norm_extra = {}
    used_hosts = set(norm_ports.values())
    for svc, rows in extra.items():
        if svc not in services or not rows:
            continue
        norm_rows = []
        seen_ctn = set()
        for r in rows:
            try:
                h, c = int(r.get("host")), int(r.get("container"))
            except (TypeError, ValueError):
                raise PackError("%s 自定义端口必须是数字: %s|%s custom ports must be numbers: %s" % (svc, r, svc, r))
            if not (1 <= h <= 65535 and 1 <= c <= 65535):
                raise PackError("%s 自定义端口超出范围 1-65535: %d:%d|%s custom ports out of range 1-65535: %d:%d" % (svc, h, c, svc, h, c))
            if h in used_hosts:
                raise PackError("%s 自定义宿主机端口 %d 与其他端口冲突|%s custom host port %d conflicts with another port" % (svc, h, svc, h))
            used_hosts.add(h)
            if c in seen_ctn:
                raise PackError("%s 同一容器端口重复映射: %d|%s duplicate container port mapping: %d" % (svc, c, svc, c))
            seen_ctn.add(c)
            norm_rows.append({"host": h, "container": c})
        norm_extra[svc] = norm_rows
    cfg["extra_ports"] = norm_extra

    # ---- 数据库备份策略(按数据库类型独立计划) ----
    b = cfg.get("backup") or {}
    engines_in = b.get("engines") if isinstance(b.get("engines"), dict) else {}
    # 兼容旧版单一计划配置: 顶层 days/hour/keep -> 三个引擎继承同一份计划
    if not engines_in and b.get("days") is not None:
        engines_in = {k: {"enabled": bool(b.get("enabled")), "days": b.get("days"),
                          "hour": b.get("hour"), "keep": b.get("keep")}
                      for k in ("mysql", "pg", "mongo")}
    eng_of = {"mysql57": "mysql", "mysql8": "mysql", "postgres": "pg", "mongodb": "mongo"}
    have_engines = {eng_of[s] for s in backupable_services({"services": services})}
    norm_eng = {}
    for key, label in (("mysql", "MySQL"), ("pg", "PostgreSQL"), ("mongo", "MongoDB")):
        e = engines_in.get(key) or {}
        enabled = bool(e.get("enabled"))
        if enabled and key not in have_engines:
            enabled = False
            warns.append("未部署 %s, 其备份计划已忽略|%s not selected, its backup plan is ignored" % (label, label))
        if enabled:
            try:
                days = sorted({int(d) for d in (e.get("days") or [])})
            except (TypeError, ValueError):
                raise PackError("%s 备份星期配置不合法|Invalid %s backup weekdays" % (label, label))
            if not days or [d for d in days if not (1 <= d <= 7)]:
                raise PackError("%s 请至少选择一个有效备份日 (1=周一 ... 7=周日)|%s: select at least one valid backup day (1=Mon ... 7=Sun)" % (label, label))
            try:
                hour, keep = int(e.get("hour", 3)), int(e.get("keep", 7))
            except (TypeError, ValueError):
                raise PackError("%s 备份时间/保留份数不合法|Invalid %s backup hour/retention" % (label, label))
            if not 0 <= hour <= 23:
                raise PackError("%s 备份小时必须 0-23|%s backup hour must be 0-23" % (label, label))
            if not 1 <= keep <= 999:
                raise PackError("%s 备份保留份数必须 1-999|%s backup retention must be 1-999" % (label, label))
            norm_eng[key] = {"enabled": True, "days": days, "hour": hour, "keep": keep}
        else:
            norm_eng[key] = {"enabled": False, "days": [], "hour": 3, "keep": 7}
    bdir = str(b.get("dir") or "/data/backup/db").strip()
    if not LINUX_PATH_RE.match(bdir) or bdir == "/":
        raise PackError("备份目录必须是 Linux 绝对路径, 如 /data/backup/db|Backup dir must be a Linux absolute path, e.g. /data/backup/db")
    cfg["backup"] = {"dir": bdir.rstrip("/"), "engines": norm_eng}

    # ---- Nginx 反向代理向导(勾选 NGINX 才生效) ----
    proxies = cfg.get("proxies") or []
    if proxies and "nginx" not in services:
        raise PackError("配置了反向代理站点, 但中间件未勾选 NGINX|Reverse-proxy sites configured but NGINX is not selected")
    norm_px = []
    if proxies:
        # 站点监听端口必须是 nginx 容器内实际映射到的端口(80/443/9000 + 自定义映射的容器端口)
        ctn_ports = {80, 443, 9000}
        for r in (cfg.get("extra_ports") or {}).get("nginx", []):
            ctn_ports.add(int(r["container"]))
        if len(proxies) > 20:
            raise PackError("反代站点数量过多(最多 20 个)|Too many proxy sites (max 20)")
        seen_lsn = set()
        for i, p in enumerate(proxies):
            p = dict(p or {})
            no = i + 1
            mode = p.get("mode") or "static"
            if mode not in ("static", "proxy"):
                raise PackError("反代站点 %d 模式不合法: %s|Proxy site %d invalid mode: %s" % (no, mode, no, mode))
            sn = str(p.get("server_name") or "").strip()
            if not sn or not re.match(r"^[A-Za-z0-9._\-*]+( +[A-Za-z0-9._\-*]+)*$", sn):
                raise PackError("反代站点 %d 域名不合法(多个用空格分隔, 通配用 *): %r|Proxy site %d invalid server_name (space-separated, wildcard *): %r" % (no, sn, no, sn))
            key = (int(p.get("listen") or 80), sn.lower())
            try:
                listen = int(p.get("listen") or 80)
            except (TypeError, ValueError):
                raise PackError("反代站点 %d 监听端口不合法" % no)
            if listen not in ctn_ports:
                raise PackError("反代站点 %d 监听端口 %d 未映射进 nginx 容器, 请先到端口映射为 NGINX 添加自定义映射(宿主机:%d → 容器:%d)"
                                % (no, listen, listen, listen))
            if key in seen_lsn:
                raise PackError("反代站点 %d 与其他站点重复: 端口 %d + 域名 %s" % (no, listen, sn))
            seen_lsn.add(key)
            body = str(p.get("body_size") or "500m").strip()
            if not re.match(r"^[0-9]+[kKmMgG]?$", body):
                raise PackError("反代站点 %d 上传大小限制不合法(如 100m): %s|Proxy site %d invalid body size (e.g. 100m): %s" % (no, body, no, body))
            ws = bool(p.get("ws"))
            ws_path = str(p.get("ws_path") or "/ws/").strip() or "/ws/"
            if ws and not re.match(r"^/[A-Za-z0-9_./-]*$", ws_path):
                raise PackError("反代站点 %d WebSocket 路径不合法: %s|Proxy site %d invalid WebSocket path: %s" % (no, ws_path, no, ws_path))
            trusted = []
            for cidr in (p.get("trusted_proxies") or []):
                cidr = str(cidr).strip()
                if not cidr:
                    continue
                if not re.match(r"^(\d{1,3}\.){3}\d{1,3}/\d{1,2}$|^[A-Fa-f0-9:]+/\d{1,3}$", cidr):
                    raise PackError("反代站点 %d 可信代理网段不合法(应为 CIDR 如 10.0.0.0/8): %s|Proxy site %d invalid trusted proxy CIDR (e.g. 10.0.0.0/8): %s" % (no, cidr, no, cidr))
                trusted.append(cidr)

            # HTTPS 证书(随包分发, 部署即配好 SSL)
            ssl_on = bool(p.get("ssl"))
            cert_pem = str(p.get("cert_pem") or "")
            key_pem = str(p.get("key_pem") or "")
            redirect = bool(p.get("redirect", True))
            if ssl_on:
                if "-----BEGIN CERTIFICATE-----" not in cert_pem:
                    raise PackError("反代站点 %d 启用了 HTTPS 但证书文件无效, 请重新选择 fullchain.pem/.crt|Proxy site %d HTTPS enabled but certificate invalid, re-select fullchain.pem/.crt" % (no, no))
                if "ENCRYPTED" in key_pem:
                    raise PackError("反代站点 %d 私钥带密码保护, 暂不支持; 请先导出无密码私钥再上传|Proxy site %d private key is passphrase-protected (unsupported); export an unencrypted key first" % (no, no))
                if not re.search(r"-----BEGIN (RSA |EC |DSA )?PRIVATE KEY-----", key_pem):
                    raise PackError("反代站点 %d 启用了 HTTPS 但私钥文件无效, 请重新选择 .key 私钥|Proxy site %d HTTPS enabled but private key invalid, re-select the .key file" % (no, no))
                if len(cert_pem) > 200_000 or len(key_pem) > 200_000:
                    raise PackError("反代站点 %d 证书/私钥文件过大(>200KB), 请核对是否选错文件|Proxy site %d cert/key file too large (>200KB), check the file" % (no, no))
            else:
                cert_pem = key_pem = ""
                redirect = False

            api_prefix, root, spa, strip = "", "", False, False
            if mode == "static":
                root = str(p.get("root") or "").strip()
                if root and not LINUX_PATH_RE.match(root):
                    raise PackError("反代站点 %d 前端目录必须是容器内绝对路径: %s|Proxy site %d web root must be an absolute path inside the container: %s" % (no, root, no, root))
                spa = bool(p.get("spa"))
                api_prefix = str(p.get("api_prefix") or "").strip()
                if api_prefix and not re.match(r"^/[A-Za-z0-9_./-]*$", api_prefix):
                    raise PackError("反代站点 %d 接口前缀不合法: %s|Proxy site %d invalid API prefix: %s" % (no, api_prefix, no, api_prefix))
                if api_prefix and not api_prefix.endswith("/"):
                    api_prefix += "/"
            else:
                api_prefix = "/"

            target_host, target_port = "", 0
            if api_prefix:
                target = str(p.get("api_target") or "").strip().rstrip("/")
                m = re.match(r"^http://([A-Za-z0-9_.-]+)(?::([0-9]{1,5}))?$", target)
                if not m:
                    raise PackError("反代站点 %d 后端地址不合法(仅支持 http://主机[:端口], 主机可为 compose 服务名): %s|Proxy site %d invalid backend (only http://host[:port], host may be a compose service): %s" % (no, target, no, target))
                target_host = m.group(1)
                target_port = int(m.group(2) or 80)
                if not (1 <= target_port <= 65535):
                    raise PackError("反代站点 %d 后端端口超出范围: %d|Proxy site %d backend port out of range: %d" % (no, target_port, no, target_port))
                if target_host in ("127.0.0.1", "localhost", "::1"):
                    target_host = "host.docker.internal"
                    p["_gw"] = True
                    warns.append("反代站点 %d 后端填的是宿主机地址, 容器内已自动改用 host.docker.internal(host-gateway)|"
                                 "Proxy site %d backend points to the host; rewritten to host.docker.internal (host-gateway)" % (no, no))
                if target_host == "host.docker.internal":
                    p["_gw"] = True
                strip = bool(p.get("strip_prefix", api_prefix != "/"))
            norm_px.append({
                "mode": mode, "server_name": sn, "listen": listen,
                "root": root, "spa": spa, "api_prefix": api_prefix,
                "target_host": target_host, "target_port": target_port,
                "strip": strip, "ws": ws, "ws_path": ws_path,
                "body_size": body, "trusted_proxies": trusted,
                "ssl": ssl_on, "redirect": redirect,
                "cert_pem": cert_pem, "key_pem": key_pem,
                "_gw": bool(p.get("_gw")),   # 后端指向宿主机: nginx 容器需要 host-gateway
            })
    cfg["proxies"] = norm_px

    # ---- 数据库(nacos / xxljob) ----
    db = cfg.get("db") or {}
    local_mysqls = [s for s in ("mysql57", "mysql8") if s in services]
    for app, schema_default in (("nacos", "nacos"), ("xxljob", "xxl_job")):
        conf = db.get(app) or {}
        if app not in services:
            db[app] = None
            continue
        mode = conf.get("mode")
        if mode not in ("local", "external"):
            raise PackError("%s 必须选择数据库来源(local/external)|%s must choose a database source (local/external)" % (app, app))
        schema = str(conf.get("schema") or schema_default).strip()
        if not re.match(r"^[A-Za-z0-9_]+$", schema):
            raise PackError("%s 数据库名不合法: %s|%s invalid schema name: %s" % (app, schema, app, schema))
        conf["schema"] = schema
        if mode == "local":
            if not local_mysqls:
                raise PackError("%s 选择使用本次部署的 MySQL, 但本次未部署 MySQL|%s uses the bundled MySQL but MySQL is not selected" % (app, app))
            svc = conf.get("local_svc")
            if svc not in local_mysqls:
                svc = "mysql8" if "mysql8" in local_mysqls else local_mysqls[0]
            conf["local_svc"] = svc
            conf["host"] = svc            # compose 服务名
            conf["host_import"] = ""
            conf["port"] = 3306
            conf["user"] = "root"
            conf["password"] = secrets_cfg["MYSQL_ROOT_PASSWORD"]
            if is_multihost(cfg, svc):
                # 多机 MySQL: 主库在远端节点上, 主部署机的 nacos/xxljob 直连主节点 IP:宿主端口(写操作直达主库)
                conf["host"] = master_server_ip(cfg, svc)
                conf["host_import"] = conf["host"]
                conf["port"] = cfg["ports"][svc]
                conf["multihost"] = True
        else:
            host = str(conf.get("host", "")).strip()
            if not host:
                raise PackError("%s 外部数据库地址不能为空|%s external database host is required" % (app, app))
            try:
                port = int(conf.get("port", 3306))
            except (TypeError, ValueError):
                raise PackError("%s 外部数据库端口不合法|%s external database port invalid" % (app, app))
            if not (1 <= port <= 65535):
                raise PackError("%s 外部数据库端口超出范围|%s external database port out of range" % (app, app))
            user = str(conf.get("user", "")).strip()
            password = str(conf.get("password", ""))
            if not USERNAME_RE.match(user):
                raise PackError("%s 外部数据库账号不合法|%s external database user invalid" % (app, app))
            if not password:
                raise PackError("%s 外部数据库密码不能为空|%s external database password is required" % (app, app))
            if not PASSWORD_RE.match(password):
                raise PackError("%s 外部数据库密码含不合法字符(禁止 空格 和 $ ` \" ' \\ ; |)|%s external database password contains illegal characters (space and $ ` \" ' \\ ; | forbidden)" % (app, app))
            conf["port"] = port
            conf["user"] = user
            conf["password"] = password
            # 127.0.0.1/localhost 指的是宿主机: compose 内换成 host-gateway 别名
            if host in ("127.0.0.1", "localhost", "::1"):
                conf["host"] = "host.docker.internal"
                conf["host_import"] = "127.0.0.1"
                conf["extra_hosts"] = True
                warns.append("%s 外部数据库填的是本机地址, 容器内已自动改用 host.docker.internal(host-gateway); "
                             "请确认该数据库监听的是 0.0.0.0 而非仅 127.0.0.1|"
                             "%s external database uses a loopback address; rewritten to host.docker.internal (host-gateway). "
                             "Make sure it listens on 0.0.0.0, not just 127.0.0.1" % (app, app))
            else:
                conf["host"] = host
                conf["host_import"] = host
                conf["extra_hosts"] = False
        db[app] = conf
    cfg["db"] = db

    # ---- 仓库文件存在性 ----
    missing = check_warehouse(cfg, catalog)
    if missing:
        raise PackError("离线包物料缺失, 请补齐后再打包:\n  - %s|Offline bundle files missing, add them to the warehouse first:\n  - %s"
                        % ("\n  - ".join(missing), "\n  - ".join(missing)))
    return cfg, warns


def check_warehouse(cfg, catalog):
    """校验本次打包需要的物料文件是否存在, 返回缺失列表"""
    arch = cfg["arch"]
    missing = []
    docker_tgz = BASE_DIR / catalog["docker"]["packages"][arch]
    compose_bin = BASE_DIR / catalog["compose"]["packages"][arch]
    if not docker_tgz.is_file():
        missing.append(str(catalog["docker"]["packages"][arch]))
    if not compose_bin.is_file():
        missing.append(str(catalog["compose"]["packages"][arch]))

    need_imgs = {}
    kafka_cluster = "kafka" in cfg["services"] and (cfg.get("topology") or {}).get("kafka") == "cluster"
    for s in cfg["services"]:
        if s == "kafka" and kafka_cluster:
            continue   # 集群形态用 bitnami 镜像, 单节点镜像 tar 不需要
        meta = catalog["services"][s]
        need_imgs[meta["images"][arch]] = True
    if kafka_cluster:
        kc = getattr(PLUGINS.get("kafka"), "CLUSTER", None)
        if kc and kc["images"][arch]:
            need_imgs[kc["images"][arch]] = True
    if (cfg.get("features") or {}).get("proxysql"):
        need_imgs[PROXYSQL_META["images"][arch]] = True
    # 外部库导表需要 mysql 客户端镜像
    needs_client = False
    for a in ("nacos", "xxljob"):
        conf = (cfg.get("db") or {}).get(a) or {}
        if conf.get("mode") == "external":
            needs_client = True
    has_local_mysql = any(s in cfg["services"] for s in ("mysql57", "mysql8"))
    if needs_client and not has_local_mysql:
        img = catalog["services"]["mysql8"]["images"][arch]
        need_imgs[img] = True
    for rel in need_imgs:
        if not (BASE_DIR / rel).is_file():
            missing.append(rel)
    return missing


# ---------------------------------------------------------------- nacos SQL 提取

def extract_nacos_sql_from_tar(tar_path):
    """从 docker save 的 OCI 归档里直接找出 mysql-schema.sql(不依赖 docker)"""
    candidates = ("mysql-schema.sql", "nacos-mysql.sql")
    with tarfile.open(tar_path, "r:*") as outer:
        # 外层 manifest.json -> 层列表
        try:
            mf_member = outer.extractfile("manifest.json")
        except KeyError:
            mf_member = None
        if mf_member is None:
            raise PackError("nacos 镜像 tar 缺少 manifest.json, 格式不受支持")
        manifests = json.loads(mf_member.read().decode("utf-8"))
        layers = []
        for m in manifests:
            layers.extend(m.get("Layers", []))
        if not layers:
            raise PackError("nacos 镜像 tar 中没有镜像层")
        for layer in layers:
            lf = outer.extractfile(layer)
            if lf is None:
                continue
            try:
                # 直接在(可 seek 的)成员流上打开嵌套 tar, 避免把整层读进内存
                with tarfile.open(fileobj=lf, mode="r:*") as lt:
                    for member in lt.getmembers():
                        if not member.isfile():
                            continue
                        base = member.name.rsplit("/", 1)[-1]
                        if base in candidates:
                            data = lt.extractfile(member).read()
                            log.info("从 %s 的 %s 中提取到 %s (%d bytes)",
                                     tar_path.name, layer[:20] + "...", member.name, len(data))
                            return data.decode("utf-8")
            except tarfile.ReadError:
                continue
    raise PackError("未能从 nacos 镜像中提取 mysql-schema.sql, 请手工导出后放到 warehouse/sql/nacos-mysql.sql")


def build_nacos_sql(cfg, catalog, prog=None):
    """返回最终 nacos.sql 内容(带建库语句); 原始 sql 缓存到 warehouse/sql/"""
    cache_dir = WAREHOUSE / "sql"
    cache = cache_dir / "nacos-mysql.sql"
    if cache.is_file():
        log.info("使用缓存的 nacos 建表 SQL: %s", cache)
        raw = cache.read_text(encoding="utf-8")
    else:
        arch = cfg["arch"]
        tar_path = BASE_DIR / catalog["services"]["nacos"]["images"][arch]
        log.info("首次打包: 从 %s 提取 nacos 建表 SQL(之后会缓存, 不再重复提取)", tar_path.name)
        if prog is not None:
            prog.stage("提取 nacos 建表 SQL(%s, 仅首次)" % tar_path.name)
        raw = extract_nacos_sql_from_tar(tar_path)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(raw, encoding="utf-8")
        log.info("nacos 建表 SQL 已缓存到 %s", cache)
    # 去掉源文件自带的建库/USE 语句, 统一由我们生成
    lines = [l for l in raw.splitlines()
             if not re.match(r"(?i)^\s*(CREATE\s+DATABASE|USE\s+`?nacos`?)", l)]
    header = ("CREATE DATABASE IF NOT EXISTS `nacos` DEFAULT CHARACTER SET utf8mb4 "
              "COLLATE utf8mb4_unicode_ci;\nUSE `nacos`;\n\n")
    return header + "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------- 生成 compose / .env / manifest

def resolve_db(cfg):
    """返回 {app: dbconf}, 并决定是否需要 mysql 客户端镜像"""
    db = cfg["db"]
    services = cfg["services"]
    has_local_mysql = any(s in services for s in ("mysql57", "mysql8"))
    mh_mysql = any(is_multihost(cfg, s) for s in ("mysql57", "mysql8"))
    needs_client = False
    for app in ("nacos", "xxljob"):
        conf = db.get(app)
        if conf and (conf["mode"] == "external" or (conf["mode"] == "local" and mh_mysql)):
            # 多机 MySQL: 主库在远端节点, 导表需从主部署机走网络导入(用客户端镜像)
            needs_client = True
    client_img = ""
    if needs_client:
        if has_local_mysql and not mh_mysql:
            client_img = "mysql:8.0.46" if "mysql8" in services else "mysql:5.7.44"
        else:
            client_img = "mysql:8.0.46"   # 打包时会把该镜像一起带上, 仅作客户端使用
    return needs_client, client_img


def backup_crons(backup):
    """备份策略 -> [(engine, crontab 时间字段)], 只返回已启用的引擎。UI 1=周一..7=周日, cron 0=周日, 如 ('mysql', '0 3 * * 0,1')"""
    out = []
    for eng in ("mysql", "pg", "mongo"):
        e = ((backup or {}).get("engines") or {}).get(eng) or {}
        if not e.get("enabled"):
            continue
        dow = ",".join(sorted(str(0 if d == 7 else d) for d in (e.get("days") or [])))
        out.append((eng, "0 %d * * %s" % (int(e.get("hour", 3)), dow)))
    return out


# 参与定时备份的数据库类服务 -> (备份.conf 中的服务数组变量名)
DB_BACKUP_SERVICES = {
    "mysql57": "MYSQL_SERVICES",
    "mysql8": "MYSQL_SERVICES",
    "postgres": "PG_SERVICES",
    "mongodb": "MONGO_SERVICES",
}


def backupable_services(cfg):
    """当前所选服务中可参与定时备份的数据库服务列表"""
    return [s for s in cfg["services"] if s in DB_BACKUP_SERVICES]


def gen_backup_conf(cfg):
    """生成 backup.conf(由 backup.sh source), 按数据库类型独立 开关/保留份数"""
    b = cfg["backup"]
    engines = b.get("engines") or {}
    picked = backupable_services(cfg)
    def eng(key):
        e = engines.get(key) or {}
        return int(bool(e.get("enabled"))), int(e.get("keep") or 7)
    mysql_on, mysql_keep = eng("mysql")
    pg_on, pg_keep = eng("pg")
    mongo_on, mongo_keep = eng("mongo")
    lines = [
        "# 由 packer.py 自动生成 — backup.sh 的配置(每种数据库独立开关/保留份数)",
        "BACKUP_DIR=%s" % bash_quote(b["dir"]),
        "",
        "# MySQL (mysqldump 按库导出)",
        "MYSQL_ENABLED=%d" % mysql_on,
        "MYSQL_SERVICES=(%s)" % " ".join(s for s in picked if s.startswith("mysql")),
        "MYSQL_KEEP=%d" % mysql_keep,
        "MYSQL_ROOT_PASSWORD=%s" % bash_quote(cfg["secrets"].get("MYSQL_ROOT_PASSWORD", "")),
        "",
        "# PostgreSQL (pg_dump 按库导出, 容器内自带客户端)",
        "PG_ENABLED=%d" % pg_on,
        "PG_SERVICES=(%s)" % " ".join(s for s in picked if s == "postgres"),
        "PG_KEEP=%d" % pg_keep,
        "POSTGRES_PASSWORD=%s" % bash_quote(cfg["secrets"].get("POSTGRES_PASSWORD", "")),
        "",
        "# MongoDB (mongodump 按库导出, 容器内自带客户端)",
        "MONGO_ENABLED=%d" % mongo_on,
        "MONGO_SERVICES=(%s)" % " ".join(s for s in picked if s == "mongodb"),
        "MONGO_KEEP=%d" % mongo_keep,
        "MONGO_USER=%s" % bash_quote(cfg["secrets"].get("MONGO_INITDB_ROOT_USERNAME", "root")),
        "MONGO_PASSWORD=%s" % bash_quote(cfg["secrets"].get("MONGO_INITDB_ROOT_PASSWORD", "")),
    ]
    return "\n".join(lines) + "\n"


def plugin_ctx(cfg):
    """传给插件的上下文: 配置/端口/密钥/数据库 + 常用片段生成器"""
    services = cfg["services"]
    ports = cfg["ports"]
    extra = cfg.get("extra_ports") or {}

    def extra_ports_lines(svc):
        return "".join('      - "%d:%d"\n' % (r["host"], r["container"]) for r in extra.get(svc, []))

    def depends_on(conf):
        if conf and conf["mode"] == "local":
            return "    depends_on:\n      %s:\n        condition: service_healthy\n" % conf["local_svc"]
        return ""

    def extra_hosts(conf):
        if conf and conf.get("extra_hosts"):
            return '    extra_hosts:\n      - "host.docker.internal:host-gateway"\n'
        return ""

    return {
        "cfg": cfg, "ports": ports, "secrets": cfg["secrets"], "db": cfg.get("db") or {},
        "lang": cfg.get("lang", "zh"), "tr": tr,
        "extra_ports_lines": extra_ports_lines,
        "depends_on": depends_on,
        "extra_hosts": extra_hosts,
        "networks_tail": "    networks:\n      - app-network\n",
        "bash_quote": bash_quote,
        "local_mysql": ("mysql8" if "mysql8" in services else "mysql57" if "mysql57" in services else ""),
    }


def gen_compose(cfg, catalog):
    services = cfg["services"]
    ports = cfg["ports"]
    db = cfg["db"]
    topology = cfg.get("topology") or {}
    features = cfg.get("features") or {}
    reps = cfg.get("replicas") or {}
    ctx = plugin_ctx(cfg)
    extra_lines = ctx["extra_ports_lines"]
    out = []
    out.append("# 由 packer.py 自动生成, 项目: %s" % cfg["project"])
    out.append("# 手工修改后请自行承担与 .env/manifest.sh 不一致的风险")
    out.append("name: %s" % cfg["project"])
    out.append("")
    out.append("services:")

    def extra_hosts(conf):
        if conf and conf.get("extra_hosts"):
            return ["    extra_hosts:", '      - "host.docker.internal:host-gateway"']
        return []

    def depends_on(conf):
        if conf and conf["mode"] == "local" and not conf.get("multihost"):
            return ["    depends_on:", "      %s:" % conf["local_svc"], "        condition: service_healthy"]
        return []

    # ---- MySQL ----
    topology = cfg.get("topology") or {}
    for s in ("mysql57", "mysql8"):
        if s not in services:
            continue
        if is_multihost(cfg, s):
            # 多机主从: 容器分布到各节点, 主部署机 compose 不含 MySQL (节点版由 gen_nodes 生成)
            continue
        meta = catalog["services"][s]
        repl_flags = []
        if s in ("mysql8", "mysql57") and topology.get(s) == "master-slave":
            # 主库: 开启 binlog + GTID, 供从库自动同步 (两代的 binlog 过期参数名不同)
            # 注意: MySQL 8.0 没有 binlog-expire-logs-days, 7 天对应 binlog_expire_logs_seconds=604800
            expire = "--binlog-expire-logs-seconds=604800" if s == "mysql8" else "--expire-logs-days=7"
            repl_flags = ["--server-id=1", "--log-bin=mysql-bin", "--gtid-mode=ON",
                          "--enforce-gtid-consistency=ON", expire]
        out.append("""
  %(svc)s:
    image: %(image)s
    container_name: %(svc)s
    restart: always
    environment:
      TZ: ${TZ}
      MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD}
    ports:
      - "%(port)d:3306"
%(extra_ports)s    volumes:
      - ./%(dir)s/data:/var/lib/mysql
      - ./%(dir)s/log:/var/log/mysql
      - ./%(dir)s/init:/docker-entrypoint-initdb.d
      - ./%(dir)s/my.cnf:/etc/mysql/conf.d/my.cnf:ro
    command:
      - --log-error=/var/log/mysql/error.log
      - --character-set-server=utf8mb4
      - --collation-server=utf8mb4_unicode_ci
      - --default-authentication-plugin=mysql_native_password
%(repl_flags)s    healthcheck:
      test: ["CMD-SHELL", "mysqladmin ping -h localhost -uroot -p\\"$$MYSQL_ROOT_PASSWORD\\""]
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 300s
    networks:
      - app-network""" % {"svc": s, "image": "%s:%s" % (meta["image"], meta["tag"]),
                          "port": ports[meta["ports"][0]["key"]], "dir": meta["data_dir"],
                          "extra_ports": extra_lines(s),
                          "repl_flags": "".join("      - %s\n" % f for f in repl_flags)})
        if s in ("mysql8", "mysql57") and topology.get(s) == "master-slave":
            # 从库: 只读, GTID 自动定位, 不挂 init 目录(数据由主库复制而来); 数量 1-2 可选
            for _r in range(reps.get(s, 1)):
                _rsvc = "%s-replica" % s if _r == 0 else "%s-replica%d" % (s, _r + 1)
                _rport = "%s_replica" % s if _r == 0 else "%s_replica%d" % (s, _r + 1)
                out.append("""
  %(svc)s:
    image: %(image)s
    container_name: %(svc)s
    restart: always
    environment:
      TZ: ${TZ}
      MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD}
    ports:
      - "%(rport)d:3306"
    volumes:
      - ./%(dir)s/data:/var/lib/mysql
      - ./%(dir)s/log:/var/log/mysql
      - ./%(dir)s/my.cnf:/etc/mysql/conf.d/my.cnf:ro
    command:
      - --log-error=/var/log/mysql/error.log
      - --character-set-server=utf8mb4
      - --collation-server=utf8mb4_unicode_ci
      - --default-authentication-plugin=mysql_native_password
      - --server-id=%(sid)d
      - --log-bin=mysql-bin
      - --gtid-mode=ON
      - --enforce-gtid-consistency=ON
      # 只保留 read-only: super-read-only 会让官方镜像首启初始化(root 也被拒写)直接失败
      # super_read_only 由 deploy.sh 在复制链路验证通过后运行时置位(幂等)
      - --read-only=ON
    healthcheck:
      test: ["CMD-SHELL", "mysqladmin ping -h localhost -uroot -p\\"$$MYSQL_ROOT_PASSWORD\\""]
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 300s
    networks:
      - app-network""" % {"svc": _rsvc,
                          "image": "%s:%s" % (meta["image"], meta["tag"]),
                          "rport": ports[_rport],
                          "dir": _rsvc,
                          "sid": 2 + _r})

    # ---- Redis ----
    if "redis" in services and not is_multihost(cfg, "redis"):
        meta = catalog["services"]["redis"]
        out.append("""
  redis:
    image: %(image)s
    container_name: redis
    restart: always
    environment:
      TZ: ${TZ}
      REDIS_PASSWORD: ${REDIS_PASSWORD}
    ports:
      - "%(port)d:6379"
%(extra_ports)s    volumes:
      - ./redis/data:/data
      - ./redis/log:/var/log/redis
      - ./redis/redis.conf:/usr/local/etc/redis/redis.conf:ro
    # masterauth 必备: 故障转移后被哨兵降级的一方要凭它连新主
    command: redis-server /usr/local/etc/redis/redis.conf --requirepass "${REDIS_PASSWORD}" --masterauth "${REDIS_PASSWORD}"
    healthcheck:
      test: ["CMD-SHELL", "redis-cli -a \\"$$REDIS_PASSWORD\\" ping | grep -q PONG"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - app-network""" % {"image": "%s:%s" % (meta["image"], meta["tag"]),
                          "port": ports["redis"],
                          "extra_ports": extra_lines("redis")})
    if topology.get("redis") == "sentinel" and not is_multihost(cfg, "redis"):
        # 哨兵形态: 1 主 + 1 从 + 3 哨兵(副本数为 3, 应用侧走 sentinel 协议)
        out.append("""
  redis-replica:
    image: %(image)s
    container_name: redis-replica
    restart: always
    environment:
      TZ: ${TZ}
      REDIS_PASSWORD: ${REDIS_PASSWORD}
    command: redis-server --requirepass "${REDIS_PASSWORD}" --masterauth "${REDIS_PASSWORD}" --replicaof redis 6379 --appendonly no
    ports:
      - "%(rport)d:6379"
    volumes:
      - ./redis-replica/data:/data
      - ./redis-replica/log:/var/log/redis
    healthcheck:
      test: ["CMD-SHELL", "redis-cli -a \\"$$REDIS_PASSWORD\\" ping | grep -q PONG"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - app-network
  redis-sentinel:
    image: %(image)s
    restart: always
    environment:
      TZ: ${TZ}
    command: redis-server /usr/local/etc/redis/sentinel.conf --sentinel
    volumes:
      - ./redis/sentinel.conf:/usr/local/etc/redis/sentinel.conf
    deploy:
      replicas: 3
    networks:
      - app-network""" % {"image": "%s:%s" % (meta["image"], meta["tag"]),
                          "rport": ports["redis_replica"]})

    # ---- Nacos ----
    if "nacos" in services:
        meta = catalog["services"]["nacos"]
        conf = db["nacos"]
        block = """
  nacos:
    image: %(image)s
    container_name: nacos
    restart: always
    environment:
      TZ: ${TZ}
      MODE: standalone
      NACOS_AUTH_ENABLE: "true"
      NACOS_AUTH_TOKEN_EXPIRE_SECONDS: 18000
      NACOS_AUTH_TOKEN: ${NACOS_AUTH_TOKEN}
      NACOS_AUTH_IDENTITY_KEY: serverIdentity
      NACOS_AUTH_IDENTITY_VALUE: security
      NACOS_AUTH_USER: ${NACOS_AUTH_USER}
      NACOS_AUTH_PASSWORD: ${NACOS_AUTH_PASSWORD}
      SPRING_DATASOURCE_PLATFORM: mysql
      MYSQL_SERVICE_HOST: %(dbhost)s
      MYSQL_SERVICE_PORT: "%(dbport)d"
      MYSQL_SERVICE_DB_NAME: %(dbname)s
      MYSQL_SERVICE_USER: "%(dbuser)s"
      MYSQL_SERVICE_PASSWORD: ${NACOS_DB_PASSWORD}
    ports:
      - "%(p_main)d:8848"
      - "%(p_grpc)d:9848"
      - "%(p_console)d:8080"
%(extra_ports)s    volumes:
      - ./nacos/data:/home/nacos/data
      - ./nacos/log:/home/nacos/logs
%(depends)s%(extra)s    networks:
      - app-network""" % {
            "image": "%s:%s" % (meta["image"], meta["tag"]),
            "dbhost": conf["host"], "dbport": conf["port"], "dbname": conf["schema"],
            "dbuser": conf["user"],
            "p_main": ports["nacos_main"], "p_grpc": ports["nacos_grpc"], "p_console": ports["nacos_console"],
            "extra_ports": extra_lines("nacos"),
            "depends": "\n".join(depends_on(conf)) + ("\n" if depends_on(conf) else ""),
            "extra": "\n".join(extra_hosts(conf)) + ("\n" if extra_hosts(conf) else ""),
        }
        out.append(block)

    # ---- XXL-Job ----
    if "xxljob" in services:
        meta = catalog["services"]["xxljob"]
        conf = db["xxljob"]
        block = """
  xxl-job:
    image: %(image)s
    container_name: xxl-job
    restart: always
    environment:
      TZ: ${TZ}
      PARAMS: >-
        --spring.datasource.url=jdbc:mysql://%(dbhost)s:%(dbport)d/%(dbname)s?useUnicode=true&characterEncoding=UTF-8&autoReconnect=true&serverTimezone=Asia/Shanghai&useSSL=false&allowPublicKeyRetrieval=true
        --spring.datasource.username=%(dbuser)s
        --spring.datasource.password=${XXL_DB_PASSWORD}
        --xxl.job.accessToken=${XXL_JOB_ACCESS_TOKEN}
    ports:
      - "%(port)d:8080"
%(extra_ports)s    volumes:
      - ./xxl-job/data:/data
      - ./xxl-job/log:/data/applogs/xxl-job
%(depends)s%(extra)s    networks:
      - app-network""" % {
            "image": "%s:%s" % (meta["image"], meta["tag"]),
            "dbhost": conf["host"], "dbport": conf["port"], "dbname": conf["schema"], "dbuser": conf["user"],
            "port": ports["xxljob"],
            "extra_ports": extra_lines("xxljob"),
            "depends": "\n".join(depends_on(conf)) + ("\n" if depends_on(conf) else ""),
            "extra": "\n".join(extra_hosts(conf)) + ("\n" if extra_hosts(conf) else ""),
        }
        out.append(block)

    # ---- MinIO ----
    if "minio" in services:
        meta = catalog["services"]["minio"]
        out.append("""
  minio:
    image: %(image)s
    container_name: minio
    restart: always
    environment:
      TZ: ${TZ}
      MINIO_ROOT_USER: ${MINIO_ROOT_USER}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD}
    ports:
      - "%(p_api)d:9000"
      - "%(p_console)d:9001"
%(extra_ports)s    volumes:
      - ./minio/data:/data
      - ./minio/log:/log
    command: server /data --console-address ":9001"
    healthcheck:
      test: ["CMD", "mc", "ready", "local"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - app-network""" % {"image": "%s:%s" % (meta["image"], meta["tag"]),
                          "p_api": ports["minio_api"], "p_console": ports["minio_console"],
                          "extra_ports": extra_lines("minio")})

    # ---- NGINX ----
    if "nginx" in services:
        # 反代站点后端指向宿主机时, 容器内需要 host-gateway 别名
        xhosts = []
        if any(p.get("_gw") for p in (cfg.get("proxies") or [])):
            xhosts = ["    extra_hosts:", '      - "host.docker.internal:host-gateway"']
        out.append("""
  nginx:
    image: %(image)s
    container_name: nginx
    restart: always
    environment:
      TZ: ${TZ}
    ports:
      - "%(p_http)d:80"
      - "%(p_https)d:443"
      - "%(p_extra)d:9000"
%(extra_ports)s    volumes:
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf:ro
      - ./nginx/conf:/etc/nginx/conf.d
      - ./nginx/html:/usr/share/nginx/html
      - ./nginx/logs:/var/log/nginx
      - ./nginx/ssl:/etc/nginx/ssl
%(xhosts)s    networks:
      - app-network""" % {"image": "nginx:%s" % catalog["services"]["nginx"]["tag"],
                          "p_http": ports["nginx_http"], "p_https": ports["nginx_https"],
                          "p_extra": ports["nginx_extra"],
                          "extra_ports": extra_lines("nginx"),
                          "xhosts": "\n".join(xhosts) + ("\n" if xhosts else "")})

    # ---- ProxySQL 读写分离(可选组件) ----
    if features.get("proxysql"):
        out.append("""
  proxysql:
    image: %(image)s
    container_name: proxysql
    restart: always
    environment:
      TZ: ${TZ}
    ports:
      - "%(p_main)d:6033"
      - "%(p_admin)d:6032"
    volumes:
      - ./proxysql/proxysql.cnf:/etc/proxysql.cnf:ro
      - ./proxysql/data:/var/lib/proxysql
    # 健康检查走数据面入口: 能通过代理查到后端才算就绪
    healthcheck:
      test: ["CMD-SHELL", "mysql --protocol=tcp -h127.0.0.1 -P6033 -uroot -p\\"$$MYSQL_ROOT_PASSWORD\\" -e 'SELECT 1' >/dev/null 2>&1 || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 30s
    networks:
      - app-network""" % {"image": "%s:%s" % (PROXYSQL_META["image"], PROXYSQL_META["tag"]),
                          "p_main": ports["proxysql"], "p_admin": ports["proxysql_admin"]})

    # ---- 插件中间件(可插拔) ----
    for s in services:
        if s in PLUGINS:
            block = PLUGINS[s].compose_block(cfg, ports, ctx)
            if block:
                out.append(block)

    out.append("""
networks:
  app-network:
    driver: bridge
""")
    return "\n".join(out) + "\n"


def gen_env(cfg, catalog):
    s = cfg["secrets"]
    lines = [
        "# 由 packer.py 自动生成 (权限600), docker compose 启动时自动读取",
        "TZ=Asia/Shanghai",
    ]
    if "MYSQL_ROOT_PASSWORD" in s:
        lines.append("MYSQL_ROOT_PASSWORD=%s" % env_quote(s["MYSQL_ROOT_PASSWORD"]))
    for app, prefix in (("nacos", "NACOS"), ("xxljob", "XXL")):
        conf = cfg["db"].get(app)
        if not conf:
            continue
        lines.append("%s_DB_PASSWORD=%s" % (prefix, env_quote(conf["password"])))
    for key in ("NACOS_AUTH_USER", "NACOS_AUTH_PASSWORD", "NACOS_AUTH_TOKEN",
                "XXL_JOB_ADMIN_PASSWORD", "XXL_JOB_ACCESS_TOKEN",
                "REDIS_PASSWORD", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"):
        if key in s:
            lines.append("%s=%s" % (key, env_quote(s[key])))
    ctx = plugin_ctx(cfg)
    for svc in cfg["services"]:
        if svc in PLUGINS and hasattr(PLUGINS[svc], "env_lines"):
            lines += PLUGINS[svc].env_lines(cfg, cfg["ports"], ctx)
    # promtail 需要挂载宿主机 docker 容器日志目录, 路径随 data-root 配置而变
    if "promtail" in cfg["services"]:
        lines.append("DOCKER_DATA_ROOT=%s" % cfg["docker_data_root"])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- K8s 集群

def _yq(v):
    """标量转 YAML 安全字面量(JSON 引号规则是 YAML 引号规则的子集)"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v), ensure_ascii=False)


def gen_cluster_inventory(cfg):
    """kk Inventory: 集群节点 = 服务器池中分配了角色的机器"""
    nodes = cfg["cluster"]["nodes"]
    lines = [
        "# 由 packer.py 生成, 勿手改; 变更请在打包页面修改后重新打包",
        "apiVersion: kubekey.kubesphere.io/v1",
        "kind: Inventory",
        "metadata:",
        '  name: "%s-cluster"' % cfg["project"],
        "spec:",
        "  hosts:",
    ]
    for n in nodes:
        lines += [
            "    %s:" % n["name"],
            "      connector:",
            "        type: ssh",
            '        host: %s' % _yq(n["ip"]),
            "        port: %d" % n["ssh"],
            "        user: %s" % _yq(n["user"]),
            '        password: %s' % _yq(n["password"]),
            "      internal_ipv4: %s" % _yq(n["ip"]),
        ]
    cp = [n["name"] for n in nodes if n["role"] == "control-plane"]
    wk = [n["name"] for n in nodes if n["role"] == "worker"]
    rg = [n["name"] for n in nodes if n["role"] == "registry"]
    lines += ["  groups:", "    k8s_cluster:", "      groups:", "        - kube_control_plane",
              "        - kube_worker", "    kube_control_plane:", "      hosts:"]
    lines += ["        - %s" % n for n in cp] or ["        # (空)"]
    lines += ["    kube_worker:", "      hosts:"]
    lines += ["        - %s" % n for n in wk] or ["        # (空)"]
    # kk 要求 etcd 组显式非空(默认堆叠 etcd 部署在控制面节点上)
    lines += ["    etcd:", "      hosts:"]
    lines += ["        - %s" % n for n in cp] or ["        # (空)"]
    if rg:
        lines += ["    image_registry:", "      hosts:"] + ["        - %s" % n for n in rg]
    return "\n".join(lines) + "\n"


def gen_cluster_config(cfg, catalog):
    """kk Config: 按页面集群配置生成完整 spec(离线/在线/HA/目录/NTP/存储/镜像仓库/运行时参数)"""
    cl = cfg["cluster"]
    ver = catalog["cluster"]["versions"][cl["kube_version"]]
    cni_type = cl["cni_type"]
    # CNI 版本跟随所选类型(条目按 kk per-minor vars 携带各 CNI 配套版本)
    cni_versions = ver.get("cni_versions") or {}
    cni_version = cni_versions.get(cni_type) or ver.get("cni_plugin", {}).get("version", "")
    cni_key = cni_type + "_version"
    mode = cl["mode"]
    online = mode == "online"
    comp = cl.get("components") or {}
    kubelet = cl.get("kubelet") or {}
    ntp = cl.get("ntp") or {}
    storage = cl.get("storage") or {}
    imgreg = cl.get("image_registry") or {}
    ha_type = cl.get("ha_type") or "local"

    # zone: cn 时在线模式的镜像/二进制自动走国内源(hub.kubesphere.com.cn 等); 离线模式保持为空
    zone_cn = online and (cl.get("zone") or "cn") == "cn"
    lines = [
        "# 由 packer.py 生成, 勿手改; 变更请在打包页面修改后重新打包",
        "apiVersion: kubekey.kubesphere.io/v1",
        "kind: Config",
        "metadata:",
        '  name: "%s-cluster"' % cfg["project"],
        "spec:",
        "  zone: %s" % _yq("cn" if zone_cn else ""),
    ]
    # zone=cn 仅由 spec.zone 生效: 在线+国区交给 kk 内置 CN 映射
    # (hub.kubesphere.com.cn/{kubernetes,coredns,flannel-io}), 不显式覆盖 registry.imageRepository —
    # 裸域名会让 etcd/coredns 丢掉命名空间(hub 实测 400/404)
    lines += [
        "  kubernetes:",
    ]
    if not zone_cn:
        # sandbox 三元组必须写全: kk containerd 配置模板用这三个键拼 sandbox 镜像,
        # 缺 registry/repository 会渲染出非法值导致容器沙箱创建失败;
        # 在线+cn 不写, 由 kk 按 zone 渲染国内 sandbox(hub.kubesphere.com.cn/kubernetes/pause)
        lines += [
            "    sandbox_image:",
            '      registry: "registry.k8s.io"',
            '      repository: "pause"',
            "      tag: %s" % _yq(ver.get("sandbox_image_tag") or "3.9"),
        ]
    lines += [
        "    kube_version: %s" % _yq(cl["kube_version"]),
        "    kube_proxy:",
        "      mode: %s" % _yq(cl["proxy_mode"]),
        "    control_plane_endpoint:",
        "      type: %s" % _yq(ha_type),
    ]
    if ha_type in ("kube-vip", "haproxy") and cl.get("ha_vip"):
        lines += ["      host: %s" % _yq(cl["ha_vip"])]

    # kubelet 参数(可选覆盖); root_dir 通过 extra_args 的 root-dir 生效
    # 注意: kk 会给每个 arg 自动加 "--" 前缀, 这里不能自带横杠(否则变成 ----root-dir)
    kubelet_lines = []
    kubelet_args = [a.lstrip("-") for a in (kubelet.get("extra_args") or [])]
    if kubelet.get("root_dir"):
        kubelet_args.insert(0, "root-dir=" + kubelet["root_dir"])
    if kubelet_args:
        kubelet_lines.append("      extra_args:")
        for a in kubelet_args:
            k, _, v = a.partition("=")
            kubelet_lines.append("        %s: %s" % (_yq(k.strip()), _yq(v.strip())))
    if kubelet.get("max_pods"):
        kubelet_lines.append("      max_pods: %d" % int(kubelet["max_pods"]))

    if kubelet.get("extra_config"):
        kubelet_lines.append("      extra_config:")
        for ln in kubelet["extra_config"].splitlines():
            kubelet_lines.append("      " + ln if ln.strip() else "")
        while kubelet_lines and kubelet_lines[-1] == "      ":
            kubelet_lines.pop()
    if kubelet_lines:
        lines += ["    kubelet:"] + kubelet_lines

    # CRI: containerd 数据目录/静态构建(glibc 老系统)/版本覆盖 + docker data-root + registry mirrors
    # kk config 结构: cri.containerd_version(直属 cri!) + cri.containerd.{static_binary, data_root},
    #                 cri.docker.data_root, cri.registry.mirrors — containerd_version 写错层级会被 kk 忽略
    # data_root 是 kk containerd 模板认的键(模板里 set $config "root" data_root), 不能写 config.root
    cd_lines = []
    if comp.get("containerd_version_override"):
        cd_lines.append("    containerd_version: %s" % _yq(comp["containerd_version_override"]))
    cd_sub = []
    if comp.get("static_binary"):
        cd_sub.append("      static_binary: true")
    if comp.get("containerd_root"):
        cd_sub.append("      data_root: %s" % _yq(comp["containerd_root"]))
    if not online:
        # 纯离线: 部署脚本先预装 containerd 并导入镜像, config_policy=overwrite 让 kk 每次都重写
        # 配置并重启(镜像落在 data_root 磁盘存储, 重启不丢); 否则版本一致时 kk 跳过配置,
        # 节点会带着部署脚本的精简配置或默认配置进入 kubeadm(sandbox 镜像不对)
        cd_sub.append("      config_policy: overwrite")
    cri_lines = []
    if cd_lines:
        cri_lines += cd_lines
    if cd_sub:
        cri_lines += ["    containerd:"] + cd_sub
    if comp.get("docker_root"):
        cri_lines += ["    docker:", "      data_root: %s" % _yq(comp["docker_root"])]
    if cl.get("registry_mirrors"):
        cri_lines += ["    registry:", "      mirrors: [%s]" % ", ".join(_yq(m) for m in cl["registry_mirrors"])]
    if cri_lines:
        lines += ["  cri:"] + cri_lines

    # etcd: 堆叠内部署 + 数据目录 + 版本(不写 deployment_type 时 kk 可能按外置集群处理)
    etcd_lines = ['    deployment_type: "internal"']
    etcd_ver = ((ver.get("components") or {}).get("etcd") or {}).get("version")
    if etcd_ver:
        etcd_lines.append("    etcd_version: %s" % _yq(etcd_ver))
    if comp.get("etcd_dir"):
        etcd_lines.append("    data_dir: %s" % _yq(comp["etcd_dir"]))
    lines += ["  etcd:"] + etcd_lines

    lines += [
        "  cni:",
        "    type: %s" % _yq(cni_type),
        "    %s: %s" % (cni_key, _yq(cni_version)),
        "    pod_cidr: %s" % _yq(cl["pod_cidr"]),
        "    service_cidr: %s" % _yq(cl["service_cidr"]),
    ]

    # 私有镜像仓库部署(harbor/docker-registry; 部署在 registry 角色节点)
    if imgreg.get("type") in ("harbor", "docker-registry"):
        lines += [
            "  image_registry:",
            "    type: %s" % _yq(imgreg["type"]),
        ]
        if imgreg.get("vip"):
            lines.append("    ha_vip: %s" % _yq(imgreg["vip"]))

    # 存储配置(localpv / nfs storageclass): 必须显式写出 — kk 默认启用 localpv(enabled+default),
    # 不写会被静默当成已启用, 离线包没带 localpv chart/镜像时部署会折在存储类这一步
    lines += [
        "  storage_class:",
        "    local:",
        "      enabled: %s" % ("true" if storage.get("localpv_enabled") else "false"),
        "      default: %s" % ("true" if (storage.get("localpv_enabled") and not storage.get("nfs_enabled")) else "false"),
    ]
    if storage.get("localpv_enabled") and storage.get("localpv_path"):
        lines.append("      path: %s" % _yq(storage["localpv_path"]))
    if storage.get("nfs_enabled"):
        lines += [
            "    nfs:",
            "      enabled: true",
            "      default: %s" % _yq(bool(storage.get("nfs_default"))),
        ]
        if storage.get("nfs_server"):
            lines.append("      server: %s" % _yq(storage["nfs_server"]))
        if storage.get("nfs_path"):
            lines.append("      path: %s" % _yq(storage["nfs_path"]))

    lines += [
        "  native:",
        "    timezone: %s" % _yq(cl["timezone"]),
    ]
    if cl.get("set_hostname") is False:
        lines.append("    set_hostname: false")
    lines += [
        "    ntp:",
        "      enabled: %s" % _yq(bool(ntp.get("enabled"))),
        "      servers:",
    ]
    lines += [f"        - {_yq(s)}" for s in (ntp.get("servers") or [])] or ["        # (空)"]

    k8s_reg = cl.get("k8s_image_registry") or ""
    if online:
        lines += [
            "  # 在线安装: fetch=true, 组件与镜像联网下载(zone=cn 走国内源)",
            "  download:",
            "    fetch: true",
        ]
        if k8s_reg:
            lines += ["    images:", "      registry: %s" % _yq(k8s_reg)]
    else:
        lines += [
            "  download:",
            "    fetch: false",
            '    artifact_file: ""',
            '    artifact_md5: ""',
        ]
    lines += [
        "  cluster_require:",
        "    # 兜底: 未打补丁的官方 kk 也能过发行版 precheck; 打补丁构建无副作用",
        "    allow_unsupported_distribution_setup: true",
    ]
    return "\n".join([ln for ln in lines if ln is not None]) + "\n"


_PREPARE = None


def _prepare_module():
    """惰性加载 prepare_cluster 模块(离线镜像清单/收集函数的单一来源, 打包与备料两处共用)"""
    global _PREPARE
    if _PREPARE is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("kk_prepare", BASE_DIR / "prepare_cluster.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _PREPARE = mod
    return _PREPARE


def _tar_valid(path):
    """tar.gz/tgz 完整性快速校验(损坏返回 False)"""
    try:
        import tarfile
        with tarfile.open(path) as tf:
            tf.getmembers()
        return True
    except Exception:
        return False


def cluster_materials(cfg, catalog):
    """集群物料: 返回 (missing, [(bundle内相对路径, 源Path)])。online 模式仅需 kk 与部署脚本。"""
    cl = cfg["cluster"]
    ccat = catalog["cluster"]
    arch = cfg["arch"]
    missing, items = [], []

    kk_rel = ccat["kk"]["binaries"].get(arch)
    kk_path = BASE_DIR / kk_rel if kk_rel else None
    if kk_path and kk_path.is_file():
        items.append(("cluster/kk", kk_path))
    else:
        missing.append("kk 二进制(%s, 须为打过信创补丁的构建): %s" % (arch, kk_rel))

    if cl["mode"] == "artifact":
        fname = ccat["artifact"]["filename_pattern"].format(kube_version=cl["kube_version"], arch=arch)
        art = BASE_DIR / ccat["artifact"]["warehouse_dir"] / fname
        if art.is_file():
            items.append(("cluster/" + fname, art))
        else:
            missing.append("kk artifact 产物: %s" % art)
    elif cl["mode"] == "online":
        pass   # 在线安装: 组件与镜像部署时联网下载(zone=cn 走国内源), 无本地物料
    else:
        ver = ccat["versions"][cl["kube_version"]]
        comps = ver.get("components") or {}
        cni_type = cl["cni_type"]
        cni_version = (ver.get("cni_versions") or {}).get(cni_type) or ver.get("cni_plugin", {}).get("version", "")
        # containerd 版本覆盖: 老内核节点(如 CentOS7 3.10 不受 containerd 2.x 支持)降级 1.7.x,
        # 仓库目录与包内 cache 布局同步切换到覆盖版本
        ct_ver_override = ((cl.get("components") or {}).get("containerd_version_override") or "").strip()
        cache_map = {"kube": "kube", "etcd": "etcd", "cni_plugins": "cni/plugins",
                     "helm": "helm", "crictl": "crictl", "containerd": "containerd", "runc": "runc"}
        for key, cache_sub in cache_map.items():
            comp = comps.get(key) or {}
            wdir_rel = comp.get("warehouse_dir")
            if not wdir_rel:
                continue
            layout = comp.get("cache_layout") or ("%s/{arch}" % cache_sub)
            if key == "containerd" and ct_ver_override:
                wdir_rel = re.sub(r"(containerd/)v[^/]+(/)", r"\g<1>%s\g<2>" % ct_ver_override, wdir_rel)
                layout = re.sub(r"(containerd/)v[^/]+(/)", r"\g<1>%s\g<2>" % ct_ver_override, layout)
            wdir = BASE_DIR / wdir_rel.format(arch=arch)
            sub = layout.format(arch=arch)
            # 排除断点续传/临时文件(xxx.p0 / xxx.part2 / xxx.tmp), 避免残缺物料混进包里;
            # tar.gz/tgz 额外做完整性校验(防中断残留的半截文件带病进包)
            real = []
            for f in wdir.glob("**/*"):
                if not f.is_file() or re.search(r"\.(p\d+|part\d*|tmp)$", f.name):
                    continue
                if f.suffix in (".tgz", ".gz") and not _tar_valid(f):
                    continue
                real.append(f)
            if not wdir.is_dir() or not real:
                missing.append("二进制缓存目录为空: %s" % wdir)
            else:
                items.append(("cluster/cache/%s" % sub, wdir))
                if key == "kube":
                    for b in ("kubeadm", "kubelet", "kubectl"):
                        if not (wdir / b).is_file():
                            missing.append("缺 %s: 应位于 %s/%s" % (b, wdir, b))
        # 纯离线镜像: 打包时收集(docker pull+save, 原生 tag), 部署前由 deploy-cluster.sh
        # 导入节点本地 containerd; 缺失时先记 missing, 打包流程会尝试自动收集
        img_src = BASE_DIR / "warehouse" / "cluster" / "images" / \
            _prepare_module().cluster_images_tar_name(cl["kube_version"], cl["cni_type"], arch)
        if img_src.is_file():
            items.append(("cluster/images/" + img_src.name, img_src))
        else:
            missing.append("k8s 离线镜像包: %s (需 Docker Desktop 自动收集)" % img_src)

        # CNI chart(+calicoctl): kk 部署 CNI 时 helm install binary_dir 下的固定路径 chart,
        # bundle 内放在 cluster/cache/cni/<type>/ 随缓存整体铺到 binary_dir 即命中
        chart_dir = BASE_DIR / "warehouse" / "cluster" / "charts" / cni_type
        chart_map = {"flannel": ["flannel-%s.tgz" % cni_version],
                     "calico": ["tigera-operator-%s.tgz" % cni_version],
                     "cilium": ["cilium-%s.tgz" % cni_version],
                     "kubeovn": ["kube-ovn-%s.tgz" % cni_version]}
        expect_charts = chart_map.get(cni_type) or []
        if expect_charts:
            miss_charts = [f for f in expect_charts if not (chart_dir / f).is_file()]
            if miss_charts:
                missing.append("CNI chart 物料: %s (应位于 %s/)" % (", ".join(miss_charts), chart_dir))
            else:
                items.append(("cluster/cache/cni/%s" % cni_type, chart_dir))
            if cni_type == "calico" and not miss_charts:
                ctl = chart_dir / cni_version / arch / ("calicoctl-linux-%s" % arch)
                if not ctl.is_file():
                    missing.append("calicoctl 二进制: %s" % ctl)

        # 存储 chart(localpv/nfs provisioner)
        storage = cl.get("storage") or {}
        if storage.get("localpv_enabled"):
            lp = ((ver.get("components") or {}).get("localpv") or {}).get("version") or "4.4.0"
            lp_chart = BASE_DIR / "warehouse" / "cluster" / "charts" / "localpv" / ("localpv-provisioner-%s.tgz" % lp)
            if lp_chart.is_file():
                items.append(("cluster/cache/storageclass/local", lp_chart.parent))
            else:
                missing.append("localpv chart: %s" % lp_chart)
        if storage.get("nfs_enabled"):
            nf = ((ver.get("components") or {}).get("nfs") or {}).get("version") or "v4.0.18"
            nf_chart = BASE_DIR / "warehouse" / "cluster" / "charts" / "nfs" / ("nfs-subdir-external-provisioner-%s.tgz" % nf)
            if nf_chart.is_file():
                items.append(("cluster/cache/storageclass/nfs", nf_chart.parent))
            else:
                missing.append("nfs provisioner chart: %s" % nf_chart)

        # 升级目标版本的离线镜像包: 升级只会替换 kube 三件套, 新版本控制面/CoreDNS 镜像
        # 需随包携带并由 upgrade-cluster.sh 导入, 否则 air-gapped 升级会联网拉镜像失败
        up_to = ((cl.get("upgrade_to") or "")).strip()
        if up_to:
            pm = _prepare_module()
            up_src = BASE_DIR / "warehouse" / "cluster" / "images" / \
                pm.cluster_images_tar_name(up_to, cni_type, arch)
            if up_src.is_file():
                items.append(("cluster/upgrade/images-%s.tar" % up_to, up_src))
            else:
                missing.append("离线镜像包(升级 %s): %s (打包时自动收集)" % (up_to, up_src))
            # kk upgrade --all 的二进制阶段按目标版本 manifest 取全套组件(kube/crictl/helm/
            # etcd/cni/containerd/runc), 而基础包缓存是部署版本的组件(如 helm v3.12.1):
            # 目标版本换了 minor 组件版本就对不上, 必须整套随包由 upgrade-cluster.sh 铺进
            # kk 缓存(真机 v1.28.15->v1.29.15 首测先后踩坑 crictl v1.29.0 与 helm v3.13.3 缺失)
            up_comps = ((ccat["versions"].get(up_to) or {}).get("components") or {})
            for key, comp in up_comps.items():
                if key == "kube":
                    continue   # kube 三件套由 pack 侧 cluster/upgrade/<版本>/ 专门携带
                wdir_rel = comp.get("warehouse_dir")
                if not wdir_rel:
                    continue
                layout = comp.get("cache_layout") or ""
                cw = BASE_DIR / wdir_rel.format(arch=arch)
                if cw.is_dir() and any(f.is_file() for f in cw.glob("**/*")):
                    items.append(("cluster/upgrade/cache/%s" % layout.format(arch=arch), cw))
                else:
                    missing.append("升级组件 %s(%s): %s (python prepare_cluster.py --download 可补)" % (key, up_to, cw))
    if not CLUSTER_SH.is_file():
        missing.append("集群部署脚本模板缺失: %s" % CLUSTER_SH)

    for d in cl["os_distros"]:
        d_dir = BASE_DIR / ccat["os_packages_dir"].format(distro=d, arch=arch)
        if d_dir.is_dir() and any(d_dir.iterdir()):
            items.append(("cluster/os/%s" % d, d_dir))
        else:
            missing.append("OS 依赖包目录为空: %s (不需要可取消勾选该发行版)" % d_dir)
    return missing, items


def gen_upgrade_sh(cfg):
    """一键集群升级脚本: 导入目标版本离线镜像 → 铺三件套到 kk 缓存 → kk upgrade cluster"""
    cl = cfg["cluster"]
    up_to = cl["upgrade_to"]
    catalog = load_catalog()
    sandbox = "registry.k8s.io/pause:%s" % (((catalog["cluster"]["versions"].get(up_to) or {})
                                             .get("sandbox_image_tag") or ""))
    return r"""#!/usr/bin/env bash
# 一键集群升级脚本 (由 packer.py 生成; 目标版本: {ver})
# 前提: 集群已通过 ./deploy.sh 部署完成; 纯离线升级(不在线拉取任何镜像)
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# ctr/kubectl 等装在 /usr/local/bin: cron/CI 等非登录 shell 的 PATH 不含它
export PATH="/usr/local/bin:/usr/local/sbin:$PATH"
[ -f manifest.sh ] || { echo "[FAIL] 缺少 manifest.sh"; exit 1; }
source ./manifest.sh
[ "${CLUSTER_ENABLED:-0}" = "1" ] || { echo "[FAIL] 本包未启用集群"; exit 1; }

KK_HOME="$PWD/kubekey"; KK_CACHE="$KK_HOME/kubekey"

# SSH 包装(与 deploy-cluster.sh 同源): 凭据取 cluster/inventory.yaml
SSH_PORT="$(sed -n 's/^        port: \([0-9][0-9]*\).*/\1/p' cluster/inventory.yaml | head -1)"; SSH_PORT="${SSH_PORT:-22}"
SSH_USER="$(sed -n 's/^        user: \(.*\)$/\1/p' cluster/inventory.yaml | head -1 | tr -d '"')"; SSH_USER="${SSH_USER:-root}"
PW="$(sed -n 's/^        password: \(.*\)$/\1/p' cluster/inventory.yaml | head -1 | tr -d '"')"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=8 -p $SSH_PORT"
# scp 的端口参数是大写 -P(-p 是保留时间戳), 不能与 ssh 共用一份 OPTS
SCP_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=8 -P $SSH_PORT"
if [ -n "$PW" ] && command -v sshpass >/dev/null 2>&1; then
  RUN_SSH() { SSHPASS="$PW" sshpass -e ssh $SSH_OPTS "$@"; }
  RUN_SCP() { SSHPASS="$PW" sshpass -e scp $SCP_OPTS "$@"; }
else
  RUN_SSH() { ssh $SSH_OPTS -o BatchMode=yes "$@"; }
  RUN_SCP() { scp $SCP_OPTS "$@"; }
fi

# 节点侧导入脚本: 写临时文件后 bash -s 下发(与 deploy-cluster.sh 的 IMP_SH 同一套路)。
# 不能用 _script 变量 + bash -c 嵌套引号: sed 行的 \\" 转义经两层 shell 解析必炸(unexpected EOF)
IMP_SH="$(mktemp /tmp/.kk-up-import.XXXXXX.sh)"
cat > "$IMP_SH" <<UP_EOF
for t in /tmp/.kk-up/*.tar; do
  [ -f "\$t" ] || continue
  ctr -n k8s.io images import --all-platforms "\$t" >/dev/null 2>&1 || echo "[WARN] \$(basename \$t) 导入部分报错(多为已存在)"
done
if [ -n "{sandbox}" ] && [ -s /etc/containerd/config.toml ]; then
  # 新版本 pause 镜像可能变化, 改写 sandbox 后重启 containerd(磁盘存储不丢镜像)
  sed -i "s#sandbox_image = \\"registry.k8s.io/pause:[^\\"]*\\"#sandbox_image = \\"{sandbox}\\"#" /etc/containerd/config.toml
  systemctl restart containerd
  sleep 2
fi
rm -rf /tmp/.kk-up
UP_EOF
import_one() {
  # $1 = LOCAL 或 user@ip
  if [ "$1" = "LOCAL" ]; then bash "$IMP_SH"; else RUN_SSH "$1" 'bash -s' < "$IMP_SH"; fi
}

echo "[INFO] 导入目标版本离线镜像 ({ver})"
LOCAL_IPS="$(hostname -I 2>/dev/null || true)"
for entry in ${CLUSTER_NODES[@]:-}; do
  IP="$(echo "$entry" | cut -d'|' -f2)"
  if echo "$LOCAL_IPS" | grep -qw "$IP"; then
    mkdir -p /tmp/.kk-up && cp -a cluster/upgrade/images-*.tar /tmp/.kk-up/ 2>/dev/null || true
    import_one LOCAL
  else
    RUN_SSH "${SSH_USER}@${IP}" "mkdir -p /tmp/.kk-up" || { echo "[FAIL] 节点 $IP 不可达"; exit 1; }
    RUN_SCP -q cluster/upgrade/images-*.tar "${SSH_USER}@${IP}:/tmp/.kk-up/" || { echo "[FAIL] 节点 $IP 镜像分发失败"; exit 1; }
    import_one "${SSH_USER}@${IP}"
  fi
done
rm -f "$IMP_SH"

# 0) 铺目标版本全套组件缓存到 kk 缓存(upgrade --all 的二进制阶段按目标版本 manifest
#    取 crictl/helm/etcd/cni/containerd/runc 全套; 只带 kube 会连环缺 crictl/helm)
if [ -d cluster/upgrade/cache ]; then
  cp -a cluster/upgrade/cache/. "$KK_CACHE/"
  echo "[INFO] 目标版本组件缓存已就位 ($KK_CACHE)"
fi
# 1) 铺目标版本 kube 三件套到 kk 缓存
mkdir -p "$KK_CACHE/kube/{ver}/{arch}"
cp -a cluster/upgrade/{ver}/{arch}/. "$KK_CACHE/kube/{ver}/{arch}/"
chmod +x "$KK_CACHE/kube/{ver}/{arch}/"*
echo "[INFO] 目标版本 {ver} 二进制已就位 ($KK_CACHE/kube/{ver}/{arch})"
# 2) 执行升级 (kubeadm 只允许单 minor 递增, kk 会自动拆步; --all 连带组件)
echo "[INFO] 开始升级: $CLUSTER_KUBE_VERSION -> {ver} (耗时较长, 请勿中断)"
./cluster/kk upgrade cluster -i cluster/inventory.yaml -c cluster/config.yaml \
  --workdir "$KK_HOME" \
  --with-kubernetes {ver} --all
echo "[INFO] 升级完成, 验证:"
KUBECONFIG=/etc/kubernetes/admin.conf kubectl get nodes -o wide
""".replace("{ver}", up_to).replace("{arch}", cfg["arch"]).replace("{sandbox}", sandbox)


def _auto_download_cluster_materials(cfg, catalog, cl_missing, prog):
    """打包时自动补齐缺失的集群二进制物料(调 prepare_cluster.py 的下载器)。
    返回 {"downloaded_mb": float, "warnings": [...]}; 无法自动生成的物料(kk/artifact)提示人工途径。"""
    arch = cfg["arch"]
    cl = cfg["cluster"]
    res = {"downloaded_mb": 0.0, "warnings": []}
    # 判定缺失项是否全部为可自动下载的缓存组件; kk/artifact 缺失不走自动下载
    needs_kk = any("kk 二进制" in m for m in cl_missing)
    needs_artifact = any("artifact 产物" in m for m in cl_missing)
    needs_cache = any("二进制缓存目录" in m or "缺 kubeadm" in m or "缺 kubelet" in m or "缺 kubectl" in m for m in cl_missing)
    needs_images = any("离线镜像包" in m for m in cl_missing)
    needs_charts = any("chart" in m or "calicoctl" in m for m in cl_missing)
    if needs_kk:
        res["warnings"].append("缺 kk 二进制: 运行 python prepare_cluster.py --kk-build 生成构建脚本"
                               "|Missing kk binary: run prepare_cluster.py --kk-build")
    if needs_artifact:
        res["warnings"].append("缺 artifact 产物: 打包时自动构建(需 Docker Desktop), 或运行 python prepare_cluster.py --artifact-export 后执行生成的 .bat"
                               "|Missing artifact: auto-built at pack time (Docker Desktop), or run prepare_cluster.py --artifact-export then the generated .bat")
    if not needs_cache and not needs_images and not needs_charts:
        return res

    pm_path = BASE_DIR / "prepare_cluster.py"
    if not pm_path.is_file():
        res["warnings"].append("缺 prepare_cluster.py, 无法自动下载")
        return res
    try:
        pm = _prepare_module()
    except Exception as e:
        res["warnings"].append("备料脚本加载失败: %s" % e)
        return res

    up_to = (cl.get("upgrade_to") or "").strip()
    if up_to:
        # 打包时缺升级物料同样自动下载; 升级三件套已在仓库则无需再下
        kube_dir = BASE_DIR / ("warehouse/cluster/kube/%s/%s" % (up_to, arch))
        if all((kube_dir / b).is_file() for b in ("kubeadm", "kubelet", "kubectl")):
            up_to = ""
    prog.stage("下载缺失集群物料")
    prog._downloads.clear()

    def cb(event, name, done, total):
        if event == "start":
            if total:
                info_msg = "需下载 %.0f MB" % (total / 1048576)
                prog.file(info_msg, total)
        elif event == "dl":
            prog._downloads[name] = [done, total]
            prog.file("%s (%.1f/%.1f MB)" % (name, done / 1048576, total / 1048576), total)
        elif event == "done":
            prog._downloads.pop(name, None)

    try:
        if needs_cache:
            downloaded, failed = pm.ensure_materials(catalog["cluster"], cl["kube_version"], arch, up_to, progress_cb=cb)
            res["downloaded_mb"] += downloaded / 1048576
            if failed:
                res["warnings"].append("自动下载失败: %s (网络恢复后重跑或 python prepare_cluster.py --download)" % ", ".join(failed))
        if needs_images:
            # 不接 cb: ensure_images 的事件单位是镜像个数, 与进度条的字节模型不兼容;
            # docker pull 本身无字节回调, 阶段提示即可
            prog.stage("收集离线镜像包(docker, 需数分钟)")
            cl = cfg["cluster"]
            versions = [cl["kube_version"]]
            if (cl.get("upgrade_to") or "").strip():
                versions.append(cl["upgrade_to"].strip())   # 升级目标版本的镜像同样随包携带
            for v in versions:
                img_dl, img_failed = pm.ensure_images(ccat, v, arch,
                                                      pm.k8s_image_specs(ccat, v, cl["cni_type"],
                                                                         ha_type=cl.get("ha_type") or "local",
                                                                         storage=cl.get("storage") or {}, arch=arch),
                                                      cl["cni_type"])
                res["downloaded_mb"] += img_dl   # ensure_images 返回值已是 MB
                if img_failed:
                    res["warnings"].append("离线镜像收集失败(%s): %s (需 Docker Desktop 运行中)" % (v, ", ".join(img_failed)))
        if needs_charts:
            prog.stage("下载 CNI/存储 chart")
            cl = cfg["cluster"]
            ch_dl, ch_failed = pm.ensure_charts(ccat, cl["kube_version"], cl["cni_type"], arch,
                                                storage=cl.get("storage") or {})
            res["downloaded_mb"] += ch_dl
            if ch_failed:
                res["warnings"].append("chart 下载失败: %s (检查网络或手工放置到 warehouse/cluster/charts/)"
                                       % ", ".join(ch_failed))
    except Exception as e:
        res["warnings"].append("自动下载异常: %s" % e)
    prog._downloads.clear()
    return res


def _auto_build_artifact(cfg, catalog, prog):
    """打包时自动构建 kk artifact 产物(打包机需 Docker Desktop + 外网)。
    镜像清单由 kk 按 kube_version+CNI 自动聚合; 返回 (ok, 失败日志尾部)。"""
    import shutil
    import subprocess
    arch = cfg["arch"]
    ver = cfg["cluster"]["kube_version"]
    ccat = catalog["cluster"]
    art_dir = (BASE_DIR / ccat["artifact"]["warehouse_dir"]).resolve()
    out_name = ccat["artifact"]["filename_pattern"].format(kube_version=ver, arch=arch)
    if (art_dir / out_name).is_file():
        return True, ""
    pm_path = BASE_DIR / "prepare_cluster.py"
    if not pm_path.is_file():
        return False, "缺 prepare_cluster.py"
    import importlib.util
    spec = importlib.util.spec_from_file_location("kk_prepare", pm_path)
    pm = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(pm)
    except Exception as e:
        return False, "备料脚本加载失败: %s" % e
    try:
        pm.cmd_artifact_export(ccat, ver)   # 生成导出配置(复用备料脚本, 不重复实现)
    except Exception as e:
        return False, "导出配置生成失败: %s" % e
    cfg_name = "artifact-config-%s-%s.yaml" % (ver, arch)
    kk_ver = (ccat.get("kk") or {}).get("version", "v4.0.7-xc1")
    kk_in = "/work/warehouse/cluster/kk/%s/%s/kk" % (kk_ver, arch)
    # kk 本地连接器固定走 `sudo -SE`; 容器无网络源装不了 sudo, 用垫片透传执行,
    # 并备好 /etc/sudoers 与 visudo(artifact_export 的 native/root 角色要改 sudoers)
    shim = ("printf '#!/bin/bash\\n[ \"$1\" = \"-SE\" ] && shift\\nexec \"$@\"\\n' > /usr/local/bin/sudo "
            "&& chmod +x /usr/local/bin/sudo "
            "&& touch /etc/sudoers "
            "&& printf '#!/bin/bash\\nexit 0\\n' > /usr/sbin/visudo && chmod +x /usr/sbin/visudo "
            "&& chmod +x %s" % kk_in)
    inner = ("sed -i 's|http://archive.ubuntu.com|http://mirrors.huaweicloud.com|g; "
             "s|http://security.ubuntu.com|http://mirrors.huaweicloud.com|g' /etc/apt/sources.list "
             "&& apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq ca-certificates >/dev/null 2>&1 "
             "&& %s && %s artifact export -c /work/warehouse/cluster/artifact/%s --workdir /work/kkstage"
             % (shim, kk_in, cfg_name))
    cmd = ["docker", "run", "--rm", "--name", "kk-artifact-build",
           "-v", BASE_DIR.resolve().as_posix() + ":/work", "-w", "/work",
           "ubuntu:22.04", "bash", "-c", inner]
    prog.stage("构建 kk artifact 产物(拉取全量镜像, 约 5~20 分钟; 需 Docker Desktop)")
    prog._downloads["artifact 构建"] = [0, 1]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace")
    except Exception as e:
        prog._downloads.clear()
        return False, "Docker 启动失败: %s" % e
    tail = []
    for line in proc.stdout:
        line = line.strip()
        if line:
            tail.append(line)
            if len(tail) > 200:
                tail.pop(0)
        prog._downloads["artifact 构建"] = [min(len(tail) + 1, 60), 60]
        if prog._cancelled:
            proc.kill()
            # docker CLI 被杀后容器仍会运行, 强制清理, 避免孤儿容器占用挂载
            subprocess.run(["docker", "rm", "-f", "kk-artifact-build"],
                           capture_output=True, timeout=30)
            prog._downloads.clear()
            shutil.rmtree(BASE_DIR / "kkstage", ignore_errors=True)
            raise PackError("已取消|Cancelled")
    proc.wait()
    prog._downloads.clear()
    # kk 固定把产物写到 {workdir}/artifact/kubekey-artifact.tgz; 搬到仓库目录并改名
    stage_tgz = BASE_DIR / "kkstage" / "artifact" / "kubekey-artifact.tgz"
    ok = proc.returncode == 0 and stage_tgz.is_file()
    if ok:
        art_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stage_tgz), str(art_dir / out_name))
    shutil.rmtree(BASE_DIR / "kkstage", ignore_errors=True)
    if ok:
        return True, ""
    return False, " | ".join(tail[-3:]) if tail else "kk artifact export 退出码 %s, 未找到导出产物" % proc.returncode


def gen_manifest_sh(cfg, catalog, client_img, summary_lines, bundle_name=None):
    ports = cfg["ports"]
    db = cfg["db"]
    services = cfg["services"]
    topology = cfg.get("topology") or {}
    mh_all = set(k for k, v in (cfg.get("multihost") or {}).items() if v.get("enabled"))
    reps = cfg.get("replicas") or {}
    features = cfg.get("features") or {}

    data_dirs = []
    chown_dirs = []
    container_names = []
    port_keys = []
    for s in services:
        meta = catalog["services"][s]
        if s == "kafka" and topology.get("kafka") == "cluster":
            if is_multihost(cfg, "kafka"):
                continue   # 多机: Kafka 容器在各节点, 主部署机清单不含
            # 集群形态: 3 个 broker 容器; 宿主机端口为 kafka_c1..3, 单节点端口键不参与
            for n in (1, 2, 3):
                container_names.append("kafka%d" % n)
                data_dirs.append("kafka%d/data" % n)
                port_keys.append(ports["kafka_c%d" % n])
            continue
        if s in ("mysql8", "mysql57", "redis") and is_multihost(cfg, s):
            continue   # 多机: 容器在各节点
        if s in PLUGINS:
            container_names.append(PLUGINS[s].META.get("container_name", s))
        else:
            container_names.append(s if s != "xxljob" else "xxl-job")
        for p in meta["ports"]:
            if s == "kafka" and topology.get("kafka") == "cluster":
                continue   # 单机集群: 单节点端口键不参与
            port_keys.append(ports[p["key"]])
        for r in (cfg.get("extra_ports") or {}).get(s, []):
            port_keys.append(r["host"])
        if s in ("mysql57", "mysql8"):
            data_dirs += ["%s/data" % meta["data_dir"], "%s/log" % meta["data_dir"], "%s/init" % meta["data_dir"]]
            chown_dirs.append("%s/log" % meta["data_dir"])
        elif s == "redis":
            data_dirs += ["redis/data", "redis/log"]
            chown_dirs.append("redis/log")
        elif s == "nginx":
            data_dirs += ["nginx/conf", "nginx/html", "nginx/logs", "nginx/ssl"]
        else:
            data_dirs += ["%s/data" % meta["data_dir"], "%s/log" % meta["data_dir"]]

    local_mysql = ""
    if "mysql8" in services and not is_multihost(cfg, "mysql8"):
        local_mysql = "mysql8"
    elif "mysql57" in services and not is_multihost(cfg, "mysql57"):
        local_mysql = "mysql57"

    # 健康实测清单: 有 compose healthcheck 的容器等待 healthy, 其余 HTTP 实测
    health_wait, health_http = [], []
    for s in services:
        if s in ("mysql8", "mysql57", "redis") and is_multihost(cfg, s):
            continue   # 多机: 健康检查在节点 install-node.sh 内完成
        cn = PLUGINS[s].META.get("container_name", s) if s in PLUGINS else (s if s != "xxljob" else "xxl-job")
        if s == "kafka" and topology.get("kafka") == "cluster":
            if not is_multihost(cfg, "kafka"):
                health_wait += ["kafka1", "kafka2", "kafka3"]
            continue
        if s in ("mysql57", "mysql8", "redis", "minio") or s in PLUGINS:
            health_wait.append(cn)
        elif s == "nginx":
            health_http.append("%s|http://127.0.0.1:%d/" % (cn, ports["nginx_http"]))
        elif s == "nacos":
            health_http.append("%s|http://127.0.0.1:%d/" % (cn, ports["nacos_console"]))
        elif s == "xxljob":
            health_http.append("%s|http://127.0.0.1:%d/xxl-job-admin/" % (cn, ports["xxljob"]))
    if "minio" in services:
        health_http.append("minio|http://127.0.0.1:%d/minio/health/live" % ports["minio_api"])

    # 集群形态附加的容器/目录/端口/健康清单 (多机时容器在节点上, 主部署机清单跳过)
    if topology.get("mysql8") == "master-slave" and "mysql8" not in mh_all:
        for _r in range(reps.get("mysql8", 1)):
            _rsvc = "mysql8-replica" if _r == 0 else "mysql8-replica%d" % (_r + 1)
            _rport = "mysql8_replica" if _r == 0 else "mysql8_replica%d" % (_r + 1)
            container_names.append(_rsvc)
            data_dirs += ["%s/data" % _rsvc, "%s/log" % _rsvc]
            chown_dirs.append("%s/log" % _rsvc)
            port_keys.append(ports[_rport])
            health_wait.append(_rsvc)
    if topology.get("mysql57") == "master-slave" and "mysql57" not in mh_all:
        for _r in range(reps.get("mysql57", 1)):
            _rsvc = "mysql57-replica" if _r == 0 else "mysql57-replica%d" % (_r + 1)
            _rport = "mysql57_replica" if _r == 0 else "mysql57_replica%d" % (_r + 1)
            container_names.append(_rsvc)
            data_dirs += ["%s/data" % _rsvc, "%s/log" % _rsvc]
            chown_dirs.append("%s/log" % _rsvc)
            port_keys.append(ports[_rport])
            health_wait.append(_rsvc)
    if topology.get("redis") == "sentinel" and "redis" not in mh_all:
        container_names.append("redis-replica")
        data_dirs += ["redis-replica/data", "redis-replica/log"]
        chown_dirs.append("redis-replica/log")
        port_keys.append(ports["redis_replica"])
        health_wait.append("redis-replica")
        # 哨兵会把故障转移结果重写回自己的配置文件, 文件属主必须是容器内 redis(999)
        # (部署布局是平铺的: conf/<svc>/* 会被 deploy.sh 放到 <DEPLOY_DIR>/<svc>/ 下)
        chown_dirs.append("redis/sentinel.conf:999")
    if features.get("proxysql"):
        container_names.append("proxysql")
        data_dirs.append("proxysql/data")
        health_wait.append("proxysql")
        port_keys += [ports["proxysql"], ports["proxysql_admin"]]

    def db_lines(app, prefix):
        conf = db.get(app)
        if not conf:
            return ["%(P)s_IMPORT=0" % {"P": prefix}]
        imported = 1
        return [
            "%(P)s_IMPORT=%(imp)d" % {"P": prefix, "imp": imported},
            "%(P)s_SQL=%(sql)s" % {"P": prefix, "sql": bash_quote("sql/%s.sql" % ("nacos" if app == "nacos" else "xxl-job"))},
            "%(P)s_SCHEMA=%(s)s" % {"P": prefix, "s": bash_quote(conf["schema"])},
            "%(P)s_DB_MODE=%(m)s" % {"P": prefix, "m": bash_quote(conf["mode"])},
            "%(P)s_DB_HOST=%(h)s" % {"P": prefix, "h": bash_quote(conf["host"])},
            "%(P)s_DB_HOST_IMPORT=%(h)s" % {"P": prefix, "h": bash_quote(conf.get("host_import", ""))},
            "%(P)s_DB_PORT=%(p)d" % {"P": prefix, "p": conf["port"]},
            "%(P)s_DB_USER=%(u)s" % {"P": prefix, "u": bash_quote(conf["user"])},
            "%(P)s_DB_PASSWORD=%(p)s" % {"P": prefix, "p": bash_quote(conf["password"])},
        ]

    lines = [
        "# 由 packer.py 自动生成 — deploy.sh 的全部决策来源, 服务器上请勿手改",
        "PROJECT=%s" % bash_quote(cfg["project"]),
        "PKG_ARCH=%s" % bash_quote(cfg["arch"]),
        "DEPLOY_LANG=%s" % bash_quote(cfg.get("lang", "zh")),
        "BUNDLE_NAME=%s" % bash_quote((bundle_name or "") + ".tar.gz"),
        "DEPLOY_DIR=%s" % bash_quote(cfg["deploy_dir"]),
        "DOCKER_DATA_ROOT=%s" % bash_quote(cfg["docker_data_root"]),
        "REGISTRY_MIRRORS=(%s)" % " ".join(bash_quote(m) for m in cfg["registry_mirrors"]),
        "PORTS_TO_CHECK=(%s)" % " ".join(str(p) for p in port_keys),
        "CONTAINER_NAMES=(%s)" % " ".join(container_names),
        "DATA_DIRS=(%s)" % " ".join(bash_quote(d) for d in data_dirs),
        "CHOWN_DIRS=(%s)" % " ".join(bash_quote(d) for d in chown_dirs),
        "LOCAL_MYSQL_SVC=%s" % bash_quote(local_mysql),
        "MYSQL_TOPOLOGY=%s" % bash_quote(topology.get(local_mysql, "single") if local_mysql else "single"),
        "MYSQL8_TOPOLOGY=%s" % bash_quote(topology.get("mysql8", "single")),
        "MYSQL57_TOPOLOGY=%s" % bash_quote(topology.get("mysql57", "single")),
        "REDIS_TOPOLOGY=%s" % bash_quote(topology.get("redis", "single")),
        "KAFKA_TOPOLOGY=%s" % bash_quote(topology.get("kafka", "single")),
        "MYSQL_MULTIHOST=%d" % (1 if (any(is_multihost(cfg, s) for s in ("mysql8", "mysql57"))) else 0),
        "MYSQL_MULTIHOST_SVC=%s" % bash_quote(next((s for s in ("mysql8", "mysql57") if is_multihost(cfg, s)), "")),
        "MYSQL_MULTIHOST_HOST=%s" % bash_quote(next((master_server_ip(cfg, s) for s in ("mysql8", "mysql57") if is_multihost(cfg, s)), "")),
        "MYSQL_MULTIHOST_PORT=%s" % next((str(ports[s]) for s in ("mysql8", "mysql57") if is_multihost(cfg, s)), "0"),
        "NODES_COUNT=%d" % len(gen_nodes(cfg, catalog)),
        "MYSQL_ROOT_PASSWORD=%s" % bash_quote(cfg["secrets"].get("MYSQL_ROOT_PASSWORD", "")),
        "MYSQL_CLIENT_IMG=%s" % bash_quote(client_img),
        "HEALTH_WAIT=(%s)" % " ".join(bash_quote(n) for n in health_wait),
        "HEALTH_HTTP=(%s)" % " ".join(bash_quote(h) for h in health_http),
    ]
    lines += db_lines("nacos", "NACOS")
    lines += db_lines("xxljob", "XXL")
    cl = cfg.get("cluster") or {}
    if cl.get("enabled"):
        lines += [
            "CLUSTER_ENABLED=1",
            "CLUSTER_KUBE_VERSION=%s" % bash_quote(cl["kube_version"]),
            "CLUSTER_MODE=%s" % bash_quote(cl["mode"]),
            "CLUSTER_ZONE=%s" % bash_quote(cl.get("zone") or ""),
            "CLUSTER_CNI=%s" % bash_quote(cl["cni_type"]),
            "CLUSTER_PROXY_MODE=%s" % bash_quote(cl["proxy_mode"]),
            "CLUSTER_HA_TYPE=%s" % bash_quote(cl.get("ha_type") or "local"),
            "CLUSTER_HA_VIP=%s" % bash_quote(cl.get("ha_vip") or ""),
            "CLUSTER_UPGRADE_TO=%s" % bash_quote(cl.get("upgrade_to") or ""),
            "CLUSTER_CERTS_RENEW_CRON=%s" % bash_quote(cl.get("certs_renew_cron") or ""),
            "CLUSTER_OS_DISTROS=(%s)" % " ".join(bash_quote(d) for d in cl["os_distros"]),
            "CLUSTER_NODES=(%s)" % " ".join(bash_quote("%s|%s|%s" % (n["name"], n["ip"], n["role"])) for n in cl["nodes"]),
            "CLUSTER_ONLY=%d" % (0 if services else 1),
            # 纯离线镜像导入参数(deploy-cluster.sh 用): sandbox 镜像与 containerd 数据目录
            "CLUSTER_SANDBOX_IMAGE=%s" % bash_quote("registry.k8s.io/pause:%s"
                                                    % (catalog["cluster"]["versions"][cl["kube_version"]].get("sandbox_image_tag") or "3.9")),
            "CLUSTER_CONTAINERD_DATA_ROOT=%s" % bash_quote((cl.get("components") or {}).get("containerd_root") or ""),
            # 卸载脚本用: 自定义 etcd/kubelet 数据目录(kk delete 只清默认路径, 这两个要兜底)
            "CLUSTER_ETCD_DATA_DIR=%s" % bash_quote((cl.get("components") or {}).get("etcd_dir") or ""),
            "CLUSTER_KUBELET_ROOT_DIR=%s" % bash_quote((cl.get("kubelet") or {}).get("root_dir") or ""),
            # 升级目标版本的 pause 镜像(与创建版本可能不同, 升级脚本需要改写 sandbox_image)
            "CLUSTER_UPGRADE_SANDBOX=%s" % bash_quote(
                ("registry.k8s.io/pause:%s" % ((catalog["cluster"]["versions"].get(cl.get("upgrade_to") or "") or {})
                                               .get("sandbox_image_tag") or "")) if cl.get("upgrade_to") else ""),
        ]
    b = cfg.get("backup") or {}
    lines += [
        "BACKUP_CRONS=(%s)" % " ".join(bash_quote("%s|%s" % (eng, cron)) for eng, cron in backup_crons(b)),
        "BACKUP_DIR=%s" % bash_quote(b.get("dir") or "/data/backup/db"),
    ]
    for s in services:
        if s in PLUGINS and hasattr(PLUGINS[s], "manifest_lines"):
            lines += PLUGINS[s].manifest_lines(cfg, ports, plugin_ctx(cfg))
    lines.append("SUMMARY_LINES=(%s)" % " ".join(bash_quote(l) for l in summary_lines))
    return "\n".join(lines) + "\n"


def build_summary_lines(cfg, catalog):
    ports = cfg["ports"]
    lines = []
    servers = cfg.get("servers") or []
    mh = cfg.get("multihost") or {}

    def mh_ip(svc, kind):
        """多机角色的节点 IP (kind: master/replica/brokers)"""
        m = mh.get(svc) or {}
        if kind == "master":
            return servers[m["master"]]["ip"] if isinstance(m.get("master"), int) and servers else "__NODE_IP__"
        if kind == "replicas":
            return [servers[i]["ip"] for i in m.get("replicas", []) if isinstance(i, int) and i < len(servers)]
        if kind == "brokers":
            return [servers[i]["ip"] for i in m.get("brokers", []) if isinstance(i, int) and i < len(servers)]
        return []

    if "nginx" in services_of(cfg):
        lines.append("NGINX        http://__IP__:%d   (配置目录 %s/nginx)|NGINX        http://__IP__:%d   (config dir %s/nginx)"
                     % (ports["nginx_http"], cfg["deploy_dir"], ports["nginx_http"], cfg["deploy_dir"]))
    if "mysql57" in services_of(cfg):
        if is_multihost(cfg, "mysql57"):
            mip = mh_ip("mysql57", "master")
            rips = mh_ip("mysql57", "replicas")
            lines.append("MySQL 5.7 多机主从  主库 %s:%d  从库 %s|MySQL 5.7 multi-host  master %s:%d  replicas %s"
                         % (mip, ports["mysql57"], ",".join("%s:%d" % (r, ports["mysql57_replica"]) for r in rips),
                            mip, ports["mysql57"], ",".join("%s:%d" % (r, ports["mysql57_replica"]) for r in rips)))
        else:
            lines.append("MySQL 5.7    __IP__:%d  mysql -h<ip> -P%d -uroot" % (ports["mysql57"], ports["mysql57"]))
            if (cfg.get("topology") or {}).get("mysql57") == "master-slave":
                lines.append("MySQL 5.7 从库 __IP__:%d  (只读, GTID 自动同步主库|replica, read-only, GTID sync from master)"
                             % ports["mysql57_replica"])
    if "mysql8" in services_of(cfg):
        if is_multihost(cfg, "mysql8"):
            mip = mh_ip("mysql8", "master")
            rips = mh_ip("mysql8", "replicas")
            lines.append("MySQL 8.0 多机主从  主库 %s:%d  从库 %s|MySQL 8.0 multi-host  master %s:%d  replicas %s"
                         % (mip, ports["mysql8"], ",".join("%s:%d" % (r, ports["mysql8_replica"]) for r in rips),
                            mip, ports["mysql8"], ",".join("%s:%d" % (r, ports["mysql8_replica"]) for r in rips)))
        else:
            lines.append("MySQL 8.0    __IP__:%d  mysql -h<ip> -P%d -uroot" % (ports["mysql8"], ports["mysql8"]))
            if (cfg.get("topology") or {}).get("mysql8") == "master-slave":
                lines.append("MySQL 8.0 从库 __IP__:%d  (只读, GTID 自动同步主库|replica, read-only, GTID sync from master)"
                             % ports["mysql8_replica"])
    if (cfg.get("features") or {}).get("proxysql"):
        lines.append("读写分离      mysql -h<ip> -P%d -uroot  (写->主库 读->从库, 管理台 <ip>:%d admin)|"
                     "Read/write split  mysql -h<ip> -P%d -uroot  (writes->master reads->replicas, admin <ip>:%d admin)"
                     % (ports["proxysql"], ports["proxysql_admin"], ports["proxysql"], ports["proxysql_admin"]))
    if "redis" in services_of(cfg):
        if is_multihost(cfg, "redis"):
            mip = mh_ip("redis", "master")
            rips = mh_ip("redis", "replicas")
            lines.append("Redis 多机哨兵  主库 %s:%d  从库 %s (每节点哨兵 :%d, 应用走 sentinel 协议)|"
                         "Redis multi-host sentinel  master %s:%d  replicas %s (sentinel :%d per node, clients use sentinel protocol)"
                         % (mip, ports["redis"], ",".join("%s:%d" % (r, ports["redis_replica"]) for r in rips),
                            ports.get("redis_sentinel", 26379), mip, ports["redis"],
                            ",".join("%s:%d" % (r, ports["redis_replica"]) for r in rips),
                            ports.get("redis_sentinel", 26379)))
        else:
            lines.append("Redis        __IP__:%d" % ports["redis"])
            if (cfg.get("topology") or {}).get("redis") == "sentinel":
                lines.append("Redis 哨兵    主+从+3哨兵 (从库 __IP__:%d, 应用走 sentinel 协议 redis-sentinel:26379)|"
                             "Redis Sentinel master+replica+3 sentinels (replica __IP__:%d, clients use sentinel protocol redis-sentinel:26379)"
                             % (ports["redis_replica"], ports["redis_replica"]))
    if "nacos" in services_of(cfg):
        lines.append("Nacos 控制台  http://__IP__:%d/nacos  (账号见 .env)|Nacos console http://__IP__:%d/nacos  (credentials in .env)" % (ports["nacos_console"], ports["nacos_console"]))
    if "xxljob" in services_of(cfg):
        lines.append("XXL-Job      http://__IP__:%d/xxl-job-admin  (admin/见 .env)|XXL-Job      http://__IP__:%d/xxl-job-admin  (admin/see .env)" % (ports["xxljob"], ports["xxljob"]))
    if "minio" in services_of(cfg):
        lines.append("MinIO        API http://__IP__:%d  控制台 http://__IP__:%d|MinIO        API http://__IP__:%d  console http://__IP__:%d"
                     % (ports["minio_api"], ports["minio_console"], ports["minio_api"], ports["minio_console"]))
    ctx = plugin_ctx(cfg)
    for s in services_of(cfg):
        if s not in PLUGINS:
            continue
        mod = PLUGINS[s]
        if hasattr(mod, "summary_lines"):
            lines += mod.summary_lines(cfg, ports, ctx)
        else:
            meta = catalog["services"][s]
            first = (meta.get("ports") or [{}])[0]
            port0 = ports.get(first.get("key", ""), first.get("default", 0))
            lines.append("%s  http://__IP__:%d" % (meta.get("label", s), port0))
    # 双语约定: 全部摘要行 "zh|en" 形式, 按 cfg.lang 取半边
    return [tr(l, cfg.get("lang", "zh")) for l in lines]


def services_of(cfg):
    return cfg["services"]


def gen_images_txt(cfg, catalog):
    """返回 [(bundle内文件名, 仓库相对路径, 短名)]; 多机部署的服务镜像改入 nodes/<name>/images/"""
    arch = cfg["arch"]
    kafka_cluster = "kafka" in cfg["services"] and (cfg.get("topology") or {}).get("kafka") == "cluster"
    kafka_mh = is_multihost(cfg, "kafka")
    items = []
    for s in cfg["services"]:
        if s == "kafka" and kafka_cluster:
            if kafka_mh:
                continue   # 多机: bitnami 镜像随节点分发, 不进主包 images.txt
            # 集群形态: 打包 bitnami 镜像(短名归一后为 bitnami/kafka:3.7.0, 归一化规则兼容)
            kc = getattr(PLUGINS.get("kafka"), "CLUSTER", None)
            if kc:
                items.append(("kafka-%s.tar" % arch, kc["images"][arch], "%s:%s" % (kc["image"], kc["tag"])))
            continue
        if s in ("mysql8", "mysql57", "redis") and is_multihost(cfg, s):
            continue   # 多机: 镜像随节点分发
        meta = catalog["services"][s]
        rel = meta["images"][arch]
        items.append(("%s-%s.tar" % (s, arch), rel, "%s:%s" % (meta["image"], meta["tag"])))
    needs_client, client_img = resolve_db(cfg)
    has_local_mysql = any(s in cfg["services"] for s in ("mysql57", "mysql8")) and not any(
        is_multihost(cfg, s) for s in ("mysql57", "mysql8"))
    if needs_client and not has_local_mysql:
        meta = catalog["services"]["mysql8"]
        rel = meta["images"][arch]
        items.append(("mysql8-%s.tar" % arch, rel, client_img))
    if (cfg.get("features") or {}).get("proxysql"):
        items.append(("proxysql-%s.tar" % arch, PROXYSQL_META["images"][arch],
                      "%s:%s" % (PROXYSQL_META["image"], PROXYSQL_META["tag"])))
    return items


# ---------------------------------------------------------------- 多机节点产物生成

def _node_kafka_block(cfg, ports, node_id, broker_ips):
    """多机 Kafka 节点 compose 块 (bitnami 3.7.0, KRaft 组合模式, 可配置宿主端口)"""
    server = cfg["servers"][node_id]
    inter, ctrl, sasl = ports["kafka_mh_inter"], ports["kafka_mh_ctrl"], ports["kafka_mh_sasl"]
    ip = server["ip"]
    voters = ",".join("%d@%s:%d" % (i + 1, cfg["servers"][b]["ip"], ctrl)
                      for i, b in enumerate((cfg["multihost"]["kafka"]["brokers"])))
    advertised = "INTERNAL://%s:%d,HOST://%s:%d" % (ip, inter, ip, sasl)
    return """
  kafka:
    image: %(image)s
    container_name: kafka
    restart: always
    user: root
    environment:
      TZ: ${TZ}
      KAFKA_ENABLE_KRAFT: "yes"
      KAFKA_KRAFT_CLUSTER_ID: %(cluster_id)s
      KAFKA_CFG_PROCESS_ROLES: broker,controller
      KAFKA_CFG_NODE_ID: %(node_id)d
      KAFKA_CFG_LISTENERS: INTERNAL://:9092,CONTROLLER://:9093,HOST://:9094
      KAFKA_CFG_LISTENER_SECURITY_PROTOCOL_MAP: INTERNAL:PLAINTEXT,CONTROLLER:PLAINTEXT,HOST:SASL_PLAINTEXT
      KAFKA_CFG_ADVERTISED_LISTENERS: %(advertised)s
      KAFKA_CFG_CONTROLLER_LISTENER_NAMES: CONTROLLER
      KAFKA_CFG_CONTROLLER_QUORUM_VOTERS: %(voters)s
      KAFKA_CFG_INTER_BROKER_LISTENER_NAME: INTERNAL
      KAFKA_CFG_SASL_ENABLED_MECHANISMS: PLAIN
      KAFKA_CFG_SASL_MECHANISM_INTER_BROKER_PROTOCOL: PLAIN
      KAFKA_CLIENT_USERS: admin
      KAFKA_CLIENT_PASSWORDS: ${KAFKA_PASSWORD}
      KAFKA_CFG_SUPER_USERS: User:admin;User:ANONYMOUS
      KAFKA_CFG_AUTHORIZER_CLASS_NAME: org.apache.kafka.metadata.authorizer.StandardAuthorizer
      KAFKA_CFG_ALLOW_EVERYONE_IF_NO_ACL_FOUND: "false"
      KAFKA_HEAP_OPTS: -Xmx1g -Xms512m
    ports:
      - "%(inter)d:9092"
      - "%(ctrl)d:9093"
      - "%(sasl)d:9094"
    volumes:
      - ./kafka/data:/bitnami/kafka
    healthcheck:
      test: ["CMD-SHELL", "/opt/bitnami/kafka/bin/kafka-broker-api-versions.sh --bootstrap-server localhost:9092 > /dev/null 2>&1 || exit 1"]
      interval: 15s
      timeout: 10s
      retries: 12
      start_period: 90s
    networks:
      - app-network""" % {
        "image": "bitnami/kafka:3.7.0", "cluster_id": KRAFT_CLUSTER_ID,
        "node_id": node_id + 1, "advertised": advertised, "voters": voters,
        "inter": inter, "ctrl": ctrl, "sasl": sasl,
    }


def _node_mysql_block(cfg, catalog, svc, role, replica_no=1):
    """多机 MySQL 节点 compose 块; role: master / replica; 容器名统一 mysql"""
    meta = catalog["services"][svc]
    ports = cfg["ports"]
    if role == "master":
        host_port = ports[svc]
        sid = 1
        repl_flags = ["--server-id=1", "--log-bin=mysql-bin", "--gtid-mode=ON",
                      "--enforce-gtid-consistency=ON",
                      "--binlog-expire-logs-seconds=604800" if svc == "mysql8" else "--expire-logs-days=7"]
    else:
        host_port = ports["%s_replica" % svc]
        sid = 10 + replica_no
        repl_flags = ["--server-id=%d" % sid, "--log-bin=mysql-bin", "--gtid-mode=ON",
                      "--enforce-gtid-consistency=ON", "--read-only=ON"]
    return """
  mysql:
    image: %(image)s
    container_name: mysql
    restart: always
    environment:
      TZ: ${TZ}
      MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD}
    ports:
      - "%(host_port)d:3306"
    volumes:
      - ./mysql/data:/var/lib/mysql
      - ./mysql/log:/var/log/mysql%(init_vol)s
      - ./mysql/my.cnf:/etc/mysql/conf.d/my.cnf:ro
    command:
      - --log-error=/var/log/mysql/error.log
      - --character-set-server=utf8mb4
      - --collation-server=utf8mb4_unicode_ci
      - --default-authentication-plugin=mysql_native_password
%(repl_flags)s    healthcheck:
      test: ["CMD-SHELL", "mysqladmin ping -h localhost -uroot -p\\"$$MYSQL_ROOT_PASSWORD\\""]
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 300s
    networks:
      - app-network""" % {
        "image": "%s:%s" % (meta["image"], meta["tag"]),
        "host_port": host_port,
        "init_vol": "\n      - ./mysql/init:/docker-entrypoint-initdb.d" if role == "master" else "",
        "repl_flags": "".join("      - %s\n" % f for f in repl_flags),
    }


def _node_redis_blocks(cfg, catalog, role, master_ip):
    """多机 Redis 节点 compose 块 (每台: redis + sentinel); role: master / replica"""
    meta = catalog["services"]["redis"]
    ports = cfg["ports"]
    replicaof = "--replicaof %(mip)s %(mport)d " % {"mip": master_ip, "mport": ports["redis"]} if role == "replica" else ""
    return """
  redis:
    image: %(image)s
    container_name: redis
    restart: always
    environment:
      TZ: ${TZ}
      REDIS_PASSWORD: ${REDIS_PASSWORD}
    ports:
      - "%(port)d:6379"
    volumes:
      - ./redis/data:/data
      - ./redis/log:/var/log/redis
      - ./redis/redis.conf:/usr/local/etc/redis/redis.conf:ro
    command: redis-server /usr/local/etc/redis/redis.conf --requirepass "${REDIS_PASSWORD}" --masterauth "${REDIS_PASSWORD}" %(replicaof)s
    healthcheck:
      test: ["CMD-SHELL", "redis-cli -a \\"$$REDIS_PASSWORD\\" ping | grep -q PONG"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - app-network
  redis-sentinel:
    image: %(image)s
    container_name: redis-sentinel
    restart: always
    environment:
      TZ: ${TZ}
    command: redis-server /usr/local/etc/redis/sentinel.conf --sentinel
    ports:
      - "%(sentinel_port)d:26379"
    volumes:
      - ./redis/sentinel.conf:/usr/local/etc/redis/sentinel.conf:ro
    networks:
      - app-network""" % {
        "image": "%s:%s" % (meta["image"], meta["tag"]),
        "port": ports["redis"] if role == "master" else ports["redis_replica"],
        "replicaof": replicaof,
        "sentinel_port": ports.get("redis_sentinel", 26379),
    }


def gen_proxysql_conf(cfg):
    """ProxySQL 读写分离配置: 写 -> HG10(主库), 读 -> HG20(从库), SELECT 自动路由到读组"""
    ports = cfg["ports"]
    pw = cfg["secrets"].get("MYSQL_ROOT_PASSWORD", "")
    mh_groups = []
    for s in ("mysql8", "mysql57"):
        if is_multihost(cfg, s):
            mh_groups.append((s, master_server_ip(cfg, s), ports[s],
                              [(cfg["servers"][i]["ip"], ports["%s_replica" % s])
                               for i in cfg["multihost"][s]["replicas"]]))
    srv_lines = []
    for s, mip, mport, replicas in mh_groups:
        srv_lines.append('\t\t{address="%s",port=%d,hostgroup=10,max_connections=200},' % (mip, mport))
        for rip, rport in replicas:
            srv_lines.append('\t\t{address="%s",port=%d,hostgroup=20,max_connections=200},' % (rip, rport))
    ver = "8.0.46" if any(s == "mysql8" for s, *_ in mh_groups) else "5.7.44"
    return """# 由打包器自动生成 (ProxySQL 读写分离, 重新部署时可覆盖)
datadir="/var/lib/proxysql"
errorlog="/var/lib/proxysql/proxysql.log"
admin_variables={
\tadmin_credentials="admin:admin;admin:admin123"
\tmysql_ifaces="0.0.0.0:6032"
}
mysql_variables={
\tthreads=4
\tmax_connections=2048
\tinterfaces="0.0.0.0:6033"
\tdefault_schema="information_schema"
\tstacksize=1048576
\tserver_version="%s"
\tconnect_timeout_server=3000
\tmonitor_username="root"
\tmonitor_password="%s"
\tmonitor_connect_interval=2000
\tmonitor_ping_interval=2000
\tmonitor_read_only_interval=1500
\tmysql_servers=(
%s
\t)
\tmysql_replication_hostgroups=(
\t\t{writer_hostgroup=10,reader_hostgroup=20,check_type="read_only"}
\t)
\tmysql_users=(
\t\t{username="root",password="%s",default_hostgroup=10,active=1}
\t)
\tmysql_query_rules=(
\t\t{rule_id=100,active=1,match_pattern="^\\\\s*SELECT .* FOR UPDATE",destination_hostgroup=10,apply=1},
\t\t{rule_id=200,active=1,match_pattern="^\\\\s*SELECT\\\\b",destination_hostgroup=20,apply=1},
\t\t{rule_id=1000,active=1,match_pattern=".*",destination_hostgroup=10,apply=1}
\t)
}
""" % (ver, pw, "\n".join(srv_lines), pw)


def _node_backup_files(cfg):
    """MySQL 主库节点的定时备份配置 (backup.sh 需要: BACKUP_DIR/MYSQL_ENABLED/MYSQL_SERVICES/MYSQL_KEEP/MYSQL_ROOT_PASSWORD)"""
    b = cfg.get("backup") or {}
    eng = ((b.get("engines") or {}).get("mysql") or {})
    if not eng.get("enabled"):
        return None
    dow = ",".join(sorted(str(0 if d == 7 else d) for d in (eng.get("days") or [])))
    return {
        "BACKUP_DIR": bash_quote(b.get("dir") or "/data/backup/db"),
        "MYSQL_ENABLED": "1",
        "MYSQL_SERVICES": '("mysql")',
        "MYSQL_KEEP": str(int(eng.get("keep", 7))),
        "MYSQL_ROOT_PASSWORD": bash_quote(cfg["secrets"].get("MYSQL_ROOT_PASSWORD", "")),
        "CRON": "0 %d * * %s" % (int(eng.get("hour", 3)), dow),
    }


def gen_nodes(cfg, catalog):
    """多机部署: 按角色生成各节点安装计划(主节点优先, 保证从库安装时主已就绪)。
    返回 [{name, server, roles, compose, env, images, conf_files, post_sql,
           health_wait, data_dirs, chown_dirs, port_checks, backup}]"""
    nodes = []
    if not has_multihost(cfg):
        return nodes
    ports = cfg["ports"]
    servers = cfg["servers"]
    acc = {}   # idx -> 计划字典

    def acc_of(idx):
        if idx not in acc:
            acc[idx] = {
                "name": servers[idx]["name"], "server": servers[idx], "roles": [],
                "compose_parts": [], "env_keys": set(), "images": [], "conf_files": {},
                "post_sql": [], "health_wait": [], "data_dirs": [], "chown_dirs": [],
                "port_checks": [], "backup": None,
            }
        return acc[idx]

    def add_image(plan, svc_or_key, rel, short):
        plan["images"].append(("%s-%s.tar" % (svc_or_key, cfg["arch"]), rel, short))

    # ---- Kafka 节点 ----
    if is_multihost(cfg, "kafka"):
        kc = PLUGINS["kafka"].CLUSTER
        for n, idx in enumerate(cfg["multihost"]["kafka"]["brokers"]):
            p = acc_of(idx)
            p["roles"].append("kafka#broker%d" % (n + 1))
            p["compose_parts"].append(_node_kafka_block(cfg, ports, idx, n))
            p["env_keys"].add("KAFKA_PASSWORD")
            p["images"].append(("kafka-%s.tar" % cfg["arch"], kc["images"][cfg["arch"]],
                                "%s:%s" % (kc["image"], kc["tag"])))
            p["health_wait"].append("kafka")
            p["data_dirs"].append("kafka/data")
            p["port_checks"] += [ports["kafka_mh_inter"], ports["kafka_mh_ctrl"], ports["kafka_mh_sasl"]]

    # ---- MySQL 节点 (主节点先于从库, 由 distribute 按计划顺序执行) ----
    for s in ("mysql8", "mysql57"):
        if not is_multihost(cfg, s):
            continue
        meta = catalog["services"][s]
        mh = cfg["multihost"][s]
        # 主库
        p = acc_of(mh["master"])
        p["roles"].append("%s#master" % s)
        p["compose_parts"].append(_node_mysql_block(cfg, catalog, s, "master"))
        p["env_keys"].add("MYSQL_ROOT_PASSWORD")
        p["images"].append(("%s-%s.tar" % (s, cfg["arch"]), meta["images"][cfg["arch"]],
                            "%s:%s" % (meta["image"], meta["tag"])))
        p["conf_files"]["conf/mysql/my.cnf"] = TPL_DIR / "mysql-my.cnf"
        p["health_wait"].append("mysql")
        p["data_dirs"] += ["mysql/data", "mysql/log"]
        p["chown_dirs"].append("mysql/log")
        p["port_checks"].append(ports[s])
        # 主库建 repl 账号 (幂等)
        pw = cfg["secrets"].get("MYSQL_ROOT_PASSWORD", "")
        p["post_sql"].append(("mysql",
            "CREATE USER IF NOT EXISTS 'repl'@'%%' IDENTIFIED BY '%s'; "
            "ALTER USER 'repl'@'%%' IDENTIFIED BY '%s'; "
            "GRANT REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO 'repl'@'%%'; FLUSH PRIVILEGES;" % (pw, pw)))
        p["backup"] = _node_backup_files(cfg)
        # 从库 (每台一个)
        for r, idx in enumerate(mh["replicas"]):
            rp = acc_of(idx)
            rp["roles"].append("%s#replica%d" % (s, r + 1))
            rp["compose_parts"].append(_node_mysql_block(cfg, catalog, s, "replica", r))
            rp["env_keys"].add("MYSQL_ROOT_PASSWORD")
            rp["images"].append(("%s-%s.tar" % (s, cfg["arch"]), meta["images"][cfg["arch"]],
                                 "%s:%s" % (meta["image"], meta["tag"])))
            rp["conf_files"]["conf/mysql/my.cnf"] = TPL_DIR / "mysql-my.cnf"
            rp["health_wait"].append("mysql")
            rp["data_dirs"] += ["mysql/data", "mysql/log"]
            rp["chown_dirs"].append("mysql/log")
            rp["port_checks"].append(ports["%s_replica" % s])
            master_ip = servers[mh["master"]]["ip"]
            mport = ports[s]
            if s == "mysql8":
                sql = ("CHANGE REPLICATION SOURCE TO SOURCE_HOST='%s', SOURCE_PORT=%d, SOURCE_USER='repl', "
                       "SOURCE_PASSWORD='%s', SOURCE_AUTO_POSITION=1, GET_MASTER_PUBLIC_KEY=1; START REPLICA;") % (master_ip, mport, pw)
            else:
                sql = ("CHANGE MASTER TO MASTER_HOST='%s', MASTER_PORT=%d, MASTER_USER='repl', "
                       "MASTER_PASSWORD='%s', MASTER_AUTO_POSITION=1; START SLAVE;") % (master_ip, mport, pw)
            rp["post_sql"].append(("mysql", sql))

    # ---- Redis 哨兵节点 ----
    if is_multihost(cfg, "redis"):
        meta = catalog["services"]["redis"]
        mh = cfg["multihost"]["redis"]
        master_ip = servers[mh["master"]]["ip"]
        mport = ports["redis"]
        # 主库机
        p = acc_of(mh["master"])
        p["roles"].append("redis#master")
        p["compose_parts"].append(_node_redis_blocks(cfg, catalog, "master", master_ip))
        p["env_keys"].add("REDIS_PASSWORD")
        p["images"].append(("redis-%s.tar" % cfg["arch"], meta["images"][cfg["arch"]],
                            "%s:%s" % (meta["image"], meta["tag"])))
        p["conf_files"]["conf/redis/redis.conf"] = TPL_DIR / "redis.conf"
        p["conf_files"]["conf/redis/sentinel.conf"] = _node_sentinel_conf(cfg, master_ip, mport, master_ip)
        p["health_wait"] += ["redis", "redis-sentinel"]
        p["data_dirs"] += ["redis/data", "redis/log"]
        p["chown_dirs"] += ["redis/log", "redis/sentinel.conf:999"]
        p["port_checks"] += [ports["redis"], ports.get("redis_sentinel", 26379)]
        # 从库机 (2 台)
        for r, idx in enumerate(mh["replicas"]):
            rp = acc_of(idx)
            rp["roles"].append("redis#replica%d" % (r + 1))
            rp["compose_parts"].append(_node_redis_blocks(cfg, catalog, "replica", master_ip))
            rp["env_keys"].add("REDIS_PASSWORD")
            rp["images"].append(("redis-%s.tar" % cfg["arch"], meta["images"][cfg["arch"]],
                                 "%s:%s" % (meta["image"], meta["tag"])))
            rp["conf_files"]["conf/redis/redis.conf"] = TPL_DIR / "redis.conf"
            rp["conf_files"]["conf/redis/sentinel.conf"] = _node_sentinel_conf(cfg, master_ip, mport,
                                                                               servers[idx]["ip"])
            rp["health_wait"] += ["redis", "redis-sentinel"]
            rp["data_dirs"] += ["redis/data", "redis/log"]
            rp["chown_dirs"] += ["redis/log", "redis/sentinel.conf:999"]
            rp["port_checks"] += [ports["redis_replica"], ports.get("redis_sentinel", 26379)]

    # ---- 组装 (主节点优先: mysql master > redis master > kafka > 其余) ----
    def role_rank(idx):
        r = acc[idx]["roles"]
        if any(x.endswith("#master") for x in r):
            return 0
        if any(x.startswith("kafka") for x in r):
            return 1
        return 2

    for idx in sorted(acc, key=role_rank):
        p = acc[idx]
        p["compose"] = ("# 由 packer.py 自动生成 (多机节点: %s)\nname: %s-%s\n\nservices:\n" % (
            p["name"], cfg["project"], p["name"])) + "\n".join(p["compose_parts"]) + \
            "\n\nnetworks:\n  app-network:\n    driver: bridge\n"
        env = ["# 由 packer.py 自动生成 (权限600, 节点 %s)" % p["name"], "TZ=Asia/Shanghai"]
        if "MYSQL_ROOT_PASSWORD" in p["env_keys"]:
            env.append("MYSQL_ROOT_PASSWORD=%s" % env_quote(cfg["secrets"]["MYSQL_ROOT_PASSWORD"]))
        if "REDIS_PASSWORD" in p["env_keys"]:
            env.append("REDIS_PASSWORD=%s" % env_quote(cfg["secrets"]["REDIS_PASSWORD"]))
        if "KAFKA_PASSWORD" in p["env_keys"]:
            env.append("KAFKA_PASSWORD=%s" % env_quote(cfg["secrets"]["KAFKA_PASSWORD"]))
        p["env"] = "\n".join(env) + "\n"
        p["summary"] = ["%s  %s@%s (SSH %d, %s)" % (p["name"], p["server"]["user"], p["server"]["ip"], p["server"]["ssh"],
                        "密码登录" if p["server"].get("password") else "免密登录")
                        + "  角色: " + ", ".join(p["roles"])]
        nodes.append(p)
    return nodes


def _node_sentinel_conf(cfg, master_ip, master_port, self_ip):
    """多机哨兵配置: monitor 指向主库节点 IP, announce 本机 IP 供对端/客户端回连"""
    port = cfg["ports"].get("redis_sentinel", 26379)
    pw = cfg["secrets"].get("REDIS_PASSWORD", "")
    return ("# 由打包器生成 (多机哨兵, 重新部署时自动覆盖)\n"
            "port 26379\n"
            "sentinel monitor mymaster %s %d 2\n"
            "sentinel auth-pass mymaster %s\n"
            "sentinel down-after-milliseconds mymaster 5000\n"
            "sentinel failover-timeout mymaster 60000\n"
            "sentinel parallel-syncs mymaster 1\n"
            "sentinel announce-ip %s\n"
            "sentinel announce-port %d\n" % (master_ip, master_port, pw, self_ip, port))


def gen_node_install_sh(cfg, node):
    """生成单节点安装脚本 (自包含: 配置桥接 -> load 镜像 -> compose up -> 健康等待 -> 初始化 SQL -> 备份 crontab)"""
    b = node.get("backup")
    backup_cron = b["CRON"] if b else ""
    backup_lines = ""
    if b:
        backup_lines = "\n".join([
            "BACKUP_DIR=%s" % b["BACKUP_DIR"],
            "MYSQL_ENABLED=%s" % b["MYSQL_ENABLED"],
            "MYSQL_SERVICES=%s" % b["MYSQL_SERVICES"],
            "MYSQL_KEEP=%s" % b["MYSQL_KEEP"],
            "MYSQL_ROOT_PASSWORD=%s" % b["MYSQL_ROOT_PASSWORD"],
        ])
    post_sql = " ".join(bash_quote("%s|%s" % (cn, sql)) for cn, sql in node["post_sql"])
    return """#!/usr/bin/env bash
# 由 packer.py 自动生成 — 节点 %(name)s 安装脚本 (自包含, 可重复执行)
set -euo pipefail
cd "$(dirname "$0")"
NODE_NAME=%(name)s
HEALTH_WAIT=(%(health_wait)s)
POST_SQL=(%(post_sql)s)
BACKUP_CRON=%(backup_cron)s

log(){ printf '[node-%%s] %%s\\n' "$NODE_NAME" "$*"; }
die(){ printf '[node-%%s] 错误: %%s\\n' "$NODE_NAME" "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "本机未安装 Docker, 请先安装 Docker 再执行"
docker compose version >/dev/null 2>&1 || docker-compose version >/dev/null 2>&1 \\
  || die "缺少 Docker Compose, 请先安装"

set -a; source ./.env; set +a

# 桥接配置目录: 包内 conf/<svc>/* -> 部署目录 <svc>/* (与主部署机 deploy.sh 同规则)
if [ -d ./conf ]; then
  for d in ./conf/*/; do
    [ -d "$d" ] || continue
    name="$(basename "$d")"
    mkdir -p "./$name"
    cp -a "$d/." "./$name/"
  done
fi

# 预建数据/日志目录并收紧属主 (compose 挂载点; 权限不匹配会导致容器写不进)
for d in %(data_dirs)s; do
  [ -z "$d" ] && continue
  mkdir -p "./$d"
done
for d in %(chown_dirs)s; do
  [ -z "$d" ] && continue
  case "$d" in
    *:*) dir="${d%%%%:*}"; uid="${d##*:}" ;;
    *)   dir="$d"; uid="999" ;;
  esac
  [ -e "./$dir" ] && chown -R "$uid:$uid" "./$dir" 2>/dev/null \\
    || log "  提示: chown $uid:$uid $dir 未生效(可忽略, 视节点用户而定)"
done

log "加载节点镜像..."
for tar in images/*.tar; do
  [ -f "$tar" ] || { log "无镜像文件"; break; }
  docker load -i "$tar" >/dev/null && log "  $(basename "$tar") 已加载"
done
# 镜像源前缀重打标(离线机 compose 按原生短名取镜像, 不重打标会触发联网拉取失败)
if [ -f images/retag-mirrors.sh ]; then
  bash images/retag-mirrors.sh images.txt
fi

log "启动容器..."
if docker compose version >/dev/null 2>&1; then
  docker compose up -d
else
  docker-compose up -d
fi

log "等待容器健康 (最长 10 分钟)..."
for cn in "${HEALTH_WAIT[@]}"; do
  for i in $(seq 1 120); do
    st="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cn" 2>/dev/null || echo missing)"
    case "$st" in healthy|running) break;; esac
    [ "$i" = 120 ] && die "$cn 健康检查超时 (当前状态: $st)"
    sleep 5
  done
  log "  $cn 就绪 ($st)"
done

if [ "${#POST_SQL[@]}" -gt 0 ] && [ -n "${POST_SQL[0]:-}" ]; then
  log "执行初始化 SQL..."
  for entry in "${POST_SQL[@]}"; do
    cn="${entry%%|*}"; sql="${entry#*|}"
    docker exec -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$cn" mysql -uroot -e "$sql" \\
      || die "初始化 SQL 执行失败: ${sql:0:60}..."
    log "  $cn SQL 完成"
  done
fi

if [ -n "$BACKUP_CRON" ] && [ -f backup.sh ] && [ -f backup.conf ]; then
  log "配置定时备份: $BACKUP_CRON"
  chmod +x backup.sh && chmod 600 backup.conf
  INST_DIR="$(pwd)"
  CRON_LINE="$BACKUP_CRON /bin/bash $INST_DIR/backup.sh >> $INST_DIR/backup.log 2>&1"
  (crontab -l 2>/dev/null | grep -vF "$INST_DIR/backup.sh" ; echo "$CRON_LINE") | crontab -
  mkdir -p "$BACKUP_DIR"
fi

log "节点 $NODE_NAME 安装完成"
docker compose ps --format 'table {{.Name}}\\t{{.Status}}' 2>/dev/null || docker ps
""" % {
        "name": bash_quote(node["name"]),
        "health_wait": " ".join(bash_quote(n) for n in node["health_wait"]),
        "data_dirs": " ".join(bash_quote(d) for d in node["data_dirs"]),
        "chown_dirs": " ".join(bash_quote(d) for d in node["chown_dirs"]),
        "post_sql": post_sql or '""',
        "backup_cron": bash_quote(backup_cron),
        "backup_lines": backup_lines,
    }


def gen_distribute_sh(cfg, nodes, catalog):
    """生成主部署机分发脚本: 预检 -> (可选装 compose) -> tar 管道传输 -> 远程安装"""
    arch = cfg["arch"]
    pkg_sub = "x86_64" if arch == "amd64" else "aarch64"
    nodes_str = " ".join(bash_quote("%s|%s|%s|%d|%s" % (n["name"], n["server"]["user"], n["server"]["ip"],
                                                        n["server"]["ssh"], n["server"].get("password") or ""))
                         for n in nodes)
    return """#!/usr/bin/env bash
# 由 packer.py 自动生成 — 多机节点分发安装 (在主部署机上执行)
# 用法: ./distribute.sh            分发全部节点 (按主节点优先顺序)
#       ./distribute.sh node2 ...  只分发指定节点 (重装/补发)
# 认证: 服务器池填写了密码 -> sshpass 密码登录 (需本机安装 sshpass);
#       留空 -> SSH 免密 (BatchMode, 需先 ssh-copy-id)
set -uo pipefail
cd "$(dirname "$0")"
NODES=(%(nodes)s)
DEPLOY_DIR=%(deploy_dir)s
COMPOSE_BIN="packages/%(pkg_sub)s/docker-compose-linux-%(pkg_sub)s"
FAILED=()

log(){ printf '\\033[32m[distribute]\\033[0m %%s\\n' "$*"; }
warn(){ printf '\\033[33m[distribute]\\033[0m %%s\\n' "$*"; }

[ ${#NODES[@]} -gt 0 ] || { echo "无多机节点, 无需分发"; exit 0; }

ONLY=("$@")
for NODE in "${NODES[@]}"; do
  IFS='|' read -r NAME RUSER RIP RSSH RPASS <<< "$NODE"
  if [ ${#ONLY[@]} -gt 0 ]; then
    keep=0; for o in "${ONLY[@]}"; do [ "$o" = "$NAME" ] && keep=1; done
    [ $keep -eq 0 ] && continue
  fi
  if [ -n "$RPASS" ] && command -v sshpass >/dev/null 2>&1; then
    export SSHPASS="$RPASS"
    SSHC=(sshpass -e ssh -p "$RSSH" -o StrictHostKeyChecking=no)
    SCPC=(sshpass -e scp -P "$RSSH" -o StrictHostKeyChecking=no -q)
    log "[$NAME] 使用密码登录 (sshpass)"
  else
    [ -n "$RPASS" ] && warn "[$NAME] 已填写密码但本机未安装 sshpass, 回退免密模式 (可 apt/yum install sshpass)"
    SSHC=(ssh -p "$RSSH" -o BatchMode=yes)
    SCPC=(scp -P "$RSSH" -q)
  fi
  log "[$NAME] 预检 $RUSER@$RIP:$RSSH ..."
  if ! "${SSHC[@]}" -o ConnectTimeout=8 "$RUSER@$RIP" \\
      "command -v docker >/dev/null 2>&1" 2>/dev/null; then
    warn "[$NAME] SSH 连接失败或未安装 Docker, 已跳过 (密码模式请确认 sshpass 已安装; 免密模式请先 ssh-copy-id)"
    FAILED+=("$NAME"); continue
  fi
  if ! "${SSHC[@]}" "$RUSER@$RIP" \\
      "docker compose version >/dev/null 2>&1 || docker-compose version >/dev/null 2>&1" 2>/dev/null; then
    log "[$NAME] 节点缺少 compose, 从包内安装二进制..."
    if "${SCPC[@]}" "$COMPOSE_BIN" "$RUSER@$RIP:/tmp/mw-compose" \\
        && "${SSHC[@]}" "$RUSER@$RIP" "mkdir -p ~/.local/bin && mv /tmp/mw-compose ~/.local/bin/docker-compose && chmod +x ~/.local/bin/docker-compose"; then
      "${SSHC[@]}" "$RUSER@$RIP" "export PATH=\\$HOME/.local/bin:\\$PATH; docker-compose version >/dev/null 2>&1" \\
        || warn "[$NAME] compose 二进制已放置于 ~/.local/bin, 若 PATH 未包含请手动处理"
    else
      warn "[$NAME] compose 传输失败, 继续尝试安装"
    fi
  fi
  log "[$NAME] 传输节点包 -> $DEPLOY_DIR ..."
  "${SSHC[@]}" "$RUSER@$RIP" "mkdir -p '$DEPLOY_DIR'" || { FAILED+=("$NAME"); continue; }
  tar czf - -C "nodes/$NAME" . | "${SSHC[@]}" "$RUSER@$RIP" "tar xzf - -C '$DEPLOY_DIR'" \\
    || { warn "[$NAME] 传输失败"; FAILED+=("$NAME"); continue; }
  log "[$NAME] 执行节点安装..."
  if "${SSHC[@]}" "$RUSER@$RIP" "cd '$DEPLOY_DIR' && PATH=\\$HOME/.local/bin:\\$PATH bash install-node.sh"; then
    log "[$NAME] 完成"
  else
    warn "[$NAME] 安装脚本执行失败, 请登录节点查看日志"
    FAILED+=("$NAME")
  fi
done

echo ""
if [ ${#FAILED[@]} -gt 0 ]; then
  warn "以下节点未成功安装: ${FAILED[*]}"
  warn "修复后可重跑: ./distribute.sh ${FAILED[*]}"
  exit 1
fi
log "全部节点安装完成"
""" % {
        "nodes": nodes_str,
        "deploy_dir": bash_quote(cfg["deploy_dir"]),
        "pkg_sub": pkg_sub,
    }


# ---------------------------------------------------------------- nginx 反代配置生成

_PX_PROXY_HEADERS = """        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;"""


def px_ssl_rel(i, site):
    """站点证书在 nginx/ssl 下的相对目录(容器内 /etc/nginx/ssl/<rel>)"""
    name = re.sub(r"[^A-Za-z0-9.-]", "_", site["server_name"].split()[0])
    return "wizard/%d_%s" % (i + 1, name)


def _px_real_ip_lines(s):
    if not s["trusted_proxies"]:
        return []
    out = ["    # 前面还有 CDN/负载均衡: 从 X-Forwarded-For 还原真实客户端 IP($remote_addr)"]
    out += ["    set_real_ip_from %s;" % c for c in s["trusted_proxies"]]
    out += ["    real_ip_header X-Forwarded-For;", "    real_ip_recursive on;"]
    return out


def _px_locations(s, up):
    out = []
    if s["api_prefix"] and s["api_prefix"] != "/":
        out.append("    location %s {" % s["api_prefix"])
        out.append("        proxy_pass http://%s%s;" % (up, "/" if s["strip"] else ""))
        out.append("        proxy_http_version 1.1;")
        out.append(_PX_PROXY_HEADERS)
        out.append('        proxy_set_header Connection        "";   # 复用 upstream 长连接')
        out += ["        proxy_connect_timeout 5s;",
                "        proxy_send_timeout   60s;",
                "        proxy_read_timeout   60s;",
                "        proxy_buffer_size    16k;",
                "        proxy_buffers        8 32k;",
                "    }"]
    if s["ws"]:
        out.append("    location %s {" % s["ws_path"])
        out.append("        proxy_pass http://%s;" % up)
        out.append("        proxy_http_version 1.1;")
        out.append(_PX_PROXY_HEADERS)
        out.append("        proxy_set_header Upgrade    $http_upgrade;")
        out.append("        proxy_set_header Connection $connection_upgrade;  # nginx.conf 内置 map, 兼容普通请求")
        out += ["        proxy_read_timeout 3600s;   # 长连接不被空闲切断",
                "        proxy_send_timeout 3600s;",
                "    }"]
    if s["mode"] == "proxy":
        out.append("    location / {")
        out.append("        proxy_pass http://%s;" % up)
        out.append("        proxy_http_version 1.1;")
        out.append(_PX_PROXY_HEADERS)
        out.append('        proxy_set_header Connection        "";')
        out += ["        proxy_connect_timeout 5s;",
                "        proxy_send_timeout   60s;",
                "        proxy_read_timeout   60s;",
                "        proxy_buffer_size    16k;",
                "        proxy_buffers        8 32k;",
                "    }"]
    else:
        if s["root"]:
            out.append("    root %s;" % s["root"])
        out.append("    location / {")
        out.append("        index index.html index.htm;")
        out.append("        try_files $uri $uri/ %s;" % ("/index.html" if s["spa"] else "=404"))
        out.append("    }")
    return out


def _px_server_block(s, up, no, listen, ssl=False, ssl_rel=""):
    out = ["server {"]
    out.append("    listen       %s;" % ("443 ssl" if ssl else listen))
    out.append("    server_name  %s;" % s["server_name"])
    out.append("    client_max_body_size %s;" % s["body_size"])
    out += _px_real_ip_lines(s)
    if ssl:
        base = "/etc/nginx/ssl/%s" % ssl_rel
        out += [
            "    ssl_certificate     %s/fullchain.pem;" % base,
            "    ssl_certificate_key %s/privkey.pem;" % base,
            "    ssl_protocols       TLSv1.2 TLSv1.3;",
            "    ssl_ciphers         HIGH:!aNULL:!MD5;",
            "    ssl_session_cache   shared:SSL_%d:10m;" % no,
            "    ssl_session_timeout 1d;",
        ]
    out += _px_locations(s, up)
    out.append("}")
    return out


def gen_nginx_confs(cfg, catalog):
    """反代向导: 生成 conf.d/default.conf 内容(默认站点 + 全部站点)。
    未配置站点时返回 None, 沿用模板 default.conf。"""
    sites = cfg.get("proxies") or []
    if not sites:
        return None

    out = [
        "# 由打包器生成 — 站点/反代规则请回到本地打包器修改后重新打包, 手工修改会在重新部署时被覆盖",
        "",
    ]
    # 默认站点(直接访问 IP 时展示欢迎页); 若某站点已用 _ 占位默认名则跳过, 避免 server_name 冲突
    if not any(s["server_name"].strip() == "_" for s in sites):
        out += [
            "# ---- 默认站点: 直接访问 IP 时展示欢迎页 ----",
            "server {",
            "    listen 80 default_server;",
            "    server_name _;",
            "    root   /usr/share/nginx/html;",
            "    index  index.html index.htm;",
            "    location / { try_files $uri $uri/ =404; }",
            "}",
        ]

    for i, s in enumerate(sites):
        no = i + 1
        up = "px_%d" % no
        out.append("")
        mode_txt = "整站反代" if s["mode"] == "proxy" else "静态+接口"
        ssl_txt = ", HTTPS" if s["ssl"] else ""
        out.append("# ---- 站点 %d: %s (listen %d, %s%s) ----" % (no, s["server_name"], s["listen"], mode_txt, ssl_txt))
        if s["api_prefix"]:
            out.append("upstream %s {" % up)
            out.append("    server %s:%d;" % (s["target_host"], s["target_port"]))
            out.append("    keepalive 32;          # upstream 长连接池, 配合 proxy_set_header Connection \"\" 复用")
            out.append("}")
        if s["ssl"]:
            rel = px_ssl_rel(i, s)
            out += _px_server_block(s, up, no, listen=443, ssl=True, ssl_rel=rel)
            if s["listen"] != 443:
                if s["redirect"]:
                    out += ["# HTTP 全部 301 跳转到 HTTPS",
                            "server {",
                            "    listen       %d;" % s["listen"],
                            "    server_name  %s;" % s["server_name"],
                            "    return 301 https://$host$request_uri;",
                            "}"]
                else:
                    out += _px_server_block(s, up, no, listen=s["listen"], ssl=False)
        else:
            out += _px_server_block(s, up, no, listen=s["listen"], ssl=False)

    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- compose 静态校验(本机有 docker 才执行)

def validate_compose_with_docker(compose_text, env_text):
    # 纯多机部署时主部署机可能没有任何服务(compose 仅骨架), 跳过校验
    body = compose_text.split("services:", 1)
    if len(body) == 2 and not re.search(r"^  \S", body[1].split("\nnetworks:", 1)[0], re.M):
        return []
    docker = shutil.which("docker")
    if not docker:
        return ["本机未安装 docker, 跳过 docker compose config 校验(服务器端 deploy.sh 启动前也会校验)"]
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        for name, text in (("docker-compose.yml", compose_text), (".env", env_text)):
            with open(td / name, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        try:
            r = subprocess.run([docker, "compose", "config", "-q"], cwd=str(td),
                               capture_output=True, text=True, timeout=120,
                               env=dict(os.environ, LANG="C"))
        except Exception as e:
            return ["docker compose config 执行失败, 跳过校验: %s" % e]
        if r.returncode != 0:
            raise PackError("生成的 docker-compose.yml 校验失败:\n%s" % (r.stderr or r.stdout))
    return []


# ---------------------------------------------------------------- 打包进度

class PackProgress:
    """线程安全的打包进度(供 Web 轮询 / CLI 日志); 内部状态用 _ 前缀, 避免与方法重名"""

    def __init__(self):
        self._lock = threading.Lock()
        self._total = 0
        self._done = 0
        self._stage = "准备"
        self._current = ""
        self._cancelled = False
        self._running = False
        self._result = None
        self._error = None
        self._t0 = None
        self._t1 = None
        self._downloads = {}   # 组件下载进度: {显示名: [done, total]}(打包时自动补料展示)

    @property
    def running(self):
        return self._running

    def begin(self, total):
        with self._lock:
            self._total = total; self._done = 0
            self._running = True; self._cancelled = False
            self._result = None; self._error = None
            self._t0 = time.time(); self._t1 = None

    def stage(self, s):
        with self._lock:
            self._stage = s

    def file(self, name, size):
        with self._lock:
            self._current = name; self._stage = "压缩打包"

    def add(self, n):
        with self._lock:
            self._done += n

    def set_total(self, total):
        with self._lock:
            self._total = total

    def cancel(self):
        with self._lock:
            self._cancelled = True

    def check(self):
        with self._lock:
            if self._cancelled:
                raise PackError("打包已取消")

    def finish(self, result):
        with self._lock:
            self._running = False; self._result = result; self._t1 = time.time()

    def fail(self, err):
        with self._lock:
            self._running = False; self._error = str(err); self._t1 = time.time()

    def snapshot(self):
        with self._lock:
            elapsed = (self._t1 or time.time()) - self._t0 if self._t0 else 0
            speed = self._done / elapsed if elapsed > 0.5 else 0
            return {
                "running": self._running,
                "stage": self._stage,
                "current": self._current,
                "total": self._total,
                "done": self._done,
                "percent": round(self._done * 100 / self._total) if self._total else 0,
                "speed": round(speed / 1048576, 1),
                "elapsed": round(elapsed),
                "eta": round((self._total - self._done) / speed) if speed > 1048576 else 0,
                "result": self._result,
                "downloads": [{"name": k, "done": v[0], "total": v[1]} for k, v in self._downloads.items()],
                "error": self._error,
            }


class LogProgress(PackProgress):
    """CLI 模式: 阶段/大文件变化时打日志"""

    def stage(self, s):
        super().stage(s)
        log.info("[进度] %s", s)

    def file(self, name, size):
        super().file(name, size)
        log.info("打包 %s (%d MB)", name, size // 1048576)


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
        big_files += [
            ("%s/packages/%s/docker-29.8.0.tgz" % (bundle_name, pkg_sub),
             BASE_DIR / catalog["docker"]["packages"][arch]),
            ("%s/packages/%s/docker-compose-linux-%s" % (bundle_name, pkg_sub, pkg_sub),
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

    # ProxySQL 读写分离配置(可选组件)
    if (cfg.get("features") or {}).get("proxysql"):
        conf_files["conf/proxysql/proxysql.cnf"] = gen_proxysql_conf(cfg)

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


# ---------------------------------------------------------------- CLI

EXAMPLE_CONFIG = {
    "project": "demo",
    "arch": "amd64",
    "services": ["nginx", "mysql8", "redis", "xxljob"],
    "ports": {"nginx_http": 80, "nginx_https": 443, "nginx_extra": 9000,
              "mysql8": 13307, "redis": 16379, "xxljob": 18081},
    "secrets": {
        "MYSQL_ROOT_PASSWORD": "Change_Me_123",
        "XXL_JOB_ADMIN_PASSWORD": "Change_Me_123",
        "XXL_JOB_ACCESS_TOKEN": "Change_Me_123",
        "REDIS_PASSWORD": "Change_Me_123",
    },
    "deploy_dir": "/data/middleware",
    "docker_data_root": "/data/docker",
    "registry_mirrors": [],
    "extra_ports": {},
    "backup": {"enabled": True, "days": [1, 2, 3, 4, 5, 6, 7], "hour": 3, "keep": 7, "dir": "/data/backup/db"},
    "db": {
        "nacos": None,
        "xxljob": {"mode": "local", "schema": "xxl_job"},
    },
}


def main():
    parser = argparse.ArgumentParser(description="中间件离线包本地打包器|Local offline bundle packer")
    parser.add_argument("--config", help="按配置文件打包(JSON), 不启动 Web 界面|Pack from a JSON config file (no Web UI)")
    parser.add_argument("--out", default=None, help="打包输出目录, 默认 dist/|Output directory, default dist/")
    parser.add_argument("--example", action="store_true", help="生成配置文件样例 example-config.json|Write sample config example-config.json")
    parser.add_argument("--port", type=int, default=8765, help="Web 界面端口, 默认 8765|Web UI port, default 8765")
    parser.add_argument("--no-browser", action="store_true", help="启动 Web 时不自动打开浏览器|Do not open the browser automatically")
    parser.add_argument("--lang", default="zh", choices=("zh", "en"), help="CLI 消息语言|CLI message language")
    parser.add_argument("--log-file", default=None, help="日志文件路径|Log file path")
    args = parser.parse_args()

    handlers = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=handlers)

    try:
        if args.example:
            Path("example-config.json").write_text(
                json.dumps(EXAMPLE_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
            log.info("已生成 example-config.json, 编辑后执行: python packer.py --config example-config.json|"
                     "example-config.json written; edit it then run: python packer.py --config example-config.json")
            return 0
        if args.config:
            cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
            catalog = load_catalog()
            result = pack(cfg, catalog, out_dir=args.out, progress=LogProgress())
            for w in result["warnings"]:
                log.warning("%s", tr(w, args.lang))
            log.info("产物: %s (%s MB)|Output: %s (%s MB)", result["path"], result["size_mb"], result["path"], result["size_mb"])
            return 0
        run_web(args.port, no_browser=args.no_browser)
        return 0
    except PackError as e:
        log.error("%s", tr(str(e), args.lang))
        return 1
    except Exception:
        log.exception("失败")
        return 1


if __name__ == "__main__":
    sys.exit(main())

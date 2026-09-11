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


def env_quote(v):
    """生成 .env 值: 含 # 时加双引号(值字符集已禁止双引号/反斜杠/$)"""
    v = str(v)
    if "#" in v or " " in v:
        return '"%s"' % v
    return v


PASSWORD_RE = re.compile(r"^[A-Za-z0-9@#%*+=_.:/~-]+$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
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
        raise PackError("项目名不合法: 只能小写字母开头, 小写字母/数字/中划线, 2-41 位")
    arch = cfg.get("arch")
    if arch not in ("amd64", "arm64"):
        raise PackError("必须选择目标架构 (amd64 / arm64)")

    services = cfg.get("services") or []
    if not services:
        raise PackError("至少选择一个中间件")
    known = list(catalog["services"].keys())
    for s in services:
        if s not in known:
            raise PackError("未知中间件: %s" % s)
        meta = catalog["services"][s]
        if arch not in meta["supported_arch"]:
            raise PackError("%s 不支持 %s 架构 (%s)" % (meta["label"], arch, meta.get("note", "")))

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
                raise PackError("端口 %s 不合法: %s" % (p["label"], raw))
            if not (1 <= v <= 65535):
                raise PackError("端口 %s 超出范围 1-65535: %s" % (p["label"], v))
            if v in seen:
                raise PackError("端口冲突: %s 和 %s 都用了 %d" % (seen[v], p["label"], v))
            seen[v] = p["label"]
            norm_ports[key] = v
    cfg["ports"] = norm_ports

    # ---- 部署形态(可选): mysql8 主从 / redis 哨兵 ----
    topo_raw = cfg.get("topology") or {}
    topology = {}
    if "mysql8" in services:
        m = str(topo_raw.get("mysql8", "single"))
        if m not in ("single", "master-slave"):
            raise PackError("MySQL 部署形态不合法: %s (single / master-slave)" % m)
        topology["mysql8"] = m
    if "mysql57" in services:
        m = str(topo_raw.get("mysql57", "single"))
        if m not in ("single", "master-slave"):
            raise PackError("MySQL 5.7 部署形态不合法: %s (single / master-slave)" % m)
        topology["mysql57"] = m
    if "redis" in services:
        r = str(topo_raw.get("redis", "single"))
        if r not in ("single", "sentinel"):
            raise PackError("Redis 部署形态不合法: %s (single / sentinel)" % r)
        topology["redis"] = r
    cfg["topology"] = topology

    # 集群形态附加端口键(并入全局查重)
    topo_port_defs = []
    if topology.get("mysql8") == "master-slave":
        topo_port_defs.append(("mysql8_replica", "MySQL 从库端口", 13308))
    if topology.get("mysql57") == "master-slave":
        topo_port_defs.append(("mysql57_replica", "MySQL 5.7 从库端口", 13309))
    if topology.get("redis") == "sentinel":
        topo_port_defs.append(("redis_replica", "Redis 从库端口", 16380))
    seen_topo = set(norm_ports.values())
    for key, label, default in topo_port_defs:
        raw = (cfg.get("ports") or {}).get(key, default)
        try:
            v = int(raw)
        except (TypeError, ValueError):
            raise PackError("端口 %s 不合法: %s" % (label, raw))
        if not (1 <= v <= 65535):
            raise PackError("端口 %s 超出范围 1-65535: %d" % (label, v))
        if v in seen_topo:
            raise PackError("端口冲突: %s 与其他端口都用了 %d" % (label, v))
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
                raise PackError("Nacos Auth Token 必须以 SecretKey 开头")
            b64part = val[len("SecretKey"):]
            try:
                raw = base64.b64decode(b64part, validate=True)
            except Exception:
                raise PackError("Nacos Auth Token 的 SecretKey 后面必须是合法 Base64")
            if len(raw) < 32:
                raise PackError("Nacos Auth Token 解码后长度必须 >= 32 字节")
        elif item.get("secret", True):
            if not PASSWORD_RE.match(val):
                raise PackError("%s 含不合法字符(禁止 空格 和 $ ` \" ' \\ ; | 字符): %s" % (item["label"], key))
            if len(val) < item.get("min_len", 6):
                raise PackError("%s 长度至少 %d 位" % (item["label"], item.get("min_len", 6)))
        else:
            if not USERNAME_RE.match(val):
                raise PackError("%s 含不合法字符: %s" % (item["label"], key))
        secrets_cfg[key] = val
    cfg["secrets"] = secrets_cfg

    # ---- 目录/镜像源 ----
    deploy_dir = str(cfg.get("deploy_dir") or catalog["defaults"]["deploy_dir"]).strip()
    data_root = str(cfg.get("docker_data_root") or catalog["defaults"]["docker_data_root"]).strip()
    if not LINUX_PATH_RE.match(deploy_dir) or deploy_dir == "/":
        raise PackError("部署目录必须是 Linux 绝对路径, 如 /data/middleware")
    if not LINUX_PATH_RE.match(data_root) or data_root == "/":
        raise PackError("Docker 数据目录必须是 Linux 绝对路径, 如 /data/docker")
    cfg["deploy_dir"] = deploy_dir.rstrip("/")
    cfg["docker_data_root"] = data_root.rstrip("/")

    mirrors = cfg.get("registry_mirrors") or []
    norm_mirrors = []
    for m in mirrors:
        m = str(m).strip()
        if not m:
            continue
        if not MIRROR_RE.match(m):
            raise PackError("镜像加速器地址不合法: %s" % m)
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
                raise PackError("%s 自定义端口必须是数字: %s" % (svc, r))
            if not (1 <= h <= 65535 and 1 <= c <= 65535):
                raise PackError("%s 自定义端口超出范围 1-65535: %d:%d" % (svc, h, c))
            if h in used_hosts:
                raise PackError("%s 自定义宿主机端口 %d 与其他端口冲突" % (svc, h))
            used_hosts.add(h)
            if c in seen_ctn:
                raise PackError("%s 同一容器端口重复映射: %d" % (svc, c))
            seen_ctn.add(c)
            norm_rows.append({"host": h, "container": c})
        norm_extra[svc] = norm_rows
    cfg["extra_ports"] = norm_extra

    # ---- 数据库备份策略(部署了 MySQL 才生效) ----
    b = cfg.get("backup") or {}
    enabled = bool(b.get("enabled"))
    if enabled and not ({"mysql57", "mysql8"} & set(services)):
        enabled = False
        warns.append("本次未部署 MySQL, 备份策略配置已忽略")
    if enabled:
        try:
            days = sorted({int(d) for d in (b.get("days") or [])})
        except (TypeError, ValueError):
            raise PackError("备份星期配置不合法")
        if not days or [d for d in days if not (1 <= d <= 7)]:
            raise PackError("请至少选择一个有效备份日 (1=周一 ... 7=周日)")
        try:
            hour, keep = int(b.get("hour", 3)), int(b.get("keep", 7))
        except (TypeError, ValueError):
            raise PackError("备份时间/保留份数不合法")
        if not 0 <= hour <= 23:
            raise PackError("备份小时必须 0-23")
        if not 1 <= keep <= 999:
            raise PackError("备份保留份数必须 1-999")
        bdir = str(b.get("dir") or "/data/backup/mysql").strip()
        if not LINUX_PATH_RE.match(bdir) or bdir == "/":
            raise PackError("备份目录必须是 Linux 绝对路径, 如 /data/backup/mysql")
        b = {"enabled": True, "days": days, "hour": hour, "keep": keep, "dir": bdir.rstrip("/")}
    else:
        b = {"enabled": False, "days": [], "hour": 3, "keep": 7, "dir": "/data/backup/mysql"}
    cfg["backup"] = b

    # ---- Nginx 反向代理向导(勾选 NGINX 才生效) ----
    proxies = cfg.get("proxies") or []
    if proxies and "nginx" not in services:
        raise PackError("配置了反向代理站点, 但中间件未勾选 NGINX")
    norm_px = []
    if proxies:
        # 站点监听端口必须是 nginx 容器内实际映射到的端口(80/443/9000 + 自定义映射的容器端口)
        ctn_ports = {80, 443, 9000}
        for r in (cfg.get("extra_ports") or {}).get("nginx", []):
            ctn_ports.add(int(r["container"]))
        if len(proxies) > 20:
            raise PackError("反代站点数量过多(最多 20 个)")
        seen_lsn = set()
        for i, p in enumerate(proxies):
            p = dict(p or {})
            no = i + 1
            mode = p.get("mode") or "static"
            if mode not in ("static", "proxy"):
                raise PackError("反代站点 %d 模式不合法: %s" % (no, mode))
            sn = str(p.get("server_name") or "").strip()
            if not sn or not re.match(r"^[A-Za-z0-9._\-*]+( +[A-Za-z0-9._\-*]+)*$", sn):
                raise PackError("反代站点 %d 域名不合法(多个用空格分隔, 通配用 *): %r" % (no, sn))
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
                raise PackError("反代站点 %d 上传大小限制不合法(如 100m): %s" % (no, body))
            ws = bool(p.get("ws"))
            ws_path = str(p.get("ws_path") or "/ws/").strip() or "/ws/"
            if ws and not re.match(r"^/[A-Za-z0-9_./-]*$", ws_path):
                raise PackError("反代站点 %d WebSocket 路径不合法: %s" % (no, ws_path))
            trusted = []
            for cidr in (p.get("trusted_proxies") or []):
                cidr = str(cidr).strip()
                if not cidr:
                    continue
                if not re.match(r"^(\d{1,3}\.){3}\d{1,3}/\d{1,2}$|^[A-Fa-f0-9:]+/\d{1,3}$", cidr):
                    raise PackError("反代站点 %d 可信代理网段不合法(应为 CIDR 如 10.0.0.0/8): %s" % (no, cidr))
                trusted.append(cidr)

            # HTTPS 证书(随包分发, 部署即配好 SSL)
            ssl_on = bool(p.get("ssl"))
            cert_pem = str(p.get("cert_pem") or "")
            key_pem = str(p.get("key_pem") or "")
            redirect = bool(p.get("redirect", True))
            if ssl_on:
                if "-----BEGIN CERTIFICATE-----" not in cert_pem:
                    raise PackError("反代站点 %d 启用了 HTTPS 但证书文件无效, 请重新选择 fullchain.pem/.crt" % no)
                if "ENCRYPTED" in key_pem:
                    raise PackError("反代站点 %d 私钥带密码保护, 暂不支持; 请先导出无密码私钥再上传" % no)
                if not re.search(r"-----BEGIN (RSA |EC |DSA )?PRIVATE KEY-----", key_pem):
                    raise PackError("反代站点 %d 启用了 HTTPS 但私钥文件无效, 请重新选择 .key 私钥" % no)
                if len(cert_pem) > 200_000 or len(key_pem) > 200_000:
                    raise PackError("反代站点 %d 证书/私钥文件过大(>200KB), 请核对是否选错文件" % no)
            else:
                cert_pem = key_pem = ""
                redirect = False

            api_prefix, root, spa, strip = "", "", False, False
            if mode == "static":
                root = str(p.get("root") or "").strip()
                if root and not LINUX_PATH_RE.match(root):
                    raise PackError("反代站点 %d 前端目录必须是容器内绝对路径: %s" % (no, root))
                spa = bool(p.get("spa"))
                api_prefix = str(p.get("api_prefix") or "").strip()
                if api_prefix and not re.match(r"^/[A-Za-z0-9_./-]*$", api_prefix):
                    raise PackError("反代站点 %d 接口前缀不合法: %s" % (no, api_prefix))
                if api_prefix and not api_prefix.endswith("/"):
                    api_prefix += "/"
            else:
                api_prefix = "/"

            target_host, target_port = "", 0
            if api_prefix:
                target = str(p.get("api_target") or "").strip().rstrip("/")
                m = re.match(r"^http://([A-Za-z0-9_.-]+)(?::([0-9]{1,5}))?$", target)
                if not m:
                    raise PackError("反代站点 %d 后端地址不合法(仅支持 http://主机[:端口], 主机可为 compose 服务名): %s" % (no, target))
                target_host = m.group(1)
                target_port = int(m.group(2) or 80)
                if not (1 <= target_port <= 65535):
                    raise PackError("反代站点 %d 后端端口超出范围: %d" % (no, target_port))
                if target_host in ("127.0.0.1", "localhost", "::1"):
                    target_host = "host.docker.internal"
                    p["_gw"] = True
                    warns.append("反代站点 %d 后端填的是宿主机地址, 容器内已自动改用 host.docker.internal(host-gateway)" % no)
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
            raise PackError("%s 必须选择数据库来源(local/external)" % app)
        schema = str(conf.get("schema") or schema_default).strip()
        if not re.match(r"^[A-Za-z0-9_]+$", schema):
            raise PackError("%s 数据库名不合法: %s" % (app, schema))
        conf["schema"] = schema
        if mode == "local":
            if not local_mysqls:
                raise PackError("%s 选择使用本次部署的 MySQL, 但本次未部署 MySQL" % app)
            svc = conf.get("local_svc")
            if svc not in local_mysqls:
                svc = "mysql8" if "mysql8" in local_mysqls else local_mysqls[0]
            conf["local_svc"] = svc
            conf["host"] = svc            # compose 服务名
            conf["host_import"] = ""
            conf["port"] = 3306
            conf["user"] = "root"
            conf["password"] = secrets_cfg["MYSQL_ROOT_PASSWORD"]
        else:
            host = str(conf.get("host", "")).strip()
            if not host:
                raise PackError("%s 外部数据库地址不能为空" % app)
            try:
                port = int(conf.get("port", 3306))
            except (TypeError, ValueError):
                raise PackError("%s 外部数据库端口不合法" % app)
            if not (1 <= port <= 65535):
                raise PackError("%s 外部数据库端口超出范围" % app)
            user = str(conf.get("user", "")).strip()
            password = str(conf.get("password", ""))
            if not USERNAME_RE.match(user):
                raise PackError("%s 外部数据库账号不合法" % app)
            if not password:
                raise PackError("%s 外部数据库密码不能为空" % app)
            if not PASSWORD_RE.match(password):
                raise PackError("%s 外部数据库密码含不合法字符(禁止 空格 和 $ ` \" ' \\ ; |)" % app)
            conf["port"] = port
            conf["user"] = user
            conf["password"] = password
            # 127.0.0.1/localhost 指的是宿主机: compose 内换成 host-gateway 别名
            if host in ("127.0.0.1", "localhost", "::1"):
                conf["host"] = "host.docker.internal"
                conf["host_import"] = "127.0.0.1"
                conf["extra_hosts"] = True
                warns.append("%s 外部数据库填的是本机地址, 容器内已自动改用 host.docker.internal(host-gateway); "
                             "请确认该数据库监听的是 0.0.0.0 而非仅 127.0.0.1" % app)
            else:
                conf["host"] = host
                conf["host_import"] = host
                conf["extra_hosts"] = False
        db[app] = conf
    cfg["db"] = db

    # ---- 仓库文件存在性 ----
    missing = check_warehouse(cfg, catalog)
    if missing:
        raise PackError("离线包物料缺失, 请补齐后再打包:\n  - " + "\n  - ".join(missing))
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
    for s in cfg["services"]:
        meta = catalog["services"][s]
        need_imgs[meta["images"][arch]] = True
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
    needs_client = False
    for app in ("nacos", "xxljob"):
        conf = db.get(app)
        if conf and conf["mode"] == "external":
            needs_client = True
    client_img = ""
    if needs_client:
        if has_local_mysql:
            client_img = "mysql:8.0.46" if "mysql8" in services else "mysql:5.7.44"
        else:
            client_img = "mysql:8.0.46"   # 打包时会把该镜像一起带上, 仅作客户端使用
    return needs_client, client_img


def backup_cron(backup):
    """备份策略 -> crontab 时间字段。UI 1=周一...7=周日, cron 0/7=周日, 生成如 '0 3 * * 1,3,0'"""
    if not backup or not backup.get("enabled"):
        return ""
    dow = ",".join(sorted(str(0 if d == 7 else d) for d in backup["days"]))
    return "0 %d * * %s" % (int(backup["hour"]), dow)


def gen_backup_conf(cfg):
    """生成 backup.conf(由 backup.sh source), 部署在部署目录"""
    b = cfg["backup"]
    svcs = [s for s in ("mysql57", "mysql8") if s in cfg["services"]]
    return "\n".join([
        "# 由 packer.py 自动生成 — backup.sh 的配置",
        "MYSQL_SERVICES=(%s)" % " ".join(svcs),
        "MYSQL_ROOT_PASSWORD=%s" % bash_quote(cfg["secrets"].get("MYSQL_ROOT_PASSWORD", "")),
        "BACKUP_DIR=%s" % bash_quote(b["dir"]),
        "BACKUP_KEEP=%d" % int(b["keep"]),
    ]) + "\n"


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
        "extra_ports_lines": extra_ports_lines,
        "depends_on": depends_on,
        "extra_hosts": extra_hosts,
        "networks_tail": "    networks:\n      - app-network\n",
        "local_mysql": ("mysql8" if "mysql8" in services else "mysql57" if "mysql57" in services else ""),
    }


def gen_compose(cfg, catalog):
    services = cfg["services"]
    ports = cfg["ports"]
    db = cfg["db"]
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
        if conf and conf["mode"] == "local":
            return ["    depends_on:", "      %s:" % conf["local_svc"], "        condition: service_healthy"]
        return []

    # ---- MySQL ----
    topology = cfg.get("topology") or {}
    for s in ("mysql57", "mysql8"):
        if s not in services:
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
      start_period: 60s
    networks:
      - app-network""" % {"svc": s, "image": "%s:%s" % (meta["image"], meta["tag"]),
                          "port": ports[meta["ports"][0]["key"]], "dir": meta["data_dir"],
                          "extra_ports": extra_lines(s),
                          "repl_flags": "".join("      - %s\n" % f for f in repl_flags)})
        if s in ("mysql8", "mysql57") and topology.get(s) == "master-slave":
            # 从库: 只读, GTID 自动定位, 不挂 init 目录(数据由主库复制而来)
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
    command:
      - --log-error=/var/log/mysql/error.log
      - --character-set-server=utf8mb4
      - --collation-server=utf8mb4_unicode_ci
      - --default-authentication-plugin=mysql_native_password
      - --server-id=2
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
      start_period: 60s
    networks:
      - app-network""" % {"svc": "%s-replica" % s,
                          "image": "%s:%s" % (meta["image"], meta["tag"]),
                          "rport": ports["%s_replica" % s],
                          "dir": "%s-replica" % s})

    # ---- Redis ----
    if "redis" in services:
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
    if topology.get("redis") == "sentinel":
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

    # ---- 插件中间件(可插拔) ----
    for s in services:
        if s in PLUGINS:
            out.append(PLUGINS[s].compose_block(cfg, ports, ctx))

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


def gen_manifest_sh(cfg, catalog, client_img, summary_lines, bundle_name=None):
    ports = cfg["ports"]
    db = cfg["db"]
    services = cfg["services"]

    data_dirs = []
    chown_dirs = []
    container_names = []
    port_keys = []
    for s in services:
        meta = catalog["services"][s]
        if s in PLUGINS:
            container_names.append(PLUGINS[s].META.get("container_name", s))
        else:
            container_names.append(s if s != "xxljob" else "xxl-job")
        for p in meta["ports"]:
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
    if "mysql8" in services:
        local_mysql = "mysql8"
    elif "mysql57" in services:
        local_mysql = "mysql57"

    # 健康实测清单: 有 compose healthcheck 的容器等待 healthy, 其余 HTTP 实测
    health_wait, health_http = [], []
    for s in services:
        cn = PLUGINS[s].META.get("container_name", s) if s in PLUGINS else (s if s != "xxljob" else "xxl-job")
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

    # 集群形态附加的容器/目录/端口/健康清单
    topology = cfg.get("topology") or {}
    if topology.get("mysql8") == "master-slave":
        container_names.append("mysql8-replica")
        data_dirs += ["mysql8-replica/data", "mysql8-replica/log"]
        chown_dirs.append("mysql8-replica/log")
        port_keys.append(ports["mysql8_replica"])
        health_wait.append("mysql8-replica")
    if topology.get("mysql57") == "master-slave":
        container_names.append("mysql57-replica")
        data_dirs += ["mysql57-replica/data", "mysql57-replica/log"]
        chown_dirs.append("mysql57-replica/log")
        port_keys.append(ports["mysql57_replica"])
        health_wait.append("mysql57-replica")
    if topology.get("redis") == "sentinel":
        container_names.append("redis-replica")
        data_dirs += ["redis-replica/data", "redis-replica/log"]
        chown_dirs.append("redis-replica/log")
        port_keys.append(ports["redis_replica"])
        health_wait.append("redis-replica")
        # 哨兵会把故障转移结果重写回自己的配置文件, 文件属主必须是容器内 redis(999)
        # (部署布局是平铺的: conf/<svc>/* 会被 deploy.sh 放到 <DEPLOY_DIR>/<svc>/ 下)
        chown_dirs.append("redis/sentinel.conf:999")

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
        "MYSQL_ROOT_PASSWORD=%s" % bash_quote(cfg["secrets"].get("MYSQL_ROOT_PASSWORD", "")),
        "MYSQL_CLIENT_IMG=%s" % bash_quote(client_img),
        "HEALTH_WAIT=(%s)" % " ".join(bash_quote(n) for n in health_wait),
        "HEALTH_HTTP=(%s)" % " ".join(bash_quote(h) for h in health_http),
    ]
    lines += db_lines("nacos", "NACOS")
    lines += db_lines("xxljob", "XXL")
    b = cfg.get("backup") or {}
    lines += [
        "BACKUP_ENABLED=%d" % (1 if b.get("enabled") else 0),
        "BACKUP_CRON=%s" % bash_quote(backup_cron(b)),
        "BACKUP_DIR=%s" % bash_quote(b.get("dir") or "/data/backup/mysql"),
    ]
    for s in services:
        if s in PLUGINS and hasattr(PLUGINS[s], "manifest_lines"):
            lines += PLUGINS[s].manifest_lines(cfg, ports, plugin_ctx(cfg))
    lines.append("SUMMARY_LINES=(%s)" % " ".join(bash_quote(l) for l in summary_lines))
    return "\n".join(lines) + "\n"


def build_summary_lines(cfg, catalog):
    ports = cfg["ports"]
    lines = []
    if "nginx" in services_of(cfg):
        lines.append("NGINX        http://__IP__:%d   (配置目录 %s/nginx)" % (ports["nginx_http"], cfg["deploy_dir"]))
    if "mysql57" in services_of(cfg):
        lines.append("MySQL 5.7    __IP__:%d  mysql -h<ip> -P%d -uroot" % (ports["mysql57"], ports["mysql57"]))
        if (cfg.get("topology") or {}).get("mysql57") == "master-slave":
            lines.append("MySQL 5.7 从库 __IP__:%d  (只读, GTID 自动同步主库)" % ports["mysql57_replica"])
    if "mysql8" in services_of(cfg):
        lines.append("MySQL 8.0    __IP__:%d  mysql -h<ip> -P%d -uroot" % (ports["mysql8"], ports["mysql8"]))
        if (cfg.get("topology") or {}).get("mysql8") == "master-slave":
            lines.append("MySQL 8.0 从库 __IP__:%d  (只读, GTID 自动同步主库)" % ports["mysql8_replica"])
    if "redis" in services_of(cfg):
        lines.append("Redis        __IP__:%d" % ports["redis"])
        if (cfg.get("topology") or {}).get("redis") == "sentinel":
            lines.append("Redis 哨兵    主+从+3哨兵 (从库 __IP__:%d, 应用走 sentinel 协议 redis-sentinel:26379)" % ports["redis_replica"])
    if "nacos" in services_of(cfg):
        lines.append("Nacos 控制台  http://__IP__:%d/nacos  (账号见 .env)" % ports["nacos_console"])
    if "xxljob" in services_of(cfg):
        lines.append("XXL-Job      http://__IP__:%d/xxl-job-admin  (admin/见 .env)" % ports["xxljob"])
    if "minio" in services_of(cfg):
        lines.append("MinIO        API http://__IP__:%d  控制台 http://__IP__:%d" % (ports["minio_api"], ports["minio_console"]))
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
    return lines


def services_of(cfg):
    return cfg["services"]


def gen_images_txt(cfg, catalog):
    """返回 [(bundle内文件名, 仓库相对路径, 短名)]"""
    arch = cfg["arch"]
    items = []
    for s in cfg["services"]:
        meta = catalog["services"][s]
        rel = meta["images"][arch]
        items.append(("%s-%s.tar" % (s, arch), rel, "%s:%s" % (meta["image"], meta["tag"])))
    needs_client, client_img = resolve_db(cfg)
    has_local_mysql = any(s in cfg["services"] for s in ("mysql57", "mysql8"))
    if needs_client and not has_local_mysql:
        meta = catalog["services"]["mysql8"]
        rel = meta["images"][arch]
        items.append(("mysql8-%s.tar" % arch, rel, client_img))
    return items


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

    bundle_name = "%s-%s-%s-offline" % (project, "x86" if arch == "amd64" else "arm",
                                        datetime.now().strftime("%Y%m%d-%H%M"))
    out_dir = Path(out_dir) if out_dir else DIST_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = out_dir / (bundle_name + ".tar.gz")

    # 大文件清单(docker/compose 安装包 + 镜像), 先算总量供进度条使用
    pkg_sub = "x86_64" if arch == "amd64" else "aarch64"
    big_files = [
        ("%s/packages/%s/docker-29.8.0.tgz" % (bundle_name, pkg_sub),
         BASE_DIR / catalog["docker"]["packages"][arch]),
        ("%s/packages/%s/docker-compose-linux-%s" % (bundle_name, pkg_sub, pkg_sub),
         BASE_DIR / catalog["compose"]["packages"][arch]),
    ]
    for fname, rel, _ in gen_images_txt(cfg, catalog):
        big_files.append(("%s/images/%s" % (bundle_name, fname), BASE_DIR / rel))
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
        if (cfg.get("topology") or {}).get("redis") == "sentinel":
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

    # ---- 插件附带的配置文件(可选 conf_files 钩子) ----
    for s in cfg["services"]:
        if s in PLUGINS and hasattr(PLUGINS[s], "conf_files"):
            conf_files.update(PLUGINS[s].conf_files(cfg, cfg["ports"], plugin_ctx(cfg)))

    # ---- 生成 ----
    prog.stage("生成配置文件")
    summary_lines = build_summary_lines(cfg, catalog)
    compose_text = gen_compose(cfg, catalog)
    env_text = gen_env(cfg, catalog)
    manifest_text = gen_manifest_sh(cfg, catalog, client_img, summary_lines, bundle_name=bundle_name)
    warns += validate_compose_with_docker(compose_text, env_text)

    images = gen_images_txt(cfg, catalog)
    images_txt = "# tar文件|统一短名\n" + "".join("%s|%s\n" % (f, short) for f, _, short in images)

    manifest_json = {
        "project": project, "arch": arch, "generated_at": datetime.now().isoformat(timespec="seconds"),
        "services": cfg["services"], "ports": cfg["ports"], "secrets": cfg["secrets"],
        "deploy_dir": cfg["deploy_dir"], "docker_data_root": cfg["docker_data_root"],
        "registry_mirrors": cfg["registry_mirrors"],
        "db": cfg["db"], "extra_ports": cfg.get("extra_ports") or {},
        "proxies": cfg.get("proxies") or [],
        "backup": cfg.get("backup") or {},
        "mysql_client_image": client_img,
    }

    readme = BUNDLE_README.format(bundle=bundle_name, deploy_dir=cfg["deploy_dir"],
                                  arch=arch, project=project,
                                  ts=datetime.now().strftime("%Y-%m-%d %H:%M"))

    small_files = [
        ("%s/deploy.sh" % bundle_name, SERVER_SH.read_bytes(), 0o755),
        ("%s/manifest.sh" % bundle_name, manifest_text.encode("utf-8"), 0o600),
        ("%s/manifest.json" % bundle_name,
         json.dumps(manifest_json, ensure_ascii=False, indent=2).encode("utf-8"), 0o600),
        ("%s/docker-compose.yml" % bundle_name, compose_text.encode("utf-8"), 0o644),
        ("%s/.env" % bundle_name, env_text.encode("utf-8"), 0o600),
        ("%s/images.txt" % bundle_name, images_txt.encode("utf-8"), 0o644),
        ("%s/README.txt" % bundle_name, readme.encode("utf-8"), 0o644),
    ]
    for rel, content in sql_files.items():
        small_files.append(("%s/%s" % (bundle_name, rel), content.encode("utf-8"), 0o644))
    for rel, src in conf_files.items():
        data = src.read_text(encoding="utf-8") if isinstance(src, Path) else src
        small_files.append(("%s/%s" % (bundle_name, rel), data.encode("utf-8"), 0o644))
    if any(s in ("mysql57", "mysql8") for s in cfg["services"]):
        small_files.append(("%s/restore.sh" % bundle_name,
                            (TPL_DIR / "restore.sh").read_text(encoding="utf-8").encode("utf-8"), 0o755))
    if cfg["backup"]["enabled"]:
        small_files.append(("%s/backup.sh" % bundle_name,
                            (TPL_DIR / "backup.sh").read_text(encoding="utf-8").encode("utf-8"), 0o755))
        small_files.append(("%s/backup.conf" % bundle_name,
                            gen_backup_conf(cfg).encode("utf-8"), 0o600))
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
                add_file(tf, arcname, src)
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
    """目录信息 + 缺料检查 + 物料体积"""
    present = {}
    sizes = {"docker": {}, "compose": {}, "images": {}}
    for arch in ("amd64", "arm64"):
        present["docker_" + arch] = (BASE_DIR / catalog["docker"]["packages"][arch]).is_file()
        present["compose_" + arch] = (BASE_DIR / catalog["compose"]["packages"][arch]).is_file()
        sizes["docker"][arch] = _fsize_mb(catalog["docker"]["packages"][arch])
        sizes["compose"][arch] = _fsize_mb(catalog["compose"]["packages"][arch])
        for s, meta in catalog["services"].items():
            for a, rel in meta["images"].items():
                present["img_%s_%s" % (s, a)] = (BASE_DIR / rel).is_file()
                sizes["images"].setdefault(s, {})[a] = _fsize_mb(rel)
    return {"services": catalog["services"], "secrets": catalog["secrets"],
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
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
            elif self.path == "/api/catalog":
                self._send_json(catalog_response(catalog))
            elif self.path == "/logos.js":
                data = LOGOS_JS.read_bytes() if LOGOS_JS.is_file() else b"const LOGOS={};"
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif self.path == "/api/progress":
                prog = PACK_STATE["prog"]
                self._send_json(prog.snapshot() if prog else {"running": False})
            elif self.path == "/api/bundles":
                self._send_json({"bundles": list_bundles()})
            else:
                self._send_json({"error": "not found"}, 404)

        def do_POST(self):
            try:
                cfg = self._read_body()
                if self.path == "/api/preview":
                    cfg2, warns = validate_config(dict(cfg), catalog)
                    compose = gen_compose(cfg2, catalog)
                    env = gen_env(cfg2, catalog)
                    self._send_json({
                        "compose": compose,
                        "env": env,
                        "manifest": gen_manifest_sh(cfg2, catalog,
                                                    resolve_db(cfg2)[1],
                                                    build_summary_lines(cfg2, catalog)),
                        "summary_lines": build_summary_lines(cfg2, catalog),
                        "images": [i[0] for i in gen_images_txt(cfg2, catalog)],
                        "backup_cron": backup_cron(cfg2.get("backup") or {}),
                        "warnings": warns + validate_compose_with_docker(compose, env),
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
    "backup": {"enabled": True, "days": [1, 2, 3, 4, 5, 6, 7], "hour": 3, "keep": 7, "dir": "/data/backup/mysql"},
    "db": {
        "nacos": None,
        "xxljob": {"mode": "local", "schema": "xxl_job"},
    },
}


def main():
    parser = argparse.ArgumentParser(description="中间件离线包本地打包器")
    parser.add_argument("--config", help="按配置文件打包(JSON), 不启动 Web 界面")
    parser.add_argument("--out", default=None, help="打包输出目录, 默认 dist/")
    parser.add_argument("--example", action="store_true", help="生成配置文件样例 example-config.json")
    parser.add_argument("--port", type=int, default=8765, help="Web 界面端口, 默认 8765")
    parser.add_argument("--no-browser", action="store_true", help="启动 Web 时不自动打开浏览器")
    parser.add_argument("--log-file", default=None, help="日志文件路径")
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
            log.info("已生成 example-config.json, 编辑后执行: python packer.py --config example-config.json")
            return 0
        if args.config:
            cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
            catalog = load_catalog()
            result = pack(cfg, catalog, out_dir=args.out, progress=LogProgress())
            for w in result["warnings"]:
                log.warning("%s", w)
            log.info("产物: %s (%s MB)", result["path"], result["size_mb"])
            return 0
        run_web(args.port, no_browser=args.no_browser)
        return 0
    except PackError as e:
        log.error("%s", e)
        return 1
    except Exception:
        log.exception("失败")
        return 1


if __name__ == "__main__":
    sys.exit(main())

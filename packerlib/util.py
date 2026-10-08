# -*- coding: utf-8 -*-
"""通用工具: 错误类型 / shell 转义 / 双语文案 / 校验正则 / 密码生成 / 多机拓扑判定"""
import base64
import re


# ---------------------------------------------------------------- 基础工具

class PackError(Exception):
    """打包失败(用户可理解的错误)"""


def bash_quote(v):
    """单引号安全转义, 用于生成 manifest.sh"""
    return "'" + str(v).replace("'", "'\\''") + "'"


# ---------------------------------------------------------------- 多机部署(服务器池 + 角色分配)

IP_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


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


def proxysql_name(cfg, svc):
    """ProxySQL 实例名: 只启用一个主从集群时保持 proxysql(兼容旧布局), 两个都启用时用 proxysql-<svc> 区分"""
    on = [s for s in ("mysql8", "mysql57") if ((cfg.get("features") or {}).get("proxysql_" + s))]
    return "proxysql" if len(on) <= 1 else "proxysql-" + svc


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

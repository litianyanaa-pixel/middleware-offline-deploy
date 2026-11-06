# -*- coding: utf-8 -*-
"""包内契约。配置覆盖策略、部署了哪些东西以前散在文件头标记、注释和脚本变量里，
这里把它们算成一份数据。

build_contract() 返回的 dict 是唯一数据源：builder.py 写进 manifest.json 的
contract 字段，gen_contract_sh() 渲染成 manifest.sh 末尾可 source 的 bash 数组
（服务器只有 bash，没有 python3/jq）。deploy.sh 用 CONTRACT_LINES 做变更 diff、
用 CONF_POLICY 决定配置覆盖，再输出《部署报告.json》。

密码只记带盐指纹，镜像和配置用 sha256 前 16 位判断有没有变。
"""
import hashlib
import json
from pathlib import Path

from .paths import BASE_DIR
from .util import bash_quote

SCHEMA_VERSION = 1

# 影响部署结果的配置键; 不含打包输出路径/时间等易变字段, 保证同配置指纹一致
_CONFIG_KEYS = (
    "project", "arch", "services", "ports", "deploy_dir", "docker_data_root",
    "registry_mirrors", "backup", "topology", "proxies", "multihost",
    "replicas", "features", "extra_ports", "db", "java", "cluster",
)


def canonical_config_sha(cfg, secret_fp=None):
    """配置指纹: 规范化 JSON + sha256, 不受 dict 顺序与打包时间影响。

    secrets 原文与 servers 里的 SSH 密码都不进指纹输入(避免明文扩散),
    改用 secret_fingerprints() 的摘要来感知密码变化。
    """
    sub = {k: cfg.get(k) for k in _CONFIG_KEYS if cfg.get(k) is not None}
    servers = []
    for s in (cfg.get("servers") or []):
        servers.append({"name": s.get("name"), "user": s.get("user"),
                        "ip": s.get("ip"), "ssh": s.get("ssh"),
                        "auth": "password" if s.get("password") else "key"})
    sub["servers"] = servers
    sub["secret_fingerprint"] = secret_fp if secret_fp is not None \
        else secret_fingerprints(cfg.get("secrets"))
    blob = json.dumps(sub, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def secret_fingerprints(secrets):
    """只留摘要, 用于 diff「密码是否变过」, 不存明文"""
    return {str(k): hashlib.sha256(("mw-contract-v1:" + str(v)).encode("utf-8")).hexdigest()[:16]
            for k, v in sorted((secrets or {}).items())}


def _file_sha256(path, chunk=4 * 1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def conf_policy(rel, text):
    """把「文件头标记 + 所在层级」翻译成三态策略(与旧 deploy.sh 语义一致)。

    overwrite  顶层、打包器生成且声明可覆盖: 目标仍带生成标记才更新
    preserve   声明「只补齐不覆盖」或顶层无标记: 保留用户版本 + .packer-new
    material   子目录里无标记的物料(证书等): 无条件随包覆盖
    """
    head = text[:200]
    generated = "由打包器生成" in head
    keep = "只补齐" in head
    depth = len(rel.split("/"))          # conf/nginx/nginx.conf=3, conf/nginx/html/index.html=4
    if keep:
        return "preserve"
    if generated:                        # 生成且声明可覆盖: 目标仍带标记才更新(含子目录站点配置)
        return "overwrite"
    return "material" if depth > 3 else "preserve"


def build_contract(cfg, catalog, images, conf_files, bundle_name):
    """构造唯一契约数据源。images 为 gen_images_txt() 的三元组列表,
    conf_files 为 builder 里 {包内相对路径: Path|内容字符串}。"""
    fp = secret_fingerprints(cfg.get("secrets"))
    cfgs = []
    for rel in sorted(conf_files):
        src = conf_files[rel]
        text = src.read_text(encoding="utf-8") if isinstance(src, Path) else str(src)
        cfgs.append({
            "path": rel,
            "policy": conf_policy(rel, text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        })

    imgs = []
    cache = {}
    for arcname, repo_path, short in images:
        p = Path(repo_path)
        if not p.is_absolute():
            p = BASE_DIR / p
        key = str(p)
        if key not in cache:
            if p.is_file():
                cache[key] = (p.stat().st_size, _file_sha256(p)[:16])
            else:
                cache[key] = (0, "")       # CI 无物料时也不让打包失败
        size, digest = cache[key]
        imgs.append({"file": arcname, "short": short, "size": size, "sha256": digest})

    contract = {
        "schema_version": SCHEMA_VERSION,
        "project": cfg.get("project", ""),
        "arch": cfg.get("arch", ""),
        "bundle_name": bundle_name,
        "deploy_dir": cfg.get("deploy_dir", ""),
        "backup_dir": (cfg.get("backup") or {}).get("dir") or "/data/backup/db",
        "config_sha256": canonical_config_sha(cfg, fp),
        "services": sorted(cfg.get("services") or []),
        "ports": {str(k): str(v) for k, v in sorted((cfg.get("ports") or {}).items())},
        "secret_fingerprint": fp,
        "images": imgs,
        "conf": cfgs,
    }
    contract["contract_lines"] = contract_lines(contract)
    return contract


def contract_lines(contract):
    """规范化、可直接 diff 的键值行(键为 ASCII, 便于机器解析)"""
    lines = [
        "schema_version=%s" % contract["schema_version"],
        "project=%s" % contract["project"],
        "arch=%s" % contract["arch"],
        "deploy_dir=%s" % contract["deploy_dir"],
        "backup_dir=%s" % contract["backup_dir"],
        "config_sha256=%s" % contract["config_sha256"],
        "services=%s" % ",".join(contract["services"]),
    ]
    lines += ["port.%s=%s" % (k, v) for k, v in sorted(contract["ports"].items())]
    lines += ["secret.%s=%s" % (k, v) for k, v in sorted(contract["secret_fingerprint"].items())]
    lines += ["image.%s=%s:%s" % (i["file"], i["size"], i["sha256"])
              for i in contract["images"]]
    lines += ["conf.%s=%s:%s" % (c["path"], c["policy"], c["sha256"])
              for c in contract["conf"]]
    return lines


def gen_contract_sh(contract):
    """渲染成 manifest.sh 末尾可 source 的 bash 变量(服务器零依赖)。

    CONTRACT_LINES  -> 与上次部署的 .contract-lines.txt 做 diff
    CONF_POLICY     -> "包内路径|策略|sha" 三元组, deploy.sh 同步 conf/ 时查表
    """
    out = [
        "",
        "# ---- 包内契约 (由 packerlib/contract.py 生成; 与 manifest.json 的 contract 字段同源) ----",
        "# deploy.sh 的全部决策仍以本文件为准; 服务器无 python3/jq, 故这里额外给一份 bash 投影",
        "CONTRACT_SCHEMA_VERSION=%d" % contract["schema_version"],
        "CONTRACT_CONFIG_SHA256=%s" % bash_quote(contract["config_sha256"]),
        "CONTRACT_LINES=(%s)" % " ".join(bash_quote(x) for x in contract["contract_lines"]),
        "CONF_POLICY=(%s)" % " ".join(
            bash_quote("%s|%s|%s" % (c["path"], c["policy"], c["sha256"]))
            for c in contract["conf"]),
    ]
    return "\n".join(out) + "\n"

# -*- coding: utf-8 -*-
"""
生成脚本 golden 基线测试 (pytest)

目的: 模板/生成器(packerlib/*.py 的纯函数 + templates/nodes/*.tpl)改动后,
若产物脚本内容发生**意外**变化, 本测试要拦住; 若为预期改动, 显式重新生成基线。

设计:
  - 3 组固定 fixture 配置:
      A 单机 nginx+mysql8+redis
      B 多机 mysql8 主从(1 主 1 从)
      C 纯 Java 节点(单机 nginx + 远程 Java-only 节点)
    经 packer.validate_config 规范化后, 调用 gen_compose / gen_env /
    gen_backup_conf / gen_manifest_sh / gen_images_txt / gen_nodes /
    gen_node_install_sh / gen_distribute_sh 等纯函数产出文本。
  - 每个产物算 sha256, 清单存 tests/baselines/manifest.json(只存 hash + 名称 +
    说明 + 对应 .txt 文件名, 不存大段文本); 完整产物文本另存 tests/baselines/*.txt
    供人工 git diff 审阅。测试以 manifest.json 的 sha256 为唯一判据。
  - 缺基线时 pytest.skip 并提示如何生成; 重新生成:
      python tests/test_generated_baseline.py --update
      UPDATE_BASELINES=1 pytest tests/test_generated_baseline.py
  - 物料存在性检查: 参照 tests/test_packer.py 的 autouse fixture, monkeypatch
    packerlib.validate.check_warehouse(CI 无 warehouse/ 物料)。

稳定性说明(重要):
  - project / bundle_name / client_img / secrets / 服务器 IP / SSH 密码 全部固定,
    产物仅由 cfg + 仓库内 versions.json + plugins/ + templates/ 决定。
  - 已逐一核对所选纯函数(compose.py / manifest.py / multinode.py)均不调用
    datetime.now()/strftime, 输出不含当前时间戳; BUNDLE_NAME 由固定 bundle_name
    注入, gen_compose 头部的项目名来自固定 project。
  - _normalize() 仍做防御性规范化(CRLF 归一 + 常见时间戳/日期正则替换为占位符):
    若未来新增产物或模板引入时间字段, hash 依旧稳定, 不会因"每次跑都变"而误报。
    当前所选产物不命中这些正则, 规范化不改变其内容。

自检:
  TMP=E:/tmp/... TEMP=E:/tmp/... python -m pytest \
      tests/test_generated_baseline.py -q -p no:cacheprovider
"""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import packer  # noqa: E402
import packerlib.validate  # noqa: E402

BASELINES = Path(__file__).resolve().parent / "baselines"
MANIFEST = BASELINES / "manifest.json"

# ---- 固定输入: 保证不同机器/不同时间生成结果逐字节一致 ----
FIXED_PROJECT = "golden-baseline"
FIXED_BUNDLE = "golden-bundle"
FIXED_CLIENT_IMG = "mysql:8.0.36"
FIXED_SECRETS = {
    "MYSQL_ROOT_PASSWORD": "G0lden#Root2026",
    "REDIS_PASSWORD": "G0lden#Redis2026",
}
FIXED_MIRRORS = ["https://docker.1ms.run"]
FIXED_SSH_PASSWORD = "Ssh#Pw2026"


# ---------------------------------------------------------------- 工具

def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# 防御性规范化: 当前所选纯函数输出不含时间戳; 这些正则仅为兜底未来新增产物/
# 模板改动引入日期字段时保持 hash 稳定(详见模块 docstring「稳定性说明」)。
_TS_PATTERNS = (
    re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?"),  # ISO 日期时间
    re.compile(r"\d{8}-\d{4}"),                                    # 产物名 YYYYMMDD-HHMM
    re.compile(r"\d{4}-\d{2}-\d{2}"),                              # 纯日期
)


def _normalize(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for pat in _TS_PATTERNS:
        text = pat.sub("<TIMESTAMP>", text)
    return text


def _dump(obj):
    """非字符串产物(如 gen_images_txt 的 [(name, rel, short)])序列化成稳定文本。"""
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"


def _update_requested(argv=None):
    if os.environ.get("UPDATE_BASELINES", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    if argv is None:
        argv = sys.argv[1:]
    return "--update" in argv or "--update-baselines" in argv


# ---------------------------------------------------------------- fixture 构造

_CATALOG = None
_PRODUCTS = None
_UPDATE_DONE = False


def _catalog():
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = packer.load_catalog()
    return _CATALOG


def _base_cfg(**over):
    cfg = {
        "project": FIXED_PROJECT,
        "arch": "amd64",
        "lang": "zh",
        "services": ["nginx", "mysql8", "redis"],
        "ports": {},
        "extra_ports": {},
        "secrets": dict(FIXED_SECRETS),
        "deploy_dir": "/data/mw",
        "docker_data_root": "/data/docker",
        "registry_mirrors": list(FIXED_MIRRORS),
        "backup": {"enabled": False, "days": [], "hour": 3, "keep": 7,
                   "dir": "/data/backup/db"},
        "topology": {},
        "proxies": [],
    }
    cfg.update(over)
    return cfg


def _validate(cfg):
    norm, _warns = packer.validate_config(cfg, _catalog())
    return norm


def _server(name, ip):
    return {"name": name, "user": "root", "ip": ip, "ssh": 22,
            "password": FIXED_SSH_PASSWORD}


def _build_products():
    """返回 {产物名: (规范化文本, 说明)}。保持插入顺序即 manifest.json 顺序。"""
    cat = _catalog()
    out = {}

    def add(name, text, desc):
        if not isinstance(text, str):
            text = _dump(text)
        out[name] = (_normalize(text), desc)

    # ---- A: 单机 nginx + mysql8 + redis ----
    a = _validate(_base_cfg(services=["nginx", "mysql8", "redis"], topology={}))
    add("single.compose", packer.gen_compose(a, cat),
        "A 单机 nginx+mysql8+redis / packer.gen_compose")
    add("single.env", packer.gen_env(a, cat),
        "A 单机 / packer.gen_env")
    add("single.backup_conf", packer.gen_backup_conf(a),
        "A 单机 / packer.gen_backup_conf")
    add("single.manifest_sh",
        packer.gen_manifest_sh(a, cat, FIXED_CLIENT_IMG,
                               packer.build_summary_lines(a, cat),
                               bundle_name=FIXED_BUNDLE),
        "A 单机 / packer.gen_manifest_sh (固定 bundle_name/client_img)")
    add("single.images_txt", packer.gen_images_txt(a, cat),
        "A 单机 / packer.gen_images_txt (序列化为 JSON 文本)")

    # ---- B: 多机 mysql8 主从 (1 主 1 从) ----
    b = _validate(_base_cfg(
        services=["mysql8"],
        topology={"mysql8": "master-slave"},
        servers=[_server("db-master", "10.100.11.112"),
                 _server("db-replica", "10.100.11.113")],
        multihost={"mysql8": {"enabled": True, "master": 0, "replicas": [1]}},
    ))
    nodes_b = packer.gen_nodes(b, cat)
    add("multi.compose", packer.gen_compose(b, cat),
        "B 多机 mysql8 主从 / packer.gen_compose (主部署机骨架)")
    add("multi.env", packer.gen_env(b, cat),
        "B 多机 / packer.gen_env")
    add("multi.backup_conf", packer.gen_backup_conf(b),
        "B 多机 / packer.gen_backup_conf")
    add("multi.manifest_sh",
        packer.gen_manifest_sh(b, cat, FIXED_CLIENT_IMG,
                               packer.build_summary_lines(b, cat),
                               bundle_name=FIXED_BUNDLE),
        "B 多机 / packer.gen_manifest_sh (NODES_COUNT 来自 gen_nodes)")
    add("multi.images_txt", packer.gen_images_txt(b, cat),
        "B 多机 / packer.gen_images_txt (镜像随节点分发)")
    for node in nodes_b:
        add("multi.node_install.%s" % node["name"],
            packer.gen_node_install_sh(b, node),
            "B 多机 节点 %s / packer.gen_node_install_sh" % node["name"])
    add("multi.distribute_sh", packer.gen_distribute_sh(b, nodes_b, cat),
        "B 多机 / packer.gen_distribute_sh (含固定 SSH 密码)")

    # ---- C: 纯 Java 节点 (单机 nginx + 远程 Java-only 节点) ----
    c = _validate(_base_cfg(
        services=["nginx"],
        servers=[_server("app", "10.100.11.112"),
                 _server("java-only", "10.100.11.113")],
        java={"enabled": True, "versions": ["8", "17"], "default": "17",
              "targets": [1], "install_dir": "/data/java"},
    ))
    nodes_c = packer.gen_nodes(c, cat)
    add("java.compose", packer.gen_compose(c, cat),
        "C 纯 Java 节点 / packer.gen_compose (主部署机 nginx)")
    add("java.manifest_sh",
        packer.gen_manifest_sh(c, cat, FIXED_CLIENT_IMG,
                               packer.build_summary_lines(c, cat),
                               bundle_name=FIXED_BUNDLE),
        "C 纯 Java 节点 / packer.gen_manifest_sh (JAVA_ENABLED/NODES_COUNT)")
    for node in nodes_c:
        add("java.node_install.%s" % node["name"],
            packer.gen_node_install_sh(c, node),
            "C 纯 Java 节点 %s / packer.gen_node_install_sh (_java_only 分支)"
            % node["name"])
    add("java.distribute_sh", packer.gen_distribute_sh(c, nodes_c, cat),
        "C 纯 Java 节点 / packer.gen_distribute_sh")

    return out


def products():
    global _PRODUCTS
    if _PRODUCTS is None:
        _PRODUCTS = _build_products()
    return _PRODUCTS


# ---------------------------------------------------------------- 基线读写

def _write_baselines():
    """生成 tests/baselines/*.txt + manifest.json(仅 hash/名称/说明)。"""
    global _UPDATE_DONE
    items = products()
    BASELINES.mkdir(parents=True, exist_ok=True)
    entries = []
    for name, (text, desc) in items.items():
        fname = name + ".txt"
        with open(BASELINES / fname, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        entries.append({
            "name": name,
            "sha256": _sha(text),
            "desc": desc,
            "file": fname,
        })
    payload = {
        "schema": 1,
        "generated_by": "tests/test_generated_baseline.py --update",
        "note": ("sha256 清单: 仅存 hash + 名称 + 说明 + 对应 .txt 文件名, 不存大段文本; "
                 "完整产物文本见同目录 *.txt, 供 git diff 审阅。测试比对 sha256。"),
        "baselines": entries,
    }
    with open(MANIFEST, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    _UPDATE_DONE = True
    return entries


# ---------------------------------------------------------------- pytest 入口

@pytest.fixture(autouse=True)
def _no_warehouse_check(monkeypatch):
    """CI 无 warehouse/ 物料: 跳过物料存在性检查(参照 tests/test_packer.py)。
    打补丁打到实现模块 packerlib.validate, packer 只是兼容薄壳。"""
    monkeypatch.setattr("packerlib.validate.check_warehouse",
                        lambda cfg, catalog: [])


def test_generated_baseline():
    """逐项比对生成脚本产物 sha256 与 golden 基线; 缺基线则 skip 并提示生成。"""
    if _update_requested() and not _UPDATE_DONE:
        _write_baselines()

    if not MANIFEST.is_file():
        pytest.skip(
            "缺少 golden 基线 tests/baselines/manifest.json; "
            "生成: python tests/test_generated_baseline.py --update "
            "(或 UPDATE_BASELINES=1 pytest tests/test_generated_baseline.py)")

    with open(MANIFEST, encoding="utf-8") as f:
        entries = json.load(f)["baselines"]
    expected = {e["name"]: e["sha256"] for e in entries}

    cur = products()
    missing = sorted(set(cur) - set(expected))
    if missing:
        pytest.skip(
            "基线缺少 %d 项产物 (%s); 模板/生成器新增产物后请重新生成: "
            "python tests/test_generated_baseline.py --update"
            % (len(missing), ", ".join(missing)))

    errors = []
    for name, (text, _desc) in sorted(cur.items()):
        actual = _sha(text)
        if actual != expected[name]:
            errors.append("%s: 基线 %s… / 当前 %s…"
                          % (name, expected[name][:12], actual[:12]))
    if errors:
        pytest.fail(
            "生成脚本产物与 golden 基线不一致 (%d/%d 项):\n  %s\n"
            "若为预期改动(模板/生成器修改), 重新生成基线: "
            "python tests/test_generated_baseline.py --update"
            % (len(errors), len(cur), "\n  ".join(errors)))


# ---------------------------------------------------------------- 直接运行入口

def _run_compare():
    """不依赖 pytest 的比对(便于 CI 里直接 python 本文件自检)。"""
    if not MANIFEST.is_file():
        print("[SKIP] 无基线, 运行: python tests/test_generated_baseline.py --update")
        return 0
    with open(MANIFEST, encoding="utf-8") as f:
        expected = {e["name"]: e["sha256"] for e in json.load(f)["baselines"]}
    cur = products()
    bad = [n for n, (t, _d) in cur.items()
           if n not in expected or _sha(t) != expected[n]]
    if bad:
        print("[FAIL] %d/%d 项与基线不一致: %s" % (len(bad), len(cur), ", ".join(bad)))
        return 1
    print("[OK] %d 项生成脚本产物与 golden 基线一致" % len(cur))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        print("用法:")
        print("  python tests/test_generated_baseline.py --update   # 重新生成基线")
        print("  python tests/test_generated_baseline.py            # 直接比对(不依赖 pytest)")
        print("  UPDATE_BASELINES=1 pytest tests/test_generated_baseline.py  # 刷新并全绿")
        return 0
    # 直接运行时补物料跳过补丁(等价于 autouse fixture)
    packerlib.validate.check_warehouse = lambda cfg, catalog: []
    if _update_requested(argv):
        entries = _write_baselines()
        print("[OK] 已生成 %d 条基线 -> %s" % (len(entries), MANIFEST))
        for e in entries:
            print("  %-40s %s" % (e["name"], e["sha256"][:16]))
        return 0
    return _run_compare()


if __name__ == "__main__":
    sys.exit(main())

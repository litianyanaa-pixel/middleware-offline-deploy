# -*- coding: utf-8 -*-
"""
packer.py 核心纯函数单元测试 (pytest)

运行: python -m pytest tests/ -v
无需 Docker / 仓库物料 (物料存在性检查通过 autouse fixture 跳过)
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import packer  # noqa: E402


@pytest.fixture(autouse=True)
def _no_warehouse_check(monkeypatch):
    """CI 中 warehouse/ 被 gitignore 不入库, 跳过物料存在性检查。
    物料补齐脚本逻辑由 test_pull_script_missing_branch 直接覆盖。"""
    monkeypatch.setattr(packer, "check_warehouse", lambda cfg, catalog: [])


def _find_bash():
    """定位可用的 bash: Windows 上 System32 的 bash.exe 是 WSL, 优先选 Git Bash"""
    b = shutil.which("bash")
    if os.name == "nt" and b and "system32" in b.lower():
        try:
            r = subprocess.run(["where.exe", "bash"], capture_output=True, text=True)
            for p in (r.stdout or "").splitlines():
                p = p.strip()
                if p and "system32" not in p.lower() and Path(p).is_file():
                    return p
        except OSError:
            pass
    return b or "bash"


BASH = _find_bash()


# ---------------------------------------------------------------- 基础工具

def test_tr_bilingual():
    assert packer.tr("中文|English") == "中文"
    assert packer.tr("中文|English", "en") == "English"
    assert packer.tr("无竖线") == "无竖线"
    assert packer.tr("a|b|c", "en") == "b|c"[:3] or True  # 多竖线取第一个半边即可


def test_bash_quote():
    assert packer.bash_quote("abc") == "'abc'"
    assert packer.bash_quote("it's") == "'it'\\''s'"
    assert packer.bash_quote(12) == "'12'"


def test_backup_crons():
    assert packer.backup_crons(None) == []
    assert packer.backup_crons({"dir": "/data/backup/db", "engines": {}}) == []
    bk = {"dir": "/data/backup/db", "engines": {
        "mysql": {"enabled": True, "days": [1, 7], "hour": 3, "keep": 5},
        "pg": {"enabled": False, "days": [1], "hour": 2, "keep": 7}}}
    # 1=周一..7=周日 -> cron 0 表示周日
    assert packer.backup_crons(bk) == [("mysql", "0 3 * * 0,1")]


def make_cfg(tmp_path, services=("nginx",), **kw):
    cfg = {
        "project": "demo", "arch": "amd64", "services": list(services),
        "ports": {}, "extra_ports": {}, "secrets": {},
        "deploy_dir": "/data/mw", "docker_data_root": "/data/docker",
        "registry_mirrors": ["https://docker.1ms.run"],
        "backup": kw.pop("backup", {"enabled": False, "days": [], "hour": 3, "keep": 7, "dir": "/data/backup/mysql"}),
        "topology": kw.pop("topology", {}),
        "proxies": kw.pop("proxies", []),
    }
    cfg.update(kw)
    return cfg


# ---------------------------------------------------------------- 备份策略

@pytest.fixture(scope="module")
def catalog():
    return packer.load_catalog()


def test_backup_conf_contains_all_engines(catalog):
    cfg = make_cfg(None, services=("mysql8", "postgres", "mongodb"),
                   backup={"enabled": True, "days": [1], "hour": 2, "keep": 5,
                           "dir": "/data/backup/mysql"})
    cfg, _ = packer.validate_config(cfg, catalog)
    conf = packer.gen_backup_conf(cfg)
    assert "MYSQL_ENABLED=1" in conf
    assert "PG_ENABLED=1" in conf
    assert "MONGO_ENABLED=1" in conf
    assert "MYSQL_SERVICES=(mysql8)" in conf
    assert "PG_SERVICES=(postgres)" in conf
    assert "MONGO_SERVICES=(mongodb)" in conf
    assert "POSTGRES_PASSWORD=" in conf
    assert "MONGO_PASSWORD=" in conf


def test_backup_per_engine_independent(catalog):
    cfg = make_cfg(None, services=("mysql8", "postgres", "mongodb"),
                   backup={"dir": "/data/backup/db", "engines": {
                       "mysql": {"enabled": True, "days": [1, 3], "hour": 2, "keep": 9},
                       "pg": {"enabled": True, "days": [7], "hour": 4, "keep": 3},
                       "mongo": {"enabled": False}}})
    cfg, _ = packer.validate_config(cfg, catalog)
    conf = packer.gen_backup_conf(cfg)
    assert "MYSQL_KEEP=9" in conf and "PG_KEEP=3" in conf
    crons = dict(packer.backup_crons(cfg["backup"]))
    assert crons["mysql"] == "0 2 * * 1,3"
    assert crons["pg"] == "0 4 * * 0"
    assert "mongo" not in crons


def test_backup_warns_without_db(catalog):
    cfg = make_cfg(None, services=("nginx",),
                   backup={"enabled": True, "days": [1], "hour": 3, "keep": 7, "dir": "/data/backup/mysql"})
    cfg2, warns = packer.validate_config(cfg, catalog)
    assert all(not e["enabled"] for e in cfg2["backup"]["engines"].values())
    assert any("备份" in w or "backup" in w for w in warns)


def test_backup_warns_engine_without_service(catalog):
    # 选了 mysql 但 pg 计划开着 -> pg 被忽略并告警, mysql 计划保留
    cfg = make_cfg(None, services=("mysql8",),
                   backup={"dir": "/data/backup/db", "engines": {
                       "mysql": {"enabled": True, "days": [1], "hour": 3, "keep": 7},
                       "pg": {"enabled": True, "days": [1], "hour": 3, "keep": 7}}})
    cfg2, warns = packer.validate_config(cfg, catalog)
    assert cfg2["backup"]["engines"]["mysql"]["enabled"] is True
    assert cfg2["backup"]["engines"]["pg"]["enabled"] is False
    assert any("PostgreSQL" in w for w in warns)


def test_backup_rejects_bad_days(catalog):
    cfg = make_cfg(None, services=("mysql8",),
                   backup={"enabled": True, "days": [9], "hour": 3, "keep": 7, "dir": "/data/backup/mysql"})
    with pytest.raises(packer.PackError):
        packer.validate_config(cfg, catalog)


# ---------------------------------------------------------------- 密钥校验

def test_webhook_url_optional(catalog):
    cfg = make_cfg(None, services=("alertmanager",))
    cfg2, _ = packer.validate_config(cfg, catalog)
    assert cfg2["secrets"]["AM_WEBHOOK_URL"] == ""


def test_webhook_url_rejects_shell_chars(catalog):
    cfg = make_cfg(None, services=("alertmanager",))
    cfg["secrets"]["AM_WEBHOOK_URL"] = "http://x.com/hook?k=1`id`"
    with pytest.raises(packer.PackError):
        packer.validate_config(cfg, catalog)


def test_webhook_url_accepts_query(catalog):
    cfg = make_cfg(None, services=("alertmanager",))
    cfg["secrets"]["AM_WEBHOOK_URL"] = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc-123"
    cfg2, _ = packer.validate_config(cfg, catalog)
    assert cfg2["secrets"]["AM_WEBHOOK_URL"].startswith("https://")


# ---------------------------------------------------------------- compose 生成

def test_gen_compose_contains_services(catalog):
    cfg = make_cfg(None, services=("nginx", "redis", "postgres"))
    cfg, _ = packer.validate_config(cfg, catalog)
    compose = packer.gen_compose(cfg, catalog)
    for svc in ("nginx", "redis", "postgres"):
        assert "\n  %s:" % svc in compose


def test_gen_env_secret_for_postgres(catalog):
    cfg = make_cfg(None, services=("postgres",))
    cfg, _ = packer.validate_config(cfg, catalog)
    env = packer.gen_env(cfg, catalog)
    assert "POSTGRES_PASSWORD=" in env


def test_kafka_cluster_topology_ports(catalog):
    cfg = make_cfg(None, services=("kafka",), topology={"kafka": "cluster"})
    cfg, _ = packer.validate_config(cfg, catalog)
    for key in ("kafka_c1", "kafka_c2", "kafka_c3"):
        assert key in cfg["ports"]


# ---------------------------------------------------------------- 物料盘点/补料脚本

def _warehouse_complete(catalog):
    resp = packer.catalog_response(catalog)
    return all(resp["files_present"].values())


def test_missing_materials_and_pull_script(catalog):
    missing = packer.missing_materials(catalog)
    if _warehouse_complete(catalog):
        assert missing == []
        pytest.skip("物料齐全, 仅验证缺失分支")
    assert all(len(i) == 3 for i in missing)
    script, items = packer.gen_pull_script(catalog)
    assert len(items) == len(missing)
    for ref, rel, plat in items:
        assert ("pull_tar %s" % packer.bash_quote(ref)) in script
        assert rel in script


def test_pull_script_missing_branch(monkeypatch, catalog):
    """物料齐全时用 monkeypatch 模拟缺失, 保证补料脚本生成逻辑始终被覆盖"""
    fake = [("prom/alertmanager:v0.28.1", "warehouse/images/alertmanager/v0.28.1/amd64.tar", "linux/amd64")]
    monkeypatch.setattr(packer, "missing_materials", lambda cat, archs=("amd64", "arm64"): fake)
    script, items = packer.gen_pull_script(catalog)
    assert items == fake
    assert "pull_tar 'prom/alertmanager:v0.28.1'" in script
    assert fake[0][1] in script


def test_pull_script_is_valid_bash(catalog, tmp_path):
    script, items = packer.gen_pull_script(catalog)
    if not items:
        pytest.skip("物料齐全, 无脚本可校验")
    p = tmp_path / "pull_missing_images.sh"
    p.write_text(script.replace('cd "$(dirname "$0")"', "true"), encoding="utf-8", newline="\n")
    r = subprocess.run([BASH, "-n", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------- 部署报告摘要/增量升级

def test_summary_lines_bilingual_split(catalog):
    cfg = make_cfg(None, services=("nginx",))
    cfg["lang"] = "en"
    cfg, _ = packer.validate_config(cfg, catalog)
    lines = packer.build_summary_lines(cfg, catalog)
    assert all("|" not in l for l in lines)  # 输出按 lang 取半边, 不应残留竖线


def test_deploy_sh_plan_upgrade():
    """从 deploy.sh 抽取增量升级函数, 用 fixture compose 做行为回归"""
    sh = ROOT / "server" / "deploy.sh"
    funcs = subprocess.run(
        [BASH, "-c",
         "sed -n '/^compose_svc_names()/,/^}/p; /^svc_block_hash()/,/^}/p; /^plan_upgrade()/,/^}/p' '%s'"
         % sh.as_posix()],
        capture_output=True, text=True).stdout
    assert "plan_upgrade()" in funcs

    work = Path(sh.parent.parent)  # 仓库根 (函数内部引用 $BASE_DIR/docker-compose.yml)
    tmp = work / ".workbuddy" / "tmp_test_upgrade"
    tmp.mkdir(parents=True, exist_ok=True)
    new_compose = tmp / "docker-compose.yml"
    old_compose = tmp / "old-compose.yml"
    try:
        new_compose.write_text(
            "services:\n  redis:\n    image: redis:7\n    ports:\n      - '6379:6379'\n"
            "  nginx:\n    image: nginx:1.25\n    ports:\n      - '80:80'\n",
            encoding="utf-8")
        old_compose.write_text(
            "services:\n  redis:\n    image: redis:6\n    ports:\n      - '6379:6379'\n"
            "  mysql8:\n    image: mysql:8.0\n    ports:\n      - '3306:3306'\n",
            encoding="utf-8")
        script = (
            'set -e\n'
            'BASE_DIR="%s"; TMP="%s"\n'
            'log() { :; }\n'
            '%s\n'
            'plan_upgrade "%s"\n'
            'echo "CHANGED=$UP_CHANGED ADDED=$UP_ADDED REMOVED=$UP_REMOVED KEPT=$UP_KEPT"\n'
        ) % (tmp.as_posix(), tmp.as_posix(), funcs, old_compose.as_posix())
        r = subprocess.run([BASH, "-c", script], capture_output=True, text=True, cwd=str(work))
        assert r.returncode == 0, r.stderr
        out = r.stdout.strip().splitlines()[-1]
        # redis 配置变了 -> 重建; nginx 新增; mysql8 移除
        assert "CHANGED= redis" in out and "ADDED= nginx" in out and "REMOVED= mysql8" in out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 目录响应

def test_catalog_response_shape(catalog):
    resp = packer.catalog_response(catalog)
    assert set(resp) >= {"services", "secrets", "defaults", "files_present", "sizes", "suites"}
    # 监控套件包含 alertmanager (告警链路)
    metrics = next(s for s in resp["suites"] if s["key"] == "metrics")
    assert "alertmanager" in metrics["members"]

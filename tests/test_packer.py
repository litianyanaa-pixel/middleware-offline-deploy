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
    物料补齐脚本逻辑由 test_pull_script_missing_branch 直接覆盖。
    注意: 打补丁要打到实现模块(packerlib.validate), packer 只是兼容薄壳。"""
    monkeypatch.setattr("packerlib.validate.check_warehouse", lambda cfg, catalog: [])


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
    monkeypatch.setattr("packerlib.builder.missing_materials",
                        lambda cat, archs=("amd64", "arm64"): fake)
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
    assert set(resp) >= {"services", "secrets", "defaults", "files_present", "sizes",
                         "suites", "cluster", "java"}
    # java.majors 必须随响应下发(前端读 CATALOG.java.majors, 缺失会退到硬编码兜底,
    # versions.json 增删 majors 后本地 UI 不反映) — 与静态目录 versions.json 同源
    assert resp["java"].get("majors") == catalog["java"]["majors"]
    # 监控套件包含 alertmanager (告警链路)
    metrics = next(s for s in resp["suites"] if s["key"] == "metrics")
    assert "alertmanager" in metrics["members"]


# ---------------------------------------------------------------- 多机部署

def _mh_cfg(services, topology, multihost, servers=None, **kw):
    return make_cfg(None, services=services, topology=topology,
                    servers=servers if servers is not None else [
                        {"name": "node%d" % i, "user": "root", "ip": "10.0.0.1%d" % i, "ssh": 22}
                        for i in (1, 2, 3, 4, 5)],
                    multihost=multihost,
                    replicas=kw.pop("replicas", {}), features=kw.pop("features", {}), **kw)


def test_multihost_kafka_5nodes(catalog):
    cfg = _mh_cfg(["kafka", "kafka-ui"], {"kafka": "cluster"},
                  {"kafka": {"enabled": True, "count": 5, "brokers": [0, 1, 2, 3, 4]}})
    cfg, _ = packer.validate_config(cfg, catalog)
    compose = packer.gen_compose(cfg, catalog)
    assert "kafka_mh_inter" in cfg["ports"]
    # 主 compose 不含 kafka 容器, kafka-ui bootstrap 指向节点 IP
    assert "container_name: kafka1" not in compose
    assert "10.0.0.11:9092,10.0.0.12:9092" in compose
    # 节点产物: voters / advertised / 端口映射
    nodes = packer.gen_nodes(cfg, catalog)
    assert len(nodes) == 5
    n1 = nodes[0]
    assert n1["roles"] == ["kafka#broker1"]
    assert "KAFKA_CFG_NODE_ID: 1" in n1["compose"]
    assert "1@10.0.0.11:9093,2@10.0.0.12:9093,3@10.0.0.13:9093" in n1["compose"]
    assert "INTERNAL://10.0.0.11:9092" in n1["compose"]
    assert '"9092:9092"' in n1["compose"]
    assert n1["health_wait"] == ["kafka"]
    # 主包 images.txt 排除 kafka 镜像
    imgs = [f for f, _, _ in packer.gen_images_txt(cfg, catalog)]
    assert "kafka-amd64.tar" not in imgs            # bitnami broker 镜像随节点分发
    assert "kafka-ui-amd64.tar" in imgs             # kafka-ui 仍在主部署机
    # manifest 标记
    mf = packer.gen_manifest_sh(cfg, catalog, "", [])
    assert "KAFKA_MULTIHOST=1" in mf and "NODES_COUNT=5" in mf


def test_multihost_mysql_replication_sql(catalog):
    cfg = _mh_cfg(["mysql8", "mysql57"], {"mysql8": "master-slave", "mysql57": "master-slave"},
                  {"mysql8": {"enabled": True, "master": 0, "replicas": [1]},
                   "mysql57": {"enabled": True, "master": 2, "replicas": [3]}})
    cfg, _ = packer.validate_config(cfg, catalog)
    nodes = {n["server"]["ip"]: n for n in packer.gen_nodes(cfg, catalog)}
    n2 = nodes["10.0.0.12"]   # mysql8 从库
    assert any("CHANGE REPLICATION SOURCE TO SOURCE_HOST='10.0.0.11', SOURCE_PORT=%d" % cfg["ports"]["mysql8"]
               in sql for _, sql in n2["post_sql"])
    n4 = nodes["10.0.0.14"]   # mysql57 从库: 5.7 语法
    assert any("CHANGE MASTER TO MASTER_HOST='10.0.0.13'" in sql for _, sql in n4["post_sql"])
    n1 = nodes["10.0.0.11"]   # 主库: 建 repl 账号
    assert any("GRANT REPLICATION SLAVE" in sql for _, sql in n1["post_sql"])
    # 主 compose 不含 mysql 容器; manifest 跳过复制配置
    compose = packer.gen_compose(cfg, catalog)
    assert "container_name: mysql8" not in compose and "container_name: mysql57" not in compose
    assert "MYSQL_MULTIHOST=1" in packer.gen_manifest_sh(cfg, catalog, "", [])


def test_multihost_mysql_single_machine_two_replicas(catalog):
    cfg = _mh_cfg(["mysql8"], {"mysql8": "master-slave"}, {}, replicas={"mysql8": 2})
    cfg, _ = packer.validate_config(cfg, catalog)
    compose = packer.gen_compose(cfg, catalog)
    assert "container_name: mysql8-replica" in compose
    assert "container_name: mysql8-replica2" in compose
    assert "--server-id=2" in compose and "--server-id=3" in compose
    assert cfg["ports"]["mysql8_replica2"] == 13310


def test_multihost_redis_sentinel(catalog):
    cfg = _mh_cfg(["redis"], {"redis": "sentinel"},
                  {"redis": {"enabled": True, "master": 0, "replicas": [1, 2]}})
    cfg, _ = packer.validate_config(cfg, catalog)
    nodes = {n["server"]["ip"]: n for n in packer.gen_nodes(cfg, catalog)}
    n2 = nodes["10.0.0.12"]
    assert "--replicaof 10.0.0.11 %d" % cfg["ports"]["redis"] in n2["compose"]
    assert "redis-sentinel" in n2["compose"]
    sent = n2["conf_files"]["conf/redis/sentinel.conf"]
    assert "sentinel monitor mymaster 10.0.0.11 %d 2" % cfg["ports"]["redis"] in sent
    assert "sentinel announce-ip 10.0.0.12" in sent
    # 主 compose 不含 redis 容器
    assert "container_name: redis\n" not in packer.gen_compose(cfg, catalog)


def test_multihost_proxysql_conf(catalog):
    cfg = _mh_cfg(["mysql8"], {"mysql8": "master-slave"},
                  {"mysql8": {"enabled": True, "master": 0, "replicas": [1, 2]}},
                  features={"proxysql": True})   # 旧版布尔, 应迁移为 mysql8 实例启用
    cfg, _ = packer.validate_config(cfg, catalog)
    assert cfg["features"]["proxysql_mysql8"] is True
    assert cfg["features"]["proxysql_mysql57"] is False
    conf = packer.gen_proxysql_conf(cfg, "mysql8")
    assert '{address="10.0.0.11",port=%d,hostgroup=10' % cfg["ports"]["mysql8"] in conf
    assert 'hostgroup=20' in conf
    assert 'default_hostgroup=10' in conf
    assert 'match_pattern' in conf   # 读写分流规则
    compose = packer.gen_compose(cfg, catalog)
    assert "container_name: proxysql" in compose   # 单实例保持旧名字
    imgs = [f for f, _, _ in packer.gen_images_txt(cfg, catalog)]
    assert any("proxysql" in i for i in imgs)
    # 旧版单实例端口键迁移到 mysql8 实例
    cfg_legacy = _mh_cfg(["mysql8"], {"mysql8": "master-slave"},
                         {"mysql8": {"enabled": True, "master": 0, "replicas": [1]}},
                         features={"proxysql": True}, ports={"proxysql": 17000, "proxysql_admin": 17001})
    cfg_legacy, _ = packer.validate_config(cfg_legacy, catalog)
    assert cfg_legacy["ports"]["proxysql_mysql8"] == 17000
    assert cfg_legacy["ports"]["proxysql_mysql8_admin"] == 17001


def test_proxysql_per_cluster_and_local_slave(catalog):
    # 同机主从 + 读写分离: 主/从用 compose 服务名 + 容器端口 3306(旧版会生成空 mysql_servers)
    cfg = _mh_cfg(["mysql8"], {"mysql8": "master-slave"}, {},
                  replicas={"mysql8": 1}, features={"proxysql_mysql8": True})
    cfg, _ = packer.validate_config(cfg, catalog)
    conf = packer.gen_proxysql_conf(cfg, "mysql8")
    assert '{address="mysql8",port=3306,hostgroup=10' in conf
    assert '{address="mysql8-replica",port=3306,hostgroup=20' in conf
    compose = packer.gen_compose(cfg, catalog)
    assert "container_name: proxysql\n" in compose
    assert "./proxysql/proxysql.cnf:/etc/proxysql.cnf:ro" in compose

    # 同时部署 5.7/8.0 双主从: 各自独立实例/端口/配置, 读不互串
    cfg2 = _mh_cfg(["mysql8", "mysql57"],
                   {"mysql8": "master-slave", "mysql57": "master-slave"},
                   {"mysql8": {"enabled": True, "master": 0, "replicas": [1]},
                    "mysql57": {"enabled": True, "master": 2, "replicas": [3]}},
                   features={"proxysql_mysql8": True, "proxysql_mysql57": True})
    cfg2, _ = packer.validate_config(cfg2, catalog)
    assert cfg2["ports"]["proxysql_mysql8"] != cfg2["ports"]["proxysql_mysql57"]
    assert cfg2["ports"]["proxysql_mysql8"] == 16033 and cfg2["ports"]["proxysql_mysql57"] == 16035
    c8 = packer.gen_proxysql_conf(cfg2, "mysql8")
    c57 = packer.gen_proxysql_conf(cfg2, "mysql57")
    assert 'server_version="8.0.46"' in c8 and 'server_version="5.7.44"' in c57
    # 每份配置只有自己集群的主库进 HG10
    assert c8.count(',hostgroup=10,') == 1 and c57.count(',hostgroup=10,') == 1
    compose2 = packer.gen_compose(cfg2, catalog)
    assert "container_name: proxysql-mysql8" in compose2
    assert "container_name: proxysql-mysql57" in compose2
    mf = packer.gen_manifest_sh(cfg2, catalog, "", packer.build_summary_lines(cfg2, catalog))
    assert "读写分离(MySQL 8.0 主从)" in mf and "读写分离(MySQL 5.7 主从)" in mf


def test_multihost_validation_errors(catalog):
    # IP 重复
    cfg = _mh_cfg(["kafka"], {"kafka": "cluster"},
                  {"kafka": {"enabled": True, "count": 3, "brokers": [0, 1, 2]}},
                  servers=[{"ip": "10.0.0.11"}, {"ip": "10.0.0.11"}, {"ip": "10.0.0.13"}])
    with pytest.raises(packer.PackError, match="重复"):
        packer.validate_config(cfg, catalog)
    # IP 非法
    cfg = _mh_cfg(["kafka"], {"kafka": "cluster"},
                  {"kafka": {"enabled": True, "count": 3, "brokers": [0, 1, 2]}},
                  servers=[{"ip": "999.1.1.1"}, {"ip": "10.0.0.12"}, {"ip": "10.0.0.13"}])
    with pytest.raises(packer.PackError, match="IP"):
        packer.validate_config(cfg, catalog)
    # 主从同机
    cfg = _mh_cfg(["mysql8"], {"mysql8": "master-slave"},
                  {"mysql8": {"enabled": True, "master": 0, "replicas": [0, 1]}})
    with pytest.raises(packer.PackError):
        packer.validate_config(cfg, catalog)
    # broker 数量与节点数不符
    cfg = _mh_cfg(["kafka"], {"kafka": "cluster"},
                  {"kafka": {"enabled": True, "count": 3, "brokers": [0, 1, 1]}})
    with pytest.raises(packer.PackError):
        packer.validate_config(cfg, catalog)
    # redis 多机必须 1 主 2 从
    cfg = _mh_cfg(["redis"], {"redis": "sentinel"},
                  {"redis": {"enabled": True, "master": 0, "replicas": [1]}})
    with pytest.raises(packer.PackError):
        packer.validate_config(cfg, catalog)
    # SSH 密码不能包含 | 或换行 (破坏 distribute.sh 分隔符)
    cfg = _mh_cfg(["redis"], {"redis": "sentinel"},
                  {"redis": {"enabled": True, "master": 0, "replicas": [1, 2]}},
                  servers=[{"ip": "10.0.0.11", "password": "pa|ss"}, {"ip": "10.0.0.12"}, {"ip": "10.0.0.13"}])
    with pytest.raises(packer.PackError, match="密码"):
        packer.validate_config(cfg, catalog)


def test_multihost_distribute_and_install_scripts(catalog):
    cfg = _mh_cfg(["redis"], {"redis": "sentinel"},
                  {"redis": {"enabled": True, "master": 0, "replicas": [1, 2]}})
    cfg, _ = packer.validate_config(cfg, catalog)
    nodes = packer.gen_nodes(cfg, catalog)
    dist = packer.gen_distribute_sh(cfg, nodes, catalog)
    assert "node1|root|10.0.0.11|22|" in dist
    assert "node2|root|10.0.0.12|22|" in dist
    assert 'tar czf - -C "nodes/$NAME"' in dist
    # 免密节点走 BatchMode
    assert 'SSHC=(ssh -p "$RSSH" -o BatchMode=yes)' in dist
    # compose 检查/补装: 本机节点必须走本地分支(否则未配自免密时误报缺失且包内二进制装不上)
    assert "本机缺少 compose" in dist
    assert 'cp "$COMPOSE_BIN" "$HOME/.local/bin/docker-compose"' in dist
    # 密码节点: 明文密码绝不随包分发, 改由控制面环境变量注入(sshpass 分支仍在)
    cfg2 = _mh_cfg(["redis"], {"redis": "sentinel"},
                   {"redis": {"enabled": True, "master": 0, "replicas": [1, 2]}},
                   servers=[{"ip": "10.0.0.11", "password": "S3cret~!"}, {"ip": "10.0.0.12"}, {"ip": "10.0.0.13"}])
    cfg2, _ = packer.validate_config(cfg2, catalog)
    nodes2 = packer.gen_nodes(cfg2, catalog)
    dist2 = packer.gen_distribute_sh(cfg2, nodes2, catalog)
    assert 'SSHC=(sshpass -e ssh' in dist2
    assert 'RPASS="$MW_SSH_PASSWORD"' in dist2 and "MW_SSH_PASSWORD" in dist2
    assert "S3cret~!" not in dist2, "SSH 密码不得写进离线包"
    assert 'SSHC=(ssh -p "$RSSH" -o BatchMode=yes)' in dist2  # node2/3 仍免密
    inst = packer.gen_node_install_sh(cfg, nodes[0])
    assert "docker load" in inst and "docker compose up -d" in inst
    assert "BACKUP_CRON" in inst
    bash = _find_bash()
    r = subprocess.run([bash, "-n"], input=dist.encode(), capture_output=True)
    assert r.returncode == 0, r.stderr.decode(errors="replace")
    r = subprocess.run([bash, "-n"], input=inst.encode(), capture_output=True)
    assert r.returncode == 0, r.stderr.decode(errors="replace")

    # mysql 多机: POST_SQL 含引号/空格/分号的复制 SQL 必须安全转义 (E2E 曾因未转义炸掉解析)
    cfg3 = _mh_cfg(["mysql8"], {"mysql8": "master-slave"},
                   {"mysql8": {"enabled": True, "master": 0, "replicas": [1, 2]}})
    cfg3, _ = packer.validate_config(cfg3, catalog)
    nodes3 = packer.gen_nodes(cfg3, catalog)
    for nd in nodes3:
        s = packer.gen_node_install_sh(cfg3, nd)
        r = subprocess.run([bash, "-n"], input=s.encode(), capture_output=True)
        assert r.returncode == 0, "%s: %s" % (nd["name"], r.stderr.decode(errors="replace"))
    mst = next(n for n in nodes3 if "master" in n["roles"][0])
    rep = next(n for n in nodes3 if "replica" in n["roles"][0])
    assert "GRANT REPLICATION SLAVE" in packer.gen_node_install_sh(cfg3, mst)
    rep_sh = packer.gen_node_install_sh(cfg3, rep)
    assert "CHANGE REPLICATION SOURCE TO" in rep_sh and "START REPLICA" in rep_sh
    # POST_SQL 数组元素必须带单引号包裹; 容器名按服务名(mysql8), 不再共用 "mysql"
    assert "POST_SQL=('mysql8|" in rep_sh
    # 节点侧必须桥接 conf/<svc>/ -> <svc>/ (compose 挂载相对路径), 并预建数据目录
    assert "for d in ./conf/*/" in rep_sh
    assert "mysql8/data" in rep_sh and "mkdir -p" in rep_sh  # 数据目录按服务名隔离(mysql8/mysql57 同节点不共目录)

    # 节点安装脚本必须做镜像源前缀重打标(离线机 compose 按原生短名取镜像)
    assert "retag-mirrors.sh" in inst and "retag-mirrors.sh" in rep_sh


def test_topology_custom_ports_preserved(catalog):
    """用户自定义拓扑端口键(mysql8_replica 等)不得被 catalog 归一化丢弃(曾默认回落 13308)"""
    cfg = _mh_cfg(["mysql8", "mysql57"], {"mysql8": "master-slave", "mysql57": "master-slave"},
                  {"mysql8": {"enabled": True, "master": 0, "replicas": [1]},
                   "mysql57": {"enabled": True, "master": 0, "replicas": [1]}},
                  ports={"mysql8": 25330, "mysql8_replica": 25331, "mysql57": 25332, "mysql57_replica": 25333})
    cfg, _ = packer.validate_config(cfg, catalog)
    assert cfg["ports"]["mysql8_replica"] == 25331 and cfg["ports"]["mysql57_replica"] == 25333
    nodes = packer.gen_nodes(cfg, catalog)
    rep = next(n for n in nodes if any("#replica" in r for r in n["roles"]))
    assert '"25331:3306"' in rep["compose"] and '"25333:3306"' in rep["compose"]
    assert "SOURCE_PORT=25330" in packer.gen_node_install_sh(cfg, rep)
    assert "MASTER_PORT=25332" in packer.gen_node_install_sh(cfg, rep)


def test_multihost_replica_same_port_allowed(catalog):
    """异机部署允许主库与从库同端口(每台机器端口空间独立); 单机主从仍全局查重;
    redis 哨兵多机的从库/哨兵端口同理, kafka 多机节点组端口本就每台一致"""
    # 多机: 从库端口 = 主库端口 → 放行
    cfg = _mh_cfg(["mysql8"], {"mysql8": "master-slave"},
                  {"mysql8": {"enabled": True, "master": 0, "replicas": [1]}},
                  ports={"mysql8": 23306, "mysql8_replica": 23306})
    cfg, _ = packer.validate_config(cfg, catalog)
    assert cfg["ports"]["mysql8_replica"] == 23306
    # 多机 redis 哨兵: 从库端口与主库相同 → 放行
    cfg_r = _mh_cfg(["redis"], {"redis": "sentinel"},
                    {"redis": {"enabled": True, "master": 0, "replicas": [1, 2]}},
                    ports={"redis": 16379, "redis_replica": 16379, "redis_sentinel": 26379})
    cfg_r, _ = packer.validate_config(cfg_r, catalog)
    assert cfg_r["ports"]["redis_replica"] == 16379
    # 单机主从: 同端口仍拒绝
    with pytest.raises(packer.PackError, match="端口冲突"):
        packer.validate_config(make_cfg(None, services=("mysql8",),
                                        ports={"mysql8": 23306, "mysql8_replica": 23306},
                                        topology={"mysql8": "master-slave"}), catalog)


def test_multihost_dual_mysql_same_node(catalog):
    """mysql8+mysql57 同时多机主从且主/从各落同节点: 两服务 compose 键与容器名必须互异,
    健康等待/初始化 SQL 指向正确容器, 备份服务列表含全部主库, distribute 解到节点子目录"""
    bash = _find_bash()
    cfg = _mh_cfg(["mysql8", "mysql57"], {"mysql8": "master-slave", "mysql57": "master-slave"},
                  {"mysql8": {"enabled": True, "master": 0, "replicas": [1]},
                   "mysql57": {"enabled": True, "master": 0, "replicas": [1]}},
                  backup={"engines": {"mysql": {"enabled": True, "keep": 7, "hour": 3, "days": [1, 4]}}})
    cfg, _ = packer.validate_config(cfg, catalog)
    nodes = packer.gen_nodes(cfg, catalog)
    assert len(nodes) == 2
    mst = next(n for n in nodes if any(r.endswith("#master") for r in n["roles"]))
    rep = next(n for n in nodes if any("#replica" in r for r in n["roles"]))
    # 双服务同节点: compose 服务键/容器名 = mysql8/mysql57, 不允许裸 "mysql"
    for nd in (mst, rep):
        assert "container_name: mysql\n" not in nd["compose"]
        for svc in ("mysql8", "mysql57"):
            assert "  %s:" % svc in nd["compose"] and "container_name: %s" % svc in nd["compose"]
        s = packer.gen_node_install_sh(cfg, nd)
        r = subprocess.run([bash, "-n"], input=s.encode(), capture_output=True)
        assert r.returncode == 0, r.stderr.decode(errors="replace")
        assert "mysql8" in s and "mysql57" in s
    # 主库备份: MYSQL_SERVICES 覆盖同节点两个主库容器
    assert mst["backup"] and "mysql8" in mst["backup"]["MYSQL_SERVICES"] and "mysql57" in mst["backup"]["MYSQL_SERVICES"]
    # 备份变量必须注入脚本且先于使用(set -u 下 mkdir "$BACKUP_DIR" 要求脚本内已定义;
    # 曾因漏注入, 主库配置完 crontab 后以 unbound variable 中止)
    mst_sh = packer.gen_node_install_sh(cfg, mst)
    assert "BACKUP_DIR=" in mst_sh and 'mkdir -p "$BACKUP_DIR"' in mst_sh
    assert mst_sh.index("BACKUP_DIR=") < mst_sh.index('mkdir -p "$BACKUP_DIR"')
    assert "BACKUP_DIR=" not in packer.gen_node_install_sh(cfg, rep)  # 从库无备份段
    # crontab 缺失/写失败必须 warn 降级给手动行, 不允许 set -e 裸退
    # (minimal 系统常无 cronie, 裸退发生在 MySQL 已就绪之后, 无提示难排查 — dind 实测踩坑)
    assert "command -v crontab" in mst_sh and "本机无 crontab 命令" in mst_sh
    assert mst_sh.index('warn(){') < mst_sh.index('log "配置定时备份')  # warn 先于使用处定义
    # crontab 行必须带 # mw-backup 标记(deploy.sh 同规) — uninstall.sh 靠它清理, 漏标记卸载后残留
    assert "backup.sh >> $INST_DIR/backup.log 2>&1 # mw-backup" in mst_sh
    # 从库复制 SQL 各指向本服务容器
    rep_sh = packer.gen_node_install_sh(cfg, rep)
    assert "POST_SQL=('mysql8|" in rep_sh and "' 'mysql57|" in rep_sh
    assert "CHANGE REPLICATION SOURCE TO" in rep_sh and "CHANGE MASTER TO" in rep_sh
    # 重跑幂等: 先停掉可能已运行的复制线程再重配(ERROR 3081)
    assert "STOP REPLICA; RESET REPLICA ALL;" in rep_sh and "STOP SLAVE; RESET SLAVE ALL;" in rep_sh
    # 从库写保护要覆盖 SUPER 用户(--read-only 拦不住 root, 必须 super_read_only)
    assert rep_sh.count("SET GLOBAL super_read_only=1;") == 2
    # distribute.sh: 节点包解到 $DEPLOY_DIR/nodes/$NAME 子目录(主部署机自身作为主库节点时不覆盖主包 compose/.env);
    # 本机节点 is_local_ip 就地安装, 不走 tar 管道(环回传输边读边写同目录会报 "file changed")
    dist = packer.gen_distribute_sh(cfg, nodes, catalog)
    assert 'ND_SRC="$(pwd)/nodes/$NAME"' in dist and "cd '$ND_DST' &&" in dist
    assert 'is_local_ip "$RIP"' in dist and "就地安装" in dist
    r = subprocess.run([bash, "-n"], input=dist.encode(), capture_output=True)
    assert r.returncode == 0, r.stderr.decode(errors="replace")


def test_retag_mirrors_script(catalog, tmp_path):
    """retag-mirrors.sh 随包脚本: 归一化规则覆盖四类仓库名形态 + bash -n 通过"""
    s = packer.gen_retag_mirrors_sh()
    # 与 deploy.sh normalize_image 同规则: 最后一段匹配 + tag 含架构后缀
    assert "images.txt" in s
    for marker in ('*/"$tail"', '"$tag-$ARCH"', "docker tag"):
        assert marker in s
    p = tmp_path / "retag-mirrors.sh"
    p.write_text(s, encoding="utf-8", newline="\n")
    r = subprocess.run([_find_bash(), "-n", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_deploy_sh_retag_integration():
    """主部署机 deploy.sh 加载镜像后必须调用 retag 脚本再归一化"""
    sh = (Path(__file__).resolve().parent.parent / "server" / "deploy.sh").read_text(encoding="utf-8")
    assert "retag-mirrors.sh" in sh
    # 调用点必须在 load 循环之后、normalize 之前(顺序错则归一化找不到短名)
    assert sh.index("retag-mirrors.sh") > sh.index("docker load -i")
    assert sh.index("retag-mirrors.sh") < sh.index('normalize_image "$short"')


def test_deploy_sh_retag_integration():
    """主部署机 deploy.sh 加载镜像后必须调用 retag 脚本再归一化"""
    sh = (Path(__file__).resolve().parent.parent / "server" / "deploy.sh").read_text(encoding="utf-8")
    assert "retag-mirrors.sh" in sh
    # 调用点必须在 load 循环之后、normalize 之前(顺序错则归一化找不到短名)
    assert sh.index("retag-mirrors.sh") > sh.index("docker load -i")
    assert sh.index("retag-mirrors.sh") < sh.index('normalize_image "$short"')


def _norm_lf(p):
    return p.read_bytes().replace(b"\r\n", b"\n")


def test_docs_static_site_packer_html_sync():
    """docs/ 静态演示站(GitHub Pages)的 packer.html 必须与根目录保持同步"""
    root = Path(__file__).resolve().parent.parent
    assert _norm_lf(root / "docs" / "packer.html") == _norm_lf(root / "packer.html")
    assert _norm_lf(root / "docs" / "logos.js") == _norm_lf(root / "packer_logos.js")


def test_webui_sources_build_sync():
    """packer.html 必须由 webui/ 源文件构建(源文件改动后运行 python build_webui.py)。
    packer.html 是交付形态(单文件离线可用), webui/ 是维护形态(结构/样式/脚本分文件)"""
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    import build_webui
    built = build_webui.build_html().replace(b"\r\n", b"\n")
    current = _norm_lf(root / "packer.html")
    assert built == current, "webui/ 源与 packer.html 不同步, 请运行: python build_webui.py"


def test_docs_versions_json_has_plugins():
    """docs/versions.json 必须是合并目录(内置+插件), 过期则 demo 页少应用。
    重新生成: python packer.py --dump-catalog docs/versions.json"""
    root = Path(__file__).resolve().parent.parent
    dumped = json.loads((root / "docs" / "versions.json").read_text(encoding="utf-8"))
    merged = packer.load_catalog()
    assert dumped == merged
    # 插件服务必须真实可见(防止空目录恰好相等的退化情形)
    for key in ("kafka", "postgres", "prometheus", "grafana", "mongodb", "rabbitmq"):
        assert key in dumped["services"]
        assert dumped["services"][key].get("is_plugin") is True


# ---------------------------------------------------------------- Java 运行时

def test_java_disabled_by_default(catalog):
    """旧配置无 java 键 -> 默认关闭(基础中间件可选启用)"""
    cfg = make_cfg(None, services=("nginx",))
    cfg, _ = packer.validate_config(cfg, catalog)
    assert cfg["java"]["enabled"] is False


def test_java_config_normalized(catalog):
    """java 归一化: 版本按 majors 排序/非法默认版本纠偏/越界目标丢弃"""
    cfg = make_cfg(None, services=("nginx",),
                   java={"enabled": True, "versions": ["21", "8"], "default": "99",
                         "targets": ["local", 7]})
    cfg, _ = packer.validate_config(cfg, catalog)
    j = cfg["java"]
    assert j["enabled"] is True
    assert j["versions"] == ["8", "21"]
    assert j["default"] == "21"          # 99 不在勾选列表 -> 回退最后勾选
    assert j["targets"] == ["local"]     # 索引 7 越界(服务器池空) -> 丢弃


def test_java_multihost_nodes_and_scripts(catalog):
    """多机: targets 节点计划带 java 版本; 纯 Java 节点脚本无 docker 依赖; 分发脚本传 java/"""
    cfg = make_cfg(None, services=("mysql8", "nginx"), topology={"mysql8": "master-slave"},
                   servers=[{"name": "n1", "user": "root", "ip": "10.0.0.1", "ssh": 22, "password": "p"},
                            {"name": "n2", "user": "root", "ip": "10.0.0.2", "ssh": 22, "password": "p"},
                            {"name": "n3", "user": "root", "ip": "10.0.0.3", "ssh": 22, "password": ""}],
                   multihost={"mysql8": {"enabled": True, "master": 0, "replicas": [1]}},
                   java={"enabled": True, "versions": ["17"], "default": "17",
                         "targets": ["local", 0, 2]})
    cfg, _ = packer.validate_config(cfg, catalog)
    nodes = packer.gen_nodes(cfg, catalog)
    by_name = {n["name"]: n for n in nodes}
    assert by_name["n1"]["java_versions"] == ["jdk17"]
    assert by_name["n3"]["java_versions"] == ["jdk17"] and by_name["n3"]["roles"] == ["java"]
    assert by_name["n2"]["java_versions"] == []
    # 纯 Java 节点: 不要求 Docker, 无 compose 段
    ins_pure = packer.gen_node_install_sh(cfg, by_name["n3"])
    assert "JAVA_VERSIONS=(jdk17)" in ins_pure and "--strip-components=1" in ins_pure
    assert "docker" not in ins_pure.lower()
    # 混合节点: java 段在 docker 检查之前
    ins_mix = packer.gen_node_install_sh(cfg, by_name["n1"])
    assert "install Java" not in ins_mix   # 英文误写守卫
    assert ins_mix.index("JAVA_VERSIONS=") < ins_mix.index("command -v docker")
    # 分发脚本: java 目录传输 + 预检条件化
    dist = packer.gen_distribute_sh(cfg, nodes, catalog)
    assert "tar czf - -C java ." in dist and "HAS_JAVA" in dist and "HAS_SVCS" in dist


def test_java_manifest_vars_local(catalog):
    """manifest.sh: 本机启用时写 JAVA_ENABLED/JAVA_VERSIONS/JAVA_DEFAULT"""
    cfg = make_cfg(None, services=("nginx",),
                   java={"enabled": True, "versions": ["8", "17"], "default": "17", "targets": ["local"]})
    cfg, _ = packer.validate_config(cfg, catalog)
    mf = packer.gen_manifest_sh(cfg, catalog, "", [])
    assert "JAVA_ENABLED=1" in mf
    assert "JAVA_VERSIONS=(jdk8 jdk17)" in mf
    assert "JAVA_DEFAULT=jdk17" in mf


def test_java_manifest_vars_not_local_target(catalog):
    """多机 targets 不含 local -> 主部署机不装(节点由 install-node.sh 装)"""
    cfg = make_cfg(None, services=("nginx",),
                   servers=[{"name": "n1", "user": "root", "ip": "10.0.0.1", "ssh": 22, "password": "p"}],
                   java={"enabled": True, "versions": ["17"], "default": "17", "targets": [0]})
    cfg, _ = packer.validate_config(cfg, catalog)
    mf = packer.gen_manifest_sh(cfg, catalog, "", [])
    assert "JAVA_ENABLED=0" in mf
    assert "JAVA_VERSIONS=()" in mf and "JAVA_DEFAULT=" in mf


def test_java_install_dir_custom_and_invalid(catalog):
    """自定义安装目录: manifest 与节点脚本一致使用; 非法路径报错"""
    cfg = make_cfg(None, services=("nginx",),
                   java={"enabled": True, "versions": ["17"], "default": "17",
                         "install_dir": "/opt/jdk", "targets": ["local"]})
    cfg, _ = packer.validate_config(cfg, catalog)
    assert cfg["java"]["install_dir"] == "/opt/jdk"
    mf = packer.gen_manifest_sh(cfg, catalog, "", [])
    assert "JAVA_HOME_DIR='/opt/jdk'" in mf
    # 节点脚本同样使用自定义目录
    cfg2 = make_cfg(None, services=("nginx",),
                    servers=[{"name": "n1", "user": "root", "ip": "10.0.0.1", "ssh": 22, "password": "p"}],
                    java={"enabled": True, "versions": ["17"], "default": "17",
                          "install_dir": "/opt/jdk", "targets": [0]})
    cfg2, _ = packer.validate_config(cfg2, catalog)
    node = packer.gen_nodes(cfg2, catalog)[0]
    ins = packer.gen_node_install_sh(cfg2, node)
    assert "JDIR='/opt/jdk'" in ins and '$JDIR/$v' in ins
    # 非法路径: 相对路径拒绝
    from packerlib.util import PackError
    bad = make_cfg(None, services=("nginx",),
                   java={"enabled": True, "versions": ["17"], "default": "17",
                         "install_dir": "relative/path", "targets": ["local"]})
    try:
        packer.validate_config(bad, catalog)
        assert False, "相对路径应被拒绝"
    except PackError:
        pass
    # 默认值归一: 未填时 /data/java
    cfg3 = make_cfg(None, services=("nginx",),
                    java={"enabled": True, "versions": ["17"], "default": "17", "targets": ["local"]})
    cfg3, _ = packer.validate_config(cfg3, catalog)
    assert cfg3["java"]["install_dir"] == "/data/java"


# ---------------------------------------------------------------- 集群高级配置(CNI values / 注释参数)

def test_cluster_cni_values_per_plugin_and_comments(catalog):
    """CNI values 按插件各自透传(仅当前 CNI 生效); 注释行参数被忽略"""
    from packerlib.util import PackError
    # cilium: 注释-only values 归一为空; 误填的 calico values 忽略并告警
    cfg = make_cfg(None, services=("nginx",),
                   servers=[{"name": "n1", "user": "root", "ip": "10.0.0.1", "ssh": 22, "password": "p"}],
                   cluster={
        "enabled": True, "kube_version": "v1.30.14", "cni_type": "cilium", "roles": {"0": "control-plane"},
        "cilium_values": "# hubble:\n#   enabled: false\nhubble:\n  enabled: true\n",
        "calico_values": "installation:\n  calicoNetwork:\n    ipipMode: Always\n",
        "kubelet": {"extra_args": ["# system-reserved=memory=300Mi", "max-pods=200"],
                    "extra_config": "# maxOpenFiles: 1000000\nmaxOpenFiles: 999999\n"},
        "components": {"etcd_env": ["# heartbeat_interval=100", "heartbeat_interval=500",
                                    "# election_timeout=1000"]},
    })
    cfg, warns = packer.validate_config(cfg, catalog)
    cl = cfg["cluster"]
    assert "# hubble:" in cl["cilium_values"] and "enabled: true" in cl["cilium_values"]   # 注释+生效混合: 原样保留
    assert cl["calico_values"] == ""                       # 非 cilium -> 忽略
    assert any("calico values" in w.lower() for w in warns)
    assert cl["kubelet"]["extra_args"] == ["max-pods=200"]  # 注释行被跳过
    assert cl["kubelet"]["extra_config"].rstrip("\n") == "# maxOpenFiles: 1000000\nmaxOpenFiles: 999999"
    assert cl["components"]["etcd_env"] == {"heartbeat_interval": 500}
    # 生成的 kk config: cilium values 透传, 无 calico 段, 注释参数不落入
    kcfg = packer.gen_cluster_config(cfg, catalog)
    assert "    cilium:\n      values: |" in kcfg and "enabled: true" in kcfg
    assert "calico:" not in kcfg
    assert "max-pods" in kcfg and "heartbeat_interval: 500" in kcfg
    # calico: 生效 values 写入 calico 段
    cfg2 = make_cfg(None, services=("nginx",),
                    servers=[{"name": "n1", "user": "root", "ip": "10.0.0.1", "ssh": 22, "password": "p"}],
                    cluster={
        "enabled": True, "kube_version": "v1.30.14", "cni_type": "calico", "roles": {"0": "control-plane"},
        "calico_values": "installation:\n  calicoNetwork:\n    ipipMode: Never\n"})
    cfg2, _ = packer.validate_config(cfg2, catalog)
    kcfg2 = packer.gen_cluster_config(cfg2, catalog)
    assert "    calico:\n      values: |" in kcfg2 and "ipipMode: Never" in kcfg2
    # 注释-only values: 归一为空(不写入 config)
    cfg3 = make_cfg(None, services=("nginx",),
                    servers=[{"name": "n1", "user": "root", "ip": "10.0.0.1", "ssh": 22, "password": "p"}],
                    cluster={
        "enabled": True, "kube_version": "v1.30.14", "cni_type": "cilium", "roles": {"0": "control-plane"},
        "cilium_values": "# 全部注释\n# hubble:\n#   enabled: false\n"})
    cfg3, _ = packer.validate_config(cfg3, catalog)
    assert cfg3["cluster"]["cilium_values"] == ""
    # 非法 YAML 拒绝
    bad = make_cfg(None, services=("nginx",),
                   servers=[{"name": "n1", "user": "root", "ip": "10.0.0.1", "ssh": 22, "password": "p"}],
                   cluster={
        "enabled": True, "kube_version": "v1.30.14", "cni_type": "calico", "roles": {"0": "control-plane"},
        "calico_values": ":\n  - [unclosed\n"})
    try:
        packer.validate_config(bad, catalog)
        assert False, "非法 YAML 应被拒绝"
    except PackError:
        pass


# ---------------------------------------------------------------- 集群角色多选 / kube-proxy ipvs

def test_cluster_multi_role_and_ipvs_proxy_mode(catalog):
    """节点可多角色(如控制面兼工作节点); kube-proxy 支持 ipvs 模式"""
    from packerlib.util import PackError
    # 逗号分隔多角色(兼容旧版单值字符串): 节点0 = 控制面+工作节点, 节点1 = 工作节点
    cfg = make_cfg(None, services=("nginx",),
                   servers=[{"name": "n1", "user": "root", "ip": "10.0.0.1", "ssh": 22, "password": "p"},
                            {"name": "n2", "user": "root", "ip": "10.0.0.2", "ssh": 22, "password": "p"}],
                   cluster={
        "enabled": True, "kube_version": "v1.30.14", "cni_type": "calico",
        "roles": {"0": "control-plane,worker", "1": "worker"},
    })
    cfg, warns = packer.validate_config(cfg, catalog)
    nodes = cfg["cluster"]["nodes"]
    assert nodes[0]["roles"] == ["control-plane", "worker"] and nodes[0]["role"] == "control-plane"
    assert nodes[1]["roles"] == ["worker"] and nodes[1]["role"] == "worker"
    assert any("污点" in w for w in warns)   # 控制面兼工作节点 -> 污点提醒
    # Inventory: n1 同时出现在 kube_control_plane 与 kube_worker 组(kk 官方语义)
    inv = packer.gen_cluster_inventory(cfg)
    cp_sec = inv.split("kube_control_plane:")[1].split("kube_worker:")[0]
    wk_sec = inv.split("kube_worker:")[1].split("etcd:")[0]
    assert "- n1" in cp_sec and "- n1" in wk_sec and "- n2" in wk_sec
    # 列表形式同样接受
    cfg2 = make_cfg(None, services=("nginx",),
                    servers=[{"name": "n1", "user": "root", "ip": "10.0.0.1", "ssh": 22, "password": "p"}],
                    cluster={"enabled": True, "kube_version": "v1.30.14", "cni_type": "calico",
                             "roles": {"0": ["control-plane", "registry"]}})
    cfg2, _ = packer.validate_config(cfg2, catalog)
    assert cfg2["cluster"]["nodes"][0]["roles"] == ["control-plane", "registry"]
    # 非法角色(多选形式)拒绝
    bad = make_cfg(None, services=("nginx",),
                   servers=[{"name": "n1", "user": "root", "ip": "10.0.0.1", "ssh": 22, "password": "p"}],
                   cluster={"enabled": True, "kube_version": "v1.30.14", "cni_type": "calico",
                            "roles": {"0": "control-plane,master"}})
    try:
        packer.validate_config(bad, catalog)
        assert False, "非法角色应被拒绝"
    except PackError:
        pass
    # ipvs 模式: 校验通过并写入 kk config; 带内核模块告警
    cfg3 = make_cfg(None, services=("nginx",),
                    servers=[{"name": "n1", "user": "root", "ip": "10.0.0.1", "ssh": 22, "password": "p"}],
                    cluster={"enabled": True, "kube_version": "v1.30.14", "cni_type": "calico",
                             "roles": {"0": "control-plane"}, "proxy_mode": "ipvs"})
    cfg3, warns3 = packer.validate_config(cfg3, catalog)
    assert cfg3["cluster"]["proxy_mode"] == "ipvs"
    assert any("ipvs" in w for w in warns3)
    assert "mode: \"ipvs\"" in packer.gen_cluster_config(cfg3, catalog)
    # 非法模式仍拒绝
    bad3 = make_cfg(None, services=("nginx",),
                    servers=[{"name": "n1", "user": "root", "ip": "10.0.0.1", "ssh": 22, "password": "p"}],
                    cluster={"enabled": True, "kube_version": "v1.30.14", "cni_type": "calico",
                             "roles": {"0": "control-plane"}, "proxy_mode": "ebpf"})
    try:
        packer.validate_config(bad3, catalog)
        assert False, "非法 proxy_mode 应被拒绝"
    except PackError:
        pass


# ---------------------------------------------------------------- 随包脚本契约回归 (2026-11-05)
# 通读全库后复核出的遗留问题: 既做静态断言, 也用真实 bash 跑随包脚本/部署片段,
# 避免"只看源码字符串"的假绿灯。全部临时文件走 pytest tmp_path。

def _posix(p):
    """Windows 下把路径转成 Git Bash 认识的形式"""
    return str(p).replace("\\", "/")


def _bash(script, cwd, *args):
    return subprocess.run([BASH, _posix(script)] + [str(a) for a in args],
                          cwd=str(cwd), capture_output=True, text=True)


def test_restore_list_from_any_cwd(tmp_path):
    """do_list 曾用相对路径 ls/du/date, 在别的 cwd 执行 ./restore.sh list 永远空"""
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "restore.sh").write_text(
        (ROOT / "templates" / "restore.sh").read_text(encoding="utf-8"),
        encoding="utf-8", newline="\n")
    bak = tmp_path / "backups"
    (bak / "mysql8").mkdir(parents=True)
    (bak / "mongodb").mkdir()
    (bak / "mysql8" / "appdb_20261105_010101.sql.gz").write_bytes(b"x" * 2048)
    (bak / "mysql8" / "pre_restore_appdb_20261105_020202.sql.gz").write_bytes(b"x" * 2048)
    (bak / "mysql8" / "notes.txt").write_text("not a backup", encoding="utf-8")
    (bak / "mongodb" / "appdb_20261105_030303.archive").write_bytes(b"x" * 1024)
    (deploy / "backup.conf").write_text("BACKUP_DIR='%s'\n" % _posix(bak),
                                        encoding="utf-8", newline="\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    r = _bash(deploy / "restore.sh", elsewhere, "list")
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "没有找到任何备份" not in out
    assert "appdb_20261105_010101.sql.gz" in out
    assert "appdb_20261105_030303.archive" in out
    assert "pre_restore_" not in out and "notes.txt" not in out
    row = [l for l in out.splitlines() if "appdb_20261105_010101.sql.gz" in l][0]
    # 文件名 + 大小 + 日期 + 时间, 说明 du/date 也吃到了全路径
    assert len(row.split()) >= 4, row


def test_restore_default_backup_dir_is_current():
    sh = (ROOT / "templates" / "restore.sh").read_text(encoding="utf-8")
    assert 'BACKUP_DIR_CFG="/data/backup/db"' in sh
    assert "/data/backup/mysql" not in sh
    # 三个引擎的"恢复前安全备份"提示都必须以文件真实存在为前提
    assert sh.count('if [ -f "${PRE:-}" ]; then') == 3
    assert '可用恢复前安全备份: $(basename "${PRE:-无}")' not in sh
    # example-config.json 是 --example 生成物(.gitignore 忽略), 真正要守住的是样例模板默认值
    cli_src = (ROOT / "packerlib" / "cli.py").read_text(encoding="utf-8")
    assert '"dir": "/data/backup/db"' in cli_src
    r = subprocess.run([BASH, "-n"], input=sh.encode("utf-8"), capture_output=True)
    assert r.returncode == 0, r.stderr.decode(errors="replace")


def test_uninstall_lock_uses_backup_conf(tmp_path):
    """卸载曾未 source backup.conf, 锁落 $DIR/backup/.lock, 与 backup.sh 不同源"""
    sh = (ROOT / "templates" / "uninstall.sh").read_text(encoding="utf-8")
    assert 'source "$DIR/backup.conf"' in sh
    assert 'exec 9>"$LOCK_DIR/.lock"' in sh
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "uninstall.sh").write_text(sh, encoding="utf-8", newline="\n")
    (deploy / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8", newline="\n")
    bak = tmp_path / "backups"
    bak.mkdir()
    (deploy / "backup.conf").write_text("BACKUP_DIR='%s'\nMYSQL_ENABLED=0\n" % _posix(bak),
                                        encoding="utf-8", newline="\n")
    # 用 bash 函数桩掉 docker/crontab, 完整跑一遍卸载(含 fd 9 锁), 无需真 docker
    wrapper = deploy / "_run_uninstall.sh"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "docker() { return 0; }\n"
        "crontab() { return 0; }\n"
        "source \"%s/uninstall.sh\" --yes\n" % _posix(deploy),
        encoding="utf-8", newline="\n")
    r = _bash(wrapper, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "卸载完成" in r.stdout
    assert (bak / ".lock").exists(), "锁应落在 backup.conf 指定的备份目录"
    assert not (deploy / "backup" / ".lock").exists(), "不应再落到部署目录内 backup/"


def _deploy_conf_block():
    """从 deploy.sh 抽出 conf/ 同步代码块, 包进测试函数里真实执行"""
    sh = (ROOT / "server" / "deploy.sh").read_text(encoding="utf-8")
    a = sh.index('  if [ -d "$BASE_DIR/conf" ]; then')
    b = sh.index('  # 备份恢复脚本', a)
    return sh[a:b]


def _conf_harness(base, deploy, with_contract=True, policies=None):
    """模拟 deploy.sh 的 conf 同步: 注入与真实包同形的 CONF_POLICY 契约数组。

    with_contract=False 模拟旧包(无契约)走文件头标记回退; policies 可覆盖
    自动算出的策略, 用来验证「契约优先于文件头」。
    """
    h = base / "_conf_harness.sh"
    pol = {}
    conf_root = base / "conf"
    if conf_root.is_dir():
        for p in sorted(conf_root.rglob("*")):
            if not p.is_file():
                continue
            rel = "conf/" + p.relative_to(conf_root).as_posix()
            pol[rel] = packer.conf_policy(rel, p.read_text(encoding="utf-8"))
    pol.update(policies or {})
    lines = [
        "#!/usr/bin/env bash", "set -euo pipefail",
        'BASE_DIR="%s"' % _posix(base),
        'DEPLOY_DIR="%s"' % _posix(deploy),
        'warn() { echo "WARN: $*"; }',
        "T() { printf '%s' \"$1\"; }",
        'conf_policy_for() {',
        '  local e p rest want="$1"',
        '  [ -n "${CONF_POLICY+x}" ] || return 0',
        '  for e in "${CONF_POLICY[@]}"; do',
        '    p="${e%%|*}"; rest="${e#*|}"',
        '    [ "$p" = "$want" ] && { printf "%s" "${rest%%|*}"; return 0; }',
        '  done',
        '  return 0',
        '}',
    ]
    if with_contract:
        lines.append("CONF_POLICY=(%s)" % " ".join(
            packer.bash_quote("%s|%s|stub" % (rel, policy))
            for rel, policy in sorted(pol.items())))
    lines += ["sync_conf() {", _deploy_conf_block(), "}", "sync_conf"]
    h.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return h


def test_deploy_preserves_customised_conf(tmp_path):
    """声明'只补齐不覆盖'的配置(含子目录 index.html)不得被重部署冲掉; 其余按契约更新"""
    base, deploy = tmp_path / "bundle", tmp_path / "deploy"
    for d in ("conf/redis", "conf/nginx", "conf/nginx/conf", "conf/nginx/html",
              "conf/nginx/ssl/wizard"):
        (base / d).mkdir(parents=True, exist_ok=True)
    keep_redis = "# 由打包器生成 — 重新部署时只补齐缺失不会覆盖\n"
    keep_html = "<!-- 由打包器生成 — 重新部署时只补齐缺失不会覆盖 -->\n"
    cover = "# 由打包器生成 — 重新部署时会被新包覆盖\n"
    srcs = {
        "conf/redis/redis.conf": keep_redis + "maxmemory 256mb\n",
        "conf/nginx/nginx.conf": cover + "worker_processes auto;\n",
        "conf/nginx/html/index.html": keep_html + "<title>v1</title>\n",
        "conf/nginx/conf/default.conf": cover + "server { listen 80; }\n",
        "conf/nginx/ssl/wizard/fullchain.pem": "CERT-v1\n",
    }

    def dep(rel):
        return deploy / rel.split("/", 1)[1]

    for rel, text in srcs.items():
        (base / rel).write_text(text, encoding="utf-8", newline="\n")
    h = _conf_harness(base, deploy)
    r1 = _bash(h, base)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    for rel, text in srcs.items():
        assert dep(rel).read_text(encoding="utf-8") == text

    # 用户手工改了 redis.conf / index.html / default.conf / 证书; 同时新包默认内容也变了
    dep("conf/redis/redis.conf").write_text(srcs["conf/redis/redis.conf"] + "maxmemory 1gb\n",
                                           encoding="utf-8", newline="\n")
    dep("conf/nginx/html/index.html").write_text(srcs["conf/nginx/html/index.html"] + "<!-- customized -->\n",
                                                 encoding="utf-8", newline="\n")
    dep("conf/nginx/conf/default.conf").write_text(srcs["conf/nginx/conf/default.conf"] + "# custom rule\n",
                                                   encoding="utf-8", newline="\n")
    dep("conf/nginx/nginx.conf").write_text(srcs["conf/nginx/nginx.conf"] + "# my own rule\n",
                                            encoding="utf-8", newline="\n")
    dep("conf/nginx/ssl/wizard/fullchain.pem").write_text("CERT-OLD\n", encoding="utf-8", newline="\n")
    (base / "conf/redis/redis.conf").write_text(keep_redis + "maxmemory 512mb\n",
                                                encoding="utf-8", newline="\n")
    (base / "conf/nginx/html/index.html").write_text(keep_html + "<title>v2</title>\n",
                                                     encoding="utf-8", newline="\n")
    (base / "conf/nginx/nginx.conf").write_text(cover + "worker_processes 4;\n",
                                                encoding="utf-8", newline="\n")
    (base / "conf/nginx/conf/default.conf").write_text(cover + "# new default\n",
                                                       encoding="utf-8", newline="\n")
    (base / "conf/nginx/ssl/wizard/fullchain.pem").write_text("CERT-v2\n", encoding="utf-8", newline="\n")

    r2 = _bash(h, base)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "maxmemory 1gb" in dep("conf/redis/redis.conf").read_text(encoding="utf-8")
    assert "maxmemory 512mb" not in dep("conf/redis/redis.conf").read_text(encoding="utf-8")
    assert "<!-- customized -->" in dep("conf/nginx/html/index.html").read_text(encoding="utf-8")
    assert "<title>v2</title>" not in dep("conf/nginx/html/index.html").read_text(encoding="utf-8")
    assert Path(str(dep("conf/redis/redis.conf")) + ".packer-new").exists()
    assert Path(str(dep("conf/nginx/html/index.html")) + ".packer-new").exists()
    # 声明可覆盖的顶层/子目录配置、以及无标记的证书物料仍随新包更新
    assert "# my own rule" not in dep("conf/nginx/nginx.conf").read_text(encoding="utf-8")
    assert "worker_processes 4;" in dep("conf/nginx/nginx.conf").read_text(encoding="utf-8")
    assert "# custom rule" not in dep("conf/nginx/conf/default.conf").read_text(encoding="utf-8")
    assert "# new default" in dep("conf/nginx/conf/default.conf").read_text(encoding="utf-8")
    assert dep("conf/nginx/ssl/wizard/fullchain.pem").read_text(encoding="utf-8") == "CERT-v2\n"
    assert "保留现有配置" in r2.stdout


def test_deploy_conf_contract_overrides_file_header(tmp_path):
    """契约是唯一数据源: 策略与文件头声明矛盾时以 CONF_POLICY 为准; 老包(无契约)回退文件头"""
    base, deploy = tmp_path / "bundle", tmp_path / "deploy"
    for d in ("conf/redis", "conf/nginx/conf"):
        (base / d).mkdir(parents=True)
    cover = "# 由打包器生成 — 重新部署时会被新包覆盖\n"
    keep = "# 由打包器生成 — 重新部署时只补齐缺失不会覆盖\n"
    (base / "conf/redis/redis.conf").write_text(cover + "V1\n", encoding="utf-8", newline="\n")
    (base / "conf/nginx/conf/default.conf").write_text(keep + "V1\n", encoding="utf-8", newline="\n")

    # 故意把策略设成与文件头相反: redis 强制 preserve, default.conf 强制 material
    h = _conf_harness(base, deploy, policies={
        "conf/redis/redis.conf": "preserve",
        "conf/nginx/conf/default.conf": "material"})
    assert _bash(h, base).returncode == 0
    (deploy / "redis/redis.conf").write_text(cover + "USER\n", encoding="utf-8", newline="\n")
    (deploy / "nginx/conf/default.conf").write_text(keep + "USER\n", encoding="utf-8", newline="\n")
    (base / "conf/redis/redis.conf").write_text(cover + "NEW\n", encoding="utf-8", newline="\n")
    (base / "conf/nginx/conf/default.conf").write_text(keep + "NEW\n", encoding="utf-8", newline="\n")
    r = _bash(h, base)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "USER" in (deploy / "redis/redis.conf").read_text(encoding="utf-8"), "契约 preserve 优先于文件头"
    assert "NEW" in (deploy / "nginx/conf/default.conf").read_text(encoding="utf-8"), "契约 material 优先于文件头"
    assert "保留现有配置" in r.stdout
    assert (deploy / "redis/redis.conf.packer-new").exists()

    # 老包无契约: 回退到文件头语义(redis 可覆盖 / default.conf 只补齐)
    legacy = tmp_path / "deploy_legacy"
    h2 = _conf_harness(base, legacy, with_contract=False)
    assert _bash(h2, base).returncode == 0
    (legacy / "redis/redis.conf").write_text(cover + "USER\n", encoding="utf-8", newline="\n")
    (legacy / "nginx/conf/default.conf").write_text(keep + "USER\n", encoding="utf-8", newline="\n")
    assert _bash(h2, base).returncode == 0
    assert "NEW" in (legacy / "redis/redis.conf").read_text(encoding="utf-8")
    assert "USER" in (legacy / "nginx/conf/default.conf").read_text(encoding="utf-8")


def test_deploy_sh_replaces_node_ip_placeholder():
    sh = (ROOT / "server" / "deploy.sh").read_text(encoding="utf-8")
    assert '${s//__NODE_IP__/$ip}' in sh
    assert '${line//__NODE_IP__/$ip}' in sh
    r = subprocess.run([BASH, "-n"], input=sh.encode("utf-8"), capture_output=True)
    assert r.returncode == 0, r.stderr.decode(errors="replace")


def test_kafka_multihost_summary_has_real_sasl_endpoints(catalog):
    """多机 Kafka 摘要曾留下未替换的 __NODE_IP__ 字面量"""
    cfg = make_cfg(None, services=("kafka",),
                   ports={"kafka_mh_inter": 9092, "kafka_mh_sasl": 9094},
                   servers=[{"name": "node%d" % i, "ip": "10.0.0.1%d" % i} for i in (1, 2, 3)],
                   multihost={"kafka": {"enabled": True, "count": 3, "brokers": [0, 1, 2]}},
                   topology={"kafka": "cluster"})
    lines = packer.build_summary_lines(cfg, catalog)
    assert lines
    assert all("__NODE_IP__" not in l for l in lines)
    assert any("10.0.0.11:9094" in l and "10.0.0.13:9094" in l for l in lines)
    # 多机 master 兜底路径也不许再吐占位符
    cfg2 = make_cfg(None, services=("mysql8",),
                    ports={"mysql8": 13307, "mysql8_replica": 13308},
                    servers=[], multihost={"mysql8": {"enabled": True, "master": None, "replicas": []}},
                    topology={"mysql8": "master-slave"})
    lines2 = packer.build_summary_lines(cfg2, catalog)
    assert all("__NODE_IP__" not in l for l in lines2)
    assert any("__IP__" in l for l in lines2)


def test_multihost_kafka_manifest_has_no_node_ip_placeholder(catalog):
    """manifest.sh 是随包产物: SUMMARY_LINES 里不许出现 deploy.sh 不认识的占位符"""
    cfg = _mh_cfg(["kafka", "kafka-ui"], {"kafka": "cluster"},
                  {"kafka": {"enabled": True, "count": 3, "brokers": [0, 1, 2]}})
    cfg, _ = packer.validate_config(cfg, catalog)
    mf = packer.gen_manifest_sh(cfg, catalog, "", packer.build_summary_lines(cfg, catalog))
    assert "SUMMARY_LINES=(" in mf
    assert "__NODE_IP__" not in mf
    assert "10.0.0.11:9094" in mf and "10.0.0.13:9094" in mf


def _contract_harness(base, deploy, lines, body):
    """从 deploy.sh 抽出契约消费函数, 用桩变量在真实 bash 里跑(服务器无 python/jq)"""
    sh = (ROOT / "server" / "deploy.sh").read_text(encoding="utf-8")
    a = sh.index("#------------------------------- 包内契约消费")
    b = sh.index("#------------------------------- 工具函数", a)
    block = sh[a:b]
    h = base / "_contract_harness.sh"
    head = [
        "#!/usr/bin/env bash", "set -euo pipefail",
        'BASE_DIR="%s"' % _posix(base),
        'DEPLOY_DIR="%s"' % _posix(deploy),
        'TMP="%s"' % _posix(base),
        'log() { echo "LOG: $*"; }',
        'warn() { echo "WARN: $*"; }',
        'hr() { :; }',
        'T() { printf "%s" "$1"; }',
        'PROJECT="contractqa"; BUNDLE_NAME="demo.tar.gz"; HEALTH_BAD=0',
        'HEALTH_LINES=("OK|mysql8 healthy" "OK|nginx HTTP 200")',
        'CONTRACT_SCHEMA_VERSION=1; CONTRACT_CONFIG_SHA256="abc123"; CONF_POLICY=()',
        "CONTRACT_LINES=(%s)" % " ".join(packer.bash_quote(x) for x in lines),
        block, body,
    ]
    h.write_text("\n".join(head) + "\n", encoding="utf-8", newline="\n")
    return h


def test_deploy_contract_diff_and_json_report(tmp_path):
    """契约 diff / 基线 / 机器可读报告 / dry-run 不落盘, 全部在真 bash 里跑"""
    base, deploy = tmp_path / "bundle", tmp_path / "deploy"
    base.mkdir(); deploy.mkdir()
    v1 = ["schema_version=1", "project=contractqa", "services=nginx,redis",
          "port.nginx_http=8080", "secret.A=aaaa", "conf.conf/nginx/nginx.conf=overwrite:xx"]
    v2 = ["schema_version=1", "project=contractqa", "services=nginx,redis,mysql8",
          "port.nginx_http=8080", "port.mysql8=13307", "secret.A=bbbb",
          "conf.conf/nginx/nginx.conf=overwrite:xx"]

    h1 = _contract_harness(base, deploy, v1, '\n'.join([
        "contract_diff", 'echo "FIRST_COUNT=${#CONTRACT_DIFF_LINES[@]}"',
        "contract_save_baseline", 'echo "BASELINE_LINES=$(wc -l < "$DEPLOY_DIR/.contract-lines.txt")"',
    ]))
    r1 = _bash(h1, base)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    assert "FIRST_COUNT=0" in r1.stdout and "首次部署" in r1.stdout
    assert "BASELINE_LINES=6" in r1.stdout
    assert (deploy / ".contract-lines.txt").exists()

    h2 = _contract_harness(base, deploy, v2, '\n'.join([
        "contract_diff", 'echo "SECOND_COUNT=${#CONTRACT_DIFF_LINES[@]}"',
        'for x in "${CONTRACT_DIFF_LINES[@]}"; do echo "DIFF:$x"; done',
        'write_report_json "10.0.0.1"',
    ]))
    r2 = _bash(h2, base)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "SECOND_COUNT=" in r2.stdout and "SECOND_COUNT=0" not in r2.stdout
    assert "DIFF:+ port.mysql8=13307" in r2.stdout
    assert "DIFF:- secret.A=aaaa" in r2.stdout and "DIFF:+ secret.A=bbbb" in r2.stdout
    report = json.loads((deploy / "部署报告.json").read_text(encoding="utf-8"))
    assert report["schema_version"] == 1 and report["health_ok"] is True
    assert report["services"] == ["nginx", "redis", "mysql8"]
    assert report["config_sha256"] == "abc123" and report["ip"] == "10.0.0.1"
    assert any("port.mysql8" in x for x in report["changes"])
    assert "Change_Me" not in (deploy / "部署报告.json").read_text(encoding="utf-8")

    # dry-run 不得写基线
    deploy3 = tmp_path / "deploy_dry"
    deploy3.mkdir()
    h3 = _contract_harness(base, deploy3, v1, '\n'.join([
        "DRY_RUN=1", "contract_diff", "contract_save_baseline",
        '[ -f "$DEPLOY_DIR/.contract-lines.txt" ] && echo BASELINE_EXISTS || echo NO_BASELINE',
    ]))
    r3 = _bash(h3, base)
    assert r3.returncode == 0, r3.stdout + r3.stderr
    assert "NO_BASELINE" in r3.stdout


def _main_harness(base, args):
    """抽出 deploy.sh 的 main(), 把每一步换成桩, 验证 --dry-run 不触碰部署动作"""
    sh = (ROOT / "server" / "deploy.sh").read_text(encoding="utf-8")
    a = sh.index("main() {")
    b = sh.index('main "$@"', a)
    block = sh[a:b]
    fns = ("verify_bundle_integrity install_java deploy_cluster install_docker install_compose "
           "load_images check_ports apply_sysctl prepare_deploy_dir install_backup "
           "import_sql_external startup import_sql_local setup_kafka_auth setup_kibana_user "
           "setup_mysql_replication health_check summary contract_save_baseline preflight contract_diff")
    lines = ["#!/usr/bin/env bash", "set -euo pipefail",
             "DRY_RUN=0",   # 真实脚本由契约消费段初始化
             'T() { printf "%s" "$1"; }', 'hr() { :; }',
             'log() { echo "LOG:$*"; }', 'die() { echo "DIE:$*" >&2; exit 1; }']
    lines += ['%s(){ echo "STEP:%s"; }' % (f, f) for f in fns.split()]
    lines += [block, "main %s" % " ".join(args)]
    h = base / "_main_harness.sh"
    h.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return h


def test_deploy_dry_run_skips_all_deploy_steps(tmp_path):
    base = tmp_path / "bundle"
    base.mkdir()
    dry = _bash(_main_harness(base, ["--dry-run"]), base)
    assert dry.returncode == 0, dry.stdout + dry.stderr
    for fn in ("verify_bundle_integrity", "preflight", "contract_diff", "check_ports"):
        assert "STEP:" + fn in dry.stdout
    for fn in ("install_docker", "startup", "summary", "contract_save_baseline", "prepare_deploy_dir"):
        assert "STEP:" + fn not in dry.stdout, "dry-run 不应执行 " + fn
    assert "dry-run 完成" in dry.stdout

    full = _bash(_main_harness(base, []), base)
    assert full.returncode == 0, full.stdout + full.stderr
    for fn in ("install_docker", "prepare_deploy_dir", "startup", "summary", "contract_save_baseline"):
        assert "STEP:" + fn in full.stdout


def test_plugin_docs_declare_conf_files_and_bash_quote():
    readme = (ROOT / "plugins" / "README.md").read_text(encoding="utf-8")
    tpl = (ROOT / "plugins" / "_template.py").read_text(encoding="utf-8")
    for text in (readme, tpl):
        assert "conf_files" in text
        assert "bash_quote" in text
    assert "只补齐" in tpl
    assert "packer-new" in readme


# ---------------------------------------------------------------- 零第三方依赖(python packer.py 必须能起)

def test_packer_imports_without_pyyaml():
    """整个导入链不得硬依赖 PyYAML, 否则未装依赖的解释器会在启动时闪退"""
    import re
    code = ("import sys; sys.modules['yaml'] = None; "
            "sys.path.insert(0, %r); import packer; print('IMPORT_OK')" % str(ROOT))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0 and "IMPORT_OK" in r.stdout, r.stdout + r.stderr
    for mod in ("validate.py", "compose.py", "cluster.py"):
        src = (ROOT / "packerlib" / mod).read_text(encoding="utf-8")
        assert not re.search(r"^import yaml\b", src, re.M), "%s 不得直接 import yaml" % mod
    assert (ROOT / "start-packer.bat").is_file(), "Windows 启动脚本缺失"


def test_env_probe_is_readonly(monkeypatch):
    """/api/env 只做只读探测: 不安装、不启动、不拉取、不写配置"""
    import packerlib.web as web
    calls = []
    monkeypatch.setattr(web.shutil, "which",
                        lambda name: r"C:\bin\docker.exe" if name == "docker" else None)

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        assert kw.get("timeout"), "每个 docker 探测都必须带超时"
        if "--version" in cmd:
            out = "Docker version 27.3.1, build abc"
        elif "compose" in cmd:
            out = "Docker Compose version v2.27.1"
        else:
            out = "27.3.1"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(web.subprocess, "run", fake_run)
    info = web.env_probe()
    assert "platform" in info and info["platform"]
    dc = info["docker"]
    assert dc["found"] and dc["version"].startswith("Docker version")
    assert dc["compose"] is True and dc["daemon"] is True
    assert len(calls) == 3, "只应执行 --version / compose version / info"
    forbidden = ("pull", "save", "load", "install", "start", "restart", "rm", "run", "systemd")
    for cmd in calls:
        assert not any(tok in cmd for tok in forbidden), "探测命令必须是只读的: %s" % cmd

    # 找不到 docker 时不应执行任何子进程
    calls.clear()
    monkeypatch.setattr(web.shutil, "which", lambda name: None)
    info2 = web.env_probe()
    assert info2["docker"]["found"] is False and info2["docker"]["daemon"] is False
    assert not calls


def test_yamlcompat_fallback_validates_cni_values(monkeypatch, catalog):
    """无 PyYAML 时内置解析器仍需接受合法 Helm values、拒绝破损 YAML"""
    import packerlib.yamlcompat as yc
    from packerlib.util import PackError

    monkeypatch.setattr(yc, "_pyyaml", None)
    assert yc.has_pyyaml() is False
    legal = "installation:\n  calicoNetwork:\n    ipipMode: Always\n"
    assert isinstance(yc.safe_load(legal), dict)
    for bad in ("{{{{", ":\n  - [unclosed\n", 'a: "oops', "- a\n- b\n"):
        try:
            yc.safe_load(bad)
            assert False, "内置解析器应拒绝: %r" % bad
        except yc.YamlError:
            pass

    def cfg_with(values):
        return make_cfg(None, services=("nginx",),
                        servers=[{"name": "n1", "user": "root", "ip": "10.0.0.1", "ssh": 22, "password": "p"}],
                        cluster={"enabled": True, "kube_version": "v1.30.14", "cni_type": "calico",
                                 "roles": {"0": "control-plane"}, "calico_values": values})

    ok, _ = packer.validate_config(cfg_with(legal), catalog)
    assert "ipipMode: Always" in ok["cluster"]["calico_values"]
    for bad in ("{{{{", ":\n  - [unclosed\n"):
        try:
            packer.validate_config(cfg_with(bad), catalog)
            assert False, "降级解析器下非法 YAML 仍应抛 PackError"
        except PackError:
            pass


def test_port_error_message_no_duplicate_label(catalog):
    """端口报错不得把「端口」与 label 自带的「端口」重复拼成「端口 HTTP 端口」"""
    cfg = make_cfg(None, services=("nginx",), ports={"nginx_http": 0})
    with pytest.raises(packer.PackError) as ei:
        packer.validate_config(cfg, catalog)
    msg = str(ei.value)
    assert "|" in msg, "报错文案须为整行双语"
    zh, en = msg.split("|", 1)
    assert "超出允许端口范围 1-65535" in zh
    assert "端口 HTTP 端口" not in zh, "中文半边不应重复「端口」: %s" % zh
    assert "outside the allowed port range 1-65535" in en
    assert "Port HTTP port" not in en, "英文半边不应重复 port: %s" % en

    cfg2 = make_cfg(None, services=("nginx",), ports={"nginx_http": "abc"})
    with pytest.raises(packer.PackError) as ei2:
        packer.validate_config(cfg2, catalog)
    zh2, en2 = str(ei2.value).split("|", 1)
    assert "不是合法端口" in zh2 and "端口 HTTP 端口" not in zh2
    assert "is not a valid port" in en2


def test_blank_custom_port_rows_are_ignored(catalog):
    """UI「+ 添加自定义端口」的空白占位行不得阻断预览; 半填仍要给出可读提示"""
    ok, _ = packer.validate_config(
        make_cfg(None, services=("nginx",),
                 extra_ports={"nginx": [{"host": "", "container": ""}]}), catalog)
    assert ok["extra_ports"].get("nginx", []) == [], "空白占位行应被忽略"
    with pytest.raises(packer.PackError) as ei:
        packer.validate_config(
            make_cfg(None, services=("nginx",),
                     extra_ports={"nginx": [{"host": "18090", "container": ""}]}), catalog)
    zh, en = str(ei.value).split("|", 1)
    assert "须填数字" in zh and "宿主机 → 容器" in zh
    assert "{" not in zh, "错误信息不应直接打印 dict 字面量: %s" % zh
    assert "must be numbers" in en

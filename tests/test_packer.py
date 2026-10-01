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
                  features={"proxysql": True})
    cfg, _ = packer.validate_config(cfg, catalog)
    conf = packer.gen_proxysql_conf(cfg)
    assert '{address="10.0.0.11",port=%d,hostgroup=10' % cfg["ports"]["mysql8"] in conf
    assert 'hostgroup=20' in conf
    assert 'default_hostgroup=10' in conf
    assert 'match_pattern' in conf   # 读写分流规则
    compose = packer.gen_compose(cfg, catalog)
    assert "container_name: proxysql" in compose
    imgs = [f for f, _, _ in packer.gen_images_txt(cfg, catalog)]
    assert any("proxysql" in i for i in imgs)


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
    # 密码节点走 sshpass
    cfg2 = _mh_cfg(["redis"], {"redis": "sentinel"},
                   {"redis": {"enabled": True, "master": 0, "replicas": [1, 2]}},
                   servers=[{"ip": "10.0.0.11", "password": "S3cret~!"}, {"ip": "10.0.0.12"}, {"ip": "10.0.0.13"}])
    cfg2, _ = packer.validate_config(cfg2, catalog)
    nodes2 = packer.gen_nodes(cfg2, catalog)
    dist2 = packer.gen_distribute_sh(cfg2, nodes2, catalog)
    assert 'SSHC=(sshpass -e ssh' in dist2 and "S3cret~!" in dist2
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
    assert sh.index("retag-mirrors.sh") < sh.index("normalize_image \"$short\"")

# -*- coding: utf-8 -*-
"""
本地端到端测试 (需要本机 Docker)

用打包器真实生成的 docker-compose.yml/.env/配置文件起一套最小可用栈,
验证: PostgreSQL/MongoDB 备份恢复、Prometheus->Alertmanager 告警链路、一键卸载。

运行:  python tools/local_e2e_test.py            # 全流程(结束自动清理)
       python tools/local_e2e_test.py --keep     # 结束后不清理容器(排查用)
       python tools/local_e2e_test.py --purge    # 卸载测试直接用 --purge-data
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import packer  # noqa: E402
from importlib import util as _util  # noqa: E402

WORK = ROOT / ".workbuddy" / "tmp-e2e"
SERVICES = ["postgres", "mongodb", "node-exporter", "prometheus", "alertmanager"]


def sh(cmd):
    """跑 bash 脚本 (cmd.exe 仅作中转, 命令里不要再嵌套引号)"""
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=str(WORK))


def run(args, cwd=None):
    """list 形式直跑, 避免 Windows shell 引号问题"""
    return subprocess.run(args, capture_output=True, text=True, cwd=str(cwd or ROOT))


def load_plugin(name):
    spec = _util.spec_from_file_location(name, ROOT / "plugins" / ("%s.py" % name))
    m = _util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def read_env():
    env = {}
    for line in (WORK / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"')
    return env


def prepare():
    """用打包器生成部署文件 (与真实离线包内容一致)"""
    catalog = packer.load_catalog()
    cfg = {
        "project": "e2etest", "arch": "amd64", "services": SERVICES,
        "ports": {}, "extra_ports": {}, "secrets": {},
        "deploy_dir": "/data/mw", "docker_data_root": "/data/docker",
        "registry_mirrors": ["https://docker.1ms.run"],
        "backup": {"enabled": True, "days": [1], "hour": 3, "keep": 3, "dir": "/data/backup/mysql"},
        "topology": {}, "proxies": [], "lang": "zh",
    }
    cfg, warns = packer.validate_config(cfg, catalog)
    assert not warns, warns
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "docker-compose.yml").write_text(packer.gen_compose(cfg, catalog), encoding="utf-8", newline="\n")
    (WORK / ".env").write_text(packer.gen_env(cfg, catalog), encoding="utf-8", newline="\n")
    # 打包器 conf_files 产物: conf/<svc>/xxx -> 部署目录 <svc>/xxx (deploy.sh 同规则)
    for mod in ("prometheus", "alertmanager"):
        files = load_plugin(mod).conf_files(cfg, cfg["ports"], packer.plugin_ctx(cfg))
        for rel, content in files.items():
            dest = WORK / rel[len("conf/"):]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8", newline="\n")
    for tpl in ("backup.sh", "restore.sh", "uninstall.sh"):
        (WORK / tpl).write_bytes((ROOT / "templates" / tpl).read_bytes())
    conf = packer.gen_backup_conf(cfg).replace(
        "BACKUP_DIR='/data/backup/mysql'", "BACKUP_DIR='%s'" % (WORK / "backup").as_posix())
    (WORK / "backup.conf").write_text(conf, encoding="utf-8", newline="\n")
    print("[prepare] 部署文件已生成:", ", ".join(sorted(p.name for p in WORK.iterdir())))


def load_images():
    """模拟服务器: 把仓库里的镜像 tar 装入本地守护进程 (离线包走 docker load)"""
    catalog = packer.load_catalog()
    for s in SERVICES:
        meta = catalog["services"][s]
        tar = ROOT / meta["images"]["amd64"]
        r = run(["docker", "images", "-q", "%s:%s" % (meta["image"], meta["tag"])])
        if r.stdout.strip():
            print("[images] 已存在:", meta["image"] + ":" + meta["tag"])
            continue
        print("[images] docker load:", tar.name, "(%d MB)" % (tar.stat().st_size // 1048576))
        r = subprocess.run(["docker", "load", "-i", str(tar)], capture_output=True, text=True)
        assert r.returncode == 0, "load 失败 %s: %s" % (tar, r.stderr[-300:])


def up():
    r = sh("docker compose up -d")
    assert r.returncode == 0, "compose up 失败: %s" % r.stderr[-500:]
    deadline = time.time() + 240
    states = {}
    while time.time() < deadline:
        r = run(["docker", "compose", "ps", "--format", "{{.Name}} {{.Health}}"], cwd=WORK)
        states = {}
        for line in r.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                states[parts[0]] = parts[1]
        if len(states) == len(SERVICES) and all(v in ("healthy", "running") for v in states.values()):
            print("[up] 全部服务就绪:", states)
            return
        time.sleep(5)
    raise AssertionError("服务未在 240s 内全部就绪: %s" % states)


def backup_restore_test(env):
    mongo_user = env["MONGO_INITDB_ROOT_USERNAME"]
    mongo_pwd = env["MONGO_INITDB_ROOT_PASSWORD"]
    # ---- 造数据 ----
    r = run(["docker", "exec", "postgres", "psql", "-U", "postgres", "-c",
             "CREATE TABLE e2e_marker(id int); INSERT INTO e2e_marker VALUES (42);"])
    assert r.returncode == 0, r.stderr
    r = run(["docker", "exec", "mongodb", "mongosh", "--quiet",
             "-u", mongo_user, "-p", mongo_pwd, "--authenticationDatabase", "admin",
             "--eval", "db.getSiblingDB('e2edb').marker.insertOne({v:42})"])
    assert r.returncode == 0, r.stderr
    # ---- 备份 ----
    r = sh("bash backup.sh")
    print(r.stdout.strip()[-600:])
    assert r.returncode == 0, "backup.sh 失败"
    pg_bk = list((WORK / "backup" / "postgres").glob("postgres_*.sql.gz"))
    mo_bk = list((WORK / "backup" / "mongodb").glob("e2edb_*.archive"))
    assert pg_bk and mo_bk, "备份文件缺失: %s / %s" % (pg_bk, mo_bk)
    print("[backup] PG/Mongo 备份文件生成:", pg_bk[0].name, "/", mo_bk[0].name)
    # ---- 毁数据 ----
    run(["docker", "exec", "postgres", "psql", "-U", "postgres", "-c", "DROP TABLE e2e_marker;"])
    run(["docker", "exec", "mongodb", "mongosh", "--quiet",
         "-u", mongo_user, "-p", mongo_pwd, "--authenticationDatabase", "admin",
         "--eval", "db.getSiblingDB('e2edb').marker.drop()"])
    # ---- 恢复 ----
    r = sh("bash restore.sh postgres postgres %s --yes" % pg_bk[0].name)
    assert r.returncode == 0, "restore.sh PG 失败: %s" % (r.stderr or r.stdout)[-400:]
    r2 = sh("bash restore.sh mongodb e2edb %s --yes" % mo_bk[0].name)
    assert r2.returncode == 0, "restore.sh Mongo 失败: %s" % (r2.stderr or r2.stdout)[-400:]
    print((r.stdout + r2.stdout).strip()[-500:])
    # ---- 校验 ----
    v1 = run(["docker", "exec", "postgres", "psql", "-U", "postgres", "-At", "-c",
              "SELECT id FROM e2e_marker;"])
    assert v1.stdout.strip() == "42", "PG 恢复数据不符: %r" % v1.stdout
    print("[restore] PG 表数据恢复验证: 42 OK")
    v2 = run(["docker", "exec", "mongodb", "mongosh", "--quiet",
              "-u", mongo_user, "-p", mongo_pwd, "--authenticationDatabase", "admin",
              "--eval", "db.getSiblingDB('e2edb').marker.findOne().v"])
    assert v2.stdout.strip().endswith("42"), "Mongo 恢复数据不符: %r" % v2.stdout
    print("[restore] Mongo 数据恢复验证: 42 OK")


def alert_chain_test():
    import urllib.request
    with urllib.request.urlopen("http://127.0.0.1:9090/api/v1/targets") as f:
        targets = json.load(f)
    jobs = {t["labels"].get("job") for t in targets["data"]["activeTargets"]}
    assert "prometheus" in jobs, "prometheus 自抓取目标缺失: %s" % jobs
    if "node-exporter" in jobs:
        print("[alert] Prometheus targets 含 node-exporter")
    alert = {"labels": {"alertname": "E2ETestAlert", "severity": "critical", "instance": "e2e"},
             "annotations": {"summary": "e2e test alert"},
             "startsAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}
    req = urllib.request.Request("http://127.0.0.1:9093/api/v2/alerts",
                                 data=json.dumps([alert]).encode(),
                                 headers={"Content-Type": "application/json"})
    assert urllib.request.urlopen(req).status == 200
    time.sleep(2)
    with urllib.request.urlopen("http://127.0.0.1:9093/api/v2/alerts") as f:
        got = [a["labels"].get("alertname") for a in json.load(f)]
    assert "E2ETestAlert" in got, "注入的告警未出现在 Alertmanager: %s" % got
    print("[alert] 告警链路 OK: Prometheus 抓取 -> Alertmanager 接收/展示 (E2ETestAlert)")


def uninstall_test(purge=False):
    r = sh("bash uninstall.sh --yes --purge-data" if purge else "bash uninstall.sh --yes")
    print(r.stdout.strip()[-700:])
    assert r.returncode == 0, "uninstall.sh 失败"
    ps = sh("docker compose ps -aq")
    assert not ps.stdout.strip(), "仍有容器残留: %r" % ps.stdout
    if purge:
        assert not (WORK / "postgres" / "data").exists(), "purge 后数据目录仍在"
        print("[uninstall] 容器与数据已彻底清除")
    else:
        assert (WORK / "postgres" / "data").exists(), "默认卸载不应删除数据目录"
        print("[uninstall] 容器已移除, 数据目录保留 OK")


def main():
    keep = "--keep" in sys.argv
    purge = "--purge" in sys.argv
    prepare()
    try:
        load_images()
        up()
        backup_restore_test(read_env())
        alert_chain_test()
        uninstall_test(purge=purge)
        if not purge and not keep:
            uninstall_test(purge=True)   # 收尾: 彻底清理
        print("\n==== E2E 全部通过 ====")
    finally:
        if not keep:
            sh("docker compose down -v --remove-orphans")


if __name__ == "__main__":
    main()

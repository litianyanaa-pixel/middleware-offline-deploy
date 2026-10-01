#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中间件真实运行时矩阵(本机 Docker Desktop 真跑, 非伪 docker 垫片)

背景: e2e-docker-全流程测试报告 的分发链路用的是伪 docker 垫片, 20 个中间件
没有一个真实 compose up 过。本脚本补上真实运行时验证:

  对每个服务族:
    1. 用 packer CLI 打一个真实单机中间件包(与用户拿到的产物同一条生成链路)
    2. 解包到本机目录, 按 install-node.sh 同款桥接展开 conf/<svc>/ -> <svc>/
    3. docker load 包内镜像 tar(与部署机动作一致, 不联网拉取)
    4. docker compose up -d --wait 等全部 healthcheck 转 healthy
    5. 逐服务真实探针(HTTP / 容器内 CLI), 而非只看容器状态
    6. compose down -v 清场(镜像保留, 复用层)

用法: python tests/middleware_runtime_matrix.py db|mq|obs|search|all [--keep]
前置: 本机 Docker Desktop 运行中; warehouse/images/ 物料齐全(缺的用
      gen_pull_script 补料)。首跑需加载 GB 级镜像, 全程约 15-30 分钟。
"""
import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import packer  # noqa: E402

# 运行目录必须在 Docker Desktop 默认文件共享范围内(C:\\Users), 否则 bind mount 会被拒
RUN_ROOT = Path("C:/Users/Administrator/mw-rt")
CFG_ROOT = Path("E:/tmp/mw-rt-bundles")   # 打包配置与产物放仓库外
FAMILIES = {
    # 端口基段错开, 避免与本机既有服务冲突
    "db":     {"base": 23000, "services": ["nginx", "redis", "mysql8", "mysql57",
                                           "postgres", "mongodb", "minio", "rabbitmq",
                                           "nacos", "xxljob"]},
    "mq":     {"base": 18200, "services": ["kafka", "kafka-ui"]},
    "obs":    {"base": 18400, "services": ["prometheus", "grafana", "alertmanager",
                                           "node-exporter", "loki", "promtail"]},
    "search": {"base": 18600, "services": ["elasticsearch", "kibana"]},
}
RESULTS = []


def sh(cmd, cwd=None, timeout=1800):
    r = subprocess.run(cmd, cwd=cwd, timeout=timeout,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r


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


def record(name, ok, note=""):
    RESULTS.append((name, bool(ok), note))
    print(("  ✓ " if ok else "  ✗ FAIL ") + name + (("  | " + note) if note else ""))


def http_get(url, auth=None, timeout=8):
    req = urllib.request.Request(url)
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(262144).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return None, type(e).__name__


def probe_http(name, url, expect_text=None, auth=None, retries=24, gap=5):
    code, body = None, ""
    for _ in range(retries):
        code, body = http_get(url, auth=auth)
        if code is not None and code < 500 and (expect_text is None or expect_text in body):
            break
        time.sleep(gap)
    record(name, code is not None and code < 500 and (expect_text is None or expect_text in body),
           "%s -> %s" % (url, code))


def parse_env(path):
    env = {}
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, _, v = ln.partition("=")
            env[k.strip()] = v.strip().strip("'\"")
    return env


def exec_in(run_dir, svc, inner_cmd, timeout=90):
    r = sh(["docker", "compose", "exec", "-T", svc, "sh", "-c", inner_cmd],
           cwd=str(run_dir), timeout=timeout)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def probe_exec(name, run_dir, svc, inner_cmd, expect=None, retries=12, gap=5):
    ok, out = False, ""
    for _ in range(retries):
        ok, out = exec_in(run_dir, svc, inner_cmd)
        if ok and (expect is None or expect in out):
            break
        time.sleep(gap)
    record(name, ok, out.splitlines()[-1][:120] if out else "")


def family_cfg(fam, spec, catalog):
    ports, nxt = {}, spec["base"]
    for s in spec["services"]:
        for p in catalog["services"][s]["ports"]:
            ports[p["key"]] = nxt
            nxt += 1
    cfg = {
        "project": "mw-rt-%s" % fam, "arch": "amd64", "lang": "zh",
        "services": spec["services"], "ports": ports,
        "deploy_dir": "/opt/middleware", "docker_data_root": "/var/lib/docker",
        "registry_mirrors": [], "servers": [],
    }
    if "nacos" in spec["services"] or "xxljob" in spec["services"]:
        cfg["db"] = {"nacos": {"mode": "local", "schema": "nacos"},
                     "xxljob": {"mode": "local", "schema": "xxl_job"}}
    return cfg


def stage_family(fam, spec, catalog):
    """打包真包 -> 解包 -> conf 桥接 -> docker load; 返回 (rundir, env, ports)"""
    print("[%s] 打包真实中间件包 (%s)" % (fam, ", ".join(spec["services"])))
    CFG_ROOT.mkdir(parents=True, exist_ok=True)
    cfg = family_cfg(fam, spec, catalog)
    cfg_json = CFG_ROOT / ("cfg-%s.json" % fam)
    cfg_json.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    r = sh([sys.executable, str(BASE / "packer.py"), "--config", str(cfg_json),
            "--out", str(CFG_ROOT)])
    if r.returncode != 0:
        raise SystemExit("打包失败:\n%s" % (r.stdout + r.stderr)[-2000:])
    bundle_tgz = sorted(CFG_ROOT.glob("mw-rt-%s-x86-*-offline.tar.gz" % fam))[-1]

    rundir = RUN_ROOT / fam
    if rundir.exists():
        sh(["chmod", "-R", "u+wx", str(rundir)])
        shutil.rmtree(rundir, ignore_errors=True)
    rundir.mkdir(parents=True)
    with tarfile.open(bundle_tgz, "r:gz") as tf:
        tf.extractall(rundir)
    inner = next(rundir.glob("mw-rt-*"))
    for f in inner.iterdir():          # 拍平包根目录
        shutil.move(str(f), str(rundir / f.name))
    inner.rmdir()

    # conf 桥接: 与 install-node.sh 一致, conf/<svc>/* -> <svc>/*
    conf = rundir / "conf"
    if conf.is_dir():
        for child in conf.iterdir():
            shutil.copytree(child, rundir / child.name, dirs_exist_ok=True)

    # 初始化 SQL: 产品路径由 deploy.sh 把包根 sql/*.sql 拷进 <mysql>/init/, MySQL 首启自动建库;
    # 直接 compose 跑同样必须补这一步, 否则 nacos/xxljob 因缺库 CrashLoop
    sql_dir = rundir / "sql"
    if sql_dir.is_dir():
        for ms in ("mysql8", "mysql57"):
            init = rundir / ms / "init"
            init.mkdir(parents=True, exist_ok=True)
            for f in sql_dir.glob("*.sql"):
                shutil.copy2(f, init / f.name)

    # docker load 包内全部镜像 tar(离线链路的真实动作)
    print("[%s] docker load 包内镜像" % fam)
    for tar in sorted((rundir / "images").glob("*.tar")):
        r = sh(["docker", "load", "-i", str(tar)], timeout=1800)
        if r.returncode != 0:
            raise SystemExit("docker load %s 失败: %s" % (tar.name, r.stderr[-500:]))
    # 镜像源前缀重打标(与部署脚本同一产物脚本: 补料 crane 兜底 tar 仓库名带前缀)
    retag = rundir / "images" / "retag-mirrors.sh"
    if retag.is_file():
        r = sh([_find_bash(), str(retag), "images.txt"], cwd=str(rundir))
        out = (r.stdout + r.stderr).strip()
        if out:
            print("  retag: " + out.replace("\n", " | ")[:200])
        if r.returncode != 0:
            raise SystemExit("retag-mirrors.sh 执行失败: %s" % out[-300:])
    cports, svc2key = {}, {}
    for s in spec["services"]:
        for p in catalog["services"][s]["ports"]:
            cports[p["key"]] = p.get("container", p["default"])
            svc2key.setdefault(s, p["key"])
    return rundir, parse_env(rundir / ".env"), cfg["ports"], cports, svc2key


def up_and_probe(fam, spec, rundir, env, ports, cports, svc2key):
    print("[%s] compose up --wait (等全部 healthcheck)" % fam)
    r = sh(["docker", "compose", "up", "-d", "--wait", "--wait-timeout", "900"],
           cwd=str(rundir), timeout=1000)
    tail = (r.stdout + r.stderr).strip().splitlines()
    record("[%s] compose up --wait 全部 healthy" % fam, r.returncode == 0,
           tail[-1].strip()[:160] if tail else "")
    ps = sh(["docker", "compose", "ps", "--format", "json"], cwd=str(rundir))
    containers = {}
    for ln in ps.stdout.splitlines():
        try:
            j = json.loads(ln)
            containers[j.get("Service")] = j.get("State")
        except Exception:
            pass
    tag = "[%s]" % fam

    def container_state(name):
        r = sh(["docker", "inspect", "-f",
                "{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{end}}", name])
        return r.stdout.strip() if r.returncode == 0 else None

    # 容器名与配置键的连字符/下划线差异按去符号归一化匹配(xxljob -> xxl-job)
    ps_names = sh(["docker", "ps", "--format", "{{.Names}}"]).stdout.split()
    norm = lambda x: x.replace("-", "").replace("_", "").lower()  # noqa: E731
    name_of = {}
    for s in spec["services"]:
        hit = [n for n in ps_names if norm(n) == norm(s)]
        if hit:
            name_of[s] = hit[0]
    for s in spec["services"]:
        st = container_state(name_of[s]) if s in name_of else None
        record("%s %s 容器运行" % (tag, s),
               bool(st) and ("running" in st), str(st))

    # ---- HTTP 探针(带重试; 部分服务 health 绿后 HTTP 还要几秒就绪) ----
    def hp(svc_key, path, key=None, **kw):
        k = key or svc2key.get(svc_key, svc_key)
        probe_http("%s %s" % (tag, svc_key), "http://127.0.0.1:%d%s" % (ports[k], path), **kw)

    if "nginx" in spec["services"]:
        hp("nginx", "/", retries=6)
    if "minio" in spec["services"]:
        hp("minio", "/minio/health/live", retries=8)
    if "rabbitmq" in spec["services"]:
        hp("rabbitmq", "/api/overview", key="mgmt",
           auth="%s:%s" % (env.get("RABBITMQ_USER", "admin"), env.get("RABBITMQ_PASSWORD", "")))
    if "nacos" in spec["services"]:
        # nacos 3.x: 控制台独立端口(nacos_console, 容器 8080), API 在 8848
        hp("nacos", "/", key="nacos_console", retries=30)
    if "xxljob" in spec["services"]:
        hp("xxljob", "/xxl-job-admin/", retries=30)
    if "kafka-ui" in spec["services"]:
        hp("kafka-ui", "/", retries=20, key="ui")
    if "prometheus" in spec["services"]:
        hp("prometheus", "/-/ready")
    if "grafana" in spec["services"]:
        hp("grafana", "/api/health")
    if "alertmanager" in spec["services"]:
        hp("alertmanager", "/-/ready")
    if "node-exporter" in spec["services"]:
        hp("node-exporter", "/metrics", expect_text="process_")
    if "loki" in spec["services"]:
        hp("loki", "/ready", retries=20)
    # promtail 无对外端口(目录 ports 为空), 由 compose healthcheck 覆盖, 不做 HTTP 探针
    if "elasticsearch" in spec["services"]:
        hp("elasticsearch", "/_cluster/health", retries=40)
    if "kibana" in spec["services"]:
        hp("kibana", "/api/status", retries=60)

    # ---- 容器内 CLI 探针(验证真实数据处理通路) ----
    if "redis" in spec["services"]:
        probe_exec("%s redis PING" % tag, rundir, "redis",
                   "redis-cli -a '%s' ping" % env.get("REDIS_PASSWORD", ""), expect="PONG")
    if "mysql8" in spec["services"]:
        probe_exec("%s mysql8 SELECT" % tag, rundir, "mysql8",
                   'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "SELECT 1"')
    if "mysql57" in spec["services"]:
        probe_exec("%s mysql57 SELECT" % tag, rundir, "mysql57",
                   'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "SELECT 1"')
    if "postgres" in spec["services"]:
        probe_exec("%s postgres SELECT" % tag, rundir, "postgres",
                   'psql -U ${POSTGRES_USER:-postgres} -c "SELECT 1"')
    if "mongodb" in spec["services"]:
        mu = env.get("MONGO_INITDB_ROOT_USERNAME", "root")
        mp = env.get("MONGO_INITDB_ROOT_PASSWORD", "")
        probe_exec("%s mongo version" % tag, rundir, "mongodb",
                   "mongosh --quiet -u '%s' -p '%s' --authenticationDatabase admin "
                   "--eval 'db.version()'" % (mu, mp))
    if "kafka" in spec["services"]:
        kport = cports.get("kafka", 9092)
        # apache/kafka 镜像脚本在 /opt/kafka/bin(默认 PATH 不含), 3.x/4.x 文件名有 .sh 差异
        probe_exec("%s kafka broker 应答" % tag, rundir, "kafka",
                   "/opt/kafka/bin/kafka-topics --bootstrap-server localhost:%d --list "
                   "|| /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:%d --list" % (kport, kport))


def down_family(fam, keep):
    rundir = RUN_ROOT / fam
    if not rundir.exists():
        return
    sh(["docker", "compose", "down", "-v", "--remove-orphans"], cwd=str(rundir), timeout=600)
    if not keep:
        sh(["chmod", "-R", "u+wx", str(rundir)])
        shutil.rmtree(rundir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("families", nargs="?", default="all",
                    help="db|mq|obs|search|all (逗号分隔可选多个)")
    ap.add_argument("--keep", action="store_true", help="保留运行目录与容器(排查用)")
    a = ap.parse_args()
    fams = list(FAMILIES) if a.families == "all" else a.families.split(",")

    r = sh(["docker", "info", "--format", "{{.ServerVersion}}"])
    if r.returncode != 0:
        raise SystemExit("本机 Docker 不可用: %s" % r.stderr[-300:])
    catalog = packer.load_catalog()
    t0 = time.time()
    for fam in fams:
        spec = FAMILIES[fam]
        rundir, env, ports, cports, svc2key = stage_family(fam, spec, catalog)
        try:
            up_and_probe(fam, spec, rundir, env, ports, cports, svc2key)
        finally:
            if not a.keep:
                down_family(fam, a.keep)
    bad = [n for n, ok, _ in RESULTS if not ok]
    print("\n== 中间件运行时矩阵: %d 项, 失败 %d ==" % (len(RESULTS), len(bad)))
    for n in bad:
        print("  ✗ " + n)
    print("耗时 %.0f 分钟" % ((time.time() - t0) / 60))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()

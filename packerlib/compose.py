# -*- coding: utf-8 -*-
"""生成 docker-compose.yml / .env / 备份配置与 compose 静态校验"""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

from .catalog import PLUGINS, PROXYSQL_META
from .nacos_sql import build_nacos_sql
from .paths import SERVER_SH, TPL_DIR, WAREHOUSE
from .util import (PackError, bash_quote, env_quote, gen_nacos_token,
                   gen_random_password, is_multihost, tr)


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

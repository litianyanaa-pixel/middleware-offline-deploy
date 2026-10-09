# -*- coding: utf-8 -*-
"""多机部署产物: 每节点 compose 片段 / 安装脚本 / 分发脚本"""
from .catalog import PLUGINS
from .paths import SERVER_SH, TPL_DIR, log
from .util import (bash_quote, env_quote, has_multihost, is_multihost,
                   master_server_ip, tr)


KRAFT_CLUSTER_ID = "iZWRiSqjZAlYwlKEqHFQWI"


# ---------------------------------------------------------------- 多机节点产物生成

def _node_kafka_block(cfg, ports, node_id, broker_ips):
    """多机 Kafka 节点 compose 块 (bitnami 3.7.0, KRaft 组合模式, 可配置宿主端口)"""
    server = cfg["servers"][node_id]
    inter, ctrl, sasl = ports["kafka_mh_inter"], ports["kafka_mh_ctrl"], ports["kafka_mh_sasl"]
    ip = server["ip"]
    voters = ",".join("%d@%s:%d" % (i + 1, cfg["servers"][b]["ip"], ctrl)
                      for i, b in enumerate((cfg["multihost"]["kafka"]["brokers"])))
    advertised = "INTERNAL://%s:%d,HOST://%s:%d" % (ip, inter, ip, sasl)
    return """
  kafka:
    image: %(image)s
    container_name: kafka
    restart: always
    user: root
    environment:
      TZ: ${TZ}
      KAFKA_ENABLE_KRAFT: "yes"
      KAFKA_KRAFT_CLUSTER_ID: %(cluster_id)s
      KAFKA_CFG_PROCESS_ROLES: broker,controller
      KAFKA_CFG_NODE_ID: %(node_id)d
      KAFKA_CFG_LISTENERS: INTERNAL://:9092,CONTROLLER://:9093,HOST://:9094
      KAFKA_CFG_LISTENER_SECURITY_PROTOCOL_MAP: INTERNAL:PLAINTEXT,CONTROLLER:PLAINTEXT,HOST:SASL_PLAINTEXT
      KAFKA_CFG_ADVERTISED_LISTENERS: %(advertised)s
      KAFKA_CFG_CONTROLLER_LISTENER_NAMES: CONTROLLER
      KAFKA_CFG_CONTROLLER_QUORUM_VOTERS: %(voters)s
      KAFKA_CFG_INTER_BROKER_LISTENER_NAME: INTERNAL
      KAFKA_CFG_SASL_ENABLED_MECHANISMS: PLAIN
      KAFKA_CFG_SASL_MECHANISM_INTER_BROKER_PROTOCOL: PLAIN
      KAFKA_CLIENT_USERS: admin
      KAFKA_CLIENT_PASSWORDS: ${KAFKA_PASSWORD}
      KAFKA_CFG_SUPER_USERS: User:admin;User:ANONYMOUS
      KAFKA_CFG_AUTHORIZER_CLASS_NAME: org.apache.kafka.metadata.authorizer.StandardAuthorizer
      KAFKA_CFG_ALLOW_EVERYONE_IF_NO_ACL_FOUND: "false"
      KAFKA_HEAP_OPTS: -Xmx1g -Xms512m
    ports:
      - "%(inter)d:9092"
      - "%(ctrl)d:9093"
      - "%(sasl)d:9094"
    volumes:
      - ./kafka/data:/bitnami/kafka
    healthcheck:
      test: ["CMD-SHELL", "/opt/bitnami/kafka/bin/kafka-broker-api-versions.sh --bootstrap-server localhost:9092 > /dev/null 2>&1 || exit 1"]
      interval: 15s
      timeout: 10s
      retries: 12
      start_period: 90s
    networks:
      - app-network""" % {
        "image": "bitnami/kafka:3.7.0", "cluster_id": KRAFT_CLUSTER_ID,
        "node_id": node_id + 1, "advertised": advertised, "voters": voters,
        "inter": inter, "ctrl": ctrl, "sasl": sasl,
    }


def _node_mysql_block(cfg, catalog, svc, role, replica_no=1):
    """多机 MySQL 节点 compose 块; role: master / replica; 容器名统一 mysql"""
    meta = catalog["services"][svc]
    ports = cfg["ports"]
    if role == "master":
        host_port = ports[svc]
        sid = 1
        repl_flags = ["--server-id=1", "--log-bin=mysql-bin", "--gtid-mode=ON",
                      "--enforce-gtid-consistency=ON",
                      "--binlog-expire-logs-seconds=604800" if svc == "mysql8" else "--expire-logs-days=7"]
    else:
        host_port = ports["%s_replica" % svc]
        sid = 10 + replica_no
        repl_flags = ["--server-id=%d" % sid, "--log-bin=mysql-bin", "--gtid-mode=ON",
                      "--enforce-gtid-consistency=ON", "--read-only=ON"]
    return """
  %(cn)s:
    image: %(image)s
    container_name: %(cn)s
    restart: always
    environment:
      TZ: ${TZ}
      MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD}
    ports:
      - "%(host_port)d:3306"
    volumes:
      - ./%(cn)s/data:/var/lib/mysql
      - ./%(cn)s/log:/var/log/mysql%(init_vol)s
      - ./%(cn)s/my.cnf:/etc/mysql/conf.d/my.cnf:ro
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
      - app-network""" % {
        "image": "%s:%s" % (meta["image"], meta["tag"]),
        "cn": svc,
        "host_port": host_port,
        "init_vol": "\n      - ./%(cn)s/init:/docker-entrypoint-initdb.d" % {"cn": svc} if role == "master" else "",
        "repl_flags": "".join("      - %s\n" % f for f in repl_flags),
    }


def _node_redis_blocks(cfg, catalog, role, master_ip):
    """多机 Redis 节点 compose 块 (每台: redis + sentinel); role: master / replica"""
    meta = catalog["services"]["redis"]
    ports = cfg["ports"]
    replicaof = "--replicaof %(mip)s %(mport)d " % {"mip": master_ip, "mport": ports["redis"]} if role == "replica" else ""
    return """
  redis:
    image: %(image)s
    container_name: redis
    restart: always
    environment:
      TZ: ${TZ}
      REDIS_PASSWORD: ${REDIS_PASSWORD}
    ports:
      - "%(port)d:6379"
    volumes:
      - ./redis/data:/data
      - ./redis/log:/var/log/redis
      - ./redis/redis.conf:/usr/local/etc/redis/redis.conf:ro
    command: redis-server /usr/local/etc/redis/redis.conf --requirepass "${REDIS_PASSWORD}" --masterauth "${REDIS_PASSWORD}" %(replicaof)s
    healthcheck:
      test: ["CMD-SHELL", "redis-cli -a \\"$$REDIS_PASSWORD\\" ping | grep -q PONG"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - app-network
  redis-sentinel:
    image: %(image)s
    container_name: redis-sentinel
    restart: always
    environment:
      TZ: ${TZ}
    command: redis-server /usr/local/etc/redis/sentinel.conf --sentinel
    ports:
      - "%(sentinel_port)d:26379"
    volumes:
      - ./redis/sentinel.conf:/usr/local/etc/redis/sentinel.conf:ro
    networks:
      - app-network""" % {
        "image": "%s:%s" % (meta["image"], meta["tag"]),
        "port": ports["redis"] if role == "master" else ports["redis_replica"],
        "replicaof": replicaof,
        "sentinel_port": ports.get("redis_sentinel", 26379),
    }


def gen_proxysql_conf(cfg, svc):
    """单集群的 ProxySQL 读写分离配置: 写 -> HG10(主库), 读 -> HG20(从库), SELECT 自动路由到读组.
    每个主从形态的 MySQL 独立一个实例(混在一对 hostgroup 里两套集群的读会互串);
    多机: 主/从用节点 IP + 宿主端口; 同机: 主/从用 compose 服务名 + 容器端口 3306"""
    ports = cfg["ports"]
    pw = cfg["secrets"].get("MYSQL_ROOT_PASSWORD", "")
    if is_multihost(cfg, svc):
        master = (master_server_ip(cfg, svc), ports[svc])
        replicas = [(cfg["servers"][i]["ip"], ports["%s_replica" % svc])
                    for i in cfg["multihost"][svc]["replicas"]]
    else:
        n = (cfg.get("replicas") or {}).get(svc, 1)
        master = (svc, 3306)
        replicas = [("%s-replica" % svc if r == 0 else "%s-replica%d" % (svc, r + 1), 3306)
                    for r in range(n)]
    srv_lines = ['\t\t{address="%s",port=%d,hostgroup=10,max_connections=200},' % master]
    for rip, rport in replicas:
        srv_lines.append('\t\t{address="%s",port=%d,hostgroup=20,max_connections=200},' % (rip, rport))
    ver = "8.0.46" if svc == "mysql8" else "5.7.44"
    return """# 由打包器自动生成 (ProxySQL 读写分离, 重新部署时可覆盖)
datadir="/var/lib/proxysql"
errorlog="/var/lib/proxysql/proxysql.log"
admin_variables={
\tadmin_credentials="admin:admin;admin:admin123"
\tmysql_ifaces="0.0.0.0:6032"
}
mysql_variables={
\tthreads=4
\tmax_connections=2048
\tinterfaces="0.0.0.0:6033"
\tdefault_schema="information_schema"
\tstacksize=1048576
\tserver_version="%s"
\tconnect_timeout_server=3000
\tmonitor_username="root"
\tmonitor_password="%s"
\tmonitor_connect_interval=2000
\tmonitor_ping_interval=2000
\tmonitor_read_only_interval=1500
\tmysql_servers=(
%s
\t)
\tmysql_replication_hostgroups=(
\t\t{writer_hostgroup=10,reader_hostgroup=20,check_type="read_only"}
\t)
\tmysql_users=(
\t\t{username="root",password="%s",default_hostgroup=10,active=1}
\t)
\tmysql_query_rules=(
\t\t{rule_id=100,active=1,match_pattern="^\\\\s*SELECT .* FOR UPDATE",destination_hostgroup=10,apply=1},
\t\t{rule_id=200,active=1,match_pattern="^\\\\s*SELECT\\\\b",destination_hostgroup=20,apply=1},
\t\t{rule_id=1000,active=1,match_pattern=".*",destination_hostgroup=10,apply=1}
\t)
}
""" % (ver, pw, "\n".join(srv_lines), pw)


def _node_backup_files(cfg, svcs):
    """MySQL 主库节点的定时备份配置 (backup.sh 需要: BACKUP_DIR/MYSQL_ENABLED/MYSQL_SERVICES/MYSQL_KEEP/MYSQL_ROOT_PASSWORD);
    svcs 为该节点上的主库服务名(backup.sh 按容器名 docker exec, mysql8/mysql57 同节点都要备份)"""
    b = cfg.get("backup") or {}
    eng = ((b.get("engines") or {}).get("mysql") or {})
    if not eng.get("enabled") or not svcs:
        return None
    dow = ",".join(sorted(str(0 if d == 7 else d) for d in (eng.get("days") or [])))
    return {
        "BACKUP_DIR": bash_quote(b.get("dir") or "/data/backup/db"),
        "MYSQL_ENABLED": "1",
        "MYSQL_SERVICES": "(%s)" % " ".join(svcs),
        "MYSQL_KEEP": str(int(eng.get("keep", 7))),
        "MYSQL_ROOT_PASSWORD": bash_quote(cfg["secrets"].get("MYSQL_ROOT_PASSWORD", "")),
        "CRON": "0 %d * * %s" % (int(eng.get("hour", 3)), dow),
    }


def gen_nodes(cfg, catalog):
    """多机部署: 按角色生成各节点安装计划(主节点优先, 保证从库安装时主已就绪)。
    返回 [{name, server, roles, compose, env, images, conf_files, post_sql,
           health_wait, data_dirs, chown_dirs, port_checks, backup}]"""
    nodes = []
    if not has_multihost(cfg):
        return nodes
    ports = cfg["ports"]
    servers = cfg["servers"]
    acc = {}   # idx -> 计划字典

    def acc_of(idx):
        if idx not in acc:
            acc[idx] = {
                "name": servers[idx]["name"], "server": servers[idx], "roles": [],
                "compose_parts": [], "env_keys": set(), "images": [], "conf_files": {},
                "post_sql": [], "health_wait": [], "data_dirs": [], "chown_dirs": [],
                "port_checks": [], "backup": None, "java_versions": [],
            }
        return acc[idx]

    def add_image(plan, svc_or_key, rel, short):
        plan["images"].append(("%s-%s.tar" % (svc_or_key, cfg["arch"]), rel, short))

    # ---- Kafka 节点 ----
    if is_multihost(cfg, "kafka"):
        kc = PLUGINS["kafka"].CLUSTER
        for n, idx in enumerate(cfg["multihost"]["kafka"]["brokers"]):
            p = acc_of(idx)
            p["roles"].append("kafka#broker%d" % (n + 1))
            p["compose_parts"].append(_node_kafka_block(cfg, ports, idx, n))
            p["env_keys"].add("KAFKA_PASSWORD")
            p["images"].append(("kafka-%s.tar" % cfg["arch"], kc["images"][cfg["arch"]],
                                "%s:%s" % (kc["image"], kc["tag"])))
            p["health_wait"].append("kafka")
            p["data_dirs"].append("kafka/data")
            p["port_checks"] += [ports["kafka_mh_inter"], ports["kafka_mh_ctrl"], ports["kafka_mh_sasl"]]

    # ---- MySQL 节点 (主节点先于从库, 由 distribute 按计划顺序执行) ----
    for s in ("mysql8", "mysql57"):
        if not is_multihost(cfg, s):
            continue
        meta = catalog["services"][s]
        mh = cfg["multihost"][s]
        # 主库
        p = acc_of(mh["master"])
        p["roles"].append("%s#master" % s)
        p["compose_parts"].append(_node_mysql_block(cfg, catalog, s, "master"))
        p["env_keys"].add("MYSQL_ROOT_PASSWORD")
        p["images"].append(("%s-%s.tar" % (s, cfg["arch"]), meta["images"][cfg["arch"]],
                            "%s:%s" % (meta["image"], meta["tag"])))
        p["conf_files"]["conf/%s/my.cnf" % s] = TPL_DIR / "mysql-my.cnf"
        p["health_wait"].append(s)
        p["data_dirs"] += ["%s/data" % s, "%s/log" % s]
        p["chown_dirs"].append("%s/log" % s)
        p["port_checks"].append(ports[s])
        # 主库建 repl 账号 (幂等)
        pw = cfg["secrets"].get("MYSQL_ROOT_PASSWORD", "")
        p["post_sql"].append((s,
            "CREATE USER IF NOT EXISTS 'repl'@'%%' IDENTIFIED BY '%s'; "
            "ALTER USER 'repl'@'%%' IDENTIFIED BY '%s'; "
            "GRANT REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO 'repl'@'%%'; FLUSH PRIVILEGES;" % (pw, pw)))
        # 备份按节点累积: mysql8/mysql57 主库可同节点, 逐一记录容器名
        p.setdefault("backup_svcs", []).append(s)
        # 从库 (每台一个)
        for r, idx in enumerate(mh["replicas"]):
            rp = acc_of(idx)
            rp["roles"].append("%s#replica%d" % (s, r + 1))
            rp["compose_parts"].append(_node_mysql_block(cfg, catalog, s, "replica", r))
            rp["env_keys"].add("MYSQL_ROOT_PASSWORD")
            rp["images"].append(("%s-%s.tar" % (s, cfg["arch"]), meta["images"][cfg["arch"]],
                                 "%s:%s" % (meta["image"], meta["tag"])))
            rp["conf_files"]["conf/%s/my.cnf" % s] = TPL_DIR / "mysql-my.cnf"
            rp["health_wait"].append(s)
            rp["data_dirs"] += ["%s/data" % s, "%s/log" % s]
            rp["chown_dirs"].append("%s/log" % s)
            rp["port_checks"].append(ports["%s_replica" % s])
            master_ip = servers[mh["master"]]["ip"]
            mport = ports[s]
            # 幂等: install-node.sh 可重复执行, 重跑时复制线程可能已在运行(ERROR 3081), 先停再清再配
            if s == "mysql8":
                sql = ("STOP REPLICA; RESET REPLICA ALL; "
                       "CHANGE REPLICATION SOURCE TO SOURCE_HOST='%s', SOURCE_PORT=%d, SOURCE_USER='repl', "
                       "SOURCE_PASSWORD='%s', SOURCE_AUTO_POSITION=1, GET_MASTER_PUBLIC_KEY=1; START REPLICA; "
                       "SET GLOBAL read_only=1; SET GLOBAL super_read_only=1;") % (master_ip, mport, pw)
            else:
                sql = ("STOP SLAVE; RESET SLAVE ALL; "
                       "CHANGE MASTER TO MASTER_HOST='%s', MASTER_PORT=%d, MASTER_USER='repl', "
                       "MASTER_PASSWORD='%s', MASTER_AUTO_POSITION=1; START SLAVE; "
                       "SET GLOBAL read_only=1; SET GLOBAL super_read_only=1;") % (master_ip, mport, pw)
            rp["post_sql"].append((s, sql))

    # ---- Redis 哨兵节点 ----
    if is_multihost(cfg, "redis"):
        meta = catalog["services"]["redis"]
        mh = cfg["multihost"]["redis"]
        master_ip = servers[mh["master"]]["ip"]
        mport = ports["redis"]
        # 主库机
        p = acc_of(mh["master"])
        p["roles"].append("redis#master")
        p["compose_parts"].append(_node_redis_blocks(cfg, catalog, "master", master_ip))
        p["env_keys"].add("REDIS_PASSWORD")
        p["images"].append(("redis-%s.tar" % cfg["arch"], meta["images"][cfg["arch"]],
                            "%s:%s" % (meta["image"], meta["tag"])))
        p["conf_files"]["conf/redis/redis.conf"] = TPL_DIR / "redis.conf"
        p["conf_files"]["conf/redis/sentinel.conf"] = _node_sentinel_conf(cfg, master_ip, mport, master_ip)
        p["health_wait"] += ["redis", "redis-sentinel"]
        p["data_dirs"] += ["redis/data", "redis/log"]
        p["chown_dirs"] += ["redis/log", "redis/sentinel.conf:999"]
        p["port_checks"] += [ports["redis"], ports.get("redis_sentinel", 26379)]
        # 从库机 (2 台)
        for r, idx in enumerate(mh["replicas"]):
            rp = acc_of(idx)
            rp["roles"].append("redis#replica%d" % (r + 1))
            rp["compose_parts"].append(_node_redis_blocks(cfg, catalog, "replica", master_ip))
            rp["env_keys"].add("REDIS_PASSWORD")
            rp["images"].append(("redis-%s.tar" % cfg["arch"], meta["images"][cfg["arch"]],
                                 "%s:%s" % (meta["image"], meta["tag"])))
            rp["conf_files"]["conf/redis/redis.conf"] = TPL_DIR / "redis.conf"
            rp["conf_files"]["conf/redis/sentinel.conf"] = _node_sentinel_conf(cfg, master_ip, mport,
                                                                               servers[idx]["ip"])
            rp["health_wait"] += ["redis", "redis-sentinel"]
            rp["data_dirs"] += ["redis/data", "redis/log"]
            rp["chown_dirs"] += ["redis/log", "redis/sentinel.conf:999"]
            rp["port_checks"] += [ports["redis_replica"], ports.get("redis_sentinel", 26379)]

    # ---- Java 运行时节点 (勾选 Java 部署目标的服务器; 可与中间件角色共存, 也可独立成纯 Java 节点) ----
    jcfg = cfg.get("java") or {}
    if jcfg.get("enabled") and jcfg.get("versions"):
        for idx in (jcfg.get("targets") or []):
            if idx == "local":
                continue
            p = acc_of(int(idx))
            p["java_versions"] = ["jdk%s" % v for v in jcfg["versions"]]
            p["roles"].append("java")

    # ---- 组装 (主节点优先: mysql master > redis master > kafka > 其余) ----
    def role_rank(idx):
        r = acc[idx]["roles"]
        if any(x.endswith("#master") for x in r):
            return 0
        if any(x.startswith("kafka") for x in r):
            return 1
        return 2

    for idx in sorted(acc, key=role_rank):
        p = acc[idx]
        # 主库节点备份配置(该节点全部主库服务一次性生成, mysql8/mysql57 同节点都进 MYSQL_SERVICES)
        if p.get("backup_svcs"):
            p["backup"] = _node_backup_files(cfg, p["backup_svcs"])
        p["java_default"] = ("jdk%s" % (jcfg.get("default") or "")) if p["java_versions"] else ""
        p["compose"] = ("# 由 packer.py 自动生成 (多机节点: %s)\nname: %s-%s\n\nservices:\n" % (
            p["name"], cfg["project"], p["name"])) + "\n".join(p["compose_parts"]) + \
            "\n\nnetworks:\n  app-network:\n    driver: bridge\n"
        env = ["# 由 packer.py 自动生成 (权限600, 节点 %s)" % p["name"], "TZ=Asia/Shanghai"]
        if "MYSQL_ROOT_PASSWORD" in p["env_keys"]:
            env.append("MYSQL_ROOT_PASSWORD=%s" % env_quote(cfg["secrets"]["MYSQL_ROOT_PASSWORD"]))
        if "REDIS_PASSWORD" in p["env_keys"]:
            env.append("REDIS_PASSWORD=%s" % env_quote(cfg["secrets"]["REDIS_PASSWORD"]))
        if "KAFKA_PASSWORD" in p["env_keys"]:
            env.append("KAFKA_PASSWORD=%s" % env_quote(cfg["secrets"]["KAFKA_PASSWORD"]))
        p["env"] = "\n".join(env) + "\n"
        p["summary"] = ["%s  %s@%s (SSH %d, %s)" % (p["name"], p["server"]["user"], p["server"]["ip"], p["server"]["ssh"],
                        "密码登录" if p["server"].get("password") else "免密登录")
                        + "  角色: " + ", ".join(p["roles"])]
        nodes.append(p)
    return nodes


def _node_sentinel_conf(cfg, master_ip, master_port, self_ip):
    """多机哨兵配置: monitor 指向主库节点 IP, announce 本机 IP 供对端/客户端回连"""
    port = cfg["ports"].get("redis_sentinel", 26379)
    pw = cfg["secrets"].get("REDIS_PASSWORD", "")
    return ("# 由打包器生成 (多机哨兵, 重新部署时自动覆盖)\n"
            "port 26379\n"
            "sentinel monitor mymaster %s %d 2\n"
            "sentinel auth-pass mymaster %s\n"
            "sentinel down-after-milliseconds mymaster 5000\n"
            "sentinel failover-timeout mymaster 60000\n"
            "sentinel parallel-syncs mymaster 1\n"
            "sentinel announce-ip %s\n"
            "sentinel announce-port %d\n" % (master_ip, master_port, pw, self_ip, port))


def _java_install_sh(node):
    """Java 运行时安装段 (Temurin JDK, 解压至 /usr/local/java/jdk<N>, profile.d 生效; 幂等, 不依赖 Docker)"""
    if not node.get("java_versions"):
        return ""
    return """# ---- Java 运行时 (Temurin JDK, 解压 /usr/local/java/jdk<N>, 写 /etc/profile.d/java.sh) ----
JAVA_VERSIONS=(%(jvers)s)
JAVA_DEFAULT=%(jdef)s
if [ ${#JAVA_VERSIONS[@]} -gt 0 ]; then
  log "安装 Java 运行时..."
  ( cd java 2>/dev/null && sha256sum -c jdk.sha256 --quiet ) || die "Java 包 sha256 校验失败 (java/jdk.sha256)"
  mkdir -p /usr/local/java
  for v in "${JAVA_VERSIONS[@]}"; do
    [ -f "./java/$v.tar.gz" ] || die "缺少 java/$v.tar.gz"
    dest="/usr/local/java/$v"
    if [ -x "$dest/bin/java" ]; then
      log "  $v 已安装, 跳过 ($("$dest/bin/java" -version 2>&1 | head -1))"
      continue
    fi
    log "  解压 $v -> $dest ..."
    rm -rf "$dest"; mkdir -p "$dest"
    tar -xzf "./java/$v.tar.gz" -C "$dest" --strip-components=1
    "$dest/bin/java" -version >/dev/null 2>&1 || die "$v 解压后无法运行 (架构与节点不符?)"
    log "  $v 就绪 ($("$dest/bin/java" -version 2>&1 | head -1))"
  done
  printf 'export JAVA_HOME=/usr/local/java/%%s\\nexport PATH=$JAVA_HOME/bin:$PATH\\n' "$JAVA_DEFAULT" > /etc/profile.d/java.sh
  chmod 644 /etc/profile.d/java.sh
  export JAVA_HOME="/usr/local/java/$JAVA_DEFAULT"; export PATH="$JAVA_HOME/bin:$PATH"
  log "Java 默认版本 $JAVA_DEFAULT ($(java -version 2>&1 | head -1))"
fi
""" % {"jvers": " ".join(node["java_versions"]), "jdef": node.get("java_default") or ""}


def _java_only_install_sh(node):
    """纯 Java 节点安装脚本 (无容器服务: 不要求节点已装 Docker/compose)"""
    return """#!/usr/bin/env bash
# 由 packer.py 自动生成 — 节点 %(name)s 安装脚本 (Java 运行时专用, 无容器服务, 可重复执行)
set -euo pipefail
cd "$(dirname "$0")"
NODE_NAME=%(name)s
log(){ printf '[node-%%s] %%s\\n' "$NODE_NAME" "$*"; }
die(){ printf '[node-%%s] 错误: %%s\\n' "$NODE_NAME" "$*" >&2; exit 1; }

%(java_sh)s

log "节点 $NODE_NAME 安装完成 (Java 运行时)"
""" % {"name": bash_quote(node["name"]), "java_sh": _java_install_sh(node)}


def gen_node_install_sh(cfg, node):
    """生成单节点安装脚本 (自包含: Java 运行时 -> 配置桥接 -> load 镜像 -> compose up -> 健康等待 -> 初始化 SQL -> 备份 crontab)"""
    if not node["compose_parts"] and node.get("java_versions"):
        return _java_only_install_sh(node)   # 纯 Java 节点: 无容器段
    java_sh = _java_install_sh(node)
    b = node.get("backup")
    backup_cron = b["CRON"] if b else ""
    backup_lines = ""
    if b:
        backup_lines = "\n".join([
            "BACKUP_DIR=%s" % b["BACKUP_DIR"],
            "MYSQL_ENABLED=%s" % b["MYSQL_ENABLED"],
            "MYSQL_SERVICES=%s" % b["MYSQL_SERVICES"],
            "MYSQL_KEEP=%s" % b["MYSQL_KEEP"],
            "MYSQL_ROOT_PASSWORD=%s" % b["MYSQL_ROOT_PASSWORD"],
        ])
    post_sql = " ".join(bash_quote("%s|%s" % (cn, sql)) for cn, sql in node["post_sql"])
    return """#!/usr/bin/env bash
# 由 packer.py 自动生成 — 节点 %(name)s 安装脚本 (自包含, 可重复执行)
set -euo pipefail
cd "$(dirname "$0")"
NODE_NAME=%(name)s
HEALTH_WAIT=(%(health_wait)s)
POST_SQL=(%(post_sql)s)
BACKUP_CRON=%(backup_cron)s

log(){ printf '[node-%%s] %%s\\n' "$NODE_NAME" "$*"; }
die(){ printf '[node-%%s] 错误: %%s\\n' "$NODE_NAME" "$*" >&2; exit 1; }

%(java_sh)s

command -v docker >/dev/null 2>&1 || die "本机未安装 Docker, 请先安装 Docker 再执行"
docker compose version >/dev/null 2>&1 || docker-compose version >/dev/null 2>&1 \\
  || die "缺少 Docker Compose, 请先安装"

set -a; source ./.env; set +a

# 桥接配置目录: 包内 conf/<svc>/* -> 部署目录 <svc>/* (与主部署机 deploy.sh 同规则)
if [ -d ./conf ]; then
  for d in ./conf/*/; do
    [ -d "$d" ] || continue
    name="$(basename "$d")"
    mkdir -p "./$name"
    cp -a "$d/." "./$name/"
  done
fi

# 预建数据/日志目录并收紧属主 (compose 挂载点; 权限不匹配会导致容器写不进)
for d in %(data_dirs)s; do
  [ -z "$d" ] && continue
  mkdir -p "./$d"
done
for d in %(chown_dirs)s; do
  [ -z "$d" ] && continue
  case "$d" in
    *:*) dir="${d%%%%:*}"; uid="${d##*:}" ;;
    *)   dir="$d"; uid="999" ;;
  esac
  [ -e "./$dir" ] && chown -R "$uid:$uid" "./$dir" 2>/dev/null \\
    || log "  提示: chown $uid:$uid $dir 未生效(可忽略, 视节点用户而定)"
done

log "加载节点镜像..."
for tar in images/*.tar; do
  [ -f "$tar" ] || { log "无镜像文件"; break; }
  docker load -i "$tar" >/dev/null && log "  $(basename "$tar") 已加载"
done
# 镜像源前缀重打标(离线机 compose 按原生短名取镜像, 不重打标会触发联网拉取失败)
if [ -f images/retag-mirrors.sh ]; then
  bash images/retag-mirrors.sh images.txt
fi

log "启动容器..."
if docker compose version >/dev/null 2>&1; then
  docker compose up -d
else
  docker-compose up -d
fi

log "等待容器健康 (最长 10 分钟)..."
for cn in "${HEALTH_WAIT[@]}"; do
  for i in $(seq 1 120); do
    st="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cn" 2>/dev/null || echo missing)"
    case "$st" in healthy|running) break;; esac
    [ "$i" = 120 ] && die "$cn 健康检查超时 (当前状态: $st)"
    sleep 5
  done
  log "  $cn 就绪 ($st)"
done

if [ "${#POST_SQL[@]}" -gt 0 ] && [ -n "${POST_SQL[0]:-}" ]; then
  log "执行初始化 SQL..."
  for entry in "${POST_SQL[@]}"; do
    cn="${entry%%|*}"; sql="${entry#*|}"
    docker exec -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$cn" mysql -uroot -e "$sql" \\
      || die "初始化 SQL 执行失败: ${sql:0:60}..."
    log "  $cn SQL 完成"
  done
fi

if [ -n "$BACKUP_CRON" ] && [ -f backup.sh ] && [ -f backup.conf ]; then
  log "配置定时备份: $BACKUP_CRON"
  chmod +x backup.sh && chmod 600 backup.conf
  INST_DIR="$(pwd)"
  CRON_LINE="$BACKUP_CRON /bin/bash $INST_DIR/backup.sh >> $INST_DIR/backup.log 2>&1"
  (crontab -l 2>/dev/null | grep -vF "$INST_DIR/backup.sh" ; echo "$CRON_LINE") | crontab -
  mkdir -p "$BACKUP_DIR"
fi

log "节点 $NODE_NAME 安装完成"
docker compose ps --format 'table {{.Name}}\\t{{.Status}}' 2>/dev/null || docker ps
""" % {
        "name": bash_quote(node["name"]),
        "java_sh": java_sh,
        "health_wait": " ".join(bash_quote(n) for n in node["health_wait"]),
        "data_dirs": " ".join(bash_quote(d) for d in node["data_dirs"]),
        "chown_dirs": " ".join(bash_quote(d) for d in node["chown_dirs"]),
        "post_sql": post_sql or '""',
        "backup_cron": bash_quote(backup_cron),
        "backup_lines": backup_lines,
    }


def gen_distribute_sh(cfg, nodes, catalog):
    """生成主部署机分发脚本: 预检 -> (可选装 compose) -> tar 管道传输 -> 远程安装"""
    arch = cfg["arch"]
    pkg_sub = "x86_64" if arch == "amd64" else "aarch64"
    nodes_str = " ".join(bash_quote("%s|%s|%s|%d|%s" % (n["name"], n["server"]["user"], n["server"]["ip"],
                                                        n["server"]["ssh"], n["server"].get("password") or ""))
                         for n in nodes)
    return """#!/usr/bin/env bash
# 由 packer.py 自动生成 — 多机节点分发安装 (在主部署机上执行)
# 用法: ./distribute.sh            分发全部节点 (按主节点优先顺序)
#       ./distribute.sh node2 ...  只分发指定节点 (重装/补发)
# 认证: 服务器池填写了密码 -> sshpass 密码登录 (需本机安装 sshpass);
#       留空 -> SSH 免密 (BatchMode, 需先 ssh-copy-id)
set -uo pipefail
cd "$(dirname "$0")"
NODES=(%(nodes)s)
DEPLOY_DIR=%(deploy_dir)s
COMPOSE_BIN="packages/%(pkg_sub)s/docker-compose-linux-%(pkg_sub)s"
FAILED=()

# 目标 IP 是否为本机地址(本机节点免 ssh/tar 管道: 环回传输边读边写同一目录, tar 报 "file changed")
is_local_ip() {
  [ "$1" = "127.0.0.1" ] && return 0
  ip -o addr show 2>/dev/null | awk '{print $4}' | grep -q "^$1/" && return 0
  hostname -I 2>/dev/null | grep -qw "$1" && return 0
  return 1
}

log(){ printf '\\033[32m[distribute]\\033[0m %%s\\n' "$*"; }
warn(){ printf '\\033[33m[distribute]\\033[0m %%s\\n' "$*"; }

[ ${#NODES[@]} -gt 0 ] || { echo "无多机节点, 无需分发"; exit 0; }

ONLY=("$@")
for NODE in "${NODES[@]}"; do
  IFS='|' read -r NAME RUSER RIP RSSH RPASS <<< "$NODE"
  if [ ${#ONLY[@]} -gt 0 ]; then
    keep=0; for o in "${ONLY[@]}"; do [ "$o" = "$NAME" ] && keep=1; done
    [ $keep -eq 0 ] && continue
  fi
  if [ -n "$RPASS" ] && command -v sshpass >/dev/null 2>&1; then
    export SSHPASS="$RPASS"
    SSHC=(sshpass -e ssh -p "$RSSH" -o StrictHostKeyChecking=no)
    SCPC=(sshpass -e scp -P "$RSSH" -o StrictHostKeyChecking=no -q)
    log "[$NAME] 使用密码登录 (sshpass)"
  else
    [ -n "$RPASS" ] && warn "[$NAME] 已填写密码但本机未安装 sshpass, 回退免密模式 (可 apt/yum install sshpass)"
    SSHC=(ssh -p "$RSSH" -o BatchMode=yes)
    SCPC=(scp -P "$RSSH" -q)
  fi
  log "[$NAME] 预检 $RUSER@$RIP:$RSSH ..."
  HAS_JAVA=0; HAS_SVCS=0
  [ -d "nodes/$NAME/java" ] && [ -n "$(ls "nodes/$NAME/java" 2>/dev/null)" ] && HAS_JAVA=1
  [ -f "nodes/$NAME/docker-compose.yml" ] && HAS_SVCS=1
  PRECHECK="true"; [ "$HAS_SVCS" = 1 ] && PRECHECK="command -v docker >/dev/null 2>&1"
  if ! "${SSHC[@]}" -o ConnectTimeout=8 "$RUSER@$RIP" "$PRECHECK" 2>/dev/null; then
    warn "[$NAME] SSH 连接失败或未安装 Docker, 已跳过 (密码模式请确认 sshpass 已安装; 免密模式请先 ssh-copy-id)"
    FAILED+=("$NAME"); continue
  fi
  if [ "$HAS_SVCS" = 1 ]; then
  if ! "${SSHC[@]}" "$RUSER@$RIP" \\
      "docker compose version >/dev/null 2>&1 || docker-compose version >/dev/null 2>&1" 2>/dev/null; then
    log "[$NAME] 节点缺少 compose, 从包内安装二进制..."
    if "${SCPC[@]}" "$COMPOSE_BIN" "$RUSER@$RIP:/tmp/mw-compose" \\
        && "${SSHC[@]}" "$RUSER@$RIP" "mkdir -p ~/.local/bin && mv /tmp/mw-compose ~/.local/bin/docker-compose && chmod +x ~/.local/bin/docker-compose"; then
      "${SSHC[@]}" "$RUSER@$RIP" "export PATH=\\$HOME/.local/bin:\\$PATH; docker-compose version >/dev/null 2>&1" \\
        || warn "[$NAME] compose 二进制已放置于 ~/.local/bin, 若 PATH 未包含请手动处理"
    else
      warn "[$NAME] compose 传输失败, 继续尝试安装"
    fi
  fi
  fi
  ND_SRC="$(pwd)/nodes/$NAME"
  ND_DST="$DEPLOY_DIR/nodes/$NAME"
  if is_local_ip "$RIP"; then
    # 本机节点: 包已解压在本机, 就地安装; 源==目标时原地用, 否则拷到部署目录子节点
    log "[$NAME] 本机节点($RIP), 就地安装 (免传输)"
    if [ ! "$ND_SRC" -ef "$ND_DST" ]; then
      mkdir -p "$ND_DST" && cp -a "$ND_SRC/." "$ND_DST/" || { warn "[$NAME] 节点目录准备失败"; FAILED+=("$NAME"); continue; }
    fi
    # Java 物料在包根 java/ (共享单份), 节点安装脚本按 ./java/ 取用
    if [ "$HAS_JAVA" = 1 ] && [ ! -d "$ND_DST/java" ]; then
      mkdir -p "$ND_DST" && cp -a java "$ND_DST/java" || { warn "[$NAME] Java 物料准备失败"; FAILED+=("$NAME"); continue; }
    fi
    if (cd "$ND_DST" && PATH="$HOME/.local/bin:$PATH" bash install-node.sh); then
      log "[$NAME] 完成"
    else
      warn "[$NAME] 安装脚本执行失败, 请登录节点查看日志"
      FAILED+=("$NAME")
    fi
    continue
  fi
  log "[$NAME] 传输节点包 -> $ND_DST ..."
  "${SSHC[@]}" "$RUSER@$RIP" "mkdir -p '$ND_DST'" || { FAILED+=("$NAME"); continue; }
  # 每节点独立子目录: 解到 $DEPLOY_DIR 根会覆盖主包 compose/.env
  if ! tar czf - -C "nodes/$NAME" . | "${SSHC[@]}" "$RUSER@$RIP" "tar xzf - -C '$ND_DST'"; then
    warn "[$NAME] 传输失败"; FAILED+=("$NAME"); continue
  fi
  if [ "$HAS_JAVA" = 1 ]; then
    log "[$NAME] 传输 Java 运行时物料 ..."
    if ! tar czf - -C java . | "${SSHC[@]}" "$RUSER@$RIP" "mkdir -p '$ND_DST/java' && tar xzf - -C '$ND_DST/java'"; then
      warn "[$NAME] Java 物料传输失败"; FAILED+=("$NAME"); continue
    fi
  fi
  log "[$NAME] 执行节点安装..."
  if "${SSHC[@]}" "$RUSER@$RIP" "cd '$ND_DST' && PATH=\\$HOME/.local/bin:\\$PATH bash install-node.sh"; then
    log "[$NAME] 完成"
  else
    warn "[$NAME] 安装脚本执行失败, 请登录节点查看日志"
    FAILED+=("$NAME")
  fi
done

echo ""
if [ ${#FAILED[@]} -gt 0 ]; then
  warn "以下节点未成功安装: ${FAILED[*]}"
  warn "修复后可重跑: ./distribute.sh ${FAILED[*]}"
  exit 1
fi
log "全部节点安装完成"
""" % {
        "nodes": nodes_str,
        "deploy_dir": bash_quote(cfg["deploy_dir"]),
        "pkg_sub": pkg_sub,
    }

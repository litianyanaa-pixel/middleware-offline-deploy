# -*- coding: utf-8 -*-
"""离线包 manifest.sh / 部署摘要 / images.txt 生成"""
from .catalog import PLUGINS, PROXYSQL_META
from .compose import backup_crons, plugin_ctx, resolve_db
from .multinode import gen_nodes
from .util import bash_quote, is_multihost, master_server_ip, proxysql_name, tr


def gen_manifest_sh(cfg, catalog, client_img, summary_lines, bundle_name=None):
    ports = cfg["ports"]
    db = cfg["db"]
    services = cfg["services"]
    topology = cfg.get("topology") or {}
    mh_all = set(k for k, v in (cfg.get("multihost") or {}).items() if v.get("enabled"))
    reps = cfg.get("replicas") or {}
    features = cfg.get("features") or {}

    data_dirs = []
    chown_dirs = []
    container_names = []
    port_keys = []
    for s in services:
        meta = catalog["services"][s]
        if s == "kafka" and topology.get("kafka") == "cluster":
            if is_multihost(cfg, "kafka"):
                continue   # 多机: Kafka 容器在各节点, 主部署机清单不含
            # 集群形态: 3 个 broker 容器; 宿主机端口为 kafka_c1..3, 单节点端口键不参与
            for n in (1, 2, 3):
                container_names.append("kafka%d" % n)
                data_dirs.append("kafka%d/data" % n)
                port_keys.append(ports["kafka_c%d" % n])
            continue
        if s in ("mysql8", "mysql57", "redis") and is_multihost(cfg, s):
            continue   # 多机: 容器在各节点
        if s in PLUGINS:
            container_names.append(PLUGINS[s].META.get("container_name", s))
        else:
            container_names.append(s if s != "xxljob" else "xxl-job")
        for p in meta["ports"]:
            if s == "kafka" and topology.get("kafka") == "cluster":
                continue   # 单机集群: 单节点端口键不参与
            port_keys.append(ports[p["key"]])
        for r in (cfg.get("extra_ports") or {}).get(s, []):
            port_keys.append(r["host"])
        if s in ("mysql57", "mysql8"):
            data_dirs += ["%s/data" % meta["data_dir"], "%s/log" % meta["data_dir"], "%s/init" % meta["data_dir"]]
            chown_dirs.append("%s/log" % meta["data_dir"])
        elif s == "redis":
            data_dirs += ["redis/data", "redis/log"]
            chown_dirs.append("redis/log")
        elif s == "nginx":
            data_dirs += ["nginx/conf", "nginx/html", "nginx/logs", "nginx/ssl"]
        else:
            data_dirs += ["%s/data" % meta["data_dir"], "%s/log" % meta["data_dir"]]

    local_mysql = ""
    if "mysql8" in services and not is_multihost(cfg, "mysql8"):
        local_mysql = "mysql8"
    elif "mysql57" in services and not is_multihost(cfg, "mysql57"):
        local_mysql = "mysql57"

    # 健康实测清单: 有 compose healthcheck 的容器等待 healthy, 其余 HTTP 实测
    health_wait, health_http = [], []
    for s in services:
        if s in ("mysql8", "mysql57", "redis") and is_multihost(cfg, s):
            continue   # 多机: 健康检查在节点 install-node.sh 内完成
        cn = PLUGINS[s].META.get("container_name", s) if s in PLUGINS else (s if s != "xxljob" else "xxl-job")
        if s == "kafka" and topology.get("kafka") == "cluster":
            if not is_multihost(cfg, "kafka"):
                health_wait += ["kafka1", "kafka2", "kafka3"]
            continue
        if s in ("mysql57", "mysql8", "redis", "minio") or s in PLUGINS:
            health_wait.append(cn)
        elif s == "nginx":
            health_http.append("%s|http://127.0.0.1:%d/" % (cn, ports["nginx_http"]))
        elif s == "nacos":
            health_http.append("%s|http://127.0.0.1:%d/" % (cn, ports["nacos_console"]))
        elif s == "xxljob":
            health_http.append("%s|http://127.0.0.1:%d/xxl-job-admin/" % (cn, ports["xxljob"]))
    if "minio" in services:
        health_http.append("minio|http://127.0.0.1:%d/minio/health/live" % ports["minio_api"])

    # 集群形态附加的容器/目录/端口/健康清单 (多机时容器在节点上, 主部署机清单跳过)
    if topology.get("mysql8") == "master-slave" and "mysql8" not in mh_all:
        for _r in range(reps.get("mysql8", 1)):
            _rsvc = "mysql8-replica" if _r == 0 else "mysql8-replica%d" % (_r + 1)
            _rport = "mysql8_replica" if _r == 0 else "mysql8_replica%d" % (_r + 1)
            container_names.append(_rsvc)
            data_dirs += ["%s/data" % _rsvc, "%s/log" % _rsvc]
            chown_dirs.append("%s/log" % _rsvc)
            port_keys.append(ports[_rport])
            health_wait.append(_rsvc)
    if topology.get("mysql57") == "master-slave" and "mysql57" not in mh_all:
        for _r in range(reps.get("mysql57", 1)):
            _rsvc = "mysql57-replica" if _r == 0 else "mysql57-replica%d" % (_r + 1)
            _rport = "mysql57_replica" if _r == 0 else "mysql57_replica%d" % (_r + 1)
            container_names.append(_rsvc)
            data_dirs += ["%s/data" % _rsvc, "%s/log" % _rsvc]
            chown_dirs.append("%s/log" % _rsvc)
            port_keys.append(ports[_rport])
            health_wait.append(_rsvc)
    if topology.get("redis") == "sentinel" and "redis" not in mh_all:
        container_names.append("redis-replica")
        data_dirs += ["redis-replica/data", "redis-replica/log"]
        chown_dirs.append("redis-replica/log")
        port_keys.append(ports["redis_replica"])
        health_wait.append("redis-replica")
        # 哨兵会把故障转移结果重写回自己的配置文件, 文件属主必须是容器内 redis(999)
        # (部署布局是平铺的: conf/<svc>/* 会被 deploy.sh 放到 <DEPLOY_DIR>/<svc>/ 下)
        chown_dirs.append("redis/sentinel.conf:999")
    for _px in ("mysql8", "mysql57"):
        if not features.get("proxysql_" + _px):
            continue
        nm = proxysql_name(cfg, _px)
        container_names.append(nm)
        data_dirs.append(nm + "/data")
        health_wait.append(nm)
        port_keys += [ports["proxysql_" + _px], ports["proxysql_" + _px + "_admin"]]

    def db_lines(app, prefix):
        conf = db.get(app)
        if not conf:
            return ["%(P)s_IMPORT=0" % {"P": prefix}]
        imported = 1
        return [
            "%(P)s_IMPORT=%(imp)d" % {"P": prefix, "imp": imported},
            "%(P)s_SQL=%(sql)s" % {"P": prefix, "sql": bash_quote("sql/%s.sql" % ("nacos" if app == "nacos" else "xxl-job"))},
            "%(P)s_SCHEMA=%(s)s" % {"P": prefix, "s": bash_quote(conf["schema"])},
            "%(P)s_DB_MODE=%(m)s" % {"P": prefix, "m": bash_quote(conf["mode"])},
            "%(P)s_DB_HOST=%(h)s" % {"P": prefix, "h": bash_quote(conf["host"])},
            "%(P)s_DB_HOST_IMPORT=%(h)s" % {"P": prefix, "h": bash_quote(conf.get("host_import", ""))},
            "%(P)s_DB_PORT=%(p)d" % {"P": prefix, "p": conf["port"]},
            "%(P)s_DB_USER=%(u)s" % {"P": prefix, "u": bash_quote(conf["user"])},
            "%(P)s_DB_PASSWORD=%(p)s" % {"P": prefix, "p": bash_quote(conf["password"])},
        ]

    lines = [
        "# 由 packer.py 自动生成 — deploy.sh 的全部决策来源, 服务器上请勿手改",
        "PROJECT=%s" % bash_quote(cfg["project"]),
        "PKG_ARCH=%s" % bash_quote(cfg["arch"]),
        "DEPLOY_LANG=%s" % bash_quote(cfg.get("lang", "zh")),
        "BUNDLE_NAME=%s" % bash_quote((bundle_name or "") + ".tar.gz"),
        "DEPLOY_DIR=%s" % bash_quote(cfg["deploy_dir"]),
        "DOCKER_DATA_ROOT=%s" % bash_quote(cfg["docker_data_root"]),
        "REGISTRY_MIRRORS=(%s)" % " ".join(bash_quote(m) for m in cfg["registry_mirrors"]),
        "PORTS_TO_CHECK=(%s)" % " ".join(str(p) for p in port_keys),
        "CONTAINER_NAMES=(%s)" % " ".join(container_names),
        "DATA_DIRS=(%s)" % " ".join(bash_quote(d) for d in data_dirs),
        "CHOWN_DIRS=(%s)" % " ".join(bash_quote(d) for d in chown_dirs),
        "LOCAL_MYSQL_SVC=%s" % bash_quote(local_mysql),
        "MYSQL_TOPOLOGY=%s" % bash_quote(topology.get(local_mysql, "single") if local_mysql else "single"),
        "MYSQL8_TOPOLOGY=%s" % bash_quote(topology.get("mysql8", "single")),
        "MYSQL57_TOPOLOGY=%s" % bash_quote(topology.get("mysql57", "single")),
        "REDIS_TOPOLOGY=%s" % bash_quote(topology.get("redis", "single")),
        "KAFKA_TOPOLOGY=%s" % bash_quote(topology.get("kafka", "single")),
        "MYSQL_MULTIHOST=%d" % (1 if (any(is_multihost(cfg, s) for s in ("mysql8", "mysql57"))) else 0),
        "MYSQL_MULTIHOST_SVC=%s" % bash_quote(next((s for s in ("mysql8", "mysql57") if is_multihost(cfg, s)), "")),
        "MYSQL_MULTIHOST_HOST=%s" % bash_quote(next((master_server_ip(cfg, s) for s in ("mysql8", "mysql57") if is_multihost(cfg, s)), "")),
        "MYSQL_MULTIHOST_PORT=%s" % next((str(ports[s]) for s in ("mysql8", "mysql57") if is_multihost(cfg, s)), "0"),
        "NODES_COUNT=%d" % len(gen_nodes(cfg, catalog)),
        "MYSQL_ROOT_PASSWORD=%s" % bash_quote(cfg["secrets"].get("MYSQL_ROOT_PASSWORD", "")),
        "MYSQL_CLIENT_IMG=%s" % bash_quote(client_img),
        "HEALTH_WAIT=(%s)" % " ".join(bash_quote(n) for n in health_wait),
        "HEALTH_HTTP=(%s)" % " ".join(bash_quote(h) for h in health_http),
    ]
    lines += db_lines("nacos", "NACOS")
    lines += db_lines("xxljob", "XXL")
    cl = cfg.get("cluster") or {}
    if cl.get("enabled"):
        lines += [
            "CLUSTER_ENABLED=1",
            "CLUSTER_KUBE_VERSION=%s" % bash_quote(cl["kube_version"]),
            "CLUSTER_MODE=%s" % bash_quote(cl["mode"]),
            "CLUSTER_ZONE=%s" % bash_quote(cl.get("zone") or ""),
            "CLUSTER_CNI=%s" % bash_quote(cl["cni_type"]),
            "CLUSTER_PROXY_MODE=%s" % bash_quote(cl["proxy_mode"]),
            "CLUSTER_HA_TYPE=%s" % bash_quote(cl.get("ha_type") or "local"),
            "CLUSTER_HA_VIP=%s" % bash_quote(cl.get("ha_vip") or ""),
            "CLUSTER_UPGRADE_TO=%s" % bash_quote(cl.get("upgrade_to") or ""),
            "CLUSTER_CERTS_RENEW_CRON=%s" % bash_quote(cl.get("certs_renew_cron") or ""),
            "CLUSTER_OS_DISTROS=(%s)" % " ".join(bash_quote(d) for d in cl["os_distros"]),
            "CLUSTER_NODES=(%s)" % " ".join(bash_quote("%s|%s|%s" % (n["name"], n["ip"], n["role"])) for n in cl["nodes"]),
            "CLUSTER_ONLY=%d" % (0 if services else 1),
            # 纯离线镜像导入参数(deploy-cluster.sh 用): sandbox 镜像与 containerd 数据目录
            "CLUSTER_SANDBOX_IMAGE=%s" % bash_quote("registry.k8s.io/pause:%s"
                                                    % (catalog["cluster"]["versions"][cl["kube_version"]].get("sandbox_image_tag") or "3.9")),
            "CLUSTER_CONTAINERD_DATA_ROOT=%s" % bash_quote((cl.get("components") or {}).get("containerd_root") or ""),
            # 卸载脚本用: 自定义 etcd/kubelet 数据目录(kk delete 只清默认路径, 这两个要兜底)
            "CLUSTER_ETCD_DATA_DIR=%s" % bash_quote((cl.get("components") or {}).get("etcd_dir") or ""),
            "CLUSTER_KUBELET_ROOT_DIR=%s" % bash_quote((cl.get("kubelet") or {}).get("root_dir") or ""),
            # 升级目标版本的 pause 镜像(与创建版本可能不同, 升级脚本需要改写 sandbox_image)
            "CLUSTER_UPGRADE_SANDBOX=%s" % bash_quote(
                ("registry.k8s.io/pause:%s" % ((catalog["cluster"]["versions"].get(cl.get("upgrade_to") or "") or {})
                                               .get("sandbox_image_tag") or "")) if cl.get("upgrade_to") else ""),
        ]
    b = cfg.get("backup") or {}
    lines += [
        "BACKUP_CRONS=(%s)" % " ".join(bash_quote("%s|%s" % (eng, cron)) for eng, cron in backup_crons(b)),
        "BACKUP_DIR=%s" % bash_quote(b.get("dir") or "/data/backup/db"),
    ]
    for s in services:
        if s in PLUGINS and hasattr(PLUGINS[s], "manifest_lines"):
            lines += PLUGINS[s].manifest_lines(cfg, ports, plugin_ctx(cfg))
    lines.append("SUMMARY_LINES=(%s)" % " ".join(bash_quote(l) for l in summary_lines))
    return "\n".join(lines) + "\n"


def build_summary_lines(cfg, catalog):
    ports = cfg["ports"]
    lines = []
    servers = cfg.get("servers") or []
    mh = cfg.get("multihost") or {}

    def mh_ip(svc, kind):
        """多机角色的节点 IP (kind: master/replica/brokers)"""
        m = mh.get(svc) or {}
        if kind == "master":
            return servers[m["master"]]["ip"] if isinstance(m.get("master"), int) and servers else "__NODE_IP__"
        if kind == "replicas":
            return [servers[i]["ip"] for i in m.get("replicas", []) if isinstance(i, int) and i < len(servers)]
        if kind == "brokers":
            return [servers[i]["ip"] for i in m.get("brokers", []) if isinstance(i, int) and i < len(servers)]
        return []

    if "nginx" in services_of(cfg):
        lines.append("NGINX        http://__IP__:%d   (配置目录 %s/nginx)|NGINX        http://__IP__:%d   (config dir %s/nginx)"
                     % (ports["nginx_http"], cfg["deploy_dir"], ports["nginx_http"], cfg["deploy_dir"]))
    if "mysql57" in services_of(cfg):
        if is_multihost(cfg, "mysql57"):
            mip = mh_ip("mysql57", "master")
            rips = mh_ip("mysql57", "replicas")
            lines.append("MySQL 5.7 多机主从  主库 %s:%d  从库 %s|MySQL 5.7 multi-host  master %s:%d  replicas %s"
                         % (mip, ports["mysql57"], ",".join("%s:%d" % (r, ports["mysql57_replica"]) for r in rips),
                            mip, ports["mysql57"], ",".join("%s:%d" % (r, ports["mysql57_replica"]) for r in rips)))
        else:
            lines.append("MySQL 5.7    __IP__:%d  mysql -h<ip> -P%d -uroot" % (ports["mysql57"], ports["mysql57"]))
            if (cfg.get("topology") or {}).get("mysql57") == "master-slave":
                lines.append("MySQL 5.7 从库 __IP__:%d  (只读, GTID 自动同步主库|replica, read-only, GTID sync from master)"
                             % ports["mysql57_replica"])
    if "mysql8" in services_of(cfg):
        if is_multihost(cfg, "mysql8"):
            mip = mh_ip("mysql8", "master")
            rips = mh_ip("mysql8", "replicas")
            lines.append("MySQL 8.0 多机主从  主库 %s:%d  从库 %s|MySQL 8.0 multi-host  master %s:%d  replicas %s"
                         % (mip, ports["mysql8"], ",".join("%s:%d" % (r, ports["mysql8_replica"]) for r in rips),
                            mip, ports["mysql8"], ",".join("%s:%d" % (r, ports["mysql8_replica"]) for r in rips)))
        else:
            lines.append("MySQL 8.0    __IP__:%d  mysql -h<ip> -P%d -uroot" % (ports["mysql8"], ports["mysql8"]))
            if (cfg.get("topology") or {}).get("mysql8") == "master-slave":
                lines.append("MySQL 8.0 从库 __IP__:%d  (只读, GTID 自动同步主库|replica, read-only, GTID sync from master)"
                             % ports["mysql8_replica"])
    for _px in ("mysql8", "mysql57"):
        if not (cfg.get("features") or {}).get("proxysql_" + _px):
            continue
        nm = "MySQL 8.0" if _px == "mysql8" else "MySQL 5.7"
        p1, p2 = ports["proxysql_" + _px], ports["proxysql_" + _px + "_admin"]
        lines.append("读写分离(%s 主从)  mysql -h<ip> -P%d -uroot  (写->主库 读->从库, 管理台 <ip>:%d admin)|"
                     "Read/write split (%s)  mysql -h<ip> -P%d -uroot  (writes->master reads->replicas, admin <ip>:%d admin)"
                     % (nm, p1, p2, nm, p1, p2))
    if "redis" in services_of(cfg):
        if is_multihost(cfg, "redis"):
            mip = mh_ip("redis", "master")
            rips = mh_ip("redis", "replicas")
            lines.append("Redis 多机哨兵  主库 %s:%d  从库 %s (每节点哨兵 :%d, 应用走 sentinel 协议)|"
                         "Redis multi-host sentinel  master %s:%d  replicas %s (sentinel :%d per node, clients use sentinel protocol)"
                         % (mip, ports["redis"], ",".join("%s:%d" % (r, ports["redis_replica"]) for r in rips),
                            ports.get("redis_sentinel", 26379), mip, ports["redis"],
                            ",".join("%s:%d" % (r, ports["redis_replica"]) for r in rips),
                            ports.get("redis_sentinel", 26379)))
        else:
            lines.append("Redis        __IP__:%d" % ports["redis"])
            if (cfg.get("topology") or {}).get("redis") == "sentinel":
                lines.append("Redis 哨兵    主+从+3哨兵 (从库 __IP__:%d, 应用走 sentinel 协议 redis-sentinel:26379)|"
                             "Redis Sentinel master+replica+3 sentinels (replica __IP__:%d, clients use sentinel protocol redis-sentinel:26379)"
                             % (ports["redis_replica"], ports["redis_replica"]))
    if "nacos" in services_of(cfg):
        lines.append("Nacos 控制台  http://__IP__:%d/nacos  (账号见 .env)|Nacos console http://__IP__:%d/nacos  (credentials in .env)" % (ports["nacos_console"], ports["nacos_console"]))
    if "xxljob" in services_of(cfg):
        lines.append("XXL-Job      http://__IP__:%d/xxl-job-admin  (admin/见 .env)|XXL-Job      http://__IP__:%d/xxl-job-admin  (admin/see .env)" % (ports["xxljob"], ports["xxljob"]))
    if "minio" in services_of(cfg):
        lines.append("MinIO        API http://__IP__:%d  控制台 http://__IP__:%d|MinIO        API http://__IP__:%d  console http://__IP__:%d"
                     % (ports["minio_api"], ports["minio_console"], ports["minio_api"], ports["minio_console"]))
    ctx = plugin_ctx(cfg)
    for s in services_of(cfg):
        if s not in PLUGINS:
            continue
        mod = PLUGINS[s]
        if hasattr(mod, "summary_lines"):
            lines += mod.summary_lines(cfg, ports, ctx)
        else:
            meta = catalog["services"][s]
            first = (meta.get("ports") or [{}])[0]
            port0 = ports.get(first.get("key", ""), first.get("default", 0))
            lines.append("%s  http://__IP__:%d" % (meta.get("label", s), port0))
    # 双语约定: 全部摘要行 "zh|en" 形式, 按 cfg.lang 取半边
    return [tr(l, cfg.get("lang", "zh")) for l in lines]


def services_of(cfg):
    return cfg["services"]


def gen_images_txt(cfg, catalog):
    """返回 [(bundle内文件名, 仓库相对路径, 短名)]; 多机部署的服务镜像改入 nodes/<name>/images/"""
    arch = cfg["arch"]
    kafka_cluster = "kafka" in cfg["services"] and (cfg.get("topology") or {}).get("kafka") == "cluster"
    kafka_mh = is_multihost(cfg, "kafka")
    items = []
    for s in cfg["services"]:
        if s == "kafka" and kafka_cluster:
            if kafka_mh:
                continue   # 多机: bitnami 镜像随节点分发, 不进主包 images.txt
            # 集群形态: 打包 bitnami 镜像(短名归一后为 bitnami/kafka:3.7.0, 归一化规则兼容)
            kc = getattr(PLUGINS.get("kafka"), "CLUSTER", None)
            if kc:
                items.append(("kafka-%s.tar" % arch, kc["images"][arch], "%s:%s" % (kc["image"], kc["tag"])))
            continue
        if s in ("mysql8", "mysql57", "redis") and is_multihost(cfg, s):
            continue   # 多机: 镜像随节点分发
        meta = catalog["services"][s]
        rel = meta["images"][arch]
        items.append(("%s-%s.tar" % (s, arch), rel, "%s:%s" % (meta["image"], meta["tag"])))
    needs_client, client_img = resolve_db(cfg)
    has_local_mysql = any(s in cfg["services"] for s in ("mysql57", "mysql8")) and not any(
        is_multihost(cfg, s) for s in ("mysql57", "mysql8"))
    if needs_client and not has_local_mysql:
        meta = catalog["services"]["mysql8"]
        rel = meta["images"][arch]
        items.append(("mysql8-%s.tar" % arch, rel, client_img))
    if (cfg.get("features") or {}).get("proxysql"):
        items.append(("proxysql-%s.tar" % arch, PROXYSQL_META["images"][arch],
                      "%s:%s" % (PROXYSQL_META["image"], PROXYSQL_META["tag"])))
    return items

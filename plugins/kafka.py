# -*- coding: utf-8 -*-
"""
Apache Kafka 插件
  - 单节点: KRaft (无 ZooKeeper), apache/kafka 官方镜像
  - 集群:   3 节点 KRaft 组合模式(broker+controller) + SASL_PLAINTEXT 多用户,
            bitnami/kafka 镜像(与生产参考环境一致)

镜像 tar 放置位置:
  单节点  warehouse/images/kafka/4.3.1/amd64.tar / arm64.tar
  集群    warehouse/images/kafka/3.7.0/amd64.tar / arm64.tar   (bitnami)
"""
SERVICE_KEY = "kafka"

# 集群形态元数据(packer.py 读取: 物料检查/镜像打包按形态切换)
CLUSTER = {
    "image": "bitnami/kafka",
    "tag": "3.7.0",
    "images": {
        "amd64": "warehouse/images/kafka/3.7.0/amd64.tar",
        "arm64": "warehouse/images/kafka/3.7.0/arm64.tar",
    },
}

META = {
    "cat": "cache",
    "label": "Kafka",
    "image": "apache/kafka",
    "tag": "4.3.1",
    "color": "#231F20",
    "data_dir": "kafka",
    "supported_arch": ["amd64", "arm64"],
    "images": {
        "amd64": "warehouse/images/kafka/4.3.1/amd64.tar",
        "arm64": "warehouse/images/kafka/4.3.1/arm64.tar",
    },
    "ports": [
        {"key": "kafka",      "label": "Kafka 端口(容器网络)|Kafka port (container network)", "default": 9092, "container": 9092},
        {"key": "kafka_host", "label": "Kafka 端口(宿主机)|Kafka port (host)",   "default": 9094, "container": 9094},
    ],
    "secrets": [
        {"key": "KAFKA_PASSWORD", "label": "Kafka SASL 密码(用户 admin)|Kafka SASL password (user admin)",
         "default": "Kf7mQ2vXwRz7xZq", "services": ["kafka"], "secret": True},
    ],
    "note": "单节点 KRaft; 集群形态为 3 节点 KRaft + SASL_PLAINTEXT(bitnami), 在「部署形态」中切换",
}

# 宿主机监听 SASL/SCRAM; 监听名用 HOST(不带下划线), 否则镜像的 env->properties
# 转换会把 listener.name.host 里的下划线也替换成点, JAAS 配置无法落地
_COMPOSE = """
  %(svc)s:
    image: %(image)s
    container_name: %(svc)s
    restart: always
    environment:
      TZ: ${TZ}
      KAFKA_NODE_ID: 1
      KAFKA_PROCESS_ROLES: broker,controller
      KAFKA_LISTENERS: PLAINTEXT://:9092,CONTROLLER://:9093,HOST://:9094
      KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://kafka:9092,HOST://localhost:9094
      KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT,HOST:SASL_PLAINTEXT
      KAFKA_CONTROLLER_LISTENER_NAMES: CONTROLLER
      KAFKA_CONTROLLER_QUORUM_VOTERS: 1@kafka:9093
      KAFKA_INTER_BROKER_LISTENER_NAME: PLAINTEXT
      # 宿主机监听 SASL/SCRAM-SHA-256 (用户 admin / KAFKA_PASSWORD, deploy.sh 启动后注册 SCRAM 用户)
      # 机制级 JAAS 键必须含连字符(4.x 忽略无机制名的通用键); env 键里的连字符会被原样保留
      KAFKA_LISTENER_NAME_HOST_SASL_ENABLED_MECHANISMS: SCRAM-SHA-256
      KAFKA_LISTENER_NAME_HOST_SCRAM-SHA-256_SASL_JAAS_CONFIG: 'org.apache.kafka.common.security.scram.ScramLoginModule required username="admin" password="${KAFKA_PASSWORD}";'
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
      KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR: 1
      KAFKA_TRANSACTION_STATE_LOG_MIN_ISR: 1
      CLUSTER_ID: AsRf1X5vZU3qNYJowN/FcA
    ports:
      - "%(port)d:9092"
      - "%(hport)d:9094"
    volumes:
      - ./kafka/data:/var/lib/kafka/data
    healthcheck:
      test: ["CMD-SHELL", "/opt/kafka/bin/kafka-broker-api-versions.sh --bootstrap-server localhost:9092 > /dev/null 2>&1 || exit 1"]
      interval: 15s
      timeout: 10s
      retries: 10
      start_period: 60s
    networks:
      - app-network"""

# ---------------------------------------------------------------- 集群形态
# 3 节点 KRaft 组合模式(每节点同时是 broker+controller), 依据生产参考环境:
#   - 内网监听 INTERNAL(PLAINTEXT): 容器网络内组件(kafka-ui 等)免鉴权直连
#   - 宿主机监听 HOST(SASL_PLAINTEXT): 多用户 PLAIN 鉴权, 广播地址 KAFKA_HOST_IP
#     (.env 默认 localhost, deploy.sh 部署时自动改写为服务器真实 IP, 供跨宿主机客户端使用)
#   - StandardAuthorizer + ANONYMOUS 超级用户: 内网 PLAINTEXT 客户端无需 ACL 即可读写
# 集群端口: 每节点只暴露 SASL 宿主机端口(kafka_c1/c2/c3 -> 容器 9094)
_CLUSTER_TMPL = """
  kafka%(n)d:
    image: %(image)s
    container_name: kafka%(n)d
    restart: always
    user: root
    environment:
      TZ: ${TZ}
      KAFKA_ENABLE_KRAFT: "yes"
      KAFKA_KRAFT_CLUSTER_ID: iZWRiSqjZAlYwlKEqHFQWI
      KAFKA_CFG_PROCESS_ROLES: broker,controller
      KAFKA_CFG_NODE_ID: %(n)d
      KAFKA_CFG_CONTROLLER_LISTENER_NAMES: CONTROLLER
      KAFKA_CFG_LISTENERS: INTERNAL://:9092,CONTROLLER://:9093,HOST://:9094
      KAFKA_CFG_LISTENER_SECURITY_PROTOCOL_MAP: INTERNAL:PLAINTEXT,CONTROLLER:PLAINTEXT,HOST:SASL_PLAINTEXT
      KAFKA_CFG_ADVERTISED_LISTENERS: INTERNAL://kafka%(n)d:9092,HOST://${KAFKA_HOST_IP:-localhost}:%(hport)d
      KAFKA_CFG_INTER_BROKER_LISTENER_NAME: INTERNAL
      KAFKA_CFG_CONTROLLER_QUORUM_VOTERS: 1@kafka1:9093,2@kafka2:9093,3@kafka3:9093
      KAFKA_CFG_SASL_ENABLED_MECHANISMS: PLAIN
      KAFKA_CFG_SASL_MECHANISM_INTER_BROKER_PROTOCOL: PLAIN
      # 多用户: 需要更多账号时在 .env 里按逗号扩展 (KAFKA_CLIENT_USERS/KAFKA_CLIENT_PASSWORDS 数量须一致)
      KAFKA_CLIENT_USERS: admin
      KAFKA_CLIENT_PASSWORDS: ${KAFKA_PASSWORD}
      KAFKA_CFG_SUPER_USERS: User:admin;User:ANONYMOUS
      KAFKA_CFG_AUTHORIZER_CLASS_NAME: org.apache.kafka.metadata.authorizer.StandardAuthorizer
      KAFKA_CFG_ALLOW_EVERYONE_IF_NO_ACL_FOUND: "false"
      KAFKA_HEAP_OPTS: -Xmx768M -Xms384M
      ALLOW_PLAINTEXT_LISTENER: "yes"
    ports:
      - "%(hport)d:9094"
    volumes:
      - ./kafka%(n)d/data:/bitnami/kafka
    healthcheck:
      test: ["CMD-SHELL", "/opt/bitnami/kafka/bin/kafka-broker-api-versions.sh --bootstrap-server localhost:9092 > /dev/null 2>&1 || exit 1"]
      interval: 15s
      timeout: 10s
      retries: 12
      start_period: 90s
    networks:
      - app-network"""

_CLUSTER_ID = "iZWRiSqjZAlYwlKEqHFQWI"


def _is_cluster(cfg):
    return ((cfg.get("topology") or {}).get("kafka") == "cluster")


def compose_block(cfg, ports, ctx):
    if _is_cluster(cfg):
        return "".join(
            _CLUSTER_TMPL % {"n": n, "image": "%s:%s" % (CLUSTER["image"], CLUSTER["tag"]),
                             "hport": ports["kafka_c%d" % n]}
            for n in (1, 2, 3))
    return _COMPOSE % {
        "svc": SERVICE_KEY,
        "image": "%s:%s" % (META["image"], META["tag"]),
        "port": ports["kafka"],
        "hport": ports["kafka_host"],
    }


def env_lines(cfg, ports, ctx):
    v = str(ctx["secrets"].get("KAFKA_PASSWORD", ""))
    q = '"%s"' % v if ("#" in v or " " in v) else v
    lines = ["KAFKA_PASSWORD=%s" % q]
    if _is_cluster(cfg):
        # 集群宿主机监听广播地址; deploy.sh 部署时会自动改写为服务器真实 IP
        lines.append("KAFKA_HOST_IP=localhost")
    return lines


def manifest_lines(cfg, ports, ctx):
    if _is_cluster(cfg):
        # bitnami 由 KAFKA_CLIENT_USERS/PASSWORDS 自动建 PLAIN 用户, 无需 SCRAM 注册;
        # 容器以 root 运行, 数据目录无需 chown
        return ["KAFKA_AUTH=0"]
    # apache/kafka 镜像以 uid 1000 运行; KAFKA_AUTH=1 触发 deploy.sh 注册 SCRAM 用户
    return ['CHOWN_DIRS+=("kafka/data:1000")',
            "KAFKA_AUTH=1",
            "KAFKA_PASSWORD=%s" % ctx["bash_quote"](str(ctx["secrets"].get("KAFKA_PASSWORD", "")))]


def summary_lines(cfg, ports, ctx):
    if _is_cluster(cfg):
        return [
            "Kafka 集群    3 节点 KRaft (bootstrap: kafka1:9092,kafka2:9092,kafka3:9092 容器网络内免鉴权)|"
            "Kafka cluster 3-node KRaft (bootstrap: kafka1:9092,kafka2:9092,kafka3:9092, PLAINTEXT inside the container network)",
            "Kafka(宿主机) __IP__:%d / %d / %d  (SASL_PLAINTEXT, 用户 admin, 密码见 .env)|"
            "Kafka (host)  __IP__:%d / %d / %d  (SASL_PLAINTEXT, user admin, password in .env)"
            % (ports["kafka_c1"], ports["kafka_c2"], ports["kafka_c3"],
               ports["kafka_c1"], ports["kafka_c2"], ports["kafka_c3"]),
        ]
    return ["Kafka         __IP__:%d (宿主机监听, SASL/SCRAM 用户 admin, 密码见 .env)|"
            "Kafka         __IP__:%d (host listener, SASL/SCRAM user admin, password in .env)"
            % (ports["kafka_host"], ports["kafka_host"]),
            "Kafka(容器网络) kafka:9092 (内网免鉴权)|Kafka (container network) kafka:9092 (PLAINTEXT, no auth)"]

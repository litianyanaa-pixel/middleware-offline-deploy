# -*- coding: utf-8 -*-
"""
Apache Kafka 插件 (KRaft 单节点, 无 ZooKeeper)

镜像 tar 放置位置:
  warehouse/images/kafka/4.3.1/amd64.tar
  warehouse/images/kafka/4.3.1/arm64.tar
"""
SERVICE_KEY = "kafka"

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
        {"key": "kafka",      "label": "Kafka 端口(容器网络)", "default": 9092, "container": 9092},
        {"key": "kafka_host", "label": "Kafka 端口(宿主机)",   "default": 9094, "container": 9094},
    ],
    "secrets": [
        {"key": "KAFKA_PASSWORD", "label": "Kafka SASL 密码(用户 admin)",
         "default": "Kf7mQ2vXwRz7xZq", "services": ["kafka"], "secret": True},
    ],
    "note": "KRaft 单节点; 宿主机监听开启 SASL/SCRAM 鉴权(用户 admin), 容器网络内 kafka:9092 免鉴权供内部组件使用",
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


def compose_block(cfg, ports, ctx):
    return _COMPOSE % {
        "svc": SERVICE_KEY,
        "image": "%s:%s" % (META["image"], META["tag"]),
        "port": ports["kafka"],
        "hport": ports["kafka_host"],
    }


def env_lines(cfg, ports, ctx):
    v = str(ctx["secrets"].get("KAFKA_PASSWORD", ""))
    q = '"%s"' % v if ("#" in v or " " in v) else v
    return ["KAFKA_PASSWORD=%s" % q]


def manifest_lines(cfg, ports, ctx):
    # apache/kafka 镜像以 uid 1000 运行; KAFKA_AUTH=1 触发 deploy.sh 注册 SCRAM 用户
    return ['CHOWN_DIRS+=("kafka/data:1000")',
            "KAFKA_AUTH=1",
            "KAFKA_PASSWORD=%s" % ctx["bash_quote"](str(ctx["secrets"].get("KAFKA_PASSWORD", "")))]


def summary_lines(cfg, ports, ctx):
    return ["Kafka         __IP__:%d (宿主机监听, SASL/SCRAM 用户 admin, 密码见 .env)"
            % ports["kafka_host"],
            "Kafka(容器网络) kafka:9092 (内网免鉴权)"]

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
    "secrets": [],
    "note": "KRaft 单节点; 容器内应用连 kafka:9092, 宿主机应用连 localhost:9094",
}


def compose_block(cfg, ports, ctx):
    return """
  %(svc)s:
    image: %(image)s
    container_name: %(svc)s
    restart: always
    environment:
      TZ: ${TZ}
      KAFKA_NODE_ID: 1
      KAFKA_PROCESS_ROLES: broker,controller
      KAFKA_LISTENERS: PLAINTEXT://:9092,CONTROLLER://:9093,PLAINTEXT_HOST://:9094
      KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://kafka:9092,PLAINTEXT_HOST://localhost:9094
      KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT,PLAINTEXT_HOST:PLAINTEXT
      KAFKA_CONTROLLER_LISTENER_NAMES: CONTROLLER
      KAFKA_CONTROLLER_QUORUM_VOTERS: 1@kafka:9093
      KAFKA_INTER_BROKER_LISTENER_NAME: PLAINTEXT
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
      KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR: 1
      KAFKA_TRANSACTION_STATE_LOG_MIN_ISR: 1
      KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS: 0
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
      - app-network""" % {
        "svc": SERVICE_KEY,
        "image": "%s:%s" % (META["image"], META["tag"]),
        "port": ports["kafka"],
        "hport": ports["kafka_host"],
    }


def manifest_lines(cfg, ports, ctx):
    # apache/kafka 镜像以 uid 1000 运行
    return ['CHOWN_DIRS+=("kafka/data:1000")']


def summary_lines(cfg, ports, ctx):
    return ["Kafka         __IP__:%d (宿主机监听) / kafka:9092 (容器网络)" % ports["kafka_host"]]

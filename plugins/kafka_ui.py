# -*- coding: utf-8 -*-
"""
Kafka UI 插件 (Kafka 可视化管理, 连接同网络的 kafka 服务)

镜像 tar 放置位置:
  warehouse/images/kafka-ui/v0.7.2/amd64.tar
  warehouse/images/kafka-ui/v0.7.2/arm64.tar
"""
SERVICE_KEY = "kafka-ui"

META = {
    "cat": "cache",
    "label": "Kafka UI",
    "image": "provectuslabs/kafka-ui",
    "tag": "v0.7.2",
    "color": "#0F6FFF",
    "data_dir": "kafka-ui",
    "supported_arch": ["amd64", "arm64"],
    "images": {
        "amd64": "warehouse/images/kafka-ui/v0.7.2/amd64.tar",
        "arm64": "warehouse/images/kafka-ui/v0.7.2/arm64.tar",
    },
    "ports": [
        {"key": "ui", "label": "Kafka UI 端口", "default": 18090, "container": 8080},
    ],
    "secrets": [],
    "note": "需与 Kafka 同选; 自动连接 kafka:9092, 浏览器打开 http://ip:18090",
}


def compose_block(cfg, ports, ctx):
    return """
  %(svc)s:
    image: %(image)s
    container_name: kafka-ui
    restart: always
    environment:
      TZ: ${TZ}
      DYNAMIC_CONFIG_ENABLED: "true"
      KAFKA_CLUSTERS_0_NAME: local
      KAFKA_CLUSTERS_0_BOOTSTRAPSERVERS: kafka:9092
    depends_on:
      - kafka
    ports:
      - "%(port)d:8080"
    networks:
      - app-network""" % {
        "svc": SERVICE_KEY,
        "image": "%s:%s" % (META["image"], META["tag"]),
        "port": ports["ui"],
    }


def manifest_lines(cfg, ports, ctx):
    # 容器内无健康检查(镜像无 wget/curl), 用宿主机 HTTP 实测兜底
    return ['HEALTH_HTTP+=("kafka-ui|http://127.0.0.1:%d")' % ports["ui"]]


def summary_lines(cfg, ports, ctx):
    return ["Kafka UI       http://__IP__:%d" % ports["ui"]]

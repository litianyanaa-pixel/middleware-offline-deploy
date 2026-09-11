# -*- coding: utf-8 -*-
"""
Node Exporter 插件 (主机指标采集, 配合 Prometheus + Grafana)

镜像 tar 放置位置:
  warehouse/images/node-exporter/v1.12.1/amd64.tar
  warehouse/images/node-exporter/v1.12.1/arm64.tar
"""
SERVICE_KEY = "node-exporter"

META = {
    "label": "Node Exporter",
    "image": "prom/node-exporter",
    "tag": "v1.12.1",
    "color": "#E6522C",
    "data_dir": "node-exporter",
    "supported_arch": ["amd64", "arm64"],
    "images": {
        "amd64": "warehouse/images/node-exporter/v1.12.1/amd64.tar",
        "arm64": "warehouse/images/node-exporter/v1.12.1/arm64.tar",
    },
    "ports": [
        {"key": "metrics", "label": "指标端口", "default": 9100, "container": 9100},
    ],
    "secrets": [],
    "note": "采集宿主机 CPU/内存/磁盘/网络指标; Grafana 预配主机监控仪表盘(需同选 Grafana)",
}


def compose_block(cfg, ports, ctx):
    return """
  %(svc)s:
    image: %(image)s
    container_name: %(svc)s
    restart: always
    pid: "host"
    command:
      - --path.rootfs=/host
    ports:
      - "%(port)d:9100"
    volumes:
      - /:/host:ro
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:9100/metrics >/dev/null || exit 1"]
      interval: 15s
      timeout: 10s
      retries: 5
      start_period: 20s
    networks:
      - app-network""" % {
        "svc": SERVICE_KEY,
        "image": "%s:%s" % (META["image"], META["tag"]),
        "port": ports["metrics"],
    }


def summary_lines(cfg, ports, ctx):
    return ["Node Exporter http://__IP__:%d/metrics  (主机指标)" % ports["metrics"]]

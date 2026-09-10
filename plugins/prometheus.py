# -*- coding: utf-8 -*-
"""
Prometheus 中间件插件 (监控采集, 自带默认抓取配置)

镜像 tar 放置位置:
  warehouse/images/prometheus/v3.14.0/amd64.tar
  warehouse/images/prometheus/v3.14.0/arm64.tar
"""
SERVICE_KEY = "prometheus"

META = {
    "label": "Prometheus",
    "image": "prom/prometheus",
    "tag": "v3.14.0",
    "color": "#E6522C",
    "data_dir": "prometheus",
    "supported_arch": ["amd64", "arm64"],
    "images": {
        "amd64": "warehouse/images/prometheus/v3.14.0/amd64.tar",
        "arm64": "warehouse/images/prometheus/v3.14.0/arm64.tar",
    },
    "ports": [
        {"key": "prom", "label": "Prometheus 端口", "default": 9090, "container": 9090},
    ],
    "secrets": [],
    "note": "使用镜像内置默认配置(抓取自身); 自定义抓取目标请改部署目录 prometheus/config/prometheus.yml",
}


def compose_block(cfg, ports, ctx):
    return """
  %(svc)s:
    image: %(image)s
    container_name: %(svc)s
    restart: always
    user: "65534:65534"
    environment:
      TZ: ${TZ}
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.path=/prometheus
      - --storage.tsdb.retention.time=15d
    ports:
      - "%(port)d:9090"
%(extra_ports)s    volumes:
      - ./prometheus/data:/prometheus
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:9090/-/ready || exit 1"]
      interval: 15s
      timeout: 10s
      retries: 8
      start_period: 30s
    networks:
      - app-network""" % {
        "svc": SERVICE_KEY,
        "image": "%s:%s" % (META["image"], META["tag"]),
        "port": ports["prom"],
        "extra_ports": ctx["extra_ports_lines"](SERVICE_KEY),
    }


def manifest_lines(cfg, ports, ctx):
    # prometheus 容器以 nobody(65534) 运行
    return ['CHOWN_DIRS+=("prometheus/data:65534")']


def summary_lines(cfg, ports, ctx):
    return ["Prometheus    http://__IP__:%d" % ports["prom"]]

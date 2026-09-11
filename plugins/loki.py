# -*- coding: utf-8 -*-
"""
Loki 插件 (日志聚合, 单节点文件存储; 配合 promtail 采集容器日志)

镜像 tar 放置位置:
  warehouse/images/loki/3.7.7/amd64.tar
  warehouse/images/loki/3.7.7/arm64.tar
"""
SERVICE_KEY = "loki"

META = {
    "label": "Loki",
    "image": "grafana/loki",
    "tag": "3.7.7",
    "color": "#F9C24A",
    "data_dir": "loki",
    "supported_arch": ["amd64", "arm64"],
    "images": {
        "amd64": "warehouse/images/loki/3.7.7/amd64.tar",
        "arm64": "warehouse/images/loki/3.7.7/arm64.tar",
    },
    "ports": [
        {"key": "loki", "label": "Loki 端口", "default": 3100, "container": 3100},
    ],
    "secrets": [],
    "note": "日志入库: 需同选 Promtail 采集容器日志; 查询: Grafana Explore 或 http://ip:3100",
}


def compose_block(cfg, ports, ctx):
    return """
  %(svc)s:
    image: %(image)s
    container_name: %(svc)s
    restart: always
    user: "10001:10001"
    command: -config.file=/etc/loki/loki-config.yml
    ports:
      - "%(port)d:3100"
    volumes:
      - ./conf/loki/loki-config.yml:/etc/loki/loki-config.yml:ro
      - ./loki/data:/loki
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:3100/ready || exit 1"]
      interval: 15s
      timeout: 10s
      retries: 8
      start_period: 30s
    networks:
      - app-network""" % {
        "svc": SERVICE_KEY,
        "image": "%s:%s" % (META["image"], META["tag"]),
        "port": ports["loki"],
    }


def manifest_lines(cfg, ports, ctx):
    # loki 容器以 uid 10001 运行
    return ['CHOWN_DIRS+=("loki/data:10001")']


def summary_lines(cfg, ports, ctx):
    return ["Loki          http://__IP__:%d  (日志查询走 Grafana Explore)" % ports["loki"]]

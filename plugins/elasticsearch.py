# -*- coding: utf-8 -*-
"""
Elasticsearch 中间件插件 (单节点, 默认关闭安全认证, 适合内网)

镜像 tar 放置位置:
  warehouse/images/elasticsearch/9.3.0/amd64.tar
  warehouse/images/elasticsearch/9.3.0/arm64.tar
"""
SERVICE_KEY = "elasticsearch"

META = {
    "cat": "obs",
    "label": "Elasticsearch",
    "image": "elasticsearch",
    "tag": "9.3.0",
    "color": "#F5B93E",
    "data_dir": "es",
    "supported_arch": ["amd64", "arm64"],
    "images": {
        "amd64": "warehouse/images/elasticsearch/9.3.0/amd64.tar",
        "arm64": "warehouse/images/elasticsearch/9.3.0/arm64.tar",
    },
    "ports": [
        {"key": "es_http", "label": "HTTP 端口", "default": 9200, "container": 9200},
    ],
    "secrets": [],
    "note": "部署时自动设置 vm.max_map_count=262144; 默认关闭安全认证(内网), 堆内存 512m 可按需调大",
}


def compose_block(cfg, ports, ctx):
    return """
  %(svc)s:
    image: %(image)s
    container_name: %(svc)s
    restart: always
    environment:
      TZ: ${TZ}
      discovery.type: single-node
      xpack.security.enabled: "false"
      ES_JAVA_OPTS: -Xms512m -Xmx512m
    ports:
      - "%(port)d:9200"
%(extra_ports)s    volumes:
      - ./es/data:/usr/share/elasticsearch/data
    healthcheck:
      test: ["CMD-SHELL", "curl -fs http://localhost:9200/_cluster/health || exit 1"]
      interval: 15s
      timeout: 10s
      retries: 12
      start_period: 90s
    networks:
      - app-network""" % {
        "svc": SERVICE_KEY,
        "image": "%s:%s" % (META["image"], META["tag"]),
        "port": ports["es_http"],
        "extra_ports": ctx["extra_ports_lines"](SERVICE_KEY),
    }


def manifest_lines(cfg, ports, ctx):
    return [
        'CHOWN_DIRS+=("es/data:1000")',
        'SYSCTL_SETTINGS=("vm.max_map_count=262144")',
    ]


def summary_lines(cfg, ports, ctx):
    return ["Elasticsearch http://__IP__:%d  (安全认证已关闭)" % ports["es_http"]]

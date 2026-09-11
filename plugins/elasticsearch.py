# -*- coding: utf-8 -*-
"""
Elasticsearch 中间件插件 (单节点, 内置安全认证: elastic 账号 / ELASTIC_PASSWORD)

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
    "secrets": [
        {"key": "ELASTIC_PASSWORD", "label": "Elasticsearch elastic 密码",
         "default": "Es6#wR9tYu@Pq4nN", "services": ["elasticsearch"], "secret": True},
    ],
    "note": "部署时自动设置 vm.max_map_count=262144; 内网单节点未启 TLS(HTTP 明文+账号认证), 堆内存 512m 可按需调大",
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
      # 安全认证开启(账号 elastic / ELASTIC_PASSWORD); 内网单节点显式关 TLS
      xpack.security.enabled: "true"
      xpack.security.http.ssl.enabled: "false"
      xpack.security.transport.ssl.enabled: "false"
      ELASTIC_PASSWORD: ${ELASTIC_PASSWORD}
      ES_JAVA_OPTS: -Xms512m -Xmx512m
    ports:
      - "%(port)d:9200"
%(extra_ports)s    volumes:
      - ./es/data:/usr/share/elasticsearch/data
    healthcheck:
      test: ["CMD-SHELL", "curl -fs -u elastic:\\"$$ELASTIC_PASSWORD\\" http://localhost:9200/_cluster/health || exit 1"]
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


def env_lines(cfg, ports, ctx):
    v = str(ctx["secrets"].get("ELASTIC_PASSWORD", ""))
    q = '"%s"' % v if ("#" in v or " " in v) else v
    return ["ELASTIC_PASSWORD=%s" % q]


def manifest_lines(cfg, ports, ctx):
    return [
        'CHOWN_DIRS+=("es/data:1000")',
        'SYSCTL_SETTINGS=("vm.max_map_count=262144")',
    ]


def summary_lines(cfg, ports, ctx):
    return ["Elasticsearch http://__IP__:%d  (账号 elastic, 密码见 .env)" % ports["es_http"]]

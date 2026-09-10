# -*- coding: utf-8 -*-
"""
Kibana 中间件插件 (Elasticsearch 可视化, 版本须与 ES 完全一致)

镜像 tar 放置位置:
  warehouse/images/kibana/9.3.0/amd64.tar
  warehouse/images/kibana/9.3.0/arm64.tar
"""
SERVICE_KEY = "kibana"

META = {
    "label": "Kibana",
    "image": "kibana",
    "tag": "9.3.0",
    "color": "#00BFB3",
    "data_dir": "kibana",
    "supported_arch": ["amd64", "arm64"],
    "images": {
        "amd64": "warehouse/images/kibana/9.3.0/amd64.tar",
        "arm64": "warehouse/images/kibana/9.3.0/arm64.tar",
    },
    "ports": [
        {"key": "kibana_web", "label": "Kibana 端口", "default": 5601, "container": 5601},
    ],
    "secrets": [],
    "note": "版本必须与 Elasticsearch 完全一致; 自动连接同网络内的 elasticsearch 服务",
}


def compose_block(cfg, ports, ctx):
    return """
  %(svc)s:
    image: %(image)s
    container_name: %(svc)s
    restart: always
    environment:
      TZ: ${TZ}
      ELASTICSEARCH_HOSTS: http://elasticsearch:9200
    depends_on:
      - elasticsearch
    ports:
      - "%(port)d:5601"
%(extra_ports)s    networks:
      - app-network""" % {
        "svc": SERVICE_KEY,
        "image": "%s:%s" % (META["image"], META["tag"]),
        "port": ports["kibana_web"],
        "extra_ports": ctx["extra_ports_lines"](SERVICE_KEY),
    }


def manifest_lines(cfg, ports, ctx):
    # kibana 容器内无健康检查(启动慢), 用宿主机 HTTP 实测兜底
    return ['HEALTH_HTTP+=("kibana|http://127.0.0.1:5601")']


def summary_lines(cfg, ports, ctx):
    return ["Kibana         http://__IP__:%d" % ports["kibana_web"]]

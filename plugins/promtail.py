# -*- coding: utf-8 -*-
"""
Promtail 插件 (采集 docker 容器 json-file 日志推送 Loki)

镜像 tar 放置位置:
  warehouse/images/promtail/3.6.11/amd64.tar
  warehouse/images/promtail/3.6.11/arm64.tar
"""
SERVICE_KEY = "promtail"

META = {
    "label": "Promtail",
    "image": "grafana/promtail",
    "tag": "3.6.11",
    "color": "#C7C7CB",
    "data_dir": "promtail",
    "supported_arch": ["amd64", "arm64"],
    "images": {
        "amd64": "warehouse/images/promtail/3.6.11/amd64.tar",
        "arm64": "warehouse/images/promtail/3.6.11/arm64.tar",
    },
    "ports": [],
    "secrets": [],
    "note": "需与 Loki 同选; 读取 /var/lib/docker/containers 日志(需 json-file 驱动, daemon.json 已配置)",
}


def compose_block(cfg, ports, ctx):
    return """
  %(svc)s:
    image: %(image)s
    container_name: %(svc)s
    restart: always
    command: -config.file=/etc/promtail/promtail-config.yml
    volumes:
      - ./promtail/promtail-config.yml:/etc/promtail/promtail-config.yml:ro
      - ${DOCKER_DATA_ROOT}/containers:/var/lib/docker/containers:ro
      - ./promtail/positions:/positions
    depends_on:
      - loki
    networks:
      - app-network""" % {
        "svc": SERVICE_KEY,
        "image": "%s:%s" % (META["image"], META["tag"]),
    }


def conf_files(cfg, ports, ctx):
    return {
        "conf/promtail/promtail-config.yml": (
            "# 由打包器生成 (Promtail 采集 docker 容器日志, 重新部署时自动覆盖)\n"
            "server:\n"
            "  http_listen_port: 9080\n"
            "positions:\n"
            "  filename: /positions/positions.yaml\n"
            "clients:\n"
            "  - url: http://loki:3100/loki/api/v1/push\n"
            "scrape_configs:\n"
            "  - job_name: docker-containers\n"
            "    static_configs:\n"
            "      - targets: [localhost]\n"
            "        labels:\n"
            "          job: docker\n"
            "          __path__: /var/lib/docker/containers/*/*.log\n"
            "    pipeline_stages:\n"
            "      - json:\n"
            "          expressions:\n"
            "            stream: stream\n"
            "            time: time\n"
            "      - labels:\n"
            "          stream:\n"
        ),
    }


def summary_lines(cfg, ports, ctx):
    return ["Promtail      采集容器日志 -> Loki (内部组件, 无对外端口)"]

# -*- coding: utf-8 -*-
"""
Grafana 中间件插件 (可视化面板, 默认数据源可手动添加 Prometheus)

镜像 tar 放置位置:
  warehouse/images/grafana/13.2.1/amd64.tar
  warehouse/images/grafana/13.2.1/arm64.tar
"""
SERVICE_KEY = "grafana"

META = {
    "label": "Grafana",
    "image": "grafana/grafana",
    "tag": "13.2.1",
    "color": "#F46800",
    "data_dir": "grafana",
    "supported_arch": ["amd64", "arm64"],
    "images": {
        "amd64": "warehouse/images/grafana/13.2.1/amd64.tar",
        "arm64": "warehouse/images/grafana/13.2.1/arm64.tar",
    },
    "ports": [
        {"key": "web", "label": "Grafana 控制台端口", "default": 3000, "container": 3000},
    ],
    "secrets": [
        {"key": "GRAFANA_ADMIN_USER",     "label": "Grafana 管理员账号", "default": "admin",           "services": ["grafana"], "secret": False},
        {"key": "GRAFANA_ADMIN_PASSWORD", "label": "Grafana 管理员密码", "default": "Xy625#608iVR@HZs", "services": ["grafana"], "secret": True},
    ],
    "note": "管理员账号密码见 .env; 数据源建议填同网络的 Prometheus: http://prometheus:9090",
}


def compose_block(cfg, ports, ctx):
    return """
  %(svc)s:
    image: %(image)s
    container_name: %(svc)s
    restart: always
    environment:
      TZ: ${TZ}
      GF_SECURITY_ADMIN_USER: ${GRAFANA_ADMIN_USER}
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD}
    ports:
      - "%(port)d:3000"
%(extra_ports)s    volumes:
      - ./grafana/data:/var/lib/grafana
    healthcheck:
      test: ["CMD-SHELL", "curl -fs http://localhost:3000/api/health || exit 1"]
      interval: 15s
      timeout: 10s
      retries: 8
      start_period: 40s
    networks:
      - app-network""" % {
        "svc": SERVICE_KEY,
        "image": "%s:%s" % (META["image"], META["tag"]),
        "port": ports["web"],
        "extra_ports": ctx["extra_ports_lines"](SERVICE_KEY),
    }


def env_lines(cfg, ports, ctx):
    def q(v):
        v = str(v)
        return '"%s"' % v if ("#" in v or " " in v) else v
    return [
        "GRAFANA_ADMIN_USER=%s" % q(ctx["secrets"].get("GRAFANA_ADMIN_USER", "")),
        "GRAFANA_ADMIN_PASSWORD=%s" % q(ctx["secrets"].get("GRAFANA_ADMIN_PASSWORD", "")),
    ]


def manifest_lines(cfg, ports, ctx):
    # grafana 容器以 uid 472 运行
    return ['CHOWN_DIRS+=("grafana/data:472")']


def summary_lines(cfg, ports, ctx):
    return ["Grafana        http://__IP__:%d  (账号见 .env)" % ports["web"]]

# -*- coding: utf-8 -*-
"""
MongoDB 中间件插件 (稳定版 8.0.x, 带内置 root 账号)

镜像 tar 放置位置:
  warehouse/images/mongodb/8.0.30/amd64.tar
  warehouse/images/mongodb/8.0.30/arm64.tar
"""
SERVICE_KEY = "mongodb"

META = {
    "cat": "db",
    "label": "MongoDB",
    "image": "mongo",
    "tag": "8.0.30",
    "color": "#47A248",
    "data_dir": "mongodb",
    "supported_arch": ["amd64", "arm64"],
    "images": {
        "amd64": "warehouse/images/mongodb/8.0.30/amd64.tar",
        "arm64": "warehouse/images/mongodb/8.0.30/arm64.tar",
    },
    "ports": [
        {"key": "mongo", "label": "MongoDB 端口", "default": 27017, "container": 27017},
    ],
    "secrets": [
        {"key": "MONGO_INITDB_ROOT_USERNAME", "label": "MongoDB root 账号", "default": "root",             "services": ["mongodb"], "secret": False},
        {"key": "MONGO_INITDB_ROOT_PASSWORD", "label": "MongoDB root 密码", "default": "zB6Dm#1NGqez.QX6", "services": ["mongodb"], "secret": True},
    ],
    "note": "root 账号仅在数据目录首次初始化时生效; 连接串 mongodb://<user>:<pwd>@ip:27017",
}


def compose_block(cfg, ports, ctx):
    return """
  %(svc)s:
    image: %(image)s
    container_name: %(svc)s
    restart: always
    environment:
      TZ: ${TZ}
      MONGO_INITDB_ROOT_USERNAME: ${MONGO_INITDB_ROOT_USERNAME}
      MONGO_INITDB_ROOT_PASSWORD: ${MONGO_INITDB_ROOT_PASSWORD}
    ports:
      - "%(port)d:27017"
%(extra_ports)s    volumes:
      - ./mongodb/data:/data/db
    healthcheck:
      test: ["CMD-SHELL", "mongosh --quiet --eval 'db.adminCommand({ ping: 1 })' | grep -q '\\"ok\\"'"]
      interval: 15s
      timeout: 10s
      retries: 8
      start_period: 40s
    networks:
      - app-network""" % {
        "svc": SERVICE_KEY,
        "image": "%s:%s" % (META["image"], META["tag"]),
        "port": ports["mongo"],
        "extra_ports": ctx["extra_ports_lines"](SERVICE_KEY),
    }


def env_lines(cfg, ports, ctx):
    def q(v):
        v = str(v)
        return '"%s"' % v if ("#" in v or " " in v) else v
    return [
        "MONGO_INITDB_ROOT_USERNAME=%s" % q(ctx["secrets"].get("MONGO_INITDB_ROOT_USERNAME", "")),
        "MONGO_INITDB_ROOT_PASSWORD=%s" % q(ctx["secrets"].get("MONGO_INITDB_ROOT_PASSWORD", "")),
    ]


def summary_lines(cfg, ports, ctx):
    return ["MongoDB       __IP__:%d  mongosh -h<ip> -u<root账号> -p" % ports["mongo"]]

# -*- coding: utf-8 -*-
"""
PostgreSQL 中间件插件

镜像 tar 放置位置(与内置中间件同一套仓库规范):
  warehouse/images/postgres/15.19/amd64.tar
  warehouse/images/postgres/15.19/arm64.tar
"""
SERVICE_KEY = "postgres"

META = {
    "cat": "db",
    "label": "PostgreSQL",
    "image": "postgres",
    "tag": "15.19",
    "color": "#336791",
    "data_dir": "postgres",
    "supported_arch": ["amd64", "arm64"],
    "images": {
        "amd64": "warehouse/images/postgres/15.19/amd64.tar",
        "arm64": "warehouse/images/postgres/15.19/arm64.tar",
    },
    "ports": [
        {"key": "pgsql", "label": "PostgreSQL 端口", "default": 15432, "container": 5432},
    ],
    "secrets": [
        {"key": "POSTGRES_PASSWORD", "label": "Postgres postgres 密码",
         "default": "B7x2IpT5Z+M~I#Mg", "services": ["postgres"], "secret": True},
    ],
    "note": "",
}


def compose_block(cfg, ports, ctx):
    return """
  %(svc)s:
    image: %(image)s
    container_name: %(svc)s
    restart: always
    shm_size: 256mb
    environment:
      TZ: ${TZ}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: postgres
    ports:
      - "%(port)d:5432"
%(extra_ports)s    volumes:
      - ./postgres/data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d postgres"]
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 30s
    networks:
      - app-network""" % {
        "svc": SERVICE_KEY,
        "image": "%s:%s" % (META["image"], META["tag"]),
        "port": ports["pgsql"],
        "extra_ports": ctx["extra_ports_lines"](SERVICE_KEY),
    }


def env_lines(cfg, ports, ctx):
    # 与 packer.env_quote 同规则: 含 # 或空格时加双引号
    v = str(ctx["secrets"].get("POSTGRES_PASSWORD", ""))
    q = '"%s"' % v if ("#" in v or " " in v) else v
    return ["POSTGRES_PASSWORD=%s" % q]


def summary_lines(cfg, ports, ctx):
    return ["PostgreSQL   __IP__:%d  psql -h<ip> -p%d -Upostgres" % (ports["pgsql"], ports["pgsql"])]

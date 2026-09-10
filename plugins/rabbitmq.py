# -*- coding: utf-8 -*-
"""
RabbitMQ 中间件插件 (management 版, 含控制台)

镜像 tar 放置位置:
  warehouse/images/rabbitmq/4.3.5-management/amd64.tar
  warehouse/images/rabbitmq/4.3.5-management/arm64.tar
"""
SERVICE_KEY = "rabbitmq"

META = {
    "label": "RabbitMQ",
    "image": "rabbitmq",
    "tag": "4.3.5-management",
    "color": "#FF6600",
    "data_dir": "rabbitmq",
    "supported_arch": ["amd64", "arm64"],
    "images": {
        "amd64": "warehouse/images/rabbitmq/4.3.5-management/amd64.tar",
        "arm64": "warehouse/images/rabbitmq/4.3.5-management/arm64.tar",
    },
    "ports": [
        {"key": "amqp", "label": "AMQP 端口",       "default": 5672,  "container": 5672},
        {"key": "mgmt", "label": "管理控制台端口", "default": 15672, "container": 15672},
    ],
    "secrets": [
        {"key": "RABBITMQ_USER",     "label": "RabbitMQ 账号",     "default": "admin",             "services": ["rabbitmq"], "secret": False},
        {"key": "RABBITMQ_PASSWORD", "label": "RabbitMQ 密码",     "default": "aFeP#iNVGj39hCZ7",  "services": ["rabbitmq"], "secret": True},
    ],
    "note": "控制台 http://ip:15672, AMQP 端口 5672; 账号密码见 .env",
}


def compose_block(cfg, ports, ctx):
    return """
  %(svc)s:
    image: %(image)s
    container_name: %(svc)s
    restart: always
    environment:
      TZ: ${TZ}
      RABBITMQ_DEFAULT_USER: ${RABBITMQ_USER}
      RABBITMQ_DEFAULT_PASS: ${RABBITMQ_PASSWORD}
    ports:
      - "%(amqp)d:5672"
      - "%(mgmt)d:15672"
%(extra_ports)s    volumes:
      - ./rabbitmq/data:/var/lib/rabbitmq
    healthcheck:
      test: ["CMD-SHELL", "rabbitmq-diagnostics -q ping"]
      interval: 15s
      timeout: 10s
      retries: 8
      start_period: 60s
    networks:
      - app-network""" % {
        "svc": SERVICE_KEY,
        "image": "%s:%s" % (META["image"], META["tag"]),
        "amqp": ports["amqp"],
        "mgmt": ports["mgmt"],
        "extra_ports": ctx["extra_ports_lines"](SERVICE_KEY),
    }


def env_lines(cfg, ports, ctx):
    def q(v):
        v = str(v)
        return '"%s"' % v if ("#" in v or " " in v) else v
    return [
        "RABBITMQ_USER=%s" % q(ctx["secrets"].get("RABBITMQ_USER", "")),
        "RABBITMQ_PASSWORD=%s" % q(ctx["secrets"].get("RABBITMQ_PASSWORD", "")),
    ]


def summary_lines(cfg, ports, ctx):
    return ["RabbitMQ      http://__IP__:%d  控制台 (AMQP __IP__:%d, 账号见 .env)" % (ports["mgmt"], ports["amqp"])]

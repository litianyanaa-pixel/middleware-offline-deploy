# -*- coding: utf-8 -*-
"""
Loki 插件 (日志聚合, 单节点文件存储; 配合 promtail 采集容器日志)

镜像 tar 放置位置:
  warehouse/images/loki/3.7.7/amd64.tar
  warehouse/images/loki/3.7.7/arm64.tar
"""
SERVICE_KEY = "loki"

META = {
    "cat": "obs",
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
      - ./loki/loki-config.yml:/etc/loki/loki-config.yml:ro
      - ./loki/data:/loki
    networks:
      - app-network""" % {
        "svc": SERVICE_KEY,
        "image": "%s:%s" % (META["image"], META["tag"]),
        "port": ports["loki"],
    }


def conf_files(cfg, ports, ctx):
    # Loki 3.x 单机文件存储最小配置(容器内 /loki, uid 10001)
    return {
        "conf/loki/loki-config.yml": (
            "# 由打包器生成 (Loki 单机配置, 重新部署时自动覆盖)\n"
            "auth_enabled: false\n\n"
            "server:\n"
            "  http_listen_port: 3100\n\n"
            "common:\n"
            "  instance_addr: 127.0.0.1\n"
            "  path_prefix: /loki\n"
            "  storage:\n"
            "    filesystem:\n"
            "      chunks_directory: /loki/chunks\n"
            "      rules_directory: /loki/rules\n"
            "  replication_factor: 1\n"
            "  ring:\n"
            "    kvstore:\n"
            "      store: inmemory\n\n"
            "schema_config:\n"
            "  configs:\n"
            "    - from: \"2024-01-01\"\n"
            "      store: tsdb\n"
            "      object_store: filesystem\n"
            "      schema: v13\n"
            "      index:\n"
            "        prefix: index_\n"
            "        period: 24h\n\n"
            "ingester:\n"
            "  wal:\n"
            "    enabled: true\n"
            "    dir: /loki/wal\n\n"
            "limits_config:\n"
            "  reject_old_samples: true\n"
            "  reject_old_samples_max_age: 168h\n"
            "  allow_structured_metadata: true\n")
    }


def manifest_lines(cfg, ports, ctx):
    # loki 容器以 uid 10001 运行
    return ['CHOWN_DIRS+=("loki/data:10001")',
            # 镜像为 distroless(无 shell/wget), 容器内健康检查不可用, 改由宿主机 curl 实测 /ready
            'HEALTH_HTTP+=("loki|http://127.0.0.1:%d/ready")' % ports["loki"]]


def summary_lines(cfg, ports, ctx):
    return ["Loki          http://__IP__:%d  (日志查询走 Grafana Explore)" % ports["loki"]]

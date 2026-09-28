# -*- coding: utf-8 -*-
"""
Prometheus 中间件插件 (监控采集, 自带默认抓取配置)

镜像 tar 放置位置:
  warehouse/images/prometheus/v3.14.0/amd64.tar
  warehouse/images/prometheus/v3.14.0/arm64.tar
"""
SERVICE_KEY = "prometheus"

META = {
    "cat": "obs",
    "label": "Prometheus",
    "image": "prom/prometheus",
    "tag": "v3.14.0",
    "color": "#E6522C",
    "data_dir": "prometheus",
    "supported_arch": ["amd64", "arm64"],
    "images": {
        "amd64": "warehouse/images/prometheus/v3.14.0/amd64.tar",
        "arm64": "warehouse/images/prometheus/v3.14.0/arm64.tar",
    },
    "ports": [
        {"key": "prom", "label": "Prometheus 端口|Prometheus port", "default": 9090, "container": 9090},
    ],
    "secrets": [],
    "note": "选配 Alertmanager 后自动生成抓取与告警链路配置; 否则用镜像内置默认配置|Auto-generates scrape & alerting config when Alertmanager is selected; otherwise built-in default",
}


def compose_block(cfg, ports, ctx):
    has_am = "alertmanager" in ctx["cfg"]["services"]
    cfg_mount = "\n      - ./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro" if has_am else ""
    return """
  %(svc)s:
    image: %(image)s
    container_name: %(svc)s
    restart: always
    user: "65534:65534"
    environment:
      TZ: ${TZ}
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.path=/prometheus
      - --storage.tsdb.retention.time=15d
    ports:
      - "%(port)d:9090"
%(extra_ports)s    volumes:
      - ./prometheus/data:/prometheus%(cfg_mount)s%(rules_mount)s
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:9090/-/ready || exit 1"]
      interval: 15s
      timeout: 10s
      retries: 8
      start_period: 30s
    networks:
      - app-network""" % {
        "svc": SERVICE_KEY,
        "image": "%s:%s" % (META["image"], META["tag"]),
        "port": ports["prom"],
        "extra_ports": ctx["extra_ports_lines"](SERVICE_KEY),
        "cfg_mount": cfg_mount,
        "rules_mount": "\n      - ./prometheus/rules:/etc/prometheus/rules:ro" if has_am else "",
    }


def manifest_lines(cfg, ports, ctx):
    # prometheus 容器以 nobody(65534) 运行
    return ['CHOWN_DIRS+=("prometheus/data:65534")']


def conf_files(cfg, ports, ctx):
    """选了 Alertmanager 才生成 prometheus.yml + 告警规则, 否则用镜像内置默认配置"""
    if "alertmanager" not in cfg["services"]:
        return {}
    gen_mark = "# 由打包器生成 (选配 Alertmanager 时启用告警链路, 重新部署时自动覆盖)\n"
    main = (
        "global:\n"
        "  scrape_interval: 30s\n"
        "  evaluation_interval: 30s\n"
        "\n"
        "rule_files:\n"
        "  - /etc/prometheus/rules/*.yml\n"
        "\n"
        "alerting:\n"
        "  alertmanagers:\n"
        "    - static_configs:\n"
        "        - targets: ['alertmanager:9093']\n"
        "\n"
        "scrape_configs:\n"
        "  - job_name: prometheus\n"
        "    static_configs:\n"
        "      - targets: ['localhost:9090']\n"
    )
    scrape = ""
    if "node-exporter" in cfg["services"]:
        scrape += (
            "  - job_name: node-exporter\n"
            "    static_configs:\n"
            "      - targets: ['node-exporter:9100']\n"
        )
    rules = (
        "# 内置告警规则示例 (可直接修改补充; 文件含\"由打包器生成\"标记, 重新部署时会被覆盖)\n"
        "groups:\n"
        "  - name: basic\n"
        "    rules:\n"
        "      - alert: InstanceDown\n"
        "        expr: up == 0\n"
        "        for: 3m\n"
        "        labels:\n"
        "          severity: critical\n"
        "        annotations:\n"
        "          summary: \"{{ $labels.job }} ({{ $labels.instance }}) 已停止上报\"\n"
        "          description: \"实例 {{ $labels.instance }} 连续 3 分钟抓取失败, 请检查服务状态\"\n"
    )
    return {
        "conf/prometheus/prometheus.yml": gen_mark + main + scrape,
        "conf/prometheus/rules/basic.yml": gen_mark + rules,
    }


def summary_lines(cfg, ports, ctx):
    return ["Prometheus    http://__IP__:%d" % ports["prom"]]

# -*- coding: utf-8 -*-
"""
Grafana 插件 (可视化面板, 数据源/仪表盘自动预配)

镜像 tar 放置位置:
  warehouse/images/grafana/13.2.1/amd64.tar
  warehouse/images/grafana/13.2.1/arm64.tar
"""
SERVICE_KEY = "grafana"

META = {
    "cat": "obs",
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
        {"key": "GRAFANA_ADMIN_USER",     "label": "Grafana 管理员账号", "default": "admin",            "services": ["grafana"], "secret": False},
        {"key": "GRAFANA_ADMIN_PASSWORD", "label": "Grafana 管理员密码", "default": "Xy625#608iVR@HZs", "services": ["grafana"], "secret": True},
    ],
    "note": "管理员账号密码见 .env; 同选 Prometheus/Loki 时数据源自动预配, 同选 Node Exporter 时预配主机监控仪表盘",
}

_DASHBOARD_JSON = """{
  "uid": "node-exporter",
  "title": "Node Exporter 主机监控",
  "tags": ["node-exporter", "packer"],
  "timezone": "browser",
  "schemaVersion": 39,
  "refresh": "30s",
  "time": {"from": "now-6h", "to": "now"},
  "panels": [
    {"id": 1, "type": "timeseries", "title": "CPU 使用率", "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0},
     "targets": [{"expr": "100 - (avg(rate(node_cpu_seconds_total{mode=\\"idle\\"}[5m])) * 100)", "refId": "A", "legendFormat": "CPU %"}],
     "fieldConfig": {"defaults": {"unit": "percent", "min": 0, "max": 100}, "overrides": []}},
    {"id": 2, "type": "timeseries", "title": "内存使用", "gridPos": {"h": 8, "w": 12, "x": 12, "y": 0},
     "targets": [
       {"expr": "(1 - node_memory_MemAvailable_bytes/node_memory_MemTotal_bytes)*100", "refId": "A", "legendFormat": "内存 %"},
       {"expr": "(node_memory_SwapTotal_bytes - node_memory_SwapFree_bytes)/node_memory_SwapTotal_bytes*100", "refId": "B", "legendFormat": "Swap %"}],
     "fieldConfig": {"defaults": {"unit": "percent", "min": 0}, "overrides": []}},
    {"id": 3, "type": "timeseries", "title": "磁盘使用率 (挂载点)", "gridPos": {"h": 8, "w": 12, "x": 0, "y": 8},
     "targets": [{"expr": "100 - (node_filesystem_avail_bytes{fstype!=\\"tmpfs\\"}/node_filesystem_size_bytes{fstype!=\\"tmpfs\\"})*100", "refId": "A", "legendFormat": "{{mountpoint}}"}],
     "fieldConfig": {"defaults": {"unit": "percent", "min": 0, "max": 100}, "overrides": []}},
    {"id": 4, "type": "timeseries", "title": "磁盘 IO", "gridPos": {"h": 8, "w": 12, "x": 12, "y": 8},
     "targets": [
       {"expr": "rate(node_disk_read_bytes_total[5m])", "refId": "A", "legendFormat": "读 {{device}}"},
       {"expr": "rate(node_disk_written_bytes_total[5m])", "refId": "B", "legendFormat": "写 {{device}}"}],
     "fieldConfig": {"defaults": {"unit": "Bps"}, "overrides": []}},
    {"id": 5, "type": "timeseries", "title": "网络流量", "gridPos": {"h": 8, "w": 12, "x": 0, "y": 16},
     "targets": [
       {"expr": "rate(node_network_receive_bytes_total{device!=\\"lo\\"}[5m])", "refId": "A", "legendFormat": "收 {{device}}"},
       {"expr": "rate(node_network_transmit_bytes_total{device!=\\"lo\\"}[5m])", "refId": "B", "legendFormat": "发 {{device}}"}],
     "fieldConfig": {"defaults": {"unit": "Bps"}, "overrides": []}},
    {"id": 6, "type": "timeseries", "title": "系统负载", "gridPos": {"h": 8, "w": 12, "x": 12, "y": 16},
     "targets": [
       {"expr": "node_load1", "refId": "A", "legendFormat": "1min"},
       {"expr": "node_load5", "refId": "B", "legendFormat": "5min"},
       {"expr": "node_load15", "refId": "C", "legendFormat": "15min"}],
     "fieldConfig": {"defaults": {"unit": "short"}, "overrides": []}}
  ]
}
"""


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
      - ./grafana/provisioning:/etc/grafana/provisioning:ro
      - ./grafana/dashboards:/opt/dashboards:ro
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


def conf_files(cfg, ports, ctx):
    """数据源 + 仪表盘自动预配: 按本次勾选的服务动态生成"""
    services = cfg["services"]
    datasources = []
    if "prometheus" in services:
        datasources.append(
            "  - name: Prometheus\n"
            "    type: prometheus\n"
            "    access: proxy\n"
            "    url: http://prometheus:9090\n"
            "    isDefault: true\n")
    if "loki" in services:
        datasources.append(
            "  - name: Loki\n"
            "    type: loki\n"
            "    access: proxy\n"
            "    url: http://loki:3100\n")
    conf = {}
    if datasources:
        conf["conf/grafana/provisioning/datasources/datasources.yml"] = (
            "# 由打包器生成 (数据源自动预配, 重新部署时自动覆盖)\n"
            "apiVersion: 1\n"
            "deleteDatasources:\n"
            "  - name: Prometheus\n    orgId: 1\n"
            "  - name: Loki\n    orgId: 1\n"
            "datasources:\n" + "".join(datasources))
        conf["conf/grafana/provisioning/dashboards/provider.yml"] = (
            "# 由打包器生成 (仪表盘目录预配, 重新部署时自动覆盖)\n"
            "apiVersion: 1\n"
            "providers:\n"
            "  - name: packer\n"
            "    orgId: 1\n"
            "    type: file\n"
            "    disableDeletion: false\n"
            "    updateIntervalSeconds: 30\n"
            "    options:\n"
            "      path: /opt/dashboards\n")
    if "node-exporter" in services and "prometheus" in services:
        conf["conf/grafana/dashboards/node-exporter.json"] = _DASHBOARD_JSON
    return conf


def manifest_lines(cfg, ports, ctx):
    # grafana 容器以 uid 472 运行
    return ['CHOWN_DIRS+=("grafana/data:472")']


def summary_lines(cfg, ports, ctx):
    return ["Grafana        http://__IP__:%d  (账号见 .env)" % ports["web"]]

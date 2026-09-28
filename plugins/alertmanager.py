# -*- coding: utf-8 -*-
"""
Alertmanager 告警插件 (对接 Prometheus 告警, webhook 通知)

镜像 tar 放置位置:
  warehouse/images/alertmanager/v0.28.1/amd64.tar
  warehouse/images/alertmanager/v0.28.1/arm64.tar

告警链路: Prometheus rules 触发 -> Alertmanager 分组/静默/抑制 -> webhook 推送
Webhook 地址支持: 企业微信机器人 / 钉钉机器人(需 prometheus-webhook-dingtalk 适配)
或任意能接收 JSON POST 的自建服务
"""
SERVICE_KEY = "alertmanager"

META = {
    "cat": "obs",
    "label": "Alertmanager",
    "image": "prom/alertmanager",
    "tag": "v0.28.1",
    "color": "#B24C63",
    "data_dir": "alertmanager",
    "supported_arch": ["amd64", "arm64"],
    "images": {
        "amd64": "warehouse/images/alertmanager/v0.28.1/amd64.tar",
        "arm64": "warehouse/images/alertmanager/v0.28.1/arm64.tar",
    },
    "ports": [
        {"key": "am", "label": "Alertmanager 端口|Alertmanager port", "default": 9093, "container": 9093},
    ],
    "secrets": [
        {"key": "AM_WEBHOOK_URL", "label": "告警 Webhook 地址 (企业微信/钉钉/自建)|Alert webhook URL",
         "default": "", "services": ["alertmanager"], "secret": False, "type": "webhook"},
    ],
    "note": "Webhook 留空则告警仅在 9093 界面展示; 钉钉机器人建议搭配 prometheus-webhook-dingtalk 适配",
}


def compose_block(cfg, ports, ctx):
    return """
  %(svc)s:
    image: %(image)s
    container_name: %(svc)s
    restart: always
    user: "65534:65534"
    environment:
      TZ: ${TZ}
    ports:
      - "%(port)d:9093"
%(extra_ports)s    volumes:
      - ./alertmanager/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro
      - ./alertmanager/data:/alertmanager
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:9093/-/ready || exit 1"]
      interval: 15s
      timeout: 10s
      retries: 8
      start_period: 20s
    networks:
      - app-network""" % {
        "svc": SERVICE_KEY,
        "image": "%s:%s" % (META["image"], META["tag"]),
        "port": ports["am"],
        "extra_ports": ctx["extra_ports_lines"](SERVICE_KEY),
    }


def conf_files(cfg, ports, ctx):
    """生成 alertmanager.yml: 配置了 Webhook 就推送到 Webhook, 否则仅界面展示"""
    webhook = str(ctx["secrets"].get("AM_WEBHOOK_URL", "")).strip()
    gen_mark = "# 由打包器生成 (重新部署时自动覆盖)\n"
    if webhook:
        receiver = (
            "route:\n"
            "  receiver: ops\n"
            "  group_by: [alertname]\n"
            "  group_wait: 30s\n"
            "  group_interval: 5m\n"
            "  repeat_interval: 4h\n"
            "\n"
            "receivers:\n"
            "  - name: ops\n"
            "    webhook_configs:\n"
            "      # 企业微信/钉钉机器人请先经适配层转换格式\n"
            "      # (钉钉: prometheus-webhook-dingtalk; 企微: webchat-hooker 等)\n"
            "      - url: '%s'\n"
            "        send_resolved: true\n" % webhook
        )
        note = ""
    else:
        receiver = (
            "route:\n"
            "  receiver: ui-only\n"
            "  group_by: [alertname]\n"
            "  group_wait: 30s\n"
            "  group_interval: 5m\n"
            "  repeat_interval: 4h\n"
            "\n"
            "receivers:\n"
            "  # 未配置 Webhook: 告警仅在 http://<ip>:%d 界面展示与静默\n"
            "  - name: ui-only\n" % ports["am"]
        )
        note = (
            "\n"
            "# 启用企微/钉钉/自建通知: 打包时填写 [告警 Webhook 地址], 重新打包部署即可\n"
        )
    return {"conf/alertmanager/alertmanager.yml": gen_mark + receiver + note}


def manifest_lines(cfg, ports, ctx):
    # alertmanager 官方镜像以 nobody(65534) 运行, 数据目录需可写
    return ['CHOWN_DIRS+=("alertmanager/data:65534")']


def summary_lines(cfg, ports, ctx):
    return ["Alertmanager  http://__IP__:%d  (告警展示/静默; Webhook 见 alertmanager.yml)|Alertmanager  http://__IP__:%d  (alerts & silences; webhook in alertmanager.yml)" % (ports["am"], ports["am"])]

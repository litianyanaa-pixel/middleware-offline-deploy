# -*- coding: utf-8 -*-
"""
中间件插件模板(可插拔接口)

用法: 复制本文件为 plugins/<你的中间件>.py (文件名不要以下划线开头), 按注释修改。
打包器启动时会自动加载 plugins/ 下所有 *.py, 页面/校验/生成/打包全部自动纳入。

必须提供:
  SERVICE_KEY     全局唯一的英文标识(即 compose 服务名)
  META            中间件元信息(结构见下, 与 versions.json 中内置服务完全同构)
  compose_block(cfg, ports, ctx)  返回该服务的 docker-compose YAML 片段字符串

可选:
  env_lines(cfg, ports, ctx)      返回追加到 .env 的行列表
  manifest_lines(cfg, ports, ctx) 返回追加到 manifest.sh 的行列表(bash)
  summary_lines(cfg, ports, ctx)  返回部署完成摘要行(含 __IP__ 占位符)

ctx 可用字段:
  ctx["cfg"]              整份配置(含 secrets/extra_ports)
  ctx["ports"]            全部端口 {key: host_port}
  ctx["secrets"]          全部密码 {"KEY": value}
  ctx["db"]               nacos/xxljob 数据库配置
  ctx["extra_ports_lines"](svc)  自定义端口映射的 YAML 行(直接放进该服务 ports: 下)
  ctx["depends_on"](conf)        依赖本地 MySQL 的 YAML 片段(conf 为 db 配置或 None)
  ctx["extra_hosts"](conf)       host-gateway 的 YAML 片段
  ctx["networks_tail"]           固定的 networks 片段字符串
  ctx["local_mysql"]             本地 MySQL 服务名(未部署为 "")
镜像包: 放到 META["images"] 指向的 warehouse 路径即可。
"""

SERVICE_KEY = "elasticsearch"

META = {
    "label": "Elasticsearch",
    "image": "elasticsearch",              # 镜像短名(仓库前缀会被自动归一化)
    "tag": "8.15.0",
    "color": "#f5b93e",                    # 卡片主题色(无 logo 图时用于字母头像)
    "data_dir": "es",                      # 部署目录下的持久化子目录名
    "supported_arch": ["amd64"],
    "container_name": "elasticsearch",     # 可选, 默认等于 SERVICE_KEY
    "images": {
        "amd64": "warehouse/images/elasticsearch/8.15.0/amd64.tar",
        # "arm64": "warehouse/images/elasticsearch/8.15.0/arm64.tar",
    },
    "ports": [
        {"key": "es_http",       "label": "HTTP 端口",      "default": 9200, "container": 9200},
        {"key": "es_transport",  "label": "Transport 端口", "default": 9300, "container": 9300},
    ],
    # 本插件额外的账号密码字段(结构同 versions.json secrets, 可省略)
    "secrets": [
        {"key": "ES_PASSWORD", "label": "Elasticsearch 密码", "default": "E1@stic#", "services": ["elasticsearch"], "secret": True},
    ],
    "note": "",
}


def compose_block(cfg, ports, ctx):
    # ctx["extra_ports_lines"](SERVICE_KEY) 会输出用户在页面添加的自定义端口映射
    return """
  %(svc)s:
    image: %(image)s
    container_name: %(cname)s
    restart: always
    environment:
      TZ: Asia/Shanghai
      discovery.type: single-node
      ELASTIC_PASSWORD: ${ES_PASSWORD}
    ports:
      - "%(http)d:9200"
%(extra_ports)s    volumes:
      - ./es/data:/usr/share/elasticsearch/data
    networks:
      - app-network""" % {
        "svc": SERVICE_KEY,
        "cname": META.get("container_name", SERVICE_KEY),
        "image": "%s:%s" % (META["image"], META["tag"]),
        "http": ports["es_http"],
        "extra_ports": ctx["extra_ports_lines"](SERVICE_KEY),
    }


def env_lines(cfg, ports, ctx):
    pw = ctx["secrets"].get("ES_PASSWORD", "")
    return ["ES_PASSWORD=%s" % pw]


def summary_lines(cfg, ports, ctx):
    return ["Elasticsearch  http://__IP__:%d" % ports["es_http"]]

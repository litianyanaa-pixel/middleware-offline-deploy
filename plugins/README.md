# plugins/ 可插拔中间件插件目录

把新的中间件做成一个 Python 文件放进本目录（文件名不要以 `_` 开头），打包器启动时自动加载，
页面勾选、端口/密码表单、compose 生成、镜像打包、部署摘要**全部自动纳入**，核心代码零改动。

```
plugins/
├── _template.py      # 模板(不会被加载), 复制成 <名字>.py 后按注释修改
├── postgres.py       # 真实插件: PostgreSQL 15.19 (双架构, 镜像在 warehouse/images/postgres/15.19/)
└── elasticsearch.py  # 例: 一个最简插件长这样
```

## 接口契约

| 必须提供 | 说明 |
|---|---|
| `SERVICE_KEY` | 全局唯一英文标识，即 compose 服务名 |
| `META` | 元信息 dict：label / image / tag / color / data_dir / supported_arch / images / ports / secrets(可选) |
| `compose_block(cfg, ports, ctx)` | 返回该服务的 docker-compose YAML 片段 |

| 可选 | 说明 |
|---|---|
| `env_lines(cfg, ports, ctx)` | 追加到 `.env` 的行 |
| `manifest_lines(cfg, ports, ctx)` | 追加到 `manifest.sh` 的 bash 行 |
| `summary_lines(cfg, ports, ctx)` | 部署完成摘要行（`__IP__` 会被替换成服务器 IP） |

`ctx` 里能拿到全部配置、密码、数据库选项，以及常用 YAML 片段生成器
（`extra_ports_lines` / `depends_on` / `extra_hosts` / `networks_tail`），详见 `_template.py` 注释。

## 上线一个新中间件的完整步骤

1. 把镜像按架构 save 到 `warehouse/images/<中间件>/<版本>/<amd64|arm64>.tar`
2. 复制 `_template.py` 为 `plugins/<中间件>.py`，改 SERVICE_KEY/META/compose_block
3. `python packer.py` 重新打开页面 —— 新中间件出现在勾选列表，仓库状态表会提示镜像缺失情况

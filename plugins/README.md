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
| `manifest_lines(cfg, ports, ctx)` | 追加到 `manifest.sh` 的 bash 行（写进 manifest.sh 的值一律用 `ctx["bash_quote"]` 包成单引号，避免空格/引号/密码特殊字符破坏 bash 语法） |
| `summary_lines(cfg, ports, ctx)` | 部署完成摘要行；`__IP__` 会被 `deploy.sh` 替换成服务器 IP，多机地址请在打包期直接用 `cfg["servers"]` 的真实 IP 拼好，**不要自造占位符**（deploy.sh 只认识 `__IP__` 与兼容兜底的 `__NODE_IP__`）。双语行写 `中文|English` |
| `conf_files(cfg, ports, ctx)` | 返回 `{"conf/<服务名>/<文件名>": 内容字符串或 pathlib.Path}`，随包进入 `conf/` 并由 `deploy.sh` 拷到部署目录同名路径（如 `conf/loki/loki.yml`） |
| `CLUSTER` | 集群形态元数据 dict(image/tag/images)。配合 `cfg["topology"][SERVICE_KEY]` 在 `compose_block` 等函数里按形态分支（参考 kafka.py 的 3 节点集群实现；packer 会自动按形态切换物料检查与镜像打包） |

`conf_files` 生成的文件头决定重部署行为：含 `由打包器生成` 且**不含** `只补齐` 的文件随新包覆盖；
声明了 `只补齐不覆盖`（如 `templates/redis.conf`）或目标文件已无生成标记的，一律保留用户版本，
新包默认配置另存为 `<文件名>.packer-new` 供人工合并。

`ctx` 里能拿到全部配置、密码、数据库选项，以及常用片段生成器与工具
（`extra_ports_lines` / `depends_on` / `extra_hosts` / `networks_tail` / `bash_quote` / `tr` / `lang` / `local_mysql`），详见 `_template.py` 注释。

## 上线一个新中间件的完整步骤

1. 把镜像按架构 save 到 `warehouse/images/<中间件>/<版本>/<amd64|arm64>.tar`
2. 复制 `_template.py` 为 `plugins/<中间件>.py`，改 SERVICE_KEY/META/compose_block
3. `python packer.py` 重新打开页面 —— 新中间件出现在勾选列表，仓库状态表会提示镜像缺失情况
4. `python packer.py --dump-catalog docs/versions.json` 同步静态演示站目录 —— **不做这步 GitHub Pages demo 页看不到新中间件**（CI 测试 `test_docs_versions_json_has_plugins` 会拦截过期目录）

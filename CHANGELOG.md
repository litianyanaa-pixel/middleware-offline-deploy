# 更新说明 (Changelog)

按时间倒序记录每次功能与修复。`docs/` 演示站随前端改动同步更新。

## 2026-10-08（静态演示站同步机制 + 部署位置提醒）

- **修复 Pages demo 只显示最初 7 个应用**：docs/ 静态站此前是手工拷贝的陈旧副本
  （versions.json 只有内置 7 服务、packer.html 落后于纯离线/在线两模式产品化）。
  新增 `python packer.py --dump-catalog docs/versions.json` 一条命令导出合并服务目录
  （内置 + 13 个插件中间件共 20 个），docs/packer.html 直接同步根目录版本，
  静态退化逻辑补透传 cluster 字段（演示页恢复 K8s 集群卡片）；
  新增防陈旧测试 `test_docs_static_site_packer_html_sync` /
  `test_docs_versions_json_has_plugins`（新增插件忘记重新生成会被 CI 拦下）；
  plugins/README 上线步骤补第 4 步
- **部署位置提醒**：打包器界面（部署目录下方 + 部署服务器池）、README 中英版「服务器部署」
  章节、落地页 docs/index.html 均明确提示——**中间件默认部署在交付包所在服务器**
  （运行 deploy.sh 的机器），跨服务器部署需配置多机拓扑
- 清理 test_packer.py 尾部重复定义的 test_deploy_sh_retag_integration（heredoc 编辑遗留）

## 2026-10-01（多机部署真机首验 + 异常总账整合）

- **多机部署真机首验通过**：mysql8 + mysql57 双主从跨机（112 主部署机就地装双主库 +
  113 SSH 分发双从库），deploy.sh → distribute.sh → install-node.sh 全链路真机跑通；
  复制取证 IO/SQL 线程 Yes、延迟 0、主库写入端到端同步、从库 super_read_only 写保护、
  幂等重跑通过；kafka 集群(3/5 台)与 redis 哨兵(3 台)需第三台真机，产物级由单测覆盖
- **多机链路修复 9 个缺陷**（tests/test_packer 30 项回归）：
  节点 MySQL 容器名/服务键按服务名生成（双 MySQL 同节点不再撞名）· 节点数据目录按服务名
  隔离（同节点双实例不再共用 datadir）· distribute.sh 节点包解到 $DEPLOY_DIR/nodes/<名>/
  子目录（主库节点为主部署机自身时不再覆盖主包 compose/.env）· 本机节点 is_local_ip
  就地安装（免 tar 环回传输的 file changed 失败）· deploy.sh 6 处 cp 同文件 -ef 守卫
  （包解进部署目录不再 set -e 中断）· 多机导表客户端镜像检查移入实际导表分支 ·
  自定义拓扑端口(mysql*_replica 等)不再被静默丢弃 · 从库初始化 SQL 幂等化
  （STOP+RESET 前缀，重跑不再 ERROR 3081）· 从库补 super_read_only（与单机主从行为一致）
- **文档整理**：docs/问题修复全记录.md 成为全项目唯一异常总账（68 条，《测试环境问题记录》
  并入为第四部分）；新增 docs/参考文献.md（全部参考过的文献/官方文档/开源社区索引）；
  README 新增「多机部署」章节与「文档索引」

## 2026-09-29

- **备份计划按数据库拆分**：MySQL / PostgreSQL / MongoDB 各自独立的启用开关、备份日、备份时间与保留份数,
  部署时写入三条独立 crontab; 生成预览与《部署报告》分别列出每库的备份计划
- **备份区按所选数据库动态展示**：勾选了哪些库就显示哪些引擎的说明(mysqldump / pg_dump -Fc / mongodump),
  顶部列出"备份对象"清单, 多选时明确提示"N 种数据库将同时纳入备份计划"; 默认备份目录改为 /data/backup/db(每库独立子目录)
- **分区序号动态重排**：隐藏的配置分区不再占号, 消除"账号密码 1 → 数据库备份 3"跳号问题
- **修复**：backup.sh 轮转函数在备份目录为空时因 `set -o pipefail` 误退出(ls 无匹配), 改用 find 实现
- **修复**：打包器所有 HTTP 响应补 `Cache-Control: no-store`, 根治浏览器缓存旧页面导致"新功能看不到"
- README 中英文档全部 7 张 UI 截图更新为当前版本(含 Alertmanager / Kafka 双形态 / 三库独立备份计划)

## 2026-09-28（晚间）

- **备份恢复扩展到 PostgreSQL / MongoDB**：backup.sh / restore.sh 支持三种引擎——
  MySQL(mysqldump --single-transaction)、PostgreSQL(pg_dump -Fc)、MongoDB(mongodump --archive --gzip),
  全部在容器内执行零宿主机依赖, 按库分文件、保留 N 份自动轮转; 恢复前自动生成安全备份(pre_restore_*), 支持 --yes 免交互
- **一键卸载 uninstall.sh**：停容器 → 清理备份 crontab → 默认保留全部数据与物料, `--purge-data` 才删数据目录
  (仅限部署目录内挂载点, 需二次确认输入 DELETE); 删除失败时如实告警不中断
- **Alertmanager 告警链路闭环**：新插件 prom/alertmanager v0.28.1(双架构), 告警 Webhook 地址可空可校验;
  选配后 Prometheus 自动生成抓取配置与告警规则并挂载, "指标监控套件 / 可观测全家桶"同步纳入
- **物料补齐一键化**：物料仓库抽屉新增"补料脚本"按钮——盘点双架构全部缺失物料并生成
  pull_missing_images.sh(docker pull 多镜像源回退 → crane 兜底, tar 自动落位)
- **工程质量**：新增 tests/test_packer.py(18 用例)与 tools/local_e2e_test.py(真实容器端到端:
  起栈 → 三库备份 → 毁数据 → 恢复校验 → 告警注入 → 卸载); GitHub Actions CI(pytest + bash 语法 + shellcheck + 打包冒烟)
- Toast 提示改为平滑消失动画; Kafka 版本号/镜像体积按部署形态动态显示(单节点 4.3.1 / 集群 3.7.0)

## 2026-09-28

- **全项目中英双语**：打包器顶栏新增 中/EN 一键切换(选择记忆在浏览器, 切换即时生效)；
  物料目录(versions.json)与全部 13 个插件的标签/端口/密码文案改为 `中文|English` 双语约定，
  打包校验提示、部署摘要、`.env` 注释按界面语言输出；CLI 新增 `--lang zh|en`；
  服务器端《部署报告》按打包时选择的语言输出中文或英文(manifest.sh 新增 DEPLOY_LANG)
- **Kafka 3 节点集群形态**：勾选 Kafka 后可在"部署形态"中切换单节点 / 3 节点集群——
  KRaft 组合模式(免 ZooKeeper)、SASL_PLAINTEXT 多用户鉴权(参照生产参考环境, bitnami/kafka 3.7.0 双架构)、
  容器网络内免鉴权(kafka-ui 自动连接 kafka1), 宿主机广播地址部署时自动改写为服务器真实 IP;
  物料检查/镜像打包/端口布局/部署摘要/健康实测全部按形态自动切换
- **增量升级**：重跑 deploy.sh 自动对比新旧编排并输出"将重建/新增/移除/保持"清单,
  未变更服务不重建, 移除的服务自动清理(--remove-orphans)
- **Kafka UI 多形态适配**：集群下自动连接 kafka1:9092
- Kafka / Elasticsearch 容器健康检查确认覆盖(此前 README 表述滞后, 实际已在 HEALTH_WAIT 清单中)

## 2026-09-23

- **修复 MySQL 健康检查误报**：探活改为"鉴权探活优先"——老机器首次启动初始化可能超过原 180 秒窗口导致误判不健康；
  compose `start_period` 放宽到 300s
- `my.cnf` 落盘权限强制 644（MySQL 会忽略全局可写的配置文件）

## 2026-09-21

- **MySQL 配置文件挂载**：`conf/<svc>/my.cnf → /etc/mysql/conf.d`，内置 `sql_mode` 严格模式声明（主库/从库各一份），
  修改 MySQL 参数不再需要进容器

## 2026-09-14 ~ 09-20（前端体验第七轮打磨）

- 配色改为蓝青渐变；"物料仓库 / 历史产物"收进顶栏侧滑抽屉，主界面更聚焦
- 步骤条改为胶囊容器 + 滑动指示动画；标签页内编号从 1 开始
- 液态玻璃（liquid glass）视觉重做
- 修复：物料/历史页空白、生成预览按钮消失、"下一步"不能正确跳到预览等问题
- HTTPS 证书上传按钮提高辨识度

## 2026-09-14

- **前端整页改版**：五步向导流程 + 部署形态独立卡片 + 全局紧凑化

## 2026-09-11

- **集群形态**：MySQL 主从复制（8.0 与 5.7 均支持，GTID 自动同步）、Redis 哨兵高可用（1 主 1 从 + 3 哨兵）
- 新增 **Kafka（KRaft 单节点）+ Kafka UI**；Kafka / Elasticsearch / Kibana / Kafka-UI 支持鉴权配置
- **观测三件套闭环**：Node Exporter（主机指标）+ Prometheus + Loki/Promtail（容器日志）+ Grafana 数据源/仪表盘自动预配
- Grafana 与 Elasticsearch 数据源联动；服务列表增加分类筛选
- 集群部署脚本在 WSL2 systemd 环境逐包真机验证

## 2026-09-10

- 新增 6 个中间件插件：**RabbitMQ / MongoDB / Prometheus / Grafana / Elasticsearch / Kibana**（全部双架构）
- GitHub Pages 演示站上线：<https://litianyanaa-pixel.github.io/middleware-offline-deploy/packer.html>

## 2026-09-10（首个版本）

- 本地 Web 打包器（纯 Python 标准库，端口 8765）+ 服务器端零交互幂等部署脚本
- 内置服务：NGINX / MySQL 5.7 / MySQL 8.0 / Redis / Nacos / XXL-Job / MinIO
- 特性：按需打包、镜像 tag 归一化、`/proc/net/tcp` 端口预检、健康实测三级降级、
  Nacos/XXL-Job 自动导库、定时备份 + 一键恢复、反向代理向导（HTTPS 证书随包分发）、部署报告自动生成

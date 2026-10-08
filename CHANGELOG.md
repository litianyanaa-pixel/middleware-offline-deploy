# 更新说明 (Changelog)

按时间倒序记录每次功能与修复。`docs/` 演示站随前端改动同步更新。

## 2026-10-08（物料仓库 K8s 独立页签）

- 「物料仓库」弹窗顶部新增两个页签：**中间件物料**（Docker/Compose 安装包 + 各中间件镜像，
  原「类型/架构」筛选只作用于这一页）与 **K8s 集群物料**（kk 二进制、各版本 artifact 产物/
  二进制缓存/离线镜像包、OS 依赖包，独立成页不再和中间件混在一张表里）；
  K8s 物料均为 amd64 且不分安装包/镜像，切到该页签时类型与架构筛选自动隐藏

## 2026-10-08（读写分离配置残留修复）

- 勾选过读写分离后，取消部署对应数据库（或把部署形态切回单机、切 arm64 架构）时，
  「端口与反代」里的读写分离端口组不再残留：开关与端口随服务取消一并清除，
  重新勾选也不带旧状态；恢复/导入一份带残留开关的旧配置同样自动对齐当前勾选。
  端口组渲染加了双保险——只有"该 MySQL 仍勾选且为主从形态"时才显示

## 2026-10-08（三条界面问题修复：端口标红/读写分离归属/待配置误报）

- 多机部署同端口不再标红：端口实时校验与打包校验同口径——多机的 MySQL 主从、Redis 哨兵
  各角色分布在不同机器，主/从/哨兵同端口合法（Kafka 多机节点组端口本就每台一致）；
  同机主从同端口仍会标红（那才是真冲突）
- 读写分离改为每个主从 MySQL 独立一个 ProxySQL 实例：同时部署 MySQL 5.7 与 8.0 时各自有
  独立开关并明确标注"只针对哪套主从"，独立入口/管理端口（8.0: 16033/16032，5.7: 16035/16034），
  配置不再混在同一对 hostgroup 里（旧版两套集群的读流量会互串）；顺带补上同机主从的
  读写分离支持（旧版会生成空 mysql_servers 的废配置）；旧配置（布尔开关/单实例端口键）
  自动迁移，单实例时容器名仍为 proxysql，部署布局不变
- 「账号与数据」tab 的"1 项待配置"误报修复：按实际勾选的应用逐项判断（旧版勾了 nacos/xxljob
  任一个就要求两个都初始化）；告警 Webhook 这类后端允许留空的项不再计入待配置
- 测试：pytest 53 passed 1 skipped（新增双实例隔离与同机主从读写分离用例、旧端口键迁移用例）；
  浏览器实测三场景通过

## 2026-10-08（重构后文档同步）

- 文档对齐 packer.py→packerlib/ 与 packer.html→webui/ 拆分：README 目录树把 packerlib
  14 个模块列全；README_EN.md 目录结构整段同步（packerlib/ / webui/ / build_webui.py +
  开发提示，顺带补上此前缺失的 prepare_cluster.py / deploy-cluster.sh / warehouse/cluster
  等 K8s 条目）；docs/k8s-README.md 与 docs/k8s-信创离线部署.md 中"逻辑都在 packer.py"
  的旧指向改为 packerlib/cluster.py 等。`python packer.py` 用法不变，无需改操作习惯

## 2026-10-08（集群高级配置折叠状态不再被联动重渲染重置）

- **修复**：勾选 K8s 集群后，展开「集群高级配置」折叠块，只要在别处切换选项（安装模式
  纯离线/在线、HA 类型、节点角色、一键填源、增删服务器等都会触发 `renderInfraTopology()`
  重建该区块），折叠块就被强制收起。现在重建前读取当前 `<details>` 的展开状态并原样
  继承，展开与否完全由用户控制（收起后重渲染也不会被强制展开）
- 浏览器实测：展开后切换安装模式/角色分配保持展开，手动收起后重渲染保持收起；
  pytest 52 passed 1 skipped

## 2026-10-08（packer 代码模块化拆分）

- **packer.py(4490 行)拆分为 packerlib/ 包(14 个功能模块)**：paths / util / catalog /
  validate / nacos_sql / compose / cluster / manifest / multinode / nginx_conf /
  progress / builder(打包主流程) / web / cli，按原分区注释按 AST 节点切分，跨模块依赖
  显式导入；packer.py 保留为**兼容薄壳**（`python packer.py` 各命令与
  `import packer` 用法不变，98 个顶层符号全量保留，`--dump-catalog` 输出与拆分前
  逐字节一致）
- 镜像 tar/补料脚本辅助函数（missing_materials / gen_pull_script / gen_retag_mirrors_sh
  等）从 web 段并入 builder 模块——本质是打包逻辑，同时消除 pack↔web 循环导入；
  模块改名 builder 避免与导出函数 `pack` 同名遮蔽；测试里 2 处 monkeypatch 相应
  改打实现模块（`packerlib.validate.check_warehouse` / `packerlib.builder.missing_materials`）
- **packer.html(2582 行)拆分为 webui/ 源文件 + build_webui.py 构建脚本**：
  template.html(结构, 231 行) / style.css(361 行) / app.js(1992 行)，构建产物与拆分前
  **逐字节一致**；packer.html 保持单文件交付形态（离线可直接打开），webui/ 是维护形态
  （改样式/脚本不用在 2500 行里翻找）
- **改 UI 流程**：改 `webui/` 源文件 → `python build_webui.py`（自动同步根目录
  packer.html 与 docs/ 副本）；新增防陈旧测试 `test_webui_sources_build_sync`
  （改了源忘构建会被 CI 拦下，已验证污染源文件时测试变红）
- 回归：pytest **52 passed**（新增 1 条）/ 冒烟打包 11 项 / 集群场景 11 项全绿；
  拆分前文件快照备份于 `backups/20261008-pre-split/`（本地留档，不入库）

## 2026-10-08（K8s 集群配置对齐 kk v4 + 多机同端口）

- **逐条对照 KubeKey v4 源码核查打包器生成的 config.yaml，修三处键位差异、补齐缺失能力**：
  - **etcd 数据目录键位修正（重要）**：v4 只读 `etcd.env.data_dir`（kk etcd 模板逐键消费
    `.etcd.env.*`），此前生成的顶层 `etcd.data_dir` 是旧版 kk 的键，v4 静默忽略 → 数据仍落
    `/var/lib/etcd` 而非自定义目录；已移入 `env` 并透出 etcd 调优 9 参数（心跳/选举/压缩保留/
    快照/后端配额/请求上限/max_snapshots/max_wals/log_level，白名单校验）
  - **kubelet 无需改**：`kubelet_args` 列表是 kk v1~v3 旧格式，v4 走 `kubelet.extra_args` map
    （kubeadm v1beta4 经 mapToNamedStringArgs 自动加 `--` 前缀），打包器现有生成即正确
  - **localpv 无需改**：v4 键就是 `storage_class.local.path`（`base_path` 是 kk v3 旧键）
- **控制面 HA 补 VIP 表单**（此前 kube-vip/haproxy 选了也无处填 VIP）：kube-vip 模式除
  `control_plane_endpoint.host` 外同步写入 `kube_vip.address`（空值会让静态 Pod 拿到空
  vip_address；kk 按该地址所在网段自动发现网卡并漂移 VIP）；haproxy 模式提示填域名或 127.0.0.2
- **证书与备份入口**：安装时续期开关（`kubernetes.certs.renew`）+ 续期 crontab（部署后写入
  主部署机 crontab 执行 `kk certs renew`，此链路原已存在仅缺 UI）+ kubeadm 配置带时间戳备份
  目录（`kubernetes.backup.kubeadm_config_dir`，kk post_install 每次部署自动备份，留空禁用）
- **CRI 运行时选择**：`cri.container_manager`（containerd 默认 / docker 仅在线安装——纯离线
  物料与预装流程只含 containerd，K8s ≥1.24 的 docker 由 kk 自动装 cri-dockerd）
- **CNI 扩展**：每节点 Pod 子网掩码 `cni.ipv4_mask_size`、Multi-CNI multus（含镜像 tag，
  离线镜像清单同步收集 `multus-cni`）、**Calico values 原样透传 helm**（`cni.calico.values`，
  v4 无独立 ipipMode/vxlanMode/mtu 键，靠 `-f` 自定义 values；YAML mapping 前置校验，非
  calico 自动忽略并告警）
- **DNS 覆盖**：CoreDNS/NodeLocalDNS 镜像 tag 与启停（`dns.*`；离线镜像清单同步用覆盖后的
  tag，关闭 nodelocaldns 时不再收集对应镜像）
- **多机主从允许主从同端口**：多机形态下从库/哨兵端口只存在于远端节点，放开与主库等端口的
  全局冲突校验（mysql8/mysql57/redis）；单机主从仍同机查重；kafka 多机节点组端口本就每台一致
- **kk-功能对照表重写**：NTP/存储/私有仓库/多CNI/kubelet/etcd 调优等十余项早已落地表单的
  能力从"暂未覆盖"移入"页面已有表单"，遗留项收敛为外部 etcd/双栈/HTTP 代理/BGP 等
- 回归：tests 51 passed 1 skipped（新增 etcd env/HA VIP/CRI+CNI+DNS/校验矩阵/多机同端口
  5 组用例）；docs/packer.html 已同步

## 2026-10-08（静态演示站同步机制 + 部署位置提醒）

- **修复多机部署从库端口输入框显示 undefined**：端口映射卡片追加的从库/哨兵端口定义
  缺 `default` 字段（mysql8_replica / mysql57_replica / redis_replica / redis_sentinel），
  首次渲染把 undefined 写进输入框；现与后端默认值对齐（13308/13309/16380/26379）。
  同时第一个端口标签由「MySQL 8.0 端口 / MySQL 5.7 端口 / Redis 端口」改为「主库端口」
  （中英双语），从库端口标签区分多机（每台从机）与同机语境；
  顺带补齐单机主从双从库的「从库2端口」（13310/13311）与单机哨兵的「从库端口」输入框
  （此前 UI 未渲染、只能用后端静默默认值）

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

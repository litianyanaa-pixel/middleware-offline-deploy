# 多机部署 Docker 全流程 E2E 测试报告

> 日期: 2026-09-29 · 版本: 27c9d73 + 密码分发改进 · 环境: Windows 10 + Docker Desktop (linux/amd64, 27.3.1)

## 1. 测试目标

验证「中间件离线部署包」多机部署链路的端到端正确性:
**Web 配置 → CLI 打包 → 主部署机 distribute.sh SSH 分发 (密码/免密) → 节点 install-node.sh 安装**,
使用操作系统容器 (debian:12-slim) 模拟真实服务器。

## 2. 测试环境设计

```
docker network mwe2e (10.66.0.0/24)
├── mwmaster       10.66.0.x   主部署机: ssh客户端 + sshpass, 只读挂载部署包 /bundle
├── mwn1           10.66.0.11  节点1 (redis 主库): sshd 密码登录 + 伪 docker 垫片
├── mwn2           10.66.0.12  节点2 (redis 从库1)
└── mwn3           10.66.0.13  节点3 (redis 从库2)
```

关键技巧: **伪 docker 垫片**。节点容器无 Docker 守护进程, 在 `/usr/local/bin/docker` 放置
Shell 垫片脚本, 记录全部调用参数到 `/var/log/fake-docker.log`, `docker inspect` 固定返回
`healthy`。既让 distribute.sh 预检与 install-node.sh 全流程真实跑通, 又能事后审计脚本
在节点上执行了哪些 docker 命令——无需 DinD 特权容器。

被测配置: redis 哨兵多机 (1 主 2 从), 3 节点均填 SSH 密码 (含 `#`、`~`、`!` 特殊字符)。

## 3. 测试步骤与结果

| # | 步骤 | 结果 |
|---|------|------|
| 1 | `python packer.py --config e2e-multihost.json --out dist` | ✅ 246MB 包生成, sha256 正常 |
| 2 | 解包检查: distribute.sh NODES 数组含 `name\|user\|ip\|ssh\|pass` 五段 | ✅ 密码正确嵌入 |
| 3 | master 容器内 `sshpass ssh root@10.66.0.11` 连通性预检 | ✅ 密码登录成功 |
| 4 | `./distribute.sh` 全量分发 3 节点 | ✅ 一次通过 |
| 5 | 节点落盘: `/opt/middleware/{compose,.env,conf,images,install-node.sh}` | ✅ 完整 |
| 6 | 伪 docker 日志: `load -i redis-amd64.tar` → `compose up -d` → `inspect redis/redis-sentinel` → `compose ps` | ✅ 调用序列符合预期 |
| 7 | node2/3 compose 含 `--replicaof 10.66.0.11 16379`; sentinel.conf `monitor mymaster 10.66.0.11 16379 2` + `announce-ip 10.66.0.12` (本机 IP) | ✅ |
| 8 | `./distribute.sh node2` 单节点重装模式 | ✅ |
| 9 | `./distribute.sh nodeX` 无效节点名 | ✅ 空操作正常退出 |
| 10 | `./deploy.sh` 主部署机脚本 (纯多机模式) | ✅ 无 systemd 时正确报错退出 (容器无 systemd, 属预期防护) |
| 11 | 密码泄露扫描: 全包 grep 三个密码 | ✅ 仅存在于 distribute.sh (sshpass 必需), manifest.json/manifest.sh/nodes/ 均干净 |

## 4. 本轮发现并修复的问题 (论文素材)

### 4.1 服务器池 UI 布局缺陷 (视觉)
- **现象**: 服务器池行复用了端口映射的 4 列网格 `160px 230px 28px 96px`, 导致 IP 输入框仅
  28px 宽、第 5 个子元素 (删除按钮) 被挤到下一行; 「+ 添加服务器」缺 `wide` 类,
  文字被塞进 30px 圆形按钮。
- **修复**: 新增 `.svcols/.svrow` 专属网格 `100px 84px minmax(120px,1fr) minmax(150px,1.3fr) 56px 34px`
  (IP 列最宽), 添加按钮补 `wide`; CDP 截图 + getBoundingClientRect 数值断言
  (用户 84px / 密码 283px / IP 367px, 删除按钮同线) 双重验证。

### 4.2 节点认证方式缺失 (功能)
- **现象**: 服务器池只能免密 (BatchMode) 分发, 无密码入口; 真实内网服务器常未配 ssh-copy-id。
- **修复**: UI 增加密码列 (type=password); validate_config 接受 `password/pass` 字段并禁止
  `|` 与换行 (会破坏 distribute.sh 的 `IFS='|'` 分隔); distribute.sh 对填密码节点改用
  `sshpass -e` (经 SSHPASS 环境变量传递, 避免 `ps` 泄露 `-p` 明文) + StrictHostKeyChecking=no,
  本机无 sshpass 时警告并回退免密; 密码以 `auth: password/key` 标注输出, **不落入**
  manifest.json / manifest.sh / 预览 API。

### 4.3 过期服务实例干扰测试 (环境)
- **现象**: 8766 端口残留旧版本打包服务实例, 响应为旧代码; 本次实际端口为 8765。
- **教训**: 每轮改码后测试前先 `netstat -ano | grep <port>` 清理旧实例, 避免打到过期进程。

### 4.4 CDP 自动化两个坑 (工具)
- 页面 JS 包在 IIFE 内, `eval_js("gotoStep(2)")` 报未定义 → 必须模拟 DOM 点击 `.wtab[data-v]`。
- `Page.captureScreenshot` 的 clip 对内部滚动容器易截出空白 → 退化为 `scrollIntoView` + 整屏截图。
- Git Bash 调 docker: `-v ...:/bundle` 的容器内路径会被 MSYS 路径转换改写 → 加 `MSYS_NO_PATHCONV=1`;
  含中文的 Windows 路径卷挂载在 Git Bash 下正常。

## 5. 结论

打包 → 分发 → 安装全链路在容器化 OS 环境一次通过; 密码分发路径 (sshpass) 与免密回退
逻辑、单节点重装、无效节点容错均验证通过。复现: `tmp_e2e/dockerenv/` 构建
`mwe2e/ossh` 镜像, 配置见 `tmp_e2e/e2e-multihost.json`。

---

## 6. 全场景扩展测试 (多机矩阵)

应用户要求补测多机集群全部场景, 5 配置 × 容器运行 (debian:12-slim, 1 主部署机 + 5 sshd 节点):

| 场景 | 内容 | 打包 | 分发+安装 | 配置核验 |
|---|---|---|---|---|
| S2 kafka5 | Kafka KRaft 5 节点多机 | ✅ 1.9GB | ✅ 5/5 | node_id/voters/advertised/端口/SASL/.env 全对 |
| S3 mysql8mh | MySQL8 一主两从 + ProxySQL + Nacos/XXL | ✅ 1.8GB | ✅ 3/3 | GRANT REPLICATION SLAVE / server-id / read-only / CHANGE REPLICATION SOURCE TO(8.0 语法) / HG10 主 HG20 从 / Nacos/XXL 指主库节点 |
| S4 mysql57mh | MySQL5.7 一主一从 | ✅ 351MB | ✅ 2/2 | CHANGE MASTER TO(5.7 语法) / START SLAVE / read-only |
| S5 keyauth | 免密模式 (BatchMode) | ✅ 246MB | ✅ 3/3 | 密钥分发→免密路径→哨兵配置全对 |
| S6 combo | Kafka3 节点+MySQL8 主从+Redis 哨兵+ProxySQL 同灌 3 台 | ✅ 3GB | 静态核验 ✅ | 三节点角色/voters/复制/哨兵仲裁 2/读写分组/密码泄露扫描全过 |

### 6.1 发现并修复的真实 Bug ⭐

1. **POST_SQL 数组双重转义 (S4 抓获, 严重)**: `gen_node_install_sh` 先把
   `"cn|sql"` 列表 bash_quote 再被模板 `" ".join(bash_quote(...))` 逐字符转义,
   生成的 `POST_SQL=(...)` 每个字符间都被塞了空格, MySQL 节点 install-node.sh
   **解析即崩**, 主从复制关系建立失败。此前单测只对 post_sql 为空的 redis 节点
   做 `bash -n`, 没覆盖 mysql 节点。修复后新增断言: mysql 多机节点脚本必须过
   `bash -n` 且 `POST_SQL=('mysql|...` 单引号包裹。
2. **节点侧 conf 目录桥接缺失 (S3 抓获)**: 主部署机 deploy.sh 有
   `conf/<svc>/* → $DEPLOY_DIR/<svc>/` 桥接, 节点 install-node.sh 没有——节点
   compose 挂载 `./redis/redis.conf` 但文件实际在 `conf/redis/` 下, 真实部署会
   挂载成空目录导致容器起不来。已在 install-node.sh 补齐桥接 + 数据目录创建。
3. **教训**: `bash -n` 语法防线必须覆盖「含复杂内插(SQL/密码/引号)」的每一种
   节点类型, 而不只是最简单的那种; 生成器双重转义类 bug 只有运行时或逐字节
   repr 检查能暴露 (`repr(line)` 看到字符间空格才定位到 join 误用)。

### 6.2 测试环境问题 (非产品 Bug)

- **Docker Desktop VM 反复崩溃**: GB 级 tar 流 SSH 传输期间引擎整体 255 退出
  (>=4 次, 16GB VM), 之后复测发现同一步骤手动执行正常。处理: 节点镜像 tar
  (>300MB) 换成等格式小代理 tar (真实大镜像流传输已在 redis 138MB×3 验证),
  编排/SSH/安装逻辑不受影响。
- **deploy.sh 主部署机冒烟**: 步骤 1-4 (docker 检测跳过/compose 检测/镜像加载
  +架构校验/磁盘与端口预检) 全过, 步骤 5 起需可写目录——:ro 挂载报
  `Read-only file system` 属预期 (真实使用为本地解包目录); 无 docker daemon
  时 `docker info` 失败正确 die 并提示 journalctl 排查, 守护逻辑有效。
- **沙箱拦截 wsl.exe**: 测试后期 Docker Desktop 的 WSL 后端被安全策略拦截,
  引擎无法稳定重启, S3 主部署机完整流程与 S6 运行时分发中断——待解除黑名单
  后可续测 (复现环境与包均已就绪)。

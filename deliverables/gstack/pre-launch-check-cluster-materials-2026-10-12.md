# 上线前全检报告 — 中间件离线部署包（origin/main..main 7 提交）

**日期**：2026-10-12
**场景**：上线前检查（代码审查 + 安全审计 + QA 测试）
**审查范围**：`git log origin/main..main` 共 7 提交、22 文件（+1560/−153）——集群角色多选+ipvs、docker CRI 离线支持、私有仓库账号密码/数据目录、进度弹窗竞态修复、docker-compose 404 修复、镜像收集三连修复+docker 自愈、tarfile 导入修复

---

## 执行摘要

- 整体结论：**No-Go**（4 项 P0 上线阻断；全部修复量小，预计合计 <100 行改动，修完复检可转 Go）
- 阻塞项数量：**4**（1 产品缺陷 + 2 安全 + 1 数据完整性）
- 最重要发现：**主打「镜像自动收集」的入口在打包时整体不可达**——`cluster.py` 自动补料函数引用未定义的 `ccat`，NameError 被 `try/except` 吞掉只留 warning（此前手工收集成功掩盖了这条断线）
- 安全侧确认一条链式风险：本地 Web API 无鉴权（drive-by 可触发打包）+ 部署脚本 sed 占位符可被数据目录值引号逃逸 → 组合构成「毒化 config → 客户节点 root RCE」链
- QA 基线全绿（pytest 63 passed / node / bash -n / py_compile），但暴露**测试执行体系三层失守**：check() 空转不断言、CI 只跑一个测试文件、旗舰回归测试从未在任何门禁中真正执行过

---

## 核心结论

| 项目 | 内容 |
|------|------|
| Go / No-Go | **No-Go**（修完 4 项 P0 后复检） |
| 严重度分布 | P0 4 / P1 4 / P2 6 / P3 4 |
| 关键行动项 | 4 条 P0 + 4 条 P1 |
| 建议负责人 | 开发（P0/P1）· 排期迭代（P2/P3） |

---

## 1. 各审查维度结论

### 代码审查 — No-Go
- 核心判断：本批「镜像自动收集」链路实际不可达（`_auto_download_cluster_materials` 引用未定义 `ccat`，AST 验证 4 处 Load 必然 NameError，被外层 except 吞成 warning）——origin/main 就潜伏，本次新代码又落进同一坏块。
- 关键建议：一行修复 + 补接线测试；`_merge_oci_tars` 失败路径须清理残缺 tar（否则下次重跑假成功）；docker CRI 离线在旧版本线（docker 23.0.6 不支持 OCI tar load）与已装发行版 docker（unit 覆写硬编码路径）两条部署路径有硬伤，至少加版本预检 fail-fast。
- 正面确认：向后兼容全部通过（旧 roles 三形态/旧 config/旧 manifest），构建产物一致性无问题。

### 安全审计（OWASP+STRIDE）— 有条件通过
- 核心判断：合理的威胁模型下（单用户本地工具 + root 部署是预期设计），存在 2 条 Critical：①本地 Web API 无鉴权且不校验 Content-Type/Host，恶意网页可用 text/plain 简单请求 drive-by 触发打包并注入完全受控配置；②部署脚本把用户可控路径经 sed/单引号拼进生成脚本，值含 `'` 即逃逸 → 客户节点 root 命令注入。两者构成链式攻击。
- 关键建议：P0 修 F-001（Content-Type/Host/Token 三项加固约 30 行）+ F-002/F-007（路径字符白名单 + 环境变量传参弃 sed 占位符）；P1 处理凭据明文 0o644 落盘与下载无哈希钉扎。
- 正面确认：tarfile 使用安全（无 extractall）、manifest 插值统一 bash_quote、服务器池/节点名严格白名单、bundle 全量 sha256 校验、git 未跟踪任何凭据。

### QA 测试与发布 — 条件 Go
- 核心判断：产品代码本身达到可上线水平（全部真实 assert 测试绿、四类语法门禁全绿、预存失败已用 origin/main 对照确认非本次回归），但**测试执行体系三层失守**：①lifecycle/offline_images 两文件用 check() 只记录不断言，pytest 下虚假绿灯；②CI 只跑 test_packer.py；③直跑模式 main() 漏登记旗舰回归测试——本批 5 项修复的回归测试实际不在任何门禁之内。
- 关键建议：合并前完成门禁修复三件套（check() 失败即断言 / CI 纳入另两个测试文件 / main() 补登记）；进度竞态与 _merge_oci_tars 补单测。
- 正面确认：多角色/ipvs 与 docs 合并目录两处有 CI 级真门禁且质量好；变异验证确认 registry tar 回归测试设计有效（虽因体系问题从未生效）。

---

## 2. 综合审查发现（去重合并后按严重度排序）

| # | 严重度 | 类别 | 位置 | 问题描述 | 建议 | 来源 |
|---|--------|------|------|---------|------|------|
| 1 | P0 | 产品缺陷 | packerlib/cluster.py:703-723 | `_auto_download_cluster_materials` 引用未定义 `ccat`，NameError 被 except 吞掉，自动补料链路（镜像/registry tar/chart）整体静默不可达 | 函数开头加 `ccat = catalog["cluster"]`；补「缺料时 monkeypatch ensure_images 断言被调用」接线测试 | 代码审查 |
| 2 | P0 | 安全 | packerlib/web.py:189-316 | Web API 无鉴权、不校验 Content-Type/Host：恶意网页 text/plain 简单请求可 drive-by 调 /api/pack 注入配置；Host 未校验留 DNS rebinding 面可读 localStorage 明文凭据 | do_POST 校验 Content-Type=application/json；校验 Host=127.0.0.1:port；启动生成随机 token 注入页面、API 要求 X-Packer-Token | 安全审计 |
| 3 | P0 | 安全 | deploy-cluster.sh:277,296-307,393-412 + validate.py:163-167 | 用户可控路径（REG_DATA/DOCKER_ROOT 等）经 sed 替换与单引号上下文拼进 root 执行脚本，`'` 逃逸即任意命令注入；_abspath 只查 startswith("/") 无字符白名单 | _abspath 用 LINUX_PATH_RE 白名单（`^/[A-Za-z0-9_./-]*$`）+ 拒 `..`；deploy 脚本改环境变量传参，删除 sed 占位符替换 | 安全审计+代码审查 |
| 4 | P0 | 数据完整性 | prepare_cluster.py `_merge_oci_tars` | 合并失败时残缺 tar 残留 warehouse/，下次重跑被「size>1MB」判断当有效物料静默打进离线包（假成功，同类形态已有前科） | 失败路径 `out.unlink(missing_ok=True)` | 代码审查 |
| 5 | P1 | 产品决策 | deploy-cluster.sh docker 分支 | 旧版本线捆绑 docker 23.0.6 不支持 `docker load` 消费 OCI tar（Engine 25.0+ 才支持）；节点已装发行版 docker 时 unit 覆写硬编码 /usr/local/bin/dockerd 致启动失败被 `|| true` 吞掉 | docker 分支 load 前版本预检 fail-fast（<25 给明确报错）；unit 仅预装分支写或 ExecStart 用探测路径；或文档声明 docker CRI 最低 K8s 版本线 | 代码审查 |
| 6 | P1 | 安全 | prepare_cluster.py:77-363 | kubeadm/kubelet 等 root 二进制经第三方 GH 代理下载，仅校验大小+tar 结构，无官方 sha256 钉扎；镜像代理无 digest 钉扎 | kube 三件套/containerd/etcd 对照官方 checksums 校验；镜像按 digest pull | 安全审计 |
| 7 | P1 | 安全 | builder.py:342-344 + cluster.py:270-275 + webui localStorage | inventory.yaml/config.yaml 固定 0o644 明文携带 SSH root 密码与仓库凭据随包分发；localStorage 存明文密码 | 配置文件写 0o600；README 提示传输后收紧权限；前端预览密码字段脱敏（P3） | 安全审计（QA 线索交叉确认） |
| 8 | P1 | 测试体系 | tests/test_cluster_lifecycle.py、test_offline_images.py、ci.yml | check() 空转不断言（pytest 虚假绿灯）、CI 只跑 test_packer.py、main() 漏登记 test_registry_tar_path_resolution——回归测试形同虚设 | ①conftest autouse 钩子：CHECKS 有失败即 assert；②CI 纳入全部测试文件 + bash -n；③main() 补登记；④修 13 项预存失败；⑤registry tar 失败上报测试注入临时路径消环境依赖 | QA |
| 9 | P2 | 安全 | validate.py:322 → deploy-cluster.sh:440-442 | certs_renew_cron 无字符校验，换行可注入任意 root crontab 条目 | 白名单正则 `^[\d*,\-/ ]+$` | 安全审计 |
| 10 | P2 | 安全 | validate.py:230 → cluster.py:148-153 | kubelet extra_config 原文嵌入 kk config，降缩进可注入任意兄弟键 | yaml.safe_load 后白名单回写或缩进强校验 | 安全审计 |
| 11 | P2 | 产品缺陷 | deploy-cluster.sh 步骤 4.5 | awk 以 `^    ports:` 为锚点插入 bind 卷，锚点缺失时静默假成功仍报「已绑定」 | 插入后 grep 复核，失败告警 | 代码审查 |
| 12 | P2 | 产品缺陷 | prepare_cluster.py `ensure_registry_image` | versions.json 缺 docker_registry_image 键时返回 (0.0, []) 静默零收集 | 返回 failed 提示 | 代码审查 |
| 13 | P2 | 测试 | tests/test_cluster_lifecycle.py:100 | 回归测试有环境依赖（本机 warehouse 已有 tar 时失败上报分支不可达） | 注入临时 warehouse 路径 | QA |
| 14 | P3 | 安全 | validate.py:428 / uninstall-cluster.sh:86 | upgrade_to 缺版本正则；uninstall safe_dir 放行任意绝对目录 | 补 `^v\d+\.\d+\.\d+$`；白名单前缀 | 安全审计 |
| 15 | P3 | 信息 | webui/app.js | 密码明文回填 input value（esc() 已转义无 XSS，后端不持久化，F-001 修复后闭环）；角色复选全量重渲染仅体验问题 | 可选：预览脱敏 | 安全审计+QA |

---

## 行动清单

| # | 行动 | 负责方 | 紧急度 | 期望完成 |
|---|------|--------|--------|---------|
| 1 | 修 #1 NameError（一行）+ 接线测试；修 #4 残缺 tar 清理（一行） | 开发 | P0 | 复检前 |
| 2 | F-001 Web API 三项加固（Content-Type/Host/Token） | 开发 | P0 | 复检前 |
| 3 | F-002/F-007 路径白名单 + deploy 脚本 env 传参弃 sed 占位符 | 开发 | P0 | 复检前 |
| 4 | docker 分支版本预检 fail-fast（#5 决策项最小实现） | 开发+产品 | P1 | 上线或紧随 |
| 5 | 测试门禁三件套：conftest 断言钩子 / CI 纳入全部测试 / main() 补登记 | 开发 | P1 | 紧随合并 |
| 6 | inventory/config 0o600 + README 凭据提示 | 开发 | P1 | 下一迭代 |
| 7 | 下载哈希钉扎（官方 checksums） | 开发 | P1 | 下一迭代 |
| 8 | #9-#14 逐项排期（crontab 白名单、extra_config、awk 复核、进度竞态/合并 tar 单测等） | 开发 | P2/P3 | 迭代排期 |

---

## 待完善 / 已知局限

- 本次为首次全面安全审计，结果可作趋势基线存档对比。
- 进度竞态修复（progress.start()）与 _merge_oci_tars 零单测，异步时序类最易回归。
- CI 覆盖缺口是既有问题（非本次引入），但被本批改动放大（+133 行 shell 无任何门禁）。
- 本机 Docker Desktop 4.94 的大流量 stream 偶发 502 现象仍在，物料收集已用逐镜像方案规避。
- 原始审查记录（代码审查清单、STRIDE 建模表、QA 执行结果表）存档于会话报告，本文为去重合并后的总账。

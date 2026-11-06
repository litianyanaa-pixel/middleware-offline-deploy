# 后续待办

从 README 的「FAQ / 已知事项」、CHANGELOG 和几轮真机记录里攒出来的清单。每条写清楚现在卡在哪、准备怎么改、改完怎么验。行号会变，只记文件和函数名。已经做完的放在最后一节。

## 安全

明文密码是这个项目现在最该补的一条。`builder.py` 写 `manifest.json` 时把 `secrets` 原样落盘，`gen_manifest_sh()`、`gen_env()` 产出的 `manifest.sh` 和 `.env` 也都带明文口令。文件权限已经收到 600，README 也写了分发管控，但包一旦被拷走密码就跟着走。打算给契约加 `contains_plaintext_secrets` 标记和脱敏导出开关，密码行改成部署时由运维注入，或者在发布归档里把含密文件剥掉；XXL-Job 首次登录改密的提醒也补进部署报告。验收：用 fixture 里的口令在产物中 grep 不到，含密文件落盘 600，新增测试断言三个生成函数都不含真实密码值。

`distribute.sh` 的明文 SSH 密码已经清掉了，见最后一节。

## 可复现

产物还做不到字节级一致：`builder.py` 里 `generated_at`、tar member 的 `mtime`、备份目录名、补料脚本注释都取当前时间，而 10-15 那份实测报告写过「同配置连打 3 次逐字节一致」，两处对不上，得先复核是 gzip 层做了归一化还是当时的描述就不准。打算接 `SOURCE_DATE_EPOCH`（或加 `--repro`），统一时间源并固定 gzip 头；CI 加一条「连打两次 sha256 相同」。不设该变量时保持现状。

契约目前只做了生成和消费，没有独立 schema 文件，部署前也不做强校验，坏包要等 bash 数组读不出来才暴露。补一份 schema 和 `deploy.sh` 启动时的检查，比继续往上堆字段要紧。

配置覆盖策略虽然已经进了 `CONF_POLICY`，但 `tests/test_packer.py` 的 `_conf_harness()` 还是把文件名硬编码在行为测试里。新增一个 conf 文件时得同时改 `deploy.sh` 和测试，容易漏。让行为测试从契约参数化生成，这条约定才算真的落地。

## 工程化

docs/ 站点现在靠 `tools/sync_pages.py` 一条命令刷新合并目录、同步站点、重建 `packer.html` 与 `versions.js` 两份、再跑 `--check`。剩下的小尾巴：`update_pages.py` 会重复 dump 一次 catalog，理想情况是收敛成只拷 `logos.js`；`--check` 也还没校验 `docs/versions.json` 与 `plugins/` 是否同步，需要一条独立回归看着。

测试环境目前靠人记：pytest 的 `TMP/TEMP` 没指到 E 盘时 `tmp_path` 会落到系统盘，仓库里没有强制配置。可以在 `tests/conftest.py` 加 session 级断言，没带环境变量直接报错提示。

加速器那张实测表里好几个源已经停服或 403，`versions.json` 预填的六个还写死着。写个只读探测脚本输出可达性和验证日期，缺料自动补齐失败时给明确降级提示，别静默继续。六个源全挂时也应该能打出「仅含已缓存物料」的包，只是要告警。

README 里还写着「插件中间件暂未纳入定时备份，backup.sh/restore.sh 仅覆盖 MySQL」，但备份早就覆盖 MySQL/PostgreSQL/MongoDB 了，两处说法打架，要统一。

## 功能

补挂从库：现在只有主从同批首次部署才自动全量同步，给已有数据的主库后加从库得手工迁数据（README 也写明了）。可以做两阶段：检测到主库有数据且从库为空时自动导出导入、定位 GTID 位点，再建复制。验收是「先单主写数据 → 后补从库」跑通，IO/SQL 线程 Yes、延迟 0。

Redis 默认不限内存，`templates/redis.conf` 的 `maxmemory` 是注释状态。打包表单加个默认按宿主机内存给安全值的字段，同时保留「不限制」选项。

arm64 的 K8s 集群还被 `validate.py` 一行硬编码挡着，页面也写「后续开放」。要放开得补齐 `third_party/kubekey` 的 arm64 产物和 `cluster.py` 的物料清单；MySQL 5.7 在 arm64 继续禁用，但提示要写清楚。

`tests/e2e_real.py` 现在把每台机当独立目标跑单机闭环，上一轮真机验过的 `distribute.sh` 跨机分发和主从复制还没进框架。加 `--topology master-slave` 分支，带上 `SHOW REPLICA STATUS` 断言和两机清场。

## 已经做完

- 包内契约（`packerlib/contract.py` → `manifest.json` 的 `contract` 字段 + `manifest.sh` 的 bash 投影），配置带 `schema_version`，旧配置自动迁移。
- `deploy.sh` 的「本次将变更」diff、只读 preflight、`--dry-run`、三态配置覆盖、《部署报告.json》。
- `distribute.sh` 不再写明文 SSH 密码，改 `MW_SSH_PASSWORD` 或免密。
- 前端 7 项改造 + 浏览器视图/流程测试，本机 Docker 只检测不安装。
- 17 条生成脚本 golden 基线、参数化真机 e2e 框架、`tools/sync_pages.py` 单一入口。
- 5 处遗留问题修复（`restore.sh list`、卸载锁、重部署配置覆盖、`__NODE_IP__`、恢复提示）和真机验证。

## 本地自检

改了生成器或模板后跑一遍：

```bash
python tests/test_generated_baseline.py --update   # 确认产物变化是预期的再刷新基线
python tools/sync_pages.py                        # 刷新 docs/ 站点并校验同步
python -m pytest tests/ -q -p no:cacheprovider
```

没带 `TMP/TEMP` 时本机 pytest 的临时目录会跑到系统盘，跑之前记得指到 E 盘。

# 更新说明 (Changelog)

按时间倒序记录每次功能与修复。`docs/` 演示站随前端改动同步更新。

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

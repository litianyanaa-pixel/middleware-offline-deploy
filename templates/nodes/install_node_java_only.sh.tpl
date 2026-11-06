#!/usr/bin/env bash
# 由 packer.py 自动生成 — 节点 @@NAME@@ 安装脚本 (Java 运行时专用, 无容器服务, 可重复执行)
set -euo pipefail
cd "$(dirname "$0")"
NODE_NAME=@@NAME@@
log(){ printf '[node-%s] %s\n' "$NODE_NAME" "$*"; }
die(){ printf '[node-%s] 错误: %s\n' "$NODE_NAME" "$*" >&2; exit 1; }

@@JAVA_SH@@

log "节点 $NODE_NAME 安装完成 (Java 运行时)"

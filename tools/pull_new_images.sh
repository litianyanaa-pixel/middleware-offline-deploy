#!/usr/bin/env bash
# 新增中间件镜像双架构拉取(免守护进程, crane -> docker load 兼容 tar)
set -u
cd "$(dirname "$0")/.."
CRANE="tools/bin/crane.exe"
M="docker.1ms.run"

# 镜像tar按仓库规范落位: warehouse/images/<key>/<version>/<arch>.tar
pull() { # $1=源引用 $2=平台 $3=输出文件
  local src="$1" plat="$2" out="$3"
  if [ -s "$out" ]; then echo "SKIP $out (已存在)"; return 0; fi
  echo "[pull] $plat $src"
  if "./$CRANE" pull --platform="$plat" "$src" "$out"; then
    echo "[ok] $out ($(du -m "$out" | cut -f1) MB)"
  else
    echo "[FAIL] $out"; rm -f "$out"; return 1
  fi
}

mkdir -p warehouse/images/rabbitmq/4.3.5-management \
         warehouse/images/mongodb/8.0.30 \
         warehouse/images/prometheus/v3.14.0 \
         warehouse/images/grafana/13.2.1 \
         warehouse/images/elasticsearch/9.3.0 \
         warehouse/images/kibana/9.3.0

for arch in amd64 arm64; do
  pull "$M/library/rabbitmq:4.3.5-management"   "linux/$arch" "warehouse/images/rabbitmq/4.3.5-management/$arch.tar"
  pull "$M/library/mongo:8.0.30"                "linux/$arch" "warehouse/images/mongodb/8.0.30/$arch.tar"
  pull "$M/prom/prometheus:v3.14.0"             "linux/$arch" "warehouse/images/prometheus/v3.14.0/$arch.tar"
  pull "$M/grafana/grafana:13.2.1"              "linux/$arch" "warehouse/images/grafana/13.2.1/$arch.tar"
  pull "docker.elastic.co/elasticsearch/elasticsearch:9.3.0" "linux/$arch" "warehouse/images/elasticsearch/9.3.0/$arch.tar"
  pull "docker.elastic.co/kibana/kibana:9.3.0"              "linux/$arch" "warehouse/images/kibana/9.3.0/$arch.tar"
done

echo "==== 汇总 ===="
ls -la warehouse/images/rabbitmq/4.3.5-management warehouse/images/mongodb/8.0.30 warehouse/images/prometheus/v3.14.0 warehouse/images/grafana/13.2.1 warehouse/images/elasticsearch/9.3.0 warehouse/images/kibana/9.3.0

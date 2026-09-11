#!/usr/bin/env bash
# 观测三件套 + Kafka 镜像双架构拉取(免守护进程)
set -u
cd "$(dirname "$0")/.."
CRANE="tools/bin/crane.exe"
M="docker.1ms.run"

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

mkdir -p warehouse/images/node-exporter/v1.12.1 \
         warehouse/images/loki/3.7.7 \
         warehouse/images/promtail/3.6.11 \
         warehouse/images/kafka/4.3.1 \
         warehouse/images/kafka-ui/v0.7.2

for arch in amd64 arm64; do
  pull "$M/prom/node-exporter:v1.12.1"       "linux/$arch" "warehouse/images/node-exporter/v1.12.1/$arch.tar"
  pull "$M/grafana/loki:3.7.7"               "linux/$arch" "warehouse/images/loki/3.7.7/$arch.tar"
  pull "$M/grafana/promtail:3.6.11"          "linux/$arch" "warehouse/images/promtail/3.6.11/$arch.tar"
  pull "$M/apache/kafka:4.3.1"               "linux/$arch" "warehouse/images/kafka/4.3.1/$arch.tar"
  pull "$M/provectuslabs/kafka-ui:v0.7.2"    "linux/$arch" "warehouse/images/kafka-ui/v0.7.2/$arch.tar"
done

echo "==== 汇总 ===="
du -sm warehouse/images/node-exporter warehouse/images/loki warehouse/images/promtail warehouse/images/kafka warehouse/images/kafka-ui 2>/dev/null

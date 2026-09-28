#!/usr/bin/env bash
# 拉取 Kafka 集群形态(bitnami/kafka 3.7.0)镜像到 warehouse/, 双架构各存一个 docker save tar
# 用法: bash tools/pull_kafka_cluster.sh
# 依赖: 本机 Docker 已启动(推荐), 或 tools/bin/crane.exe(免守护进程, 见 pull_obs_kafka.sh)
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="bitnami/kafka:3.7.0"
MIRROR="swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/bitnami/kafka:3.7.0"   # 2026-09 实测
OUT_DIR="warehouse/images/kafka/3.7.0"
mkdir -p "$OUT_DIR"

echo "== 1/2 amd64 =="
docker pull --platform linux/amd64 "$MIRROR"
docker tag "$MIRROR" "$IMAGE"
docker save "$IMAGE" -o "$OUT_DIR/amd64.tar"
echo "  -> $OUT_DIR/amd64.tar"

echo "== 2/2 arm64 =="
# 注意: 部分国内镜像源只有 amd64 清单(华为云 swr 实测), 需要多源尝试或走 crane
if docker pull --platform linux/arm64 "$MIRROR" 2>/dev/null; then
  docker tag "$MIRROR" "$IMAGE"
  docker save "$IMAGE" -o "$OUT_DIR/arm64.tar"
else
  echo "  docker pull arm64 失败, 回退 crane (tools/bin/crane.exe):"
  CRANE="tools/bin/crane"
  [ -x "$CRANE.exe" ] && CRANE="tools/bin/crane.exe"
  for src in "bitnami/kafka:3.7.0" \
             "docker.xuanyuan.me/bitnami/kafka:3.7.0" \
             "docker.m.daocloud.io/bitnami/kafka:3.7.0" \
             "docker.1panel.live/bitnami/kafka:3.7.0"; do
    echo "  try: $src"
    if "$CRANE" pull --platform=linux/arm64 "$src" /tmp/kafka-arm64.tar; then
      docker load -i /tmp/kafka-arm64.tar
      docker tag "$src" "$IMAGE" 2>/dev/null || docker tag "$(docker images --format '{{.Repository}}:{{.Tag}}' | grep 'bitnami/kafka' | head -1)" "$IMAGE"
      docker save "$IMAGE" -o "$OUT_DIR/arm64.tar"
      break
    fi
  done
fi
echo "  -> $OUT_DIR/arm64.tar"
echo "完成。集群形态物料: $OUT_DIR/{amd64,arm64}.tar"

#!/usr/bin/env bash
# 拉取 Alertmanager (prom/alertmanager v0.28.1) 镜像到 warehouse/, 双架构各存一个 docker save tar
# 用法: bash tools/pull_alertmanager.sh
# 依赖: 本机 Docker 已启动(推荐), 或 tools/bin/crane.exe(免守护进程)
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="prom/alertmanager:v0.28.1"
MIRRORS="docker.1ms.run docker.1panel.live docker.xuanyuan.me"
OUT_DIR="warehouse/images/alertmanager/v0.28.1"
mkdir -p "$OUT_DIR"

pull_amd64() {
  for m in $MIRRORS; do
    echo "try docker pull $m/prom/alertmanager:v0.28.1 (amd64)"
    if docker pull --platform linux/amd64 "$m/prom/alertmanager:v0.28.1"; then
      docker tag "$m/prom/alertmanager:v0.28.1" "$IMAGE"
      docker save "$IMAGE" -o "$OUT_DIR/amd64.tar"
      return 0
    fi
  done
  return 1
}

pull_arm64() {
  # 优先守护进程, 失败回退 crane (免守护进程, 见 pull_obs_kafka.sh)
  for m in $MIRRORS; do
    if docker pull --platform linux/arm64 "$m/prom/alertmanager:v0.28.1" 2>/dev/null; then
      docker tag "$m/prom/alertmanager:v0.28.1" "$IMAGE"
      docker save "$IMAGE" -o "$OUT_DIR/arm64.tar"
      return 0
    fi
  done
  echo "docker pull arm64 失败, 回退 crane:"
  CRANE="tools/bin/crane"
  [ -x "$CRANE.exe" ] && CRANE="tools/bin/crane.exe"
  for m in $MIRRORS; do
    echo "  try crane: $m/prom/alertmanager:v0.28.1"
    if "$CRANE" pull --platform=linux/arm64 "$m/prom/alertmanager:v0.28.1" /tmp/am-arm64.tar; then
      docker load -i /tmp/am-arm64.tar
      # crane tar 的仓库名可能带镜像源前缀, 统一改回官方短名
      docker tag "$m/prom/alertmanager:v0.28.1" "$IMAGE" 2>/dev/null || \
        docker tag "$(docker images --format '{{.Repository}}:{{.Tag}}' | grep 'alertmanager' | head -1)" "$IMAGE"
      docker save "$IMAGE" -o "$OUT_DIR/arm64.tar"
      return 0
    fi
  done
  return 1
}

echo "== 1/2 amd64 =="
pull_amd64 || { echo "[FAIL] amd64 拉取失败"; exit 1; }
echo "  -> $OUT_DIR/amd64.tar"

echo "== 2/2 arm64 =="
pull_arm64 || { echo "[FAIL] arm64 拉取失败"; exit 1; }
echo "  -> $OUT_DIR/arm64.tar"

echo "完成。物料: $OUT_DIR/{amd64,arm64}.tar"
docker images --format '{{.Repository}}:{{.Tag}}  {{.Size}}' | grep alertmanager || true

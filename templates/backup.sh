#!/usr/bin/env bash
#===============================================================================
#  MySQL 定时备份脚本 (由 packer.py 生成到离线包, 部署后位于部署目录)
#
#  用法:   由 crontab 调度执行, 也可手工执行: ./backup.sh
#  依赖:   docker (直接使用 mysql 容器内自带的 mysqldump, 无需宿主机装客户端)
#  行为:   按库分文件导出(排除系统库) -> gzip -> 保留最近 BACKUP_KEEP 份
#  配置:   同目录 backup.conf
#===============================================================================
set -euo pipefail

CONF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/backup.conf"
[ -f "$CONF" ] || { echo "[backup] 缺少 backup.conf"; exit 1; }
# shellcheck disable=SC1090
source "$CONF"

log() { echo "[$(date '+%F %T')] $*"; }

mkdir -p "$BACKUP_DIR"
exec 9>"$BACKUP_DIR/.lock"
flock -n 9 || { log "已有备份任务在运行, 本次跳过"; exit 0; }

TS="$(date +%Y%m%d_%H%M%S)"
FAIL=0

for SVC in "${MYSQL_SERVICES[@]}"; do
  # 容器必须健康才备份
  st="$(docker inspect -f '{{.State.Health.Status}}' "$SVC" 2>/dev/null || echo na)"
  if [ "$st" != "healthy" ]; then
    log "[$SVC] 容器状态 $st, 跳过本次备份"
    FAIL=1; continue
  fi

  mkdir -p "$BACKUP_DIR/$SVC"

  # 枚举用户库(排除系统库), 按库分文件, 便于单库恢复
  DBS="$(docker exec -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$SVC" mysql -uroot -N \
    -e "SELECT schema_name FROM information_schema.schemata
        WHERE schema_name NOT IN ('information_schema','mysql','performance_schema','sys');" 2>/dev/null || true)"
  if [ -z "$DBS" ]; then
    log "[$SVC] 未枚举到任何用户库, 跳过"
    FAIL=1; continue
  fi

  for DB in $DBS; do
    OUT="$BACKUP_DIR/$SVC/${DB}_${TS}.sql.gz"
    if docker exec -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$SVC" \
        mysqldump -uroot --single-transaction --routines --triggers --events \
        --default-character-set=utf8mb4 --databases "$DB" 2>/dev/null | gzip > "$OUT" \
       && [ "$(stat -c%s "$OUT" 2>/dev/null || echo 0)" -gt 500 ]; then
      log "[$SVC] $DB -> $(basename "$OUT") ($(du -h "$OUT" | cut -f1))"
    else
      rm -f "$OUT"
      log "[$SVC] $DB 备份失败!"
      FAIL=1
    fi
  done

  # 轮转: 每个服务目录只保留最近 BACKUP_KEEP 份
  ls -1t "$BACKUP_DIR/$SVC"/*.sql.gz 2>/dev/null | tail -n +$((BACKUP_KEEP + 1)) | while read -r old; do
    rm -f "$old"; log "[$SVC] 轮转删除: $(basename "$old")"
  done
done

if [ "$FAIL" -eq 0 ]; then log "备份全部完成"; else log "备份完成(部分失败, 见上方日志)"; exit 1; fi

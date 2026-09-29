#!/usr/bin/env bash
#===============================================================================
#  数据库定时备份脚本 (由 packer.py 生成到离线包, 部署后位于部署目录)
#  支持: MySQL(mysqldump) / PostgreSQL(pg_dump) / MongoDB(mongodump)
#        全部使用容器内自带客户端, 宿主机无需安装任何数据库工具
#
#  用法:   ./backup.sh [mysql|pg|mongo|all]   缺省 all(执行所有已启用的引擎)
#          crontab 里按引擎分别调度, 每种数据库可有独立的星期/时间/保留份数
#  行为:   按库分文件导出(排除系统库) -> 压缩 -> 各引擎保留最近 N 份
#  配置:   同目录 backup.conf (由打包器按本次所选数据库自动生成)
#===============================================================================
set -euo pipefail

CONF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/backup.conf"
[ -f "$CONF" ] || { echo "[backup] 缺少 backup.conf"; exit 1; }
# shellcheck disable=SC1090
source "$CONF"

TARGET="${1:-all}"
case "$TARGET" in mysql|pg|mongo|all) ;; *)
  echo "[backup] 用法: $0 [mysql|pg|mongo|all]"; exit 2 ;;
esac

log() { echo "[$(date '+%F %T')] $*"; }

mkdir -p "$BACKUP_DIR"
exec 9>"$BACKUP_DIR/.lock"
# flock 用于防止备份并发(服务器端 util-linux 自带); 无 flock 的环境(如本机测试)直接跳过
if command -v flock >/dev/null 2>&1; then
  flock -n 9 || { log "已有备份任务在运行, 本次跳过"; exit 0; }
fi

TS="$(date +%Y%m%d_%H%M%S)"
FAIL=0
healthy() { [ "$(docker inspect -f '{{.State.Health.Status}}' "$1" 2>/dev/null || echo na)" = "healthy" ]; }
rotate()  { # $1=服务目录 $2=文件名通配 $3=保留份数 (文件名含时间戳, 字典序=时间序; find 空结果安全)
  local old
  find "$1" -maxdepth 1 -type f -name "$2" | sort -r | tail -n +$(( $3 + 1 )) | while read -r old; do
    rm -f "$old"; log "轮转删除: $(basename "$old")"
  done
}

#================= MySQL (mysqldump 按库导出) =================
do_mysql() {
  [ "${MYSQL_ENABLED:-0}" = "1" ] || return 0
  for SVC in "${MYSQL_SERVICES[@]}"; do
    [ -n "$SVC" ] || continue
    if ! healthy "$SVC"; then
      log "[$SVC] 容器状态异常, 跳过本次备份"
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

    rotate "$BACKUP_DIR/$SVC" '*.sql.gz' "${MYSQL_KEEP:-7}"
  done
}

#================= PostgreSQL (pg_dump 按库导出) =================
do_pg() {
  [ "${PG_ENABLED:-0}" = "1" ] || return 0
  for SVC in "${PG_SERVICES[@]}"; do
    [ -n "$SVC" ] || continue
    if ! healthy "$SVC"; then
      log "[$SVC] 容器状态异常, 跳过本次备份"
      FAIL=1; continue
    fi

    mkdir -p "$BACKUP_DIR/$SVC"

    # 枚举用户库(排除模板库)
    DBS="$(docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$SVC" \
      psql -U postgres -At -c "SELECT datname FROM pg_database WHERE NOT datistemplate;" 2>/dev/null || true)"
    if [ -z "$DBS" ]; then
      log "[$SVC] 未枚举到任何用户库, 跳过"
      FAIL=1; continue
    fi

    for DB in $DBS; do
      case "$DB" in *[!A-Za-z0-9_]*) continue ;; esac   # 跳过异常库名
      OUT="$BACKUP_DIR/$SVC/${DB}_${TS}.sql.gz"
      if docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$SVC" \
          pg_dump -U postgres --no-owner --no-privileges -d "$DB" 2>/dev/null | gzip > "$OUT" \
         && [ "$(stat -c%s "$OUT" 2>/dev/null || echo 0)" -gt 200 ]; then
        log "[$SVC] $DB -> $(basename "$OUT") ($(du -h "$OUT" | cut -f1))"
      else
        rm -f "$OUT"
        log "[$SVC] $DB 备份失败!"
        FAIL=1
      fi
    done

    rotate "$BACKUP_DIR/$SVC" '*.sql.gz' "${PG_KEEP:-7}"
  done
}

#================= MongoDB (mongodump 按库导出, archive+gzip) =================
do_mongo() {
  [ "${MONGO_ENABLED:-0}" = "1" ] || return 0
  for SVC in "${MONGO_SERVICES[@]}"; do
    [ -n "$SVC" ] || continue
    if ! healthy "$SVC"; then
      log "[$SVC] 容器状态异常, 跳过本次备份"
      FAIL=1; continue
    fi

    mkdir -p "$BACKUP_DIR/$SVC"

    # 枚举用户库(排除系统库)
    DBS="$(docker exec "$SVC" mongosh --quiet \
      -u "$MONGO_USER" -p "$MONGO_PASSWORD" --authenticationDatabase admin \
      --eval 'db.adminCommand({listDatabases:1}).databases.map(d=>d.name).join("\n")' 2>/dev/null \
      | grep -vE '^(admin|config|local)$' | grep -v '^$' || true)"
    if [ -z "$DBS" ]; then
      log "[$SVC] 未枚举到任何用户库, 跳过"
      FAIL=1; continue
    fi

    for DB in $DBS; do
      case "$DB" in *[!A-Za-z0-9_-]*) continue ;; esac   # 跳过异常库名
      OUT="$BACKUP_DIR/$SVC/${DB}_${TS}.archive"
      if docker exec "$SVC" mongodump \
          -u "$MONGO_USER" -p "$MONGO_PASSWORD" --authenticationDatabase admin \
          --db "$DB" --archive --gzip > "$OUT" 2>/dev/null \
         && [ "$(stat -c%s "$OUT" 2>/dev/null || echo 0)" -gt 200 ]; then
        log "[$SVC] $DB -> $(basename "$OUT") ($(du -h "$OUT" | cut -f1))"
      else
        rm -f "$OUT"
        log "[$SVC] $DB 备份失败!"
        FAIL=1
      fi
    done

    rotate "$BACKUP_DIR/$SVC" '*.archive' "${MONGO_KEEP:-7}"
  done
}

case "$TARGET" in
  mysql) do_mysql ;;
  pg)    do_pg ;;
  mongo) do_mongo ;;
  all)   do_mysql; do_pg; do_mongo ;;
esac

if [ "$FAIL" -eq 0 ]; then log "备份完成[$TARGET]"; else log "备份完成[$TARGET](部分失败, 见上方日志)"; exit 1; fi

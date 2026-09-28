#!/usr/bin/env bash
#===============================================================================
#  数据库备份恢复脚本 (由 packer.py 生成到离线包, 部署后位于部署目录)
#  支持: MySQL (mysqldump) / PostgreSQL (pg_dump) / MongoDB (mongodump)
#       全部使用容器内自带客户端, 宿主机无需安装任何数据库工具
#
#  用法:
#    ./restore.sh list                            列出备份目录里的全部可用备份
#    ./restore.sh <服务名> <库名>                 恢复该库最近一份备份
#    ./restore.sh <服务名> <库名> <文件名>        恢复指定的备份文件
#    以上命令均可追加 --yes 跳过交互确认(用于自动化)
#    服务名即 compose 服务/容器名: mysql8 / postgres / mongodb ...
#
#  行为: 恢复前会先把目标库当前内容做一次安全备份(pre_restore_*), 恢复即覆盖同名库/表。
#  配置: 同目录 backup.conf (优先) 或 .env 中的对应密码
#===============================================================================
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR_CFG="/data/backup/mysql"

log() { echo "[$(date '+%F %T')] $*"; }
err() { echo "[$(date '+%F %T')] [ERROR] $*" >&2; }
die() { err "$*"; exit 1; }

# ---- 读取配置: backup.conf 优先, 否则从 .env 取各密码 ----
if [ -f "$DIR/backup.conf" ]; then
  # shellcheck disable=SC1090
  source "$DIR/backup.conf"
elif [ -f "$DIR/.env" ]; then
  MYSQL_ROOT_PASSWORD="$(grep -E '^MYSQL_ROOT_PASSWORD=' "$DIR/.env" | head -1 | cut -d= -f2- | sed 's/^"//; s/"$//')"
  POSTGRES_PASSWORD="$(grep -E '^POSTGRES_PASSWORD=' "$DIR/.env" | head -1 | cut -d= -f2- | sed 's/^"//; s/"$//')"
  MONGO_USER="$(grep -E '^MONGO_INITDB_ROOT_USERNAME=' "$DIR/.env" | head -1 | cut -d= -f2- | sed 's/^"//; s/"$//')"
  MONGO_PASSWORD="$(grep -E '^MONGO_INITDB_ROOT_PASSWORD=' "$DIR/.env" | head -1 | cut -d= -f2- | sed 's/^"//; s/"$//')"
fi
BACKUP_DIR="${BACKUP_DIR:-$BACKUP_DIR_CFG}"
[ -d "$BACKUP_DIR" ] || die "备份目录不存在: $BACKUP_DIR"

CONFIRM=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --yes|-y) CONFIRM=1 ;;
    *) ARGS+=("$a") ;;
  esac
done

# 与 backup.sh 共用同一把锁, 避免备份/恢复同时进行
exec 9>"$BACKUP_DIR/.lock"

# 列出全部备份 (MySQL/PostgreSQL: .sql.gz, MongoDB: .archive)
do_list() {
  local svc f n=0
  echo "备份目录: $BACKUP_DIR"
  for svc in "$BACKUP_DIR"/*/; do
    [ -d "$svc" ] || continue
    svc="$(basename "$svc")"
    echo "---- $svc ----"
    while IFS= read -r f; do
      [ -z "$f" ] && continue
      n=$((n+1))
      printf "  %s   %8s   %s\n" "$(basename "$f")" "$(du -h "$f" | cut -f1)" "$(date -r "$f" '+%F %T')"
    done < <(ls -1t "$svc" 2>/dev/null | grep -v '^pre_restore_' | grep -E '\.(sql\.gz|archive)$' || true)
  done
  [ "$n" -gt 0 ] || echo "  (没有找到任何备份)"
  echo
  echo "恢复用法: ./restore.sh <服务名> <库名> [文件名]  (加 --yes 免确认)"
}

# 定位最新一份备份: <svc> <db> <通配>
list_latest() {
  ls -1t "$BACKUP_DIR/$1/${2}_"$3 2>/dev/null \
    | grep -v "pre_restore_" | head -1 || true
}

container_running() { # <svc>
  docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null | grep -q true
}

ask_confirm() { # <说明>
  if [ "$CONFIRM" -ne 1 ]; then
    printf '确认把以上备份恢复到 %s ? 恢复会覆盖同名数据。输入 yes 继续: ' "$1"
    read -r ans
    [ "$ans" = "yes" ] || die "已取消"
  fi
}

#================= MySQL =================
restore_mysql() {
  local SVC="$1" DB="$2" FILE="$3"
  [ -n "${MYSQL_ROOT_PASSWORD:-}" ] || die "未找到 MYSQL_ROOT_PASSWORD (缺少 backup.conf 且 .env 中无该配置)"
  printf '%s' "$DB" | grep -qE '^[A-Za-z0-9_]+$' || die "库名不合法: $DB"

  # 恢复前安全备份
  local NOW_TS PRE DB_EXISTS
  NOW_TS="$(date +%Y%m%d_%H%M%S)"
  PRE="$BACKUP_DIR/$SVC/pre_restore_${DB}_${NOW_TS}.sql.gz"
  DB_EXISTS="$(docker exec -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$SVC" mysql -uroot -N \
    -e "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='$DB';" 2>/dev/null || echo 0)"
  if [ "${DB_EXISTS:-0}" -gt 0 ]; then
    mkdir -p "$BACKUP_DIR/$SVC"
    log "目标库已存在, 先做恢复前安全备份 -> $(basename "$PRE")"
    docker exec -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$SVC" \
      mysqldump -uroot --single-transaction --routines --triggers --events \
      --default-character-set=utf8mb4 --databases "$DB" 2>/dev/null | gzip > "$PRE" \
      && [ "$(stat -c%s "$PRE" 2>/dev/null || echo 0)" -gt 500 ] \
      || die "恢复前安全备份失败, 已中止恢复 (原备份文件未动)"
    log "安全备份完成 ($(du -h "$PRE" | cut -f1))"
  else
    log "目标库当前不存在(首次恢复), 跳过安全备份"
  fi

  ask_confirm "$SVC/$DB"
  log "开始导入(大库可能耗时较久, 请勿中断)..."
  gunzip -c "$FILE" | docker exec -i -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$SVC" \
    mysql -uroot --default-character-set=utf8mb4 2>&1 | grep -v "Using a password" || true
  local CNT
  CNT="$(docker exec -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$SVC" mysql -uroot -N \
    -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$DB';" 2>/dev/null || echo 0)"
  log "恢复完成: $SVC/$DB 现有表数量 $CNT"
  log "如需回退本次恢复, 可用恢复前安全备份: $(basename "${PRE:-无}")"
}

#================= PostgreSQL =================
restore_pg() {
  local SVC="$1" DB="$2" FILE="$3"
  [ -n "${POSTGRES_PASSWORD:-}" ] || die "未找到 POSTGRES_PASSWORD (缺少 backup.conf 且 .env 中无该配置)"
  printf '%s' "$DB" | grep -qE '^[A-Za-z0-9_]+$' || die "库名不合法: $DB"

  local NOW_TS PRE DB_EXISTS
  NOW_TS="$(date +%Y%m%d_%H%M%S)"
  PRE="$BACKUP_DIR/$SVC/pre_restore_${DB}_${NOW_TS}.sql.gz"
  DB_EXISTS="$(docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$SVC" \
    psql -U postgres -At -c "SELECT COUNT(*) FROM pg_database WHERE datname='$DB';" 2>/dev/null || echo 0)"
  if [ "${DB_EXISTS:-0}" -gt 0 ]; then
    mkdir -p "$BACKUP_DIR/$SVC"
    log "目标库已存在, 先做恢复前安全备份 -> $(basename "$PRE")"
    docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$SVC" \
      pg_dump -U postgres --no-owner --no-privileges -d "$DB" 2>/dev/null | gzip > "$PRE" \
      && [ "$(stat -c%s "$PRE" 2>/dev/null || echo 0)" -gt 200 ] \
      || die "恢复前安全备份失败, 已中止恢复 (原备份文件未动)"
    log "安全备份完成 ($(du -h "$PRE" | cut -f1))"
  else
    log "目标库当前不存在(首次恢复), 跳过安全备份"
  fi

  ask_confirm "$SVC/$DB"
  log "开始导入(大库可能耗时较久, 请勿中断)..."
  gunzip -c "$FILE" | docker exec -i -e PGPASSWORD="$POSTGRES_PASSWORD" "$SVC" \
    psql -U postgres -d "$DB" --set=ON_ERROR_STOP=1 -q 2>&1 || die "psql 导入报错(已中止, 可用安全备份回退)"
  log "恢复完成: $SVC/$DB"
  log "如需回退本次恢复, 可用恢复前安全备份: $(basename "${PRE:-无}")"
}

#================= MongoDB =================
restore_mongo() {
  local SVC="$1" DB="$2" FILE="$3"
  [ -n "${MONGO_PASSWORD:-}" ] || die "未找到 MONGO 密码 (缺少 backup.conf 且 .env 中无该配置)"
  MONGO_USER="${MONGO_USER:-root}"
  printf '%s' "$DB" | grep -qE '^[A-Za-z0-9_-]+$' || die "库名不合法: $DB"
  local AUTH=(-u "$MONGO_USER" -p "$MONGO_PASSWORD" --authenticationDatabase admin)

  local NOW_TS PRE DB_EXISTS
  NOW_TS="$(date +%Y%m%d_%H%M%S)"
  PRE="$BACKUP_DIR/$SVC/pre_restore_${DB}_${NOW_TS}.archive"
  DB_EXISTS="$(docker exec "$SVC" mongosh --quiet "${AUTH[@]}" \
    --eval "db.adminCommand({listDatabases:1}).databases.some(d=>d.name==='$DB')" 2>/dev/null | tail -1 || echo false)"
  if [ "$DB_EXISTS" = "true" ]; then
    mkdir -p "$BACKUP_DIR/$SVC"
    log "目标库已存在, 先做恢复前安全备份 -> $(basename "$PRE")"
    docker exec "$SVC" mongodump "${AUTH[@]}" --db "$DB" --archive --gzip > "$PRE" 2>/dev/null \
      && [ "$(stat -c%s "$PRE" 2>/dev/null || echo 0)" -gt 200 ] \
      || die "恢复前安全备份失败, 已中止恢复 (原备份文件未动)"
    log "安全备份完成 ($(du -h "$PRE" | cut -f1))"
  else
    log "目标库当前不存在(首次恢复), 跳过安全备份"
  fi

  ask_confirm "$SVC/$DB"
  log "开始导入(大库可能耗时较久, 请勿中断)..."
  docker exec -i "$SVC" mongorestore "${AUTH[@]}" --archive --gzip --drop < "$FILE" 2>&1 \
    | grep -iE "done|error" || true
  log "恢复完成: $SVC/$DB (mongorestore --drop 已覆盖同名集合)"
  log "如需回退本次恢复, 可用恢复前安全备份: $(basename "${PRE:-无}")"
}

[ "${#ARGS[@]}" -ge 1 ] && [ "${ARGS[0]}" = "list" ] && { do_list; exit 0; }

[ "${#ARGS[@]}" -ge 2 ] || { do_list; die "参数不足: ./restore.sh <服务名> <库名> [文件名]"; }

SVC="${ARGS[0]}"
DB="${ARGS[1]}"
F="${ARGS[2]:-}"

container_running "$SVC" || die "容器未运行或不存在: $SVC (docker ps 查看)"

# 按引擎匹配通配: mysql/postgres 为 .sql.gz, mongodb 为 .archive
case "$SVC" in
  *mongo*) GLOB='*.archive' ;;
  *)       GLOB='*.sql.gz' ;;
esac

# 定位备份文件
if [ -n "$F" ]; then
  case "$F" in /*) FILE="$F" ;; *) FILE="$BACKUP_DIR/$SVC/$F" ;; esac
else
  FILE="$(list_latest "$SVC" "$DB" "$GLOB")"
fi
[ -n "${FILE:-}" ] && [ -f "$FILE" ] || die "未找到备份文件: ${F:-($SVC/$DB 下没有 ${DB}_*$GLOB)}"
FILE="$(cd "$(dirname "$FILE")" && pwd)/$(basename "$FILE")"

log "准备恢复: $FILE"
log "目标: 容器 $SVC / 数据库 $DB"

case "$SVC" in
  *mongo*)            restore_mongo "$SVC" "$DB" "$FILE" ;;
  *postgres*|*pgsql*) restore_pg    "$SVC" "$DB" "$FILE" ;;
  *)                  restore_mysql "$SVC" "$DB" "$FILE" ;;
esac

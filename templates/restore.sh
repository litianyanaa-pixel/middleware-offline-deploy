#!/usr/bin/env bash
#===============================================================================
#  MySQL 备份恢复脚本 (由 packer.py 生成到离线包, 部署后位于部署目录)
#
#  用法:
#    ./restore.sh list                            列出备份目录里的全部可用备份
#    ./restore.sh <mysql服务> <库名>               恢复该库最近一份备份
#    ./restore.sh <mysql服务> <库名> <文件名>      恢复指定的 .sql.gz
#    以上命令均可追加 --yes 跳过交互确认(用于自动化)
#
#  行为: 恢复前会先把目标库当前内容做一次安全备份(pre_restore_*.sql.gz),
#        恢复即重放 mysqldump(自带 DROP TABLE, 同名表会被备份文件覆盖)。
#  依赖: docker (直接使用 mysql 容器内自带的客户端, 宿主机无需装 mysql)
#  配置: 同目录 backup.conf (优先) 或 .env 中的 MYSQL_ROOT_PASSWORD
#===============================================================================
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR_CFG="/data/backup/mysql"

log() { echo "[$(date '+%F %T')] $*"; }
err() { echo "[$(date '+%F %T')] [ERROR] $*" >&2; }
die() { err "$*"; exit 1; }

# ---- 读取配置: backup.conf 优先, 否则从 .env 取 root 密码 ----
if [ -f "$DIR/backup.conf" ]; then
  # shellcheck disable=SC1090
  source "$DIR/backup.conf"
elif [ -f "$DIR/.env" ]; then
  MYSQL_ROOT_PASSWORD="$(grep -E '^MYSQL_ROOT_PASSWORD=' "$DIR/.env" | head -1 | cut -d= -f2- | sed 's/^"//; s/"$//')"
fi
[ -n "${MYSQL_ROOT_PASSWORD:-}" ] || die "未找到 MYSQL_ROOT_PASSWORD (缺少 backup.conf 且 .env 中无该配置)"
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

strip_q() { local v="$1"; case "$v" in \"*\") v="${v#\"}"; v="${v%\"}" ;; \'*\') v="${v#\'}"; v="${v%\'}" ;; esac; printf '%s' "$v"; }

# 列出全部备份
do_list() {
  local svc f n=0
  echo "备份目录: $BACKUP_DIR"
  for svc in "$BACKUP_DIR"/*/; do
    [ -d "$svc" ] || continue
    svc="$(basename "$svc")"
    echo "---- $svc ----"
    # ls -1t 按时间倒序; 逐行补上大小与时间
    while IFS= read -r f; do
      [ -z "$f" ] && continue
      n=$((n+1))
      printf "  %s   %8s   %s\n" "$(basename "$f")" "$(du -h "$f" | cut -f1)" "$(date -r "$f" '+%F %T')"
    done < <(ls -1t "$BACKUP_DIR/$svc"/*.sql.gz 2>/dev/null || true)
  done
  [ "$n" -gt 0 ] || echo "  (没有找到任何 .sql.gz 备份)"
  echo
  echo "恢复用法: ./restore.sh <mysql服务> <库名> [文件名]  (加 --yes 免确认)"
}

list_latest() { # <svc> <db>  -> 输出最新一份备份路径(不含 pre_restore_)
  ls -1t "$BACKUP_DIR/$1/${2}_"*.sql.gz 2>/dev/null \
    | grep -v "pre_restore_" | head -1 || true
}

[ "${#ARGS[@]}" -ge 1 ] && [ "${ARGS[0]}" = "list" ] && { do_list; exit 0; }

[ "${#ARGS[@]}" -ge 2 ] || { do_list; die "参数不足: ./restore.sh <mysql服务> <库名> [文件名]"; }

SVC="${ARGS[0]}"
DB="${ARGS[1]}"
F="${ARGS[2]:-}"

# 参数合法性: 库名只能字母数字下划线(要拼进 SQL, 必须严卡)
printf '%s' "$DB" | grep -qE '^[A-Za-z0-9_]+$' || die "库名不合法: $DB"
docker inspect -f '{{.State.Running}}' "$SVC" 2>/dev/null | grep -q true \
  || die "mysql 容器未运行或不存在: $SVC (docker ps 查看)"

# 定位备份文件
if [ -n "$F" ]; then
  case "$F" in /*) FILE="$F" ;; *) FILE="$BACKUP_DIR/$SVC/$F" ;; esac
else
  FILE="$(list_latest "$SVC" "$DB")"
fi
[ -n "${FILE:-}" ] && [ -f "$FILE" ] || die "未找到备份文件: ${F:-($SVC/$DB 下没有 ${DB}_*.sql.gz)}"
FILE="$(cd "$(dirname "$FILE")" && pwd)/$(basename "$FILE")"

log "准备恢复: $FILE"
log "目标: 容器 $SVC / 数据库 $DB"

# 恢复前安全备份目标库当前内容
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

# 确认
if [ "$CONFIRM" -ne 1 ]; then
  printf '确认把以上备份恢复到 %s/%s ? 恢复会覆盖同名表数据。输入 yes 继续: ' "$SVC" "$DB"
  read -r ans
  [ "$ans" = "yes" ] || die "已取消"
fi

log "开始导入(大库可能耗时较久, 请勿中断)..."
gunzip -c "$FILE" | docker exec -i -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$SVC" \
  mysql -uroot --default-character-set=utf8mb4 2>&1 | grep -v "Using a password" || true

CNT="$(docker exec -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$SVC" mysql -uroot -N \
  -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$DB';" 2>/dev/null || echo 0)"
log "恢复完成: $SVC/$DB 现有表数量 $CNT"
log "如需回退本次恢复, 可用恢复前安全备份: $(basename "${PRE:-无}")"

#!/usr/bin/env bash
#===============================================================================
#  一键卸载脚本 (由 packer.py 生成到离线包, 部署后位于部署目录)
#
#  行为: 停止并移除本目录 docker-compose 管理的全部容器与网络, 清理备份定时任务;
#        默认【保留】全部数据目录与物料, 可随时重新部署恢复。
#        加 --purge-data 时额外删除数据目录与命名数据卷 (不可恢复, 需二次确认)。
#
#  用法:
#    ./uninstall.sh                 卸载容器, 保留数据 (推荐, 可随时重装)
#    ./uninstall.sh --purge-data    卸载容器并删除数据 (危险, 需二次确认)
#    以上均可加 --yes 跳过确认
#
#  保留内容: 镜像 tar 物料、备份目录(backup/)、部署报告、docker-compose.yml/.env
#===============================================================================
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE="$DIR/docker-compose.yml"

log() { echo "[$(date '+%F %T')] $*"; }
warn() { echo "[$(date '+%F %T')] [WARN] $*"; }
die() { echo "[$(date '+%F %T')] [ERROR] $*" >&2; exit 1; }

YES=0
PURGE=0
for a in "$@"; do
  case "$a" in
    --yes|-y)       YES=1 ;;
    --purge-data)   PURGE=1 ;;
    *) die "未知参数: $a (支持 --purge-data / --yes)" ;;
  esac
done

[ -f "$COMPOSE" ] || die "未找到 docker-compose.yml: $COMPOSE (请在部署目录内执行)"
command -v docker >/dev/null 2>&1 || die "docker 不可用"

# ---- 卸载前收集本 compose 项目的容器挂载的宿主机数据目录 (bind mounts) ----
BIND_DIRS=()
add_bind() { # $1=目录(相对或绝对)
  local d="$1"
  [ -n "$d" ] || return 0
  case "$d" in
    ./*) d="$DIR/${d#./}" ;;
    ../*) d="$DIR/$d" ;;
  esac
  case " ${BIND_DIRS[*]-} " in *" $d "*) ;; *) BIND_DIRS+=("$d") ;; esac
}
for cid in $(docker compose -f "$COMPOSE" ps -aq 2>/dev/null || true); do
  while IFS= read -r src; do
    add_bind "$src"
  done < <(docker inspect -f '{{range .Mounts}}{{if eq .type "bind"}}{{.Source}}
{{end}}{{end}}' "$cid" 2>/dev/null || true)
done
# 兜底: 容器已被清理过时(如先普通卸载再 purge), 直接从 compose 文件解析相对 bind 挂载
while IFS= read -r src; do
  add_bind "$src"
done < <(grep -E '^[[:space:]]+-[[:space:]]+\.' "$COMPOSE" 2>/dev/null | sed 's/.*[[:space:]]-\+[[:space:]]*//' | cut -d: -f1 || true)

log "===== 卸载计划 ====="
log "部署目录 : $DIR"
log "容器/网络: docker compose down (--remove-orphans)"
log "定时任务 : 移除 crontab 中 # mw-backup 备份任务"
if [ "$PURGE" -eq 1 ]; then
  log "数据目录 : 删除 (${#BIND_DIRS[@]} 个 bind 目录 + 命名数据卷)"
  for d in "${BIND_DIRS[@]+"${BIND_DIRS[@]}"}"; do log "  - $d"; done
else
  log "数据目录 : 全部保留 (需要彻底清数据请加 --purge-data)"
fi
log "保留内容 : 镜像 tar 物料 / backup 备份 / 部署报告 / compose 与 .env"
log "===================="

if [ "$YES" -ne 1 ]; then
  printf '确认卸载? 输入 yes 继续: '
  read -r ans
  [ "$ans" = "yes" ] || die "已取消"
fi

# ---- 1. 停止并移除容器与网络 (与备份共用同一把锁, 避免与备份并发) ----
mkdir -p "${BACKUP_DIR:-$DIR/backup}" 2>/dev/null || true
exec 9>"${BACKUP_DIR:-$DIR/backup}/.lock" 2>/dev/null || true
command -v flock >/dev/null 2>&1 && { flock -n 9 2>/dev/null || warn "可能有备份任务在运行, 继续卸载"; }

log "[1/3] 停止并移除容器与网络..."
if [ "$PURGE" -eq 1 ]; then
  docker compose -f "$COMPOSE" down -v --remove-orphans || warn "docker compose down -v 返回非零, 请 docker ps -a 检查残留"
else
  docker compose -f "$COMPOSE" down --remove-orphans || warn "docker compose down 返回非零, 请 docker ps -a 检查残留"
fi

# ---- 2. 清理备份定时任务 ----
log "[2/3] 清理备份定时任务..."
if crontab -l 2>/dev/null | grep -q "# mw-backup"; then
  (crontab -l 2>/dev/null | grep -v "# mw-backup" || true) | crontab -
  log "已移除 # mw-backup 定时任务"
else
  log "无备份定时任务"
fi

# ---- 3. 可选: 删除数据目录 (仅限部署目录内的 bind 目录, 目录外一律不动) ----
if [ "$PURGE" -eq 1 ]; then
  if [ "$YES" -ne 1 ]; then
    printf '即将删除上面列出的全部数据目录与数据卷, 不可恢复! 输入 DELETE 确认: '
    read -r ans2
    [ "$ans2" = "DELETE" ] || { log "已保留数据目录 (仅卸载了容器)"; exit 0; }
  fi
  log "[3/3] 删除数据目录..."
  PURGE_FAIL=0
  for d in "${BIND_DIRS[@]+"${BIND_DIRS[@]}"}"; do
    case "$d" in
      "$DIR"/*)
        if rm -rf "$d" 2>/dev/null && [ ! -e "$d" ]; then
          log "  已删除: $d"
        else
          warn "  删除失败: $d (可能被进程占用, 请稍后手工删除)"
          PURGE_FAIL=1
        fi ;;
      *) warn "  跳过部署目录之外的挂载点: $d (如确认不再需要请手工处理)" ;;
    esac
  done
  if [ "$PURGE_FAIL" -eq 0 ]; then
    log "数据目录已清除"
  else
    warn "数据目录部分清除, 存在删除失败的目录, 请按上方提示手工处理"
  fi
else
  log "[3/3] 跳过 (数据目录已保留)"
fi

log "卸载完成。镜像 tar 物料与备份文件仍在原处, 重新解压/部署即可恢复服务。"

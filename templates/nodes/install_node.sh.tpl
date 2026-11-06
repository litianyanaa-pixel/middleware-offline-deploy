#!/usr/bin/env bash
# 由 packer.py 自动生成 — 节点 @@NAME@@ 安装脚本 (自包含, 可重复执行)
set -euo pipefail
cd "$(dirname "$0")"
NODE_NAME=@@NAME@@
HEALTH_WAIT=(@@HEALTH_WAIT@@)
POST_SQL=(@@POST_SQL@@)
BACKUP_CRON=@@BACKUP_CRON@@@@BACKUP_VARS@@

log(){ printf '[node-%s] %s\n' "$NODE_NAME" "$*"; }
warn(){ printf '[node-%s] 警告: %s\n' "$NODE_NAME" "$*" >&2; }
die(){ printf '[node-%s] 错误: %s\n' "$NODE_NAME" "$*" >&2; exit 1; }

@@JAVA_SH@@

command -v docker >/dev/null 2>&1 || die "本机未安装 Docker, 请先安装 Docker 再执行"
docker compose version >/dev/null 2>&1 || docker-compose version >/dev/null 2>&1 \
  || die "缺少 Docker Compose, 请先安装"

set -a; source ./.env; set +a

# 桥接配置目录: 包内 conf/<svc>/* -> 部署目录 <svc>/* (与主部署机 deploy.sh 同规则)
if [ -d ./conf ]; then
  for d in ./conf/*/; do
    [ -d "$d" ] || continue
    name="$(basename "$d")"
    mkdir -p "./$name"
    cp -a "$d/." "./$name/"
  done
fi

# 预建数据/日志目录并收紧属主 (compose 挂载点; 权限不匹配会导致容器写不进)
for d in @@DATA_DIRS@@; do
  [ -z "$d" ] && continue
  mkdir -p "./$d"
done
for d in @@CHOWN_DIRS@@; do
  [ -z "$d" ] && continue
  case "$d" in
    *:*) dir="${d%%:*}"; uid="${d##*:}" ;;
    *)   dir="$d"; uid="999" ;;
  esac
  [ -e "./$dir" ] && chown -R "$uid:$uid" "./$dir" 2>/dev/null \
    || log "  提示: chown $uid:$uid $dir 未生效(可忽略, 视节点用户而定)"
done

log "加载节点镜像..."
for tar in images/*.tar; do
  [ -f "$tar" ] || { log "无镜像文件"; break; }
  docker load -i "$tar" >/dev/null && log "  $(basename "$tar") 已加载"
done
# 镜像源前缀重打标(离线机 compose 按原生短名取镜像, 不重打标会触发联网拉取失败)
if [ -f images/retag-mirrors.sh ]; then
  bash images/retag-mirrors.sh images.txt
fi

log "启动容器..."
if docker compose version >/dev/null 2>&1; then
  docker compose up -d
else
  docker-compose up -d
fi

log "等待容器健康 (最长 10 分钟)..."
for cn in "${HEALTH_WAIT[@]}"; do
  for i in $(seq 1 120); do
    st="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cn" 2>/dev/null || echo missing)"
    case "$st" in healthy|running) break;; esac
    [ "$i" = 120 ] && die "$cn 健康检查超时 (当前状态: $st)"
    sleep 5
  done
  log "  $cn 就绪 ($st)"
done

if [ "${#POST_SQL[@]}" -gt 0 ] && [ -n "${POST_SQL[0]:-}" ]; then
  # compose 的 mysqladmin ping 健康检查在首启初始化的临时 server 阶段也会通过,
  # 必须再用真实鉴权探活等 root 可用, 否则下面的 SQL 撞上初始化中的库必 Access denied
  WAITED=" "
  for entry in "${POST_SQL[@]}"; do
    cn="${entry%|*}"
    case "$WAITED" in *" $cn "*) continue ;; esac
    log "等待 $cn 可用(账号鉴权, 首次初始化可达数分钟)..."
    ok=""
    for i in $(seq 1 100); do
      if docker exec -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$cn" mysql -uroot -N -e "SELECT 1;" >/dev/null 2>&1; then
        ok=1; break
      fi
      sleep 3
    done
    [ -n "$ok" ] || die "$cn 初始化超时(root 鉴权不可用), 请 docker logs $cn 排查后重跑本脚本"
    log "  $cn 可用"
    WAITED="$WAITED$cn "
  done
  log "执行初始化 SQL..."
  for entry in "${POST_SQL[@]}"; do
    cn="${entry%|*}"; sql="${entry#*|}"
    docker exec -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$cn" mysql -uroot -e "$sql" \
      || die "初始化 SQL 执行失败: ${sql:0:60}..."
    log "  $cn SQL 完成"
  done
fi

if [ -n "$BACKUP_CRON" ] && [ -f backup.sh ] && [ -f backup.conf ]; then
  log "配置定时备份: $BACKUP_CRON"
  chmod +x backup.sh && chmod 600 backup.conf
  INST_DIR="$(pwd)"
  # 行尾 # mw-backup 标记与主部署机 deploy.sh 同规: uninstall.sh 靠它清理节点备份 crontab
  CRON_LINE="$BACKUP_CRON /bin/bash $INST_DIR/backup.sh >> $INST_DIR/backup.log 2>&1 # mw-backup"
  # 备份属辅助功能: cron 未装(minimal 系统常见)或 crontab 写失败时 warn 降级并给出手动行,
  # 不让已就绪的节点判死(此前无 crontab 命令时 set -e 裸退 exit 1 且无任何提示)
  if command -v crontab >/dev/null 2>&1; then
    (crontab -l 2>/dev/null | grep -vF "$INST_DIR/backup.sh" ; echo "$CRON_LINE") | crontab - \
      || warn "crontab 写入失败, 请手动安装: $CRON_LINE"
  else
    warn "本机无 crontab 命令(未装 cron?), 定时备份未配置; 安装 cronie 后重跑本脚本或手动添加: $CRON_LINE"
  fi
  mkdir -p "$BACKUP_DIR"
fi

log "节点 $NODE_NAME 安装完成"
docker compose ps --format 'table {{.Name}}\t{{.Status}}' 2>/dev/null || docker ps

#!/usr/bin/env bash
#===============================================================================
#
#  中间件离线部署脚本(服务器端, 零交互)
#
#  本脚本由本地打包器(packer.py)生成到离线包内, 所有部署决策(中间件/端口/
#  密码/数据库/目录)已在打包时写入 manifest.sh, 服务器上不再问任何问题。
#
#  用法:  root 用户执行  ./deploy.sh
#  特性:  幂等可重跑; 已装 Docker 则跳过; 已有部署配置自动备份
#  兼容:  bash 4.2+(CentOS 7); x86_64 / aarch64
#
#===============================================================================
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR"

#------------------------------- 日志 -------------------------------
RED='\033[31m'; GREEN='\033[32m'; YELLOW='\033[33m'; CYAN='\033[36m'; NC='\033[0m'
log()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*"; }
die()  { err "$*"; exit 1; }
hr()   { echo -e "${CYAN}--------------------------------------------------------------${NC}"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

#------------------------------- 0. 前置检查 -------------------------------
[ "$(id -u)" -eq 0 ] || die "请使用 root 用户执行本脚本"
[ -f "$BASE_DIR/manifest.sh" ] || die "缺少 manifest.sh, 请勿拆散离线包"
# shellcheck disable=SC1091
source "$BASE_DIR/manifest.sh"

command -v tar >/dev/null 2>&1 || die "缺少 tar 命令"
command -v systemctl >/dev/null 2>&1 || die "缺少 systemctl, 仅支持 systemd 系统"

ARCH_RAW="$(uname -m)"
case "$ARCH_RAW" in
  x86_64)       ARCH="amd64" ;;
  aarch64|arm64) ARCH="arm64" ;;
  *) die "不支持的架构: $ARCH_RAW (仅支持 x86_64 / aarch64)" ;;
esac
[ "$ARCH" = "$PKG_ARCH" ] || die "本包为 ${PKG_ARCH} 架构, 与当前服务器(${ARCH_RAW})不符, 请使用对应架构的离线包"

PKG_SUBDIR="x86_64"; [ "$ARCH" = "arm64" ] && PKG_SUBDIR="aarch64"
MYSQL_CLIENT_IMG="${MYSQL_CLIENT_IMG:-}"

# 校验原始压缩包完整性(.sha256 与包一起拷贝到服务器时生效); 找不到包则跳过
verify_bundle_integrity() {
  command -v sha256sum >/dev/null 2>&1 || { warn "未检测到 sha256sum, 跳过离线包完整性校验"; return 0; }
  [ -n "${BUNDLE_NAME:-}" ] || return 0
  local archive="" cand
  for cand in "$BASE_DIR/$BUNDLE_NAME" "$BASE_DIR/../$BUNDLE_NAME" "$PWD/$BUNDLE_NAME" "$PWD/../$BUNDLE_NAME"; do
    [ -f "$cand" ] && { archive="$cand"; break; }
  done
  if [ -z "$archive" ]; then
    log "未找到原始压缩包 $BUNDLE_NAME, 跳过完整性校验; 如需校验请在包所在目录执行: sha256sum -c $BUNDLE_NAME.sha256"
    return 0
  fi
  if [ ! -f "$archive.sha256" ]; then
    warn "缺少 $BUNDLE_NAME.sha256, 跳过完整性校验 (建议将 .sha256 与压缩包一起拷贝)"
    return 0
  fi
  log "校验离线包完整性: $BUNDLE_NAME ..."
  if ( cd "$(dirname "$archive")" && sha256sum -c "$BUNDLE_NAME.sha256" >/dev/null 2>&1 ); then
    log "完整性校验通过"
  else
    die "离线包完整性校验失败: $BUNDLE_NAME 与 .sha256 不符, 传输过程中可能损坏, 请重新拷贝后再试"
  fi
}

log "服务器架构: ${ARCH_RAW} (${ARCH}) | 项目: ${PROJECT:-middleware}"
hr

#------------------------------- 工具函数 -------------------------------
# 架构键映射: 本包内部统一用 amd64/arm64(与 docker inspect 一致)
arch_name() { echo "$ARCH"; }

# 宿主机端口是否处于 LISTEN。
# 不依赖 ss/netstat 等网络工具(精简/离线系统常未安装): 优先解析内核
# /proc/net/tcp(6)——任何 Linux 恒有; 只有 /proc 不可读时才退回网络工具。
port_listening() {
  local p="$1" hex f
  hex=$(printf '%04X' "$p")
  for f in /proc/net/tcp /proc/net/tcp6; do
    [ -r "$f" ] || continue
    # $2=本地地址(十六进制), $4=套接字状态(0A=LISTEN)
    if awk -v hx="$hex" '$4 == "0A" && $2 ~ (":" hx "$") { found=1 } END { exit !found }' "$f" 2>/dev/null; then
      return 0
    fi
  done
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":${p}\$" && return 0
  fi
  if command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | awk 'NR>2{print $4}' | grep -q ":${p}\$" && return 0
  fi
  return 1
}

# 检查宿主机端口占用; 已被本套部署的容器占用的端口视为空闲(支持重跑)
check_port_free() {
  local p="$1" line cname
  while read -r line; do
    [ -z "$line" ] && continue
    cname="${line%%|*}"
    if printf '%s\n' "${CONTAINER_NAMES[@]+"${CONTAINER_NAMES[@]}"}" | grep -qx "$cname"; then
      return 0   # 自己人的端口
    fi
    err "端口 $p 已被容器 ${cname} 占用"
    die "请修改 manifest.sh 中的端口或在 docker-compose.yml 调整后重试"
  done <<EOF
$(docker ps --format '{{.Names}}|{{.Ports}}' 2>/dev/null | grep -E "0\.0\.0\.0:${p}->|:${p}->" || true)
EOF
  if port_listening "$p"; then
    die "端口 $p 已被宿主机其他进程占用, 请调整端口后重试"
  fi
}

# 磁盘空间预检: 镜像包装不进必然失败; 展开层+运行数据按镜像体积 2 倍+1GB 预估。
# 只用 df/du(coreutils 恒有), 不依赖网络工具。
check_disk() {
  local img_mb need_mb free_mb dev path seen=""
  img_mb=$(du -sm "$BASE_DIR/images" 2>/dev/null | awk '{print $1}')
  [ -n "$img_mb" ] || img_mb=0
  need_mb=$(( img_mb * 2 + 1024 ))
  for path in "$DOCKER_DATA_ROOT" "$DEPLOY_DIR"; do
    [ -n "$path" ] || continue
    while [ ! -d "$path" ]; do path="$(dirname "$path")"; done   # 找最近的存在祖先目录(同分区)
    [ -w "$path" ] || die "目录不可写: $path (请检查权限或更换部署目录)"
    dev="$(df -Pm "$path" | awk 'NR==2{print $1}')"
    case ",$seen," in *",$dev,"*) continue ;; esac
    seen="$seen,$dev"
    free_mb="$(df -Pm "$path" | awk 'NR==2{print $4}')"
    if [ "${free_mb:-0}" -lt $(( img_mb + 512 )) ]; then
      die "磁盘空间不足: $path 所在分区($dev)仅剩 ${free_mb}MB, 仅镜像包就需要 ${img_mb}MB"
    fi
    if [ "${free_mb:-0}" -lt "$need_mb" ]; then
      warn "$path 所在分区可用 ${free_mb}MB, 建议预留约 ${need_mb}MB(镜像展开+运行数据), 可能影响部署或后续运行"
    else
      log "磁盘预检: $path 分区可用 ${free_mb}MB (镜像 ${img_mb}MB, 建议预留 ≥ ${need_mb}MB)"
    fi
  done
}

# 备份已有部署配置(compose/.env/conf/sql), 数据目录不动
backup_existing() {
  local ts dir f
  if [ -f "$DEPLOY_DIR/docker-compose.yml" ] || [ -f "$DEPLOY_DIR/.env" ]; then
    ts="$(date +%Y%m%d%H%M%S)"
    dir="$BASE_DIR/backup/$ts"
    mkdir -p "$dir"
    for f in docker-compose.yml .env; do
      if [ -f "$DEPLOY_DIR/$f" ]; then
        cp -a "$DEPLOY_DIR/$f" "$dir/"
      fi
    done
    if [ -d "$DEPLOY_DIR/conf" ]; then
      cp -a "$DEPLOY_DIR/conf" "$dir/"
    fi
    log "检测到已有部署配置, 已备份到: $dir (数据目录未改动)"
    OLD_ENV_FILE="$dir/.env"
  fi
}

# 若本地 MySQL 已有数据, root 密码不允许和上次不同(否则改了也无效, 应用会连不上)
guard_mysql_password() {
  [ -n "$LOCAL_MYSQL_SVC" ] || return 0
  [ -n "${OLD_ENV_FILE:-}" ] || return 0
  [ -f "$OLD_ENV_FILE" ] || return 0
  local data_dir="$DEPLOY_DIR/$LOCAL_MYSQL_SVC/data"
  [ -d "$data_dir" ] && [ -n "$(ls -A "$data_dir" 2>/dev/null || true)" ] || return 0
  local old_pw new_pw
  old_pw="$(grep -E '^MYSQL_ROOT_PASSWORD=' "$OLD_ENV_FILE" | head -1 | cut -d= -f2-)"
  new_pw="$(grep -E '^MYSQL_ROOT_PASSWORD=' "$BASE_DIR/.env" | head -1 | cut -d= -f2-)"
  if [ -n "$old_pw" ] && [ "$old_pw" != "$new_pw" ]; then
    die "检测到 $LOCAL_MYSQL_SVC 已有数据且 root 密码与上次不同!
        MySQL 密码仅在首次初始化时生效, 请沿用旧密码(备份: $OLD_ENV_FILE),
        或清空 $data_dir 后重新部署。"
  fi
}

#------------------------------- 1. 安装 Docker -------------------------------
DOCKERD_OK=0
if command -v dockerd >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  DOCKERD_OK=1
fi

write_daemon_json() { # $1=目标路径
  local dest="$1" m line
  {
    echo '{'
    echo "  \"data-root\": \"${DOCKER_DATA_ROOT}\","
    echo '  "storage-driver": "overlay2",'
    echo '  "exec-opts": ["native.cgroupdriver=systemd"],'
    echo '  "log-driver": "json-file",'
    echo '  "log-opts": { "max-size": "100m", "max-file": "3" },'
    echo '  "live-restore": true,'
    echo '  "max-concurrent-downloads": 10,'
    printf '  "registry-mirrors": ['
    m=0
    for line in "${REGISTRY_MIRRORS[@]+"${REGISTRY_MIRRORS[@]}"}"; do
      [ $m -gt 0 ] && printf ', '
      printf '"%s"' "$line"
      m=$((m+1))
    done
    echo '],'
    echo '  "insecure-registries": []'
    echo '}'
  } > "$dest"
}

install_docker() {
  hr; log "【1/9】检查 / 安装 Docker"
  mkdir -p /etc/docker
  if [ "$DOCKERD_OK" -eq 1 ]; then
    log "Docker 已安装且运行正常, 跳过安装 ($(docker -v 2>/dev/null || true))"
    systemctl enable docker containerd >/dev/null 2>&1 || true
    log "已确保 docker/containerd 开机自启"
    if [ ! -f /etc/docker/daemon.json ]; then
      write_daemon_json /etc/docker/daemon.json
      log "已为现有 Docker 补写 daemon.json(日志轮转/数据目录), 重启 Docker 生效"
      mkdir -p "$DOCKER_DATA_ROOT"
      systemctl restart docker
      sleep 2
      docker info >/dev/null 2>&1 || die "Docker 重启失败, 请执行 journalctl -u docker 查看"
    else
      write_daemon_json /etc/docker/daemon.json.packer
      warn "已存在 /etc/docker/daemon.json, 保持不变; 本包建议配置已写到 daemon.json.packer 供人工合并"
    fi
    return 0
  fi

  local tgz="$BASE_DIR/packages/$PKG_SUBDIR/docker-*.tgz"
  tgz=$(ls $tgz 2>/dev/null | head -1 || true)
  [ -n "$tgz" ] && [ -f "$tgz" ] || die "缺少离线包: packages/$PKG_SUBDIR/docker-*.tgz"
  log "解压安装 $(basename "$tgz") ..."
  tar -xzf "$tgz" -C "$TMP"
  install -m 755 "$TMP"/docker/* /usr/local/bin/

  cat > /etc/systemd/system/containerd.service <<'EOF'
[Unit]
Description=containerd container runtime
After=network.target

[Service]
ExecStartPre=/sbin/modprobe overlay
ExecStart=/usr/local/bin/containerd
Delegate=yes
KillMode=process
Restart=always
RestartSec=5
LimitNOFILE=1048576
LimitNPROC=infinity
LimitCORE=infinity

[Install]
WantedBy=multi-user.target
EOF

  cat > /etc/systemd/system/docker.service <<'EOF'
[Unit]
Description=Docker Application Container Engine
After=network-online.target containerd.service firewalld.service
Wants=network-online.target containerd.service

[Service]
Type=notify
ExecStart=/usr/local/bin/dockerd
ExecReload=/bin/kill -s HUP $MAINPID
TimeoutSec=0
RestartSec=2
Restart=always
StartLimitBurst=3
StartLimitInterval=60s
LimitNOFILE=infinity
LimitNPROC=infinity
LimitCORE=infinity
TasksMax=infinity
Delegate=yes
KillMode=process

[Install]
WantedBy=multi-user.target
EOF

  command -v iptables >/dev/null 2>&1 || warn "未检测到 iptables, Docker 端口映射可能失败, 请确认系统已安装"
  write_daemon_json /etc/docker/daemon.json
  mkdir -p "$DOCKER_DATA_ROOT"
  log "daemon.json 已写入 (data-root=${DOCKER_DATA_ROOT})"
  systemctl daemon-reload
  systemctl enable --now containerd >/dev/null 2>&1 || true
  systemctl enable --now docker     >/dev/null 2>&1 || true
  sleep 2
  docker info >/dev/null 2>&1 || die "Docker 启动失败, 请执行 journalctl -u docker 查看日志"
  log "Docker $(docker -v | awk '{print $3}') 安装完成, 已设开机自启"
}

#------------------------------- 2. 安装 Docker Compose -------------------------------
install_compose() {
  hr; log "【2/9】检查 / 安装 Docker Compose"
  if docker compose version >/dev/null 2>&1; then
    log "Docker Compose 已安装, 跳过"
    return 0
  fi
  local bin="$BASE_DIR/packages/$PKG_SUBDIR/docker-compose-linux-*"
  bin=$(ls $bin 2>/dev/null | head -1 || true)
  [ -n "$bin" ] && [ -f "$bin" ] || die "缺少离线包: packages/$PKG_SUBDIR/docker-compose-linux-*
        (本包架构: $PKG_SUBDIR)"
  mkdir -p /usr/local/libexec/docker/cli-plugins
  install -m 755 "$bin" /usr/local/libexec/docker/cli-plugins/docker-compose
  [ ! -e /usr/local/bin/docker-compose ] && ln -sf /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose || true
  docker compose version >/dev/null 2>&1 || die "Docker Compose 安装失败"
  log "Docker Compose 安装完成: $(docker compose version | awk '{print $NF}')"
}

#------------------------------- 3. 加载镜像 + tag 归一化 -------------------------------
# images.txt 每行: tar文件名|统一短名(如 nginx:1.31.5)
# 归一化规则: tar 内镜像无论带什么仓库前缀(docker.m.daocloud.io/xxx 等)、
#            tag 是否带 -amd64/-arm64 后缀, 统一 docker tag 成短名;
#            并校验镜像 architecture 与服务器架构一致, 不一致直接报错拦截。
normalize_image() {
  local short="$1" name tag need repo rt found cand c_repo c_tag c_arch
  case "$short" in
    *:*) ;;
    *) die "images.txt 短名格式错误: '$short' (应为 仓库:标签, 检查注释行是否被误解析)" ;;
  esac
  name="${short%%:*}"; tag="${short#*:}"
  need="$ARCH"

  if docker image inspect "$short" >/dev/null 2>&1; then
    c_arch=$(docker image inspect -f '{{.Architecture}}' "$short")
    if [ "$c_arch" != "$need" ]; then
      die "镜像 $short 已存在但架构为 $c_arch, 与服务器($need)不符; 请执行 docker rmi $short 后重试"
    fi
    return 0
  fi

  found=""
  while read -r rt; do
    [ -z "$rt" ] && continue
    case "$rt" in
      *:*) c_repo="${rt%:*}"; c_tag="${rt##*:}" ;;
      *)   continue ;;
    esac
    [ "$c_repo" = "$name" ] || case "$c_repo" in */"$name") ;; *) continue ;; esac
    case "$c_tag" in
      "$tag"|"$tag-amd64"|"$tag-arm64") ;;
      *) continue ;;
    esac
    c_arch=$(docker image inspect -f '{{.Architecture}}' "$rt" 2>/dev/null || echo unknown)
    if [ "$c_arch" != "$need" ]; then
      warn "跳过 $rt: 架构 $c_arch 与服务器($need)不符"
      continue
    fi
    found="$rt"
    break
  done <<EOF
$(docker images --format '{{.Repository}}:{{.Tag}}')
EOF

  if [ -n "$found" ]; then
    if [ "$found" != "$short" ]; then
      docker tag "$found" "$short"
      log "镜像归一化: $found -> $short"
    fi
  else
    die "未找到可用镜像: $short (名称/前缀/架构均不匹配), 请检查 images/ 目录"
  fi
}

load_images() {
  hr; log "【3/9】加载离线镜像"
  [ -f "$BASE_DIR/images.txt" ] || die "缺少 images.txt"
  local f line file short
  while IFS='|' read -r file short; do
    case "$file" in ''|\#*) continue ;; esac
    [ -z "$short" ] && continue
    f="$BASE_DIR/images/$file"
    [ -f "$f" ] || die "缺少镜像包: images/$file"
    log "加载 $file ..."
    docker load -i "$f" >/dev/null
  done < "$BASE_DIR/images.txt"
  while IFS='|' read -r file short; do
    case "$file" in ''|\#*) continue ;; esac
    [ -z "$short" ] && continue
    normalize_image "$short"
  done < "$BASE_DIR/images.txt"
  log "镜像全部就绪(已统一为短名, 架构校验通过)"
}

#------------------------------- 4. 磁盘与端口预检 -------------------------------
check_ports() {
  hr; log "【4/9】磁盘与端口预检"
  check_disk
  local p
  for p in "${PORTS_TO_CHECK[@]+"${PORTS_TO_CHECK[@]}"}"; do
    [ -z "$p" ] && continue
    check_port_free "$p"
    log "端口 $p 可用"
  done
}

#------------------------------- 5. 准备部署目录 -------------------------------
prepare_deploy_dir() {
  hr; log "【5/9】准备部署目录: $DEPLOY_DIR"
  OLD_ENV_FILE=""
  backup_existing
  guard_mysql_password

  mkdir -p "$DEPLOY_DIR"
  cp -f "$BASE_DIR/docker-compose.yml" "$DEPLOY_DIR/docker-compose.yml"
  cp -f "$BASE_DIR/.env"               "$DEPLOY_DIR/.env"
  chmod 600 "$DEPLOY_DIR/.env"

  local d
  for d in "${DATA_DIRS[@]+"${DATA_DIRS[@]}"}"; do
    [ -z "$d" ] && continue
    mkdir -p "$DEPLOY_DIR/$d"
  done

  # mysql/redis 容器内以 uid 999 运行, 日志目录须可写, 否则起不来
  for d in "${CHOWN_DIRS[@]+"${CHOWN_DIRS[@]}"}"; do
    [ -z "$d" ] && continue
    if [ -d "$DEPLOY_DIR/$d" ]; then
      chown -R 999:999 "$DEPLOY_DIR/$d" 2>/dev/null || warn "chown 999:999 $d 失败, 若服务无法写日志请手工处理"
    fi
  done

  # 服务配置文件(nginx/redis 等): 打包器生成的文件(带"由打包器生成"标记)随重新部署覆盖,
  # 其余只补齐缺失, 不覆盖人工修改; 子目录(如 nginx/ssl/wizard 证书)为打包器生成, 整体覆盖
  if [ -d "$BASE_DIR/conf" ]; then
    local svc_dir name src dest
    for svc_dir in "$BASE_DIR"/conf/*/; do
      [ -d "$svc_dir" ] || continue
      name="$(basename "$svc_dir")"
      mkdir -p "$DEPLOY_DIR/$name"
      for src in "$svc_dir"*; do
        [ -e "$src" ] || continue
        dest="$DEPLOY_DIR/$name/$(basename "$src")"
        if [ -d "$src" ]; then
          mkdir -p "$dest"
          cp -a "$src/." "$dest/"
        elif [ -f "$dest" ] && head -c 200 "$dest" 2>/dev/null | grep -q "由打包器生成"; then
          cp -f "$src" "$dest"
        else
          cp -an "$src" "$dest" 2>/dev/null || true
        fi
      done
    done
  fi

  # 备份恢复脚本(部署了本地 MySQL 才有恢复对象)
  if [ -n "$LOCAL_MYSQL_SVC" ] && [ -f "$BASE_DIR/restore.sh" ]; then
    cp -f "$BASE_DIR/restore.sh" "$DEPLOY_DIR/restore.sh"
    chmod 700 "$DEPLOY_DIR/restore.sh"
    log "已安装备份恢复脚本: $DEPLOY_DIR/restore.sh (./restore.sh list 查看用法)"
  fi

  # 本地 MySQL 首启自动导表的 init 目录
  if [ -n "$LOCAL_MYSQL_SVC" ] && [ -d "$BASE_DIR/sql" ]; then
    mkdir -p "$DEPLOY_DIR/$LOCAL_MYSQL_SVC/init"
    cp -f "$BASE_DIR"/sql/*.sql "$DEPLOY_DIR/$LOCAL_MYSQL_SVC/init/" 2>/dev/null || true
  fi

  # SELinux 环境兜底
  if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce 2>/dev/null || true)" = "Enforcing" ]; then
    chcon -R system_u:object_r:container_file_t:s0 "$DEPLOY_DIR" 2>/dev/null \
      || warn "SELinux 标签设置失败, 如遇挂载权限问题请手工执行: chcon -R system_u:object_r:container_file_t:s0 $DEPLOY_DIR"
    log "SELinux 环境已自动设置容器文件标签"
  fi
  log "配置与持久化目录就绪"
}

# 安装/卸载数据库备份定时任务(crontab), 幂等可重跑
install_backup() {
  if [ "${BACKUP_ENABLED:-0}" = "1" ]; then
    hr; log "【6/9】配置数据库定时备份"
    cp -f "$BASE_DIR/backup.sh"   "$DEPLOY_DIR/backup.sh"
    cp -f "$BASE_DIR/backup.conf" "$DEPLOY_DIR/backup.conf"
    chmod 700 "$DEPLOY_DIR/backup.sh"
    chmod 600 "$DEPLOY_DIR/backup.conf"
    mkdir -p "$BACKUP_DIR"
    local cron_line="$BACKUP_CRON $DEPLOY_DIR/backup.sh >> $DEPLOY_DIR/backup.log 2>&1 # mw-backup"
    (crontab -l 2>/dev/null | grep -v "# mw-backup" || true; echo "$cron_line") | crontab -
    log "定时备份已安装: crontab '$BACKUP_CRON' (保留最近份, 见 manifest.sh)"
    log "备份目录: $BACKUP_DIR, 手工备份: $DEPLOY_DIR/backup.sh"
  else
    # 未启用则清理可能存在的旧任务
    if crontab -l 2>/dev/null | grep -q "# mw-backup"; then
      (crontab -l 2>/dev/null | grep -v "# mw-backup" || true) | crontab -
      log "已移除旧的备份定时任务"
    fi
  fi
}

#------------------------------- 6. SQL 导入 -------------------------------
# 表已存在则跳过 -> 幂等, 支持反复重跑
# 外部库导入在 compose up 之前执行(避免 Nacos/XXL-Job 首启因缺表报错);
# 本地库导入在 up 且 MySQL 健康之后执行(首启也会由 init 目录自动导入, 此处兜底)。

# 本地库: docker exec 进 mysql 容器查询/导入
local_table_count() { # <svc> <schema>
  docker exec -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$1" mysql -uroot -N \
    -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$2';" 2>/dev/null || echo 0
}
local_import() { # <svc> <sqlfile>
  docker exec -i -e MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$1" \
    mysql -uroot --default-character-set=utf8mb4 -f 2>/dev/null < "$2"
}

# 外部库: 用 mysql 客户端镜像 + host 网络查询/导入(127.0.0.1 也正确指向宿主机)
remote_query() { # <sql> ; 需先设置 R_HOST R_PORT R_USER R_PASS ; 失败返回非0
  docker run --rm -i --network host -e MYSQL_PWD="$R_PASS" "$MYSQL_CLIENT_IMG" \
    mysql -h"$R_HOST" -P"$R_PORT" -u"$R_USER" --default-character-set=utf8mb4 -N -e "$1" 2>/dev/null
}
remote_import() { # <sqlfile>
  docker run --rm -i --network host -e MYSQL_PWD="$R_PASS" "$MYSQL_CLIENT_IMG" \
    mysql -h"$R_HOST" -P"$R_PORT" -u"$R_USER" --default-character-set=utf8mb4 -f < "$1"
}

wait_mysql_healthy() { # 等本地 mysql 健康检查通过
  local svc="$1" i
  log "等待 $svc 就绪(首次初始化约 1 分钟)..."
  for i in $(seq 1 100); do
    if [ "$(docker inspect -f '{{.State.Health.Status}}' "$svc" 2>/dev/null || echo na)" = "healthy" ]; then
      log "$svc 已就绪"
      return 0
    fi
    sleep 3
  done
  die "$svc 健康检查超时, 请执行 docker logs $svc 排查"
}

import_sql_external() { # 外部库: 在 compose up 之前导表
  hr; log "【7/9】初始化外部数据库(已初始化过会自动跳过)"
  local sql schema cnt label

  if [ "${NACOS_IMPORT:-0}" = "1" ] && [ "$NACOS_DB_MODE" = "external" ]; then
    [ -n "$MYSQL_CLIENT_IMG" ] || die "外部数据库导入缺少 mysql 客户端镜像, 请重新打包"
    sql="$BASE_DIR/$NACOS_SQL"; schema="$NACOS_SCHEMA"
    R_HOST="$NACOS_DB_HOST_IMPORT"; R_PORT="$NACOS_DB_PORT"; R_USER="$NACOS_DB_USER"; R_PASS="$NACOS_DB_PASSWORD"
    log "测试外部数据库连接: $R_HOST:$R_PORT (用户 $R_USER)"
    remote_query "SELECT 1;" >/dev/null || die "无法连接外部数据库 $R_HOST:$R_PORT, 请检查地址/账号/防火墙"
    cnt=$(remote_query "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$schema';") || cnt=0
    if [ "${cnt:-0}" -gt 0 ]; then
      log "Nacos 库($schema)已有表, 跳过导入"
    else
      remote_import "$sql" && log "Nacos SQL 已导入外部库 $R_HOST:$R_PORT/$schema" \
        || warn "Nacos SQL 导入报错, 请人工核对后重跑"
    fi
  fi

  if [ "${XXL_IMPORT:-0}" = "1" ] && [ "$XXL_DB_MODE" = "external" ]; then
    [ -n "$MYSQL_CLIENT_IMG" ] || die "外部数据库导入缺少 mysql 客户端镜像, 请重新打包"
    sql="$BASE_DIR/$XXL_SQL"; schema="$XXL_SCHEMA"
    R_HOST="$XXL_DB_HOST_IMPORT"; R_PORT="$XXL_DB_PORT"; R_USER="$XXL_DB_USER"; R_PASS="$XXL_DB_PASSWORD"
    log "测试外部数据库连接: $R_HOST:$R_PORT (用户 $R_USER)"
    remote_query "SELECT 1;" >/dev/null || die "无法连接外部数据库 $R_HOST:$R_PORT, 请检查地址/账号/防火墙"
    cnt=$(remote_query "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$schema';") || cnt=0
    if [ "${cnt:-0}" -gt 0 ]; then
      log "XXL-Job 库($schema)已有表, 跳过导入"
    else
      remote_import "$sql" && log "XXL-Job SQL 已导入外部库 $R_HOST:$R_PORT/$schema" \
        || warn "XXL-Job SQL 导入报错, 请人工核对后重跑"
    fi
  fi
}

import_sql_local() { # 本地库: 在 compose up 且 MySQL 健康之后兜底导表
  local svc sql schema cnt
  log "初始化本地数据库(幂等, 已导入过自动跳过)"

  if [ "${NACOS_IMPORT:-0}" = "1" ] && [ "$NACOS_DB_MODE" = "local" ]; then
    svc="$LOCAL_MYSQL_SVC"; sql="$BASE_DIR/$NACOS_SQL"; schema="$NACOS_SCHEMA"
    wait_mysql_healthy "$svc"
    cnt=$(local_table_count "$svc" "$schema")
    if [ "${cnt:-0}" -gt 0 ]; then
      log "Nacos 库($schema)已有表, 跳过导入"
    else
      local_import "$svc" "$sql" && log "Nacos SQL 已导入本地库 $svc/$schema" \
        || warn "Nacos SQL 导入报错, 请人工核对(若为表已存在告警可忽略)"
    fi
  fi

  if [ "${XXL_IMPORT:-0}" = "1" ] && [ "$XXL_DB_MODE" = "local" ]; then
    svc="$LOCAL_MYSQL_SVC"; sql="$BASE_DIR/$XXL_SQL"; schema="$XXL_SCHEMA"
    wait_mysql_healthy "$svc"
    cnt=$(local_table_count "$svc" "$schema")
    if [ "${cnt:-0}" -gt 0 ]; then
      log "XXL-Job 库($schema)已有表, 跳过导入"
    else
      local_import "$svc" "$sql" && log "XXL-Job SQL 已导入本地库 $svc/$schema" \
        || warn "XXL-Job SQL 导入报错, 请人工核对"
    fi
  fi
}

#------------------------------- 7. 启动 -------------------------------
startup() {
  hr; log "【8/9】启动服务 (docker compose up -d)"
  (cd "$DEPLOY_DIR" && docker compose up -d)
  sleep 5
  (cd "$DEPLOY_DIR" && docker compose ps)
}

#------------------------------- 9. 健康实测 -------------------------------
# manifest.sh 中的探测清单(打包时生成):
#   HEALTH_WAIT=(容器名...)   等容器 compose 健康检查转 healthy
#   HEALTH_HTTP=("名|url")    从宿主机 curl 实测 HTTP 可用性(2xx/3xx 通过)
wait_healthy() { # <容器名> <最大轮数(每轮3s)>
  local name="$1" rounds="${2:-60}" i st
  for i in $(seq 1 "$rounds"); do
    st="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name" 2>/dev/null || echo gone)"
    case "$st" in
      healthy) return 0 ;;
      gone)    return 2 ;;   # 容器不存在/已退出
      none)    return 0 ;;   # 未定义健康检查, 交给 HTTP 探测
    esac
    sleep 3
  done
  return 1
}

# HTTP 健康实测的客户端三级降级: curl -> wget -> bash 内建 /dev/tcp。
# 精简/离线系统可能三者皆缺 curl/wget, /dev/tcp 是 bash 自带能力, 无需任何外部工具。
HTTP_FETCH="curl"
detect_http_client() {
  if   command -v curl >/dev/null 2>&1; then HTTP_FETCH="curl"
  elif command -v wget >/dev/null 2>&1; then HTTP_FETCH="wget"
  else HTTP_FETCH="tcp"
  fi
}

http_code_once() { # <url> -> 输出单个 HTTP 状态码(失败输出 000; tcp 降级时 https 输出 TCP)
  local url="$1" code="" line
  case "$HTTP_FETCH" in
    curl)
      code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$url" 2>/dev/null || true)"
      echo "${code:-000}"; return ;;
    wget)
      code="$(wget -q -S -T 5 -O /dev/null "$url" 2>&1 | awk '/^[[:space:]]*HTTP\//{print $2; exit}' || true)"
      echo "${code:-000}"; return ;;
  esac
  # /dev/tcp 降级: 发裸 HTTP/1.0 请求读状态行; https 无法在 bash 内做 TLS, 只验证 TCP 端口可达
  local rest hostport path host port
  rest="${url#*://}"
  hostport="${rest%%/*}"
  case "$rest" in */*) path="/${rest#*/}" ;; *) path="/" ;; esac
  host="${hostport%%:*}"
  port="${hostport##*:}"
  [ "$port" = "$host" ] && port=80
  line=""
  if exec 3<>"/dev/tcp/$host/$port" 2>/dev/null; then
    if [ "${url%%://*}" = "https" ]; then
      exec 3>&- 3<&- || true
      echo "TCP"; return
    fi
    printf 'GET %s HTTP/1.0\r\nHost: %s\r\nUser-Agent: deploy-health\r\n\r\n' "$path" "$hostport" >&3
    read -t 5 -r line <&3 || line=""
    exec 3>&- 3<&- || true
  fi
  code="$(printf '%s' "$line" | awk '{print $2}')"
  echo "${code:-000}"
}

http_probe_wait() { # <url> <最大轮数(每轮3s)> -> 输出最后一个 HTTP 状态码
  local url="$1" rounds="${2:-20}" i code="000"
  for i in $(seq 1 "$rounds"); do
    code="$(http_code_once "$url")"
    case "$code" in 2??|3??|TCP) echo "$code"; return 0 ;; esac
    sleep 3
  done
  echo "$code"; return 1
}

health_check() {
  hr; log "【9/9】服务健康实测"
  HEALTH_LINES=()
  HEALTH_BAD=0
  detect_http_client
  case "$HTTP_FETCH" in
    curl) : ;;
    wget) log "未检测到 curl, HTTP 实测改用 wget" ;;
    tcp)  warn "未检测到 curl/wget, HTTP 实测降级为 bash /dev/tcp 裸请求(https 仅验证端口连通性)" ;;
  esac

  local name r url code rc
  for name in "${HEALTH_WAIT[@]+"${HEALTH_WAIT[@]}"}"; do
    [ -z "$name" ] && continue
    printf "  %-12s 容器健康检查 ...\r" "$name"
    rc=0
    wait_healthy "$name" 60 || rc=$?
    case "$rc" in
      0) HEALTH_LINES+=("OK|$name|容器健康检查通过") ;;
      2) HEALTH_LINES+=("BAD|$name|容器未运行(排查: docker ps -a | grep $name)"); HEALTH_BAD=1 ;;
      *) HEALTH_LINES+=("BAD|$name|健康检查超时(排查: docker logs $name)"); HEALTH_BAD=1 ;;
    esac
  done
  for r in "${HEALTH_HTTP[@]+"${HEALTH_HTTP[@]}"}"; do
    [ -z "$r" ] && continue
    name="${r%%|*}"; url="${r#*|}"
    printf "  %-12s HTTP 实测 %-38s\r" "$name" "$url"
    code="$(http_probe_wait "$url" 20 || true)"
    case "$code" in
      2??|3??) HEALTH_LINES+=("OK|$name|HTTP $code  $url") ;;
      TCP)     HEALTH_LINES+=("OK|$name|TCP 端口可达(无 curl/wget, 未验证 TLS)  $url") ;;
      *)       HEALTH_LINES+=("BAD|$name|HTTP $code  $url (排查: docker logs $name)"); HEALTH_BAD=1 ;;
    esac
  done
  echo ""
  if [ "$HEALTH_BAD" -eq 0 ]; then
    log "健康实测全部通过"
  else
    warn "部分服务健康实测未通过, 明细见下方部署摘要 (服务可能仍在启动中, 可稍后 docker compose ps 复查)"
  fi
}

detect_ip() {
  local ip=""
  ip=$(hostname -I 2>/dev/null | awk '{print $1}') || true
  if [ -z "$ip" ]; then
    ip=$(ip -4 addr show scope global 2>/dev/null | awk '/inet /{split($2,a,"/");print a[2];exit}') || true
  fi
  [ -z "$ip" ] && ip=$(hostname 2>/dev/null) || true
  echo "${ip:-127.0.0.1}"
}

# 日常运维命令清单: 终端直接打印 + 原文写进部署报告(两处保证一致)
print_commands() {
  local s
  echo "  cd $DEPLOY_DIR"
  echo "  docker compose ps                      # 容器状态一览"
  echo "  docker compose logs -f                 # 跟看全部日志"
  for s in "${CONTAINER_NAMES[@]+"${CONTAINER_NAMES[@]}"}"; do
    [ -z "$s" ] && continue
    printf '  docker compose logs -f %-14s # %s 日志\n' "$s" "$s"
  done
  echo "  docker compose restart                 # 重启全部(单个: docker compose restart <服务名>)"
  echo "  docker compose down                    # 停止(数据目录保留, 不删除)"
  if [ -f "$DEPLOY_DIR/backup.sh" ]; then
    echo "  ./backup.sh                            # 手工全量备份(按库分文件)"
  fi
  if [ -f "$DEPLOY_DIR/restore.sh" ]; then
    echo "  ./restore.sh list                      # 列出数据库备份"
  fi
}

rpt() { printf '%s\n' "$*" >> "$REPORT_FILE"; }

# 生成部署报告(纯文本, 每次执行 deploy.sh 自动覆盖重写)
write_report() { # $1=本机IP
  local ip="$1" hl st txt s
  REPORT_FILE="$DEPLOY_DIR/部署报告.txt"
  local now os_pretty docker_ver compose_ver
  now="$(date '+%Y-%m-%d %H:%M:%S')"
  os_pretty="$( ( . /etc/os-release 2>/dev/null && printf '%s' "$PRETTY_NAME" ) || head -1 /etc/redhat-release 2>/dev/null || true)"
  [ -n "$os_pretty" ] || os_pretty="$(uname -s)"
  docker_ver="$(docker -v 2>/dev/null || echo unknown)"
  compose_ver="$(docker compose version --short 2>/dev/null || docker compose version 2>/dev/null | head -1 || echo unknown)"
  : > "$REPORT_FILE"

  rpt "================================================================"
  rpt "  中间件离线部署报告    项目: ${PROJECT:-middleware}"
  rpt "================================================================"
  rpt ""
  rpt "部署时间  : $now"
  rpt "服务器    : $(hostname 2>/dev/null || uname -n)  ($(uname -m))"
  rpt "系统      : $os_pretty"
  rpt "内核      : $(uname -r)"
  rpt "本机IP    : $ip"
  rpt "部署包    : ${BUNDLE_NAME:-unknown(已解包)}"
  rpt "Docker    : $docker_ver"
  rpt "Compose   : $compose_ver"
  rpt "部署目录  : $DEPLOY_DIR"
  rpt "Docker数据: $DOCKER_DATA_ROOT"
  rpt ""
  rpt "----------------------------------------------------------------"
  rpt "一、服务清单与访问入口"
  rpt "----------------------------------------------------------------"
  for s in "${SUMMARY_LINES[@]+"${SUMMARY_LINES[@]}"}"; do
    [ -z "$s" ] && continue
    rpt "  ${s//__IP__/$ip}"
  done
  rpt ""
  rpt "----------------------------------------------------------------"
  rpt "二、健康实测结果"
  rpt "----------------------------------------------------------------"
  if [ -n "${HEALTH_LINES+x}" ]; then
    for hl in "${HEALTH_LINES[@]+"${HEALTH_LINES[@]}"}"; do
      st="${hl%%|*}"; txt="${hl#*|}"
      if [ "$st" = "OK" ]; then rpt "  ✓ $txt"; else rpt "  ✗ $txt"; fi
    done
  else
    rpt "  未执行"
  fi
  rpt ""
  rpt "----------------------------------------------------------------"
  rpt "三、日常运维命令(在 $DEPLOY_DIR 下执行)"
  rpt "----------------------------------------------------------------"
  print_commands >> "$REPORT_FILE"
  rpt ""
  rpt "----------------------------------------------------------------"
  rpt "四、账号与安全"
  rpt "----------------------------------------------------------------"
  rpt "  账号密码文件: $DEPLOY_DIR/.env (权限600, 含全部明文密码, 请妥善管控)"
  if grep -qE '^  nacos:' "$DEPLOY_DIR/docker-compose.yml" 2>/dev/null; then
    rpt "  Nacos 控制台 : 入口见「一」; 初始账号在 .env (NACOS_AUTH_USER/NACOS_AUTH_PASSWORD), 登录后请改密"
  fi
  if grep -qE '^  xxljob:' "$DEPLOY_DIR/docker-compose.yml" 2>/dev/null; then
    rpt "  XXL-Job 控制台: 入口见「一」; 初始账号 admin, 初始密码为 .env 中 XXL_JOB_ADMIN_PASSWORD(打包时按该值初始化), 登录后请改密"
  fi
  if grep -qE '^  minio:' "$DEPLOY_DIR/docker-compose.yml" 2>/dev/null; then
    rpt "  MinIO        : root 账号在 .env (MINIO_ROOT_USER/MINIO_ROOT_PASSWORD)"
  fi
  rpt "  端口/密码等变更请用本地打包器重新打包, 勿直接改服务器文件"
  rpt ""
  rpt "----------------------------------------------------------------"
  rpt "五、数据库备份"
  rpt "----------------------------------------------------------------"
  if [ "${BACKUP_ENABLED:-0}" = "1" ]; then
    rpt "  定时备份: 已启用, crontab '$BACKUP_CRON'"
    rpt "  备份目录: $BACKUP_DIR (按库分文件 gzip, 轮转策略见 backup.conf)"
    [ -f "$DEPLOY_DIR/backup.sh" ]  && rpt "  手工备份: $DEPLOY_DIR/backup.sh"
    [ -f "$DEPLOY_DIR/restore.sh" ] && rpt "  备份恢复: $DEPLOY_DIR/restore.sh list (恢复指定库: ./restore.sh <服务> <库名>)"
  else
    rpt "  未启用 (如需, 在本地打包器配置后重新打包部署)"
  fi
  rpt ""
  rpt "本报告由 deploy.sh 在部署完成时自动生成, 每次执行会覆盖重写"
  chmod 644 "$REPORT_FILE" 2>/dev/null || true
}

summary() {
  local ip line
  ip="$(detect_ip)"
  hr; log "部署完成! 部署目录: $DEPLOY_DIR"
  echo
  for line in "${SUMMARY_LINES[@]+"${SUMMARY_LINES[@]}"}"; do
    [ -z "$line" ] && continue
    echo "  ${line//__IP__/$ip}"
  done
  if [ -n "${HEALTH_LINES+x}" ]; then
    echo
    if [ "${HEALTH_BAD:-0}" -eq 0 ]; then
      echo -e "  ${GREEN}健康实测${NC}"
    else
      echo -e "  ${RED}健康实测(部分未通过)${NC}"
    fi
    local hl st txt
    for hl in "${HEALTH_LINES[@]+"${HEALTH_LINES[@]}"}"; do
      st="${hl%%|*}"; txt="${hl#*|}"
      if [ "$st" = "OK" ]; then
        echo -e "    ${GREEN}✓${NC} $txt"
      else
        echo -e "    ${RED}✗${NC} $txt"
      fi
    done
  fi
  echo
  echo -e "  账号密码: $DEPLOY_DIR/.env (权限600)"
  if [ -f "$DEPLOY_DIR/restore.sh" ]; then
    echo -e "  备份恢复: $DEPLOY_DIR/restore.sh list   (恢复指定库: ./restore.sh <服务> <库名>)"
  fi
  echo
  echo -e "  ${CYAN}常用命令(在部署目录下执行, 日志用 docker compose 查看)${NC}"
  print_commands
  echo
  write_report "$ip"
  log "部署报告已生成: $DEPLOY_DIR/部署报告.txt (每次执行 deploy.sh 自动重写)"
  echo -e "  ${YELLOW}注意: 端口/密码等变更请用本地打包器重新打包, 勿直接改服务器文件${NC}"
  hr
}

#===============================================================================
main() {
  verify_bundle_integrity
  install_docker
  install_compose
  load_images
  check_ports
  prepare_deploy_dir
  install_backup
  import_sql_external
  startup
  import_sql_local
  health_check
  summary
}
main "$@"

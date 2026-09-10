#!/usr/bin/env bash
#===============================================================================
#
#  离线中间件一键部署脚本 (支持 x86_64 / aarch64)
#
#  功能:
#    1. 离线安装 Docker / Docker Compose(自动识别服务器架构)
#    2. 配置 daemon.json(数据目录/日志/镜像加速器) + 开机自启
#    3. 交互式选择要部署的中间件, 自动生成 docker-compose.yml
#    4. NGINX / MySQL(5.7|8.0) / Redis / Nacos / XXL-Job / MinIO
#    5. Nacos / XXL-Job 自动建库导表, 数据库可选"本次部署"或"外部"
#
#  目录结构(离线包由你提前放入):
#    packages/docker-x86_64.tgz | docker-aarch64.tgz        # Docker 静态包
#    packages/docker-compose-x86_64 | docker-compose-aarch64 # Compose 插件
#    images/*.tar | *.tar.gz                                 # 中间件镜像包
#    sql/xxl-job.sql                                         # XXL-Job 建表SQL
#    conf/nginx.conf  conf/redis.conf                        # 配置模板
#
#  用法:  root 用户执行  ./deploy.sh
#
#===============================================================================
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$BASE_DIR/packages"
IMG_DIR="$BASE_DIR/images"
SQL_DIR="$BASE_DIR/sql"
CONF_DIR="$BASE_DIR/conf"

#------------------------------- 镜像版本(按需修改) -------------------------------
IMG_NGINX="nginx:1.31.5"
IMG_MYSQL57="mysql:5.7.44"
IMG_MYSQL8="mysql:8.0.46"
IMG_NACOS="nacos/nacos-server:v3.2.4"
IMG_XXLJOB="xuxueli/xxl-job-admin:3.3.0"
IMG_MINIO="minio/minio:RELEASE.2025-04-22T22-12-26Z"
IMG_REDIS="redis:8.6.5"

#------------------------------- 颜色 / 日志 -------------------------------
RED='\033[31m'; GREEN='\033[32m'; YELLOW='\033[33m'; CYAN='\033[36m'; NC='\033[0m'
log()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()  { echo -e "${RED}[ERROR]${NC}  $*"; }
die()  { err "$*"; exit 1; }
hr()   { echo -e "${CYAN}------------------------------------------------------------${NC}"; }

#------------------------------- 通用输入函数 -------------------------------
ask() { # ask "提示" "默认值(可空)"
  local prompt="$1" default="${2:-}" reply
  if [[ -n "$default" ]]; then
    read -r -p "$(echo -e "${CYAN}${prompt}${NC} [${default}]: ")" reply
    echo "${reply:-$default}"
  else
    read -r -p "$(echo -e "${CYAN}${prompt}${NC}: ")" reply
    echo "$reply"
  fi
}

ask_yes() { # ask_yes "提示" [默认y]
  local prompt="$1" default="${2:-y}" reply
  read -r -p "$(echo -e "${CYAN}${prompt}${NC} (y/n) [${default}]: ")" reply
  reply="${reply:-$default}"
  [[ "$reply" =~ ^[Yy]$ ]]
}

ask_port() { # ask_port "服务名" 默认端口
  local svc="$1" port
  while true; do
    port="$(ask "${svc} 宿主机端口" "$2")"
    if [[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -ge 1 ] && [ "$port" -le 65535 ]; then
      echo "$port"; return 0
    fi
    err "${svc} 端口不合法, 请重新输入"
  done
}

#------------------------------- 0. 前置检查 -------------------------------
[ "$(id -u)" -eq 0 ] || die "请使用 root 用户执行本脚本"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)               ARCH_KEY="x86_64"  ;;
  aarch64|arm64)        ARCH_KEY="aarch64" ;;
  *) die "不支持的架构: $ARCH" ;;
esac
log "当前服务器架构: ${ARCH} -> 使用 ${ARCH_KEY} 离线包"
command -v tar  >/dev/null || die "缺少 tar 命令"

#------------------------------- 1. 安装 Docker -------------------------------
install_docker() {
  hr; log "【1/9】检查 / 安装 Docker"
  if command -v dockerd >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    log "Docker 已安装且运行正常, 跳过安装"
    return 0
  fi
  local tgz="$PKG_DIR/docker-${ARCH_KEY}.tgz"
  [ -f "$tgz" ] || die "缺少离线包: $tgz\n        下载: https://download.docker.com/linux/static/stable/${ARCH_KEY}/"
  log "解压 $tgz ..."
  rm -rf /tmp/docker-bin && mkdir -p /tmp/docker-bin
  tar -xzf "$tgz" -C /tmp/docker-bin
  install -m 755 /tmp/docker-bin/docker/* /usr/local/bin/
  rm -rf /tmp/docker-bin

  # --- containerd systemd 服务 ---
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

  # --- docker systemd 服务 ---
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

  # --- 配置 daemon.json ---
  local data_root mirrors mirror_list=""
  data_root="$(ask "Docker 数据目录(data-root)" "/data/docker")"
  read -r -p "$(echo -e "${CYAN}镜像加速器地址(空格分隔, 回车跳过)${NC}: ")" mirrors
  if [[ -n "$mirrors" ]]; then
    for m in $mirrors; do mirror_list+="\"$m\", "; done
    mirror_list="[ ${mirror_list%, } ]"
  else
    mirror_list="[]"
  fi
  mkdir -p "$data_root" /etc/docker
  cat > /etc/docker/daemon.json <<EOF
{
  "data-root": "${data_root}",
  "storage-driver": "overlay2",
  "exec-opts": ["native.cgroupdriver=systemd"],
  "log-driver": "json-file",
  "log-opts": { "max-size": "100m", "max-file": "3" },
  "live-restore": true,
  "max-concurrent-downloads": 10,
  "registry-mirrors": ${mirror_list},
  "insecure-registries": []
}
EOF
  log "daemon.json 已写入: /etc/docker/daemon.json (data-root=${data_root})"

  systemctl daemon-reload
  systemctl enable --now containerd >/dev/null 2>&1
  systemctl enable --now docker     >/dev/null 2>&1
  sleep 2
  docker info >/dev/null 2>&1 || die "Docker 启动失败, 请执行 journalctl -u docker 查看日志"
  log "Docker $(docker -v | awk -F'[ ,]' '{print $3}') 安装完成并已开机自启"
}

#------------------------------- 2. 安装 Docker Compose -------------------------------
install_compose() {
  hr; log "【2/9】检查 / 安装 Docker Compose"
  if docker compose version >/dev/null 2>&1; then
    log "Docker Compose 已安装, 跳过"
    return 0
  fi
  local bin="$PKG_DIR/docker-compose-${ARCH_KEY}"
  [ -f "$bin" ] || die "缺少离线包: $bin\n        下载: https://github.com/docker/compose/releases (docker-compose-linux-${ARCH_KEY})"
  mkdir -p /usr/local/libexec/docker/cli-plugins
  install -m 755 "$bin" /usr/local/libexec/docker/cli-plugins/docker-compose
  docker compose version >/dev/null || die "Docker Compose 安装失败"
  log "Docker Compose 安装完成"
}

#------------------------------- 3. 加载离线镜像 -------------------------------
load_images() {
  hr; log "【3/9】加载离线镜像"
  shopt -s nullglob
  local files=("$IMG_DIR"/*.tar "$IMG_DIR"/*.tar.gz)
  [ ${#files[@]} -gt 0 ] || die "images/ 目录中没有镜像包(.tar / .tar.gz)"
  local f
  for f in "${files[@]}"; do
    log "加载 $(basename "$f") ..."
    docker load -i "$f" >/dev/null
  done
  log "共加载 ${#files[@]} 个镜像包"
  normalize_images
}

# 将任意仓库前缀的镜像统一打短名标签, 如 docker.m.daocloud.io/nacos/nacos-server:v3.2.4 -> nacos/nacos-server:v3.2.4
normalize_images() {
  local short name tag found
  for short in "${IMG_OF[@]}"; do
    name="${short%:*}"; tag="${short##*:}"
    # 已是短名且存在 -> 跳过
    if docker image inspect "$short" >/dev/null 2>&1; then
      continue
    fi
    # 在已加载镜像中找 repo 以 /${name} 结尾(或等于 name)且 tag 相同的镜像
    found=$(docker images --format '{{.Repository}}:{{.Tag}}'             | awk -v n="$name" -v t="$tag" -F: '{
                repo = $1; for(i=2;i<NF;i++) repo = repo ":" $i; tag = $NF;
                if (tag == t && (repo == n || repo ~ ("(^|/)" n "$"))) { print repo ":" tag; exit }
              }')
    if [[ -n "$found" ]]; then
      log "镜像名归一化: ${found}  ->  ${short}"
      docker tag "$found" "$short"
    else
      warn "未找到镜像 ${short} (或其带前缀版本), 请检查 images/ 中的 tar 包"
    fi
  done
}

#------------------------------- 4. 选择中间件 + 端口 -------------------------------
declare -a SELECTED=()
declare -A PORTS=()
declare -A IMG_OF=([nginx]=$IMG_NGINX [mysql57]=$IMG_MYSQL57 [mysql8]=$IMG_MYSQL8 \
                   [nacos]=$IMG_NACOS [xxljob]=$IMG_XXLJOB [minio]=$IMG_MINIO [redis]=$IMG_REDIS)

has() { local s; for s in "${SELECTED[@]}"; do [[ "$s" == "$1" ]] && return 0; done; return 1; }

select_middleware() {
  hr; log "【4/9】选择要部署的中间件"
  cat <<'EOF'
  1) NGINX            1.31.5
  2) MySQL            5.7.44
  3) MySQL            8.0.46
  4) Nacos            v3.2.4
  5) XXL-Job-Admin    3.3.0
  6) MinIO            RELEASE.2025-04-22
  7) Redis            (latest)
EOF
  local input
  input="$(ask "请输入序号(逗号分隔, 回车=全部部署)" "1,2,3,4,5,6,7")"
  input="${input// /,}"
  local i
  IFS=',' read -ra arr <<< "$input"
  for i in "${arr[@]}"; do
    case "$i" in
      1) SELECTED+=("nginx")   ;; 2) SELECTED+=("mysql57") ;;
      3) SELECTED+=("mysql8")  ;; 4) SELECTED+=("nacos")   ;;
      5) SELECTED+=("xxljob")  ;; 6) SELECTED+=("minio")   ;;
      7) SELECTED+=("redis")   ;;
      *) warn "忽略无效序号: $i" ;;
    esac
  done
  [ ${#SELECTED[@]} -gt 0 ] || die "未选择任何中间件"

  # 端口
  has nginx   && { PORTS[nginx_http]=$(ask_port "NGINX HTTP"  80)
                   PORTS[nginx_https]=$(ask_port "NGINX HTTPS" 443)
                   PORTS[nginx_extra]=$(ask_port "NGINX 额外端口(如9000)" 9000); }
  has mysql57 && PORTS[mysql57]=$(ask_port "MySQL 5.7" 13306)
  has mysql8  && PORTS[mysql8]=$(ask_port  "MySQL 8.0" 13307)
  has redis   && PORTS[redis]=$(ask_port   "Redis"     16379)
  has nacos   && { PORTS[nacos_main]=$(ask_port   "Nacos 主端口"   8848)
                   PORTS[nacos_grpc]=$(ask_port   "Nacos gRPC"     9848)
                   PORTS[nacos_console]=$(ask_port "Nacos 控制台" 18080); }
  has xxljob  && PORTS[xxljob]=$(ask_port  "XXL-Job"   18081)
  has minio   && { PORTS[minio_api]=$(ask_port     "MinIO API"     18082)
                   PORTS[minio_console]=$(ask_port "MinIO 控制台" 18083); }
}

#------------------------------- 5. 部署目录 + 账号密码(.env) -------------------------------
DEPLOY_DIR=""
MYSQL_ROOT_PASSWORD=""; NACOS_AUTH_USER=""; NACOS_AUTH_PASSWORD=""; NACOS_AUTH_TOKEN=""
XXL_JOB_ADMIN_PASSWORD=""; XXL_JOB_ACCESS_TOKEN=""; REDIS_PASSWORD=""
MINIO_ROOT_USER=""; MINIO_ROOT_PASSWORD=""

setup_env() {
  hr; log "【5/9】部署目录与账号密码"
  DEPLOY_DIR="$(ask "中间件部署目录" "/data/middleware")"
  mkdir -p "$DEPLOY_DIR"

  log "请确认各组件密码(直接回车使用默认值):"
  MYSQL_ROOT_PASSWORD="$(ask    "MySQL root 密码"      "MJ2R3DZ/VG9=KbX@")"
  NACOS_AUTH_USER="$(ask        "Nacos 控制台账号"     "nacos")"
  NACOS_AUTH_PASSWORD="$(ask    "Nacos 控制台密码"     "T=S:2lJR@V%0D#HT")"
  NACOS_AUTH_TOKEN="$(ask       "Nacos auth token"     "zvO4kaTOrl8hKBVA6xPWr3WSyjtrGCZT8ZiSMMqhUmrH8KsnRPaXoRIMnhhS7Bnh")"
  XXL_JOB_ADMIN_PASSWORD="$(ask "XXL-Job admin 密码"   "TCPG241hPPZP")"
  XXL_JOB_ACCESS_TOKEN="$(ask   "XXL-Job accessToken"  "p2rXngWLGEd7FLGE0q9oTsbFYhKVCnFiERMzaay8X61E2xQt")"
  REDIS_PASSWORD="$(ask         "Redis 密码"           "PY34%@MiDp~2#lhg")"
  MINIO_ROOT_USER="$(ask        "MinIO 用户名"         "admin")"
  MINIO_ROOT_PASSWORD="$(ask    "MinIO 密码"           "g7orzms97%CpCd2/")"

  cat > "$DEPLOY_DIR/.env" <<EOF
# 自动生成, 可手工修改; docker compose 启动时自动读取
TZ=Asia/Shanghai
MYSQL_ROOT_PASSWORD=${MYSQL_ROOT_PASSWORD}
NACOS_AUTH_USER=${NACOS_AUTH_USER}
NACOS_AUTH_PASSWORD=${NACOS_AUTH_PASSWORD}
NACOS_AUTH_TOKEN=${NACOS_AUTH_TOKEN}
XXL_JOB_ADMIN_PASSWORD=${XXL_JOB_ADMIN_PASSWORD}
XXL_JOB_ACCESS_TOKEN=${XXL_JOB_ACCESS_TOKEN}
REDIS_PASSWORD=${REDIS_PASSWORD}
MINIO_ROOT_USER=${MINIO_ROOT_USER}
MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD}
EOF
  chmod 600 "$DEPLOY_DIR/.env"
  log ".env 已生成: $DEPLOY_DIR/.env"
}

#------------------------------- 6. 数据库选择(Nacos / XXL-Job) -------------------------------
DB_LOCAL_HOST=""   # 本次部署的 mysql 服务名(空=未部署)
NACOS_DB_HOST=""; NACOS_DB_PORT=""; NACOS_DB_NAME="nacos";   NACOS_DB_USER=""; NACOS_DB_PASS_LITERAL=""
XXL_DB_HOST="";  XXL_DB_PORT="";  XXL_DB_NAME="xxl_job";     XXL_DB_USER="";  XXL_DB_PASS_LITERAL=""

pick_local_mysql() {
  if has mysql57 && has mysql8; then
    local c; c="$(ask "Nacos/XXL-Job 使用哪个 MySQL? (1=mysql57 2=mysql8)" "2")"
    [ "$c" = "1" ] && DB_LOCAL_HOST="mysql57" || DB_LOCAL_HOST="mysql8"
  elif has mysql57; then DB_LOCAL_HOST="mysql57"
  elif has mysql8;  then DB_LOCAL_HOST="mysql8"
  else DB_LOCAL_HOST=""; fi
}

choose_db() { # choose_db <nacos|xxljob>
  local app="$1"
  local -n OUT_HOST="${app^^}_DB_HOST"  # NACOS_DB_HOST / XXL_DB_HOST
  local -n OUT_PORT="${app^^}_DB_PORT"
  local -n OUT_NAME="${app^^}_DB_NAME"
  local -n OUT_USER="${app^^}_DB_USER"
  local -n OUT_PASS="${app^^}_DB_PASS_LITERAL"
  local default_db="nacos"; [ "$app" = "xxljob" ] && default_db="xxl_job"

  hr; log "【${app}】数据库连接配置"
  if [[ -n "$DB_LOCAL_HOST" ]]; then
    echo -e "  ${CYAN}1)${NC} 使用本次部署的 MySQL (服务名: ${DB_LOCAL_HOST}, 推荐)"
    echo -e "  ${CYAN}2)${NC} 使用外部数据库"
    local c; c="$(ask "请选择" "1")"
    if [[ "$c" == "1" ]]; then
      OUT_HOST="$DB_LOCAL_HOST"; OUT_PORT="3306"; OUT_USER="root"
      OUT_NAME="$default_db";    OUT_PASS='${MYSQL_ROOT_PASSWORD}'
      log "${app} 将连接本次部署的 MySQL: ${DB_LOCAL_HOST}:3306/${default_db}"
      return 0
    fi
  else
    warn "本次未部署 MySQL, 请填写外部数据库信息"
  fi
  OUT_HOST="$(ask "数据库地址" "127.0.0.1")"
  OUT_PORT="$(ask "数据库端口" "3306")"
  OUT_NAME="$(ask "数据库名"   "$default_db")"
  OUT_USER="$(ask "数据库账号" "root")"
  OUT_PASS="$(ask "数据库密码")"
  OUT_PASS="$(echo "$OUT_PASS" | sed 's/[&/\]/\\&/g')"   # 转义, 防止 URL 特殊字符破坏 yml
}

#------------------------------- 7. 准备 SQL -------------------------------
NACOS_SQL_FILE=""

prepare_nacos_sql() {
  hr; log "【7/9】准备 Nacos 初始化 SQL"
  if [ -f "$SQL_DIR/nacos-mysql.sql" ]; then
    log "使用预置 SQL: $SQL_DIR/nacos-mysql.sql"
  else
    log "从镜像 ${IMG_NACOS} 中提取 mysql-schema.sql ..."
    local cid="" p
    cid=$(docker create "${IMG_NACOS}" 2>/dev/null) || die "无法创建临时容器, 请检查 nacos 镜像是否已加载"
    local ok=1
    for p in /home/nacos/conf/mysql-schema.sql /home/nacos/conf/nacos-mysql.sql /conf/mysql-schema.sql; do
      if docker cp "$cid:$p" "$SQL_DIR/nacos-mysql-raw.sql" 2>/dev/null; then ok=0; break; fi
    done
    docker rm -f "$cid" >/dev/null 2>&1
    [ $ok -eq 0 ] || die "未能从镜像中提取 nacos SQL, 请手工导出后放到 sql/nacos-mysql.sql 再重试"
    # 去掉源文件中可能自带的建库语句, 统一由我们生成
    sed -i -e '/CREATE DATABASE/I,+2d' "$SQL_DIR/nacos-mysql-raw.sql" 2>/dev/null || true
    mv "$SQL_DIR/nacos-mysql-raw.sql" "$SQL_DIR/nacos-mysql.sql"
  fi
  # 加上创库语句
  {
    echo "CREATE DATABASE IF NOT EXISTS \`nacos\` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
    echo "USE \`nacos\`;"
    cat "$SQL_DIR/nacos-mysql.sql"
  } > "$SQL_DIR/nacos.sql"
  NACOS_SQL_FILE="$SQL_DIR/nacos.sql"
  log "Nacos SQL 就绪: $NACOS_SQL_FILE"
}

check_xxl_sql() {
  [ -f "$SQL_DIR/xxl-job.sql" ] || die "缺少 $SQL_DIR/xxl-job.sql (XXL-Job 建表脚本)"
}

#------------------------------- 8. 生成 docker-compose.yml + 配置 -------------------------------
gen_compose() {
  hr; log "【8/9】生成 docker-compose.yml 及持久化目录: $DEPLOY_DIR"
  local f="$DEPLOY_DIR/docker-compose.yml"

  # 目录
  mkdir -p "$DEPLOY_DIR"/{nginx/{conf,html,logs,ssl},redis/{data,log},nacos/{data,log},xxl-job/{data,log},minio/{data,log}}
  has mysql57 && mkdir -p "$DEPLOY_DIR/mysql57"/{data,log,init}
  has mysql8  && mkdir -p "$DEPLOY_DIR/mysql8"/{data,log,init}

  # --- 配置文件 ---
  if has nginx; then
    cp "$CONF_DIR/nginx.conf" "$DEPLOY_DIR/nginx/nginx.conf"
    [ -f "$DEPLOY_DIR/nginx/conf/default.conf" ] || cat > "$DEPLOY_DIR/nginx/conf/default.conf" <<'EOF'
server {
    listen       80;
    server_name  _;
    root   /usr/share/nginx/html;
    index  index.html index.htm;
    location / { try_files $uri $uri/ =404; }
    error_page 500 502 503 504 /50x.html;
    location = /50x.html { root /usr/share/nginx/html; }
}
EOF
    [ -f "$DEPLOY_DIR/nginx/html/index.html" ] || echo '<h1>It works (offline-middleware)</h1>' > "$DEPLOY_DIR/nginx/html/index.html"
  fi
  has redis && cp "$CONF_DIR/redis.conf" "$DEPLOY_DIR/redis/redis.conf"

  # --- 本地 MySQL 初始化 SQL(首启自动导入) ---
  if [[ -n "$DB_LOCAL_HOST" ]]; then
    has nacos  && [ -n "$NACOS_SQL_FILE" ] && cp "$NACOS_SQL_FILE" "$DEPLOY_DIR/$DB_LOCAL_HOST/init/nacos.sql"
    has xxljob && cp "$SQL_DIR/xxl-job.sql" "$DEPLOY_DIR/$DB_LOCAL_HOST/init/xxl-job.sql"
  fi

  # ---------------- compose 头 ----------------
  cat > "$f" <<'EOF'
# 由 deploy.sh 自动生成, 可手工微调
name: middleware

services:
EOF

  # ---------------- MySQL ----------------
  local mservice="" mimage="" mport=""
  for mservice in mysql57 mysql8; do
    has "$mservice" || continue
    mimage="${IMG_OF[$mservice]}"; mport="${PORTS[$mservice]}"
    cat >> "$f" <<EOF

  ${mservice}:
    image: ${mimage}
    container_name: ${mservice}
    restart: always
    environment:
      TZ: \${TZ}
      MYSQL_ROOT_PASSWORD: \${MYSQL_ROOT_PASSWORD}
    ports:
      - "${mport}:3306"
    volumes:
      - ./${mservice}/data:/var/lib/mysql
      - ./${mservice}/log:/var/log/mysql
      - ./${mservice}/init:/docker-entrypoint-initdb.d
    command: >
      --log-error=/var/log/mysql/error.log
      --character-set-server=utf8mb4
      --collation-server=utf8mb4_unicode_ci
      --default-authentication-plugin=mysql_native_password
    healthcheck:
      test: ["CMD-SHELL", "mysqladmin ping -h localhost -uroot -p\"\$\$MYSQL_ROOT_PASSWORD\""]
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 40s
    networks:
      - app-network
EOF
  done

  # ---------------- Redis ----------------
  if has redis; then
    cat >> "$f" <<EOF

  redis:
    image: ${IMG_REDIS}
    container_name: redis
    restart: always
    environment:
      TZ: \${TZ}
    ports:
      - "${PORTS[redis]}:6379"
    volumes:
      - ./redis/data:/data
      - ./redis/log:/var/log/redis
      - ./redis/redis.conf:/usr/local/etc/redis/redis.conf
    command: redis-server /usr/local/etc/redis/redis.conf --requirepass "\${REDIS_PASSWORD}"
    healthcheck:
      test: ["CMD-SHELL", "redis-cli -a \$\$REDIS_PASSWORD ping | grep -q PONG"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - app-network
EOF
  fi

  # ---------------- Nacos ----------------
  if has nacos; then
    cat >> "$f" <<EOF

  nacos:
    image: ${IMG_NACOS}
    container_name: nacos
    restart: always
    environment:
      TZ: \${TZ}
      MODE: standalone
      SPRING_DATASOURCE_PLATFORM: mysql
      MYSQL_SERVICE_HOST: ${NACOS_DB_HOST}
      MYSQL_SERVICE_PORT: ${NACOS_DB_PORT}
      MYSQL_SERVICE_DB_NAME: ${NACOS_DB_NAME}
      MYSQL_SERVICE_USER: ${NACOS_DB_USER}
      MYSQL_SERVICE_PASSWORD: "${NACOS_DB_PASS_LITERAL}"
      NACOS_AUTH_ENABLE: "true"
      NACOS_AUTH_TOKEN_EXPIRE_SECONDS: 18000
      NACOS_AUTH_TOKEN: \${NACOS_AUTH_TOKEN}
      NACOS_AUTH_IDENTITY_KEY: serverIdentity
      NACOS_AUTH_IDENTITY_VALUE: security
      NACOS_AUTH_USER: \${NACOS_AUTH_USER}
      NACOS_AUTH_PASSWORD: \${NACOS_AUTH_PASSWORD}
    ports:
      - "${PORTS[nacos_main]}:8848"
      - "${PORTS[nacos_grpc]}:9848"
      - "${PORTS[nacos_console]}:8080"
    volumes:
      - ./nacos/data:/home/nacos/data
      - ./nacos/log:/home/nacos/logs
EOF
    [[ -n "$DB_LOCAL_HOST" ]] && cat >> "$f" <<EOF
    depends_on:
      ${DB_LOCAL_HOST}:
        condition: service_healthy
EOF
    cat >> "$f" <<'EOF'
    networks:
      - app-network
EOF
  fi

  # ---------------- XXL-Job ----------------
  if has xxljob; then
    cat >> "$f" <<EOF

  xxl-job:
    image: ${IMG_XXLJOB}
    container_name: xxl-job
    restart: always
    environment:
      TZ: \${TZ}
      PARAMS: >-
        --spring.datasource.url=jdbc:mysql://${XXL_DB_HOST}:${XXL_DB_PORT}/${XXL_DB_NAME}?useUnicode=true&characterEncoding=UTF-8&autoReconnect=true&serverTimezone=Asia/Shanghai&useSSL=false
        --spring.datasource.username=${XXL_DB_USER}
        --spring.datasource.password=${XXL_DB_PASS_LITERAL}
        --xxl.job.accessToken=\${XXL_JOB_ACCESS_TOKEN}
    ports:
      - "${PORTS[xxljob]}:8080"
    volumes:
      - ./xxl-job/data:/data
      - ./xxl-job/log:/data/applogs/xxl-job
EOF
    [[ -n "$DB_LOCAL_HOST" ]] && cat >> "$f" <<EOF
    depends_on:
      ${DB_LOCAL_HOST}:
        condition: service_healthy
EOF
    cat >> "$f" <<'EOF'
    networks:
      - app-network
EOF
  fi

  # ---------------- MinIO ----------------
  if has minio; then
    cat >> "$f" <<EOF

  minio:
    image: ${IMG_MINIO}
    container_name: minio
    restart: always
    environment:
      TZ: \${TZ}
      MINIO_ROOT_USER: \${MINIO_ROOT_USER}
      MINIO_ROOT_PASSWORD: \${MINIO_ROOT_PASSWORD}
    ports:
      - "${PORTS[minio_api]}:9000"
      - "${PORTS[minio_console]}:9001"
    volumes:
      - ./minio/data:/data
      - ./minio/log:/log
    command: server /data --console-address ":9001"
    healthcheck:
      test: ["CMD-SHELL", "mc ready local || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - app-network
EOF
  fi

  # ---------------- NGINX ----------------
  if has nginx; then
    cat >> "$f" <<EOF

  nginx:
    image: ${IMG_NGINX}
    container_name: nginx
    restart: always
    environment:
      TZ: \${TZ}
    ports:
      - "${PORTS[nginx_http]}:80"
      - "${PORTS[nginx_https]}:443"
      - "${PORTS[nginx_extra]}:9000"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf:ro
      - ./nginx/conf:/etc/nginx/conf.d
      - ./nginx/html:/usr/share/nginx/html
      - ./nginx/logs:/var/log/nginx
      - ./nginx/ssl:/etc/nginx/ssl
    networks:
      - app-network
EOF
  fi

  # ---------------- 网络 ----------------
  cat >> "$f" <<'EOF'

networks:
  app-network:
    driver: bridge
EOF

  # 兜底: 再次确保短名镜像存在(防止 images/ 包加载后名称异常)
  normalize_images

  # 校验
  (cd "$DEPLOY_DIR" && docker compose config -q) || die "docker-compose.yml 校验失败, 请检查"
  log "docker-compose.yml 生成并校验通过: $f"
}

#------------------------------- 9. 启动 + 外部库导表 -------------------------------
import_sql_to_remote() { # import_sql_to_remote <sqlfile> <host> <port> <user> <pass>
  local sqlfile="$1" host="$2" port="$3" user="$4" pass="$5" img="$IMG_MYSQL8"
  has mysql8 && img="$IMG_MYSQL8"; has mysql57 && ! has mysql8 && img="$IMG_MYSQL57"
  log "导入 $sqlfile 到 ${host}:${port} ..."
  docker run --rm -i --network app-network "$img" \
    mysql -h"$host" -P"$port" -u"$user" "-p${pass}" --default-character-set=utf8mb4 -f < "$sqlfile" \
    || warn "SQL 导入出现告警(可能是重复执行), 请人工确认"
}

import_sql_to_local() { # import_sql_to_local <服务名> <sqlfile>
  local svc="$1" sqlfile="$2"
  log "等待 $svc 就绪后导入 $(basename "$sqlfile") ..."
  local i
  for i in $(seq 1 60); do
    if docker exec "$svc" mysqladmin ping -uroot -p"$MYSQL_ROOT_PASSWORD" >/dev/null 2>&1; then break; fi
    sleep 3
  done
  docker exec -i "$svc" sh -c "exec mysql -uroot -p\"\$MYSQL_ROOT_PASSWORD\" --default-character-set=utf8mb4 -f" \
    < "$sqlfile" || warn "SQL 导入出现告警(可能是重复执行), 请人工确认"
}

startup() {
  hr; log "【9/9】启动服务"
  (cd "$DEPLOY_DIR" && docker compose up -d)
  sleep 3

  # 外部数据库导入
  has nacos  && [[ -z "$DB_LOCAL_HOST" ]] && import_sql_to_remote "$NACOS_SQL_FILE" "$NACOS_DB_HOST" "$NACOS_DB_PORT" "$NACOS_DB_USER" "$NACOS_DB_PASS_LITERAL"
  has xxljob && [[ -z "$DB_LOCAL_HOST" ]] && import_sql_to_remote "$SQL_DIR/xxl-job.sql" "$XXL_DB_HOST" "$XXL_DB_PORT" "$XXL_DB_USER" "$XXL_DB_PASS_LITERAL"
  # 本地数据库兜底导入(非首启时 init 目录不会自动执行)
  if [[ -n "$DB_LOCAL_HOST" ]]; then
    has nacos  && import_sql_to_local "$DB_LOCAL_HOST" "$NACOS_SQL_FILE"
    has xxljob && import_sql_to_local "$DB_LOCAL_HOST" "$SQL_DIR/xxl-job.sql"
  fi
  (cd "$DEPLOY_DIR" && docker compose ps)
}

#------------------------------- 摘要 -------------------------------
summary() {
  local ip; ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  hr
  log "部署完成! 部署目录: ${DEPLOY_DIR}"
  echo
  printf "  %-14s %-8s %s\n" "组件" "端口" "访问/说明"
  has nginx   && printf "  %-14s %-8s http://%s:%s\n"  "NGINX"   "${PORTS[nginx_http]}"  "$ip" "${PORTS[nginx_http]}"
  has mysql57 && printf "  %-14s %-8s mysql -h%s -P%s -uroot\n" "MySQL5.7" "${PORTS[mysql57]}" "$ip" "${PORTS[mysql57]}"
  has mysql8  && printf "  %-14s %-8s mysql -h%s -P%s -uroot\n" "MySQL8.0" "${PORTS[mysql8]}"  "$ip" "${PORTS[mysql8]}"
  has redis   && printf "  %-14s %-8s redis-cli -h %s -p %s -a '***'\n" "Redis" "${PORTS[redis]}" "$ip" "${PORTS[redis]}"
  has nacos   && printf "  %-14s %-8s http://%s:%s/nacos  (%s)\n" "Nacos" "${PORTS[nacos_console]}" "$ip" "${PORTS[nacos_console]}" "$NACOS_AUTH_USER"
  has xxljob  && printf "  %-14s %-8s http://%s:%s/xxl-job-admin  (admin)\n" "XXL-Job" "${PORTS[xxljob]}" "$ip" "${PORTS[xxljob]}"
  has minio   && printf "  %-14s %-8s http://%s:%s (API) / http://%s:%s (控制台)\n" "MinIO" "${PORTS[minio_api]}" "$ip" "${PORTS[minio_api]}" "$ip" "${PORTS[minio_console]}"
  hr
  echo -e "  账号密码见: ${DEPLOY_DIR}/.env (权限600)"
  echo -e "  管理命令: cd ${DEPLOY_DIR} && docker compose ps|logs|restart|down"
  echo -e "  ${YELLOW}SELinux 开启的服务器如遇挂载权限问题, 执行: chcon -R system_u:object_r:container_file_t:s0 ${DEPLOY_DIR}${NC}"
}

#===============================================================================
main() {
  install_docker
  install_compose
  load_images
  select_middleware
  setup_env
  pick_local_mysql
  has nacos  && { choose_db nacos;  prepare_nacos_sql; }
  has xxljob && { choose_db xxljob; check_xxl_sql; }
  gen_compose
  startup
  summary
}
main "$@"

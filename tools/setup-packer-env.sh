#!/bin/bash
# 功能: 打包机环境一键准备(Linux 多发行版): 按需安装/检查 Python3、Docker Engine、Go(构建 kk 用)
# 用法:
#   sudo bash tools/setup-packer-env.sh              # 安装缺失组件(幂等, 已装且满足版本则跳过)
#   bash tools/setup-packer-env.sh --check           # 只盘点不安装(全齐退出码 0, 缺失退出码 1)
#   sudo bash tools/setup-packer-env.sh --only go    # 只装某一个: python|docker|go
#   sudo bash tools/setup-packer-env.sh --cn         # 国内网络: Go 走 golang.google.cn, Docker 走阿里云镜像源
#   GO_VERSION=1.25.4 sudo bash tools/setup-packer-env.sh --only go
# 说明:
#   - 打包器(packer.py)本身只需 Python3 标准库; Docker 用于 kk artifact 导出; Go 用于源码构建 kk(third_party/kubekey)
#   - Windows 打包机不适用本脚本: Python/Docker Desktop/Go 用官方安装包, 见 README「环境准备」
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="${BASE_DIR}/setup-packer-env.log"
CN=0; CHECK=0; ONLY=""
GO_VERSION="${GO_VERSION:-1.25.4}"          # kk 构建: third_party/kubekey 的 go.work 要求 1.25+
MIN_PY=3                                     # 打包器纯标准库, 任意 python3 均可

info() { echo "[$(date '+%F %T')] [INFO] $*" | tee -a "$LOG_FILE"; }
warn() { echo "[$(date '+%F %T')] [WARN] $*" | tee -a "$LOG_FILE" >&2; }
die()  { echo "[$(date '+%F %T')] [FAIL] $*" | tee -a "$LOG_FILE" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- 参数
while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1 ;;
    --cn)    CN=1 ;;
    --only)  ONLY="${2:?--only 需要 python|docker|go}"; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) die "未知参数: $1 (见 -h)" ;;
  esac
  shift
done
want() { [ -z "$ONLY" ] || [ "$ONLY" = "$1" ]; }

# ---------------------------------------------------------------- 环境
[ "$(uname -s)" = "Linux" ] || die "本脚本仅支持 Linux 打包机; Windows/macOS 请按 README 手动安装 Python3/Docker/Go"
. /etc/os-release 2>/dev/null || die "未识别的 Linux(缺 /etc/os-release)"
ID="${ID:-unknown}"; VER_ID="${VERSION_ID:-}"
ARCH="$(uname -m)"; case "$ARCH" in x86_64) GOARCH=amd64 ;; aarch64) GOARCH=arm64 ;; *) die "不支持的架构: $ARCH" ;; esac

if [ "$(id -u)" = "0" ]; then SUDO=""; else have sudo || die "请以 root 运行或先安装 sudo"; SUDO="sudo"; fi
info "目标环境: ${PRETTY_NAME:-$ID $VER_ID} ${ARCH}  (cn=$CN check=$CHECK only=${ONLY:-全部})"
[ "$CHECK" = "1" ] || [ "$(id -u)" = "0" ] || warn "非 root 运行: 安装步骤将通过 sudo 执行"

PKG=""; [ "$CHECK" = "1" ] || {
  case "$ID" in
    debian|ubuntu|uos|deepin|kylin)  PKG=apt ;;
    centos|rhel|rocky|almalinux|fedora|openeuler|anolis|alinux|tencentos|opencloudos) PKG=dnf ;;
    *) die "暂不支持的发行版: $ID (可手动安装 python3/docker/go 后重跑 --check)" ;;
  esac
}
pkg_install() {  # pkg_install 包名...
  [ $# -gt 0 ] || return 0
  if [ "$PKG" = apt ]; then
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
  else
    if have dnf; then $SUDO dnf install -y "$@"; else $SUDO yum install -y "$@"; fi
  fi
}
refresh_idx() {
  if [ "$PKG" = apt ]; then $SUDO apt-get update -qq; else have dnf && $SUDO dnf makecache --quiet || $SUDO yum makecache -q || true; fi
}

MISSING=0   # --check 模式的结论

# ---------------------------------------------------------------- python3
py_ver() { python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo none; }
if want python; then
  v="$(py_ver)"
  if [ "$v" != "none" ]; then
    info "python3 已安装: $v (打包器只需标准库, 版本无硬性要求)"
  elif [ "$CHECK" = "1" ]; then
    warn "python3 未安装"; MISSING=1
  else
    info "安装 python3 ..."
    refresh_idx; pkg_install python3
    info "python3 安装完成: $(py_ver)"
  fi
fi

# ---------------------------------------------------------------- docker
docker_ok() { have docker && docker info >/dev/null 2>&1; }
if want docker; then
  if docker_ok; then
    info "docker 已就绪: $(docker --version)  compose: $(docker compose version 2>/dev/null || echo '无 compose 插件(可选)')"
  elif [ "$CHECK" = "1" ]; then
    warn "docker 未安装或引擎未运行 (用于 kk artifact 导出与镜像补料)"; MISSING=1
  else
    info "安装 docker engine ..."
    have curl || { refresh_idx; pkg_install curl ca-certificates; }
    # 首选官方脚本(覆盖面最广, 含 compose 插件); 国内网 --cn 走阿里云镜像源
    if curl -fsSL --connect-timeout 8 "https://get.docker.com" -o "$(mktemp)"; then
      if [ "$CN" = "1" ]; then curl -fsSL "https://get.docker.com" | sh -s -- --mirror Aliyun; else curl -fsSL "https://get.docker.com" | sh; fi
    else
      warn "get.docker.com 不可达, 回退发行版仓库安装"
      refresh_idx
      if [ "$PKG" = apt ]; then pkg_install docker.io docker-compose-plugin || pkg_install docker.io
      else pkg_install docker-engine || pkg_install docker || pkg_install moby-engine; fi
    fi
    $SUDO systemctl enable --now docker 2>/dev/null || $SUDO service docker start || true
    sleep 2
    docker_ok || warn "docker 已安装但引擎未就绪(手动执行: systemctl status docker); WSL2 需在发行版内启用 systemd"
    id -nG "$(whoami)" 2>/dev/null | grep -qw docker || { $SUDO usermod -aG docker "$(whoami)" && warn "已将 $(whoami) 加入 docker 组, 重新登录后免 sudo 生效"; }
  fi
fi

# ---------------------------------------------------------------- go
go_ok() { have go && [ "$(go version 2>/dev/null | grep -oE 'go[0-9]+' | tr -d go | cut -d. -f1,2 | awk -F. '{print ($1>1)||($1==1&&$2>=25)}')" = "1" ]; }
if want go; then
  if go_ok; then
    info "go 已满足 (>=1.25, kk 构建): $(go version)"
  elif [ "$CHECK" = "1" ]; then
    have go && warn "go 版本过低(需 >=1.25): $(go version)" || warn "go 未安装 (源码构建 kk 用; 仅使用 Releases 现成 kk 二进制则不需要)"; MISSING=1
  else
    info "安装 go ${GO_VERSION} (${GOARCH}) ..."
    $SUDO rm -rf /usr/local/go
    # 包管理器里的 golang 普遍过旧, 统一用官方 tarball; 国内默认 golang.google.cn 镜像
    if [ "$CN" = "1" ]; then
      go_url="https://golang.google.cn/dl/go${GO_VERSION}.linux-${GOARCH}.tar.gz"
      alt_url="https://mirrors.aliyun.com/golang/go${GO_VERSION}.linux-${GOARCH}.tar.gz"
    else
      go_url="https://go.dev/dl/go${GO_VERSION}.linux-${GOARCH}.tar.gz"
      alt_url="https://golang.google.cn/dl/go${GO_VERSION}.linux-${GOARCH}.tar.gz"
    fi
    tmp_tgz="$(mktemp --suffix=.tar.gz)"
    curl -fsSL --connect-timeout 10 "$go_url" -o "$tmp_tgz" || curl -fsSL --connect-timeout 10 "$alt_url" -o "$tmp_tgz" \
      || die "go${GO_VERSION} 下载失败(检查版本号是否存在: https://golang.google.cn/dl/)"
    $SUDO tar -C /usr/local -xzf "$tmp_tgz" && rm -f "$tmp_tgz"
    $SUDO tee /etc/profile.d/go.sh >/dev/null <<'EOF'
export PATH=$PATH:/usr/local/go/bin
export GOPROXY=https://goproxy.cn,direct
EOF
    $SUDO chmod 644 /etc/profile.d/go.sh
    export PATH=$PATH:/usr/local/go/bin
    have go || die "go 安装后仍不可用(检查 /usr/local/go/bin/go)"
    info "go 安装完成: $(go version); 新终端自动生效(/etc/profile.d/go.sh, 含国内 GOPROXY)"
  fi
fi

# ---------------------------------------------------------------- 结论
if [ "$CHECK" = "1" ]; then
  [ "$MISSING" = "0" ] && info "盘点通过: 打包环境齐备" || die "盘点发现缺失项, 见上方 WARN (去掉 --check 重跑可自动安装)"
else
  info "完成。后续: 打包= python packer.py (或浏览器 http://127.0.0.1:8765); kk 构建= python prepare_cluster.py --kk-build; artifact 导出= python prepare_cluster.py --artifact-export"
fi

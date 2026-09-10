#!/bin/bash
# 回归测试: images.txt 解析(注释行) + normalize_image 匹配逻辑
# 数据全部来自真实仓库: images.txt 取最新 dist 包; mock 的 docker images 列表
# 直接读 warehouse 各镜像 tar 内 manifest.json 的 RepoTags (即 docker load 后的真名)
set -euo pipefail
cd "$(dirname "$0")/.."

BUNDLE_TGZ=$(ls -t dist/*.tar.gz 2>/dev/null | head -1 || true)
SMOKE_CREATED=0
if [ -z "$BUNDLE_TGZ" ]; then
  echo "[INFO] dist/ 下暂无产物, 先用 tests/pack_smoke_config.json 打一个"
  python packer.py --config tests/pack_smoke_config.json --out dist/.smoke >/dev/null 2>&1
  BUNDLE_TGZ=$(ls -t dist/.smoke/*.tar.gz | head -1)
  SMOKE_CREATED=1
fi
echo "[INFO] 数据源: $BUNDLE_TGZ"

# 1) 从包里取 images.txt
python - "$BUNDLE_TGZ" <<'PYEOF'
import sys, tarfile
t = tarfile.open(sys.argv[1], 'r')
root = t.getnames()[0].split('/')[0]
open('tests/.images_txt_real', 'wb').write(t.extractfile(root + '/images.txt').read())
t.close()
PYEOF

# 2) mock docker images 列表 = images.txt 中各镜像 tar 的真实 RepoTags
python - <<'PYEOF' > tests/.docker_images_mock
import json, tarfile
v = json.load(open('versions.json', encoding='utf-8'))
tags = []
for line in open('tests/.images_txt_real', encoding='utf-8'):
    line = line.strip()
    if not line or line.startswith('#'):
        continue
    fname = line.split('|')[0].strip()
    svc = fname.rsplit('-', 1)[0]           # mysql8-amd64.tar -> mysql8
    arch = 'arm64' if '-arm64' in fname else 'amd64'
    rel = v['services'][svc]['images'][arch]
    t = tarfile.open(rel, 'r')
    m = json.load(t.extractfile('manifest.json'))
    for e in m:
        tags += e.get('RepoTags', [])
    t.close()
print('\n'.join(tags))
PYEOF

DOCKER_IMAGES="$(cat tests/.docker_images_mock)"
ARCH=amd64
die() { echo "[ERROR] $*"; exit 1; }
log() { echo "[INFO] $*"; }

normalize_image() {
  local short="$1" name tag need rt found c_repo c_tag
  case "$short" in
    *:*) ;;
    *) die "images.txt 短名格式错误: '$short'" ;;
  esac
  name="${short%%:*}"; tag="${short#*:}"
  need="$ARCH"
  found=""
  while read -r rt; do
    rt="${rt%$'\r'}"                      # Windows python 输出可能带 \r
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
    found="$rt"; break
  done <<INNER
$DOCKER_IMAGES
INNER
  [ -n "$found" ] || die "未找到可用镜像: $short"
  log "OK: $short <- $found"
}

# 3) 用修复后的双循环解析真实 images.txt
MATCHED=0
while IFS='|' read -r file short; do
  case "$file" in ''|\#*) continue ;; esac
  [ -z "$short" ] && continue
  normalize_image "$short"
  MATCHED=$((MATCHED+1))
done < tests/.images_txt_real
echo "== 匹配成功 $MATCHED 个镜像 =="
[ "$MATCHED" -gt 0 ]

# 4) 注释行绝不能被当成短名处理(否则会重演 '统一短名' 报错)
BAD=0
while IFS='|' read -r file short; do
  case "$file" in ''|\#*) continue ;; esac
  [ -z "$short" ] && continue
  case "$short" in 统一短名*) BAD=$((BAD+1)) ;; esac
done < tests/.images_txt_real
[ "$BAD" -eq 0 ] && echo "== 注释行已正确跳过 =="
rm -f tests/.images_txt_real tests/.docker_images_mock
[ "$SMOKE_CREATED" -eq 1 ] && rm -rf dist/.smoke
exit 0

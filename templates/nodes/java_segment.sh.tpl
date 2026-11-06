# ---- Java 运行时 (Temurin JDK, 解压 @@JDIR@@/jdk<N>, 写 /etc/profile.d/java.sh) ----
JDIR=@@JDIR_Q@@
JAVA_VERSIONS=(@@JVERS@@)
JAVA_DEFAULT=@@JDEF@@
if [ ${#JAVA_VERSIONS[@]} -gt 0 ]; then
  log "安装 Java 运行时..."
  ( cd java 2>/dev/null && sha256sum -c jdk.sha256 --quiet ) || die "Java 包 sha256 校验失败 (java/jdk.sha256)"
  mkdir -p "$JDIR"
  for v in "${JAVA_VERSIONS[@]}"; do
    [ -f "./java/$v.tar.gz" ] || die "缺少 java/$v.tar.gz"
    dest="$JDIR/$v"
    if [ -x "$dest/bin/java" ]; then
      log "  $v 已安装, 跳过 ($("$dest/bin/java" -version 2>&1 | head -1))"
      continue
    fi
    log "  解压 $v -> $dest ..."
    rm -rf "$dest"; mkdir -p "$dest"
    tar -xzf "./java/$v.tar.gz" -C "$dest" --strip-components=1
    "$dest/bin/java" -version >/dev/null 2>&1 || die "$v 解压后无法运行 (架构与节点不符?)"
    log "  $v 就绪 ($("$dest/bin/java" -version 2>&1 | head -1))"
  done
  printf 'export JAVA_HOME=%s/%s\nexport PATH=$JAVA_HOME/bin:$PATH\n' "$JDIR" "$JAVA_DEFAULT" > /etc/profile.d/java.sh
  chmod 644 /etc/profile.d/java.sh
  export JAVA_HOME="$JDIR/$JAVA_DEFAULT"; export PATH="$JAVA_HOME/bin:$PATH"
  log "Java 默认版本 $JAVA_DEFAULT ($(java -version 2>&1 | head -1))"
fi

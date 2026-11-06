#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一条命令同步 docs/ 演示站和根目录的前端产物。

顺序不能调换:

1. `packer.py --dump-catalog docs/versions.json` 先刷新插件合并目录，它是 versions.js 的数据源。
2. `tools/update_pages.py` 同步 docs/logos.js 等静态文件。
3. `build_webui.py` 重新构建 packer.html 两份和 versions.js 两份，覆盖第 2 步可能拷入的旧产物。
4. `build_webui.py --check` 校验同步。

只跑 build_webui.py 会拿旧目录生成 versions.js；只跑 update_pages.py 会把根目录旧的
packer.html 拷进 docs/。改完前端统一用本脚本收口。第 2 步会重复 dump 一次 catalog，
结果幂等，暂时留着。

用法: python tools/sync_pages.py
"""
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PY = sys.executable

STEPS = [
    ("刷新合并服务目录", ["packer.py", "--dump-catalog", "docs/versions.json"]),
    ("同步 Pages 站点", ["tools/update_pages.py"]),
    ("构建 UI 双份 + versions.js", ["build_webui.py"]),
    ("同步门禁校验", ["build_webui.py", "--check"]),
]


def main(argv=None):
    if argv and ("--help" in argv or "-h" in argv):
        print(__doc__)
        return 0
    for label, args in STEPS:
        cmd = [PY] + [str(a) for a in args]
        print("[..] %s -> %s" % (label, " ".join(str(a) for a in args)))
        rc = subprocess.run(cmd, cwd=str(BASE)).returncode
        if rc != 0:
            print("[FAIL] %s 失败 (exit=%d), 中止后续步骤" % (label, rc))
            return rc or 1
        print("[OK] %s" % label)
    print("\n[OK] docs/ 站点与根目录产物已按单一入口同步完成")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

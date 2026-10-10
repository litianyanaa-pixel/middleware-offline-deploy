#!/usr/bin/env python3
"""同步 GitHub Pages 演示站 (docs/ 目录)。

Pages 站点源目录为 docs/, 需要 packer 前端三件套的副本:
  packer.html / packer_logos.js / versions.json
前端有改动后重跑本脚本, 再提交推送即可更新在线演示。

用法: python tools/update_pages.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DOCS = BASE / "docs"
# packer.html 引用的是相对路径 logos.js/versions.json
# 注意: docs/versions.json 必须是合并目录(内置+插件, packer.py --dump-catalog 生成);
# 根目录 versions.json 仅含内置服务, 直接拷贝会导致 Pages demo 丢失全部插件!
FILES = [
    ("packer.html", "packer.html"),
    ("packer_logos.js", "logos.js"),   # 本地由 packer.py 服务在 /logos.js, 静态站需同名副本
]

DOCS.mkdir(exist_ok=True)
for src, dst in FILES:
    shutil.copy2(BASE / src, DOCS / dst)
print("已同步到 docs/:", ", ".join(dst for _, dst in FILES))

# versions.json: dump 合并目录(内置+插件), 而非拷贝根目录纯内置版
r = subprocess.run([sys.executable, str(BASE / "packer.py"), "--dump-catalog",
                    str(DOCS / "versions.json")], cwd=BASE)
if r.returncode != 0:
    sys.exit("dump-catalog 失败, docs/versions.json 未更新(保留原文件)")
print("下一步: git add docs && git commit && git push")

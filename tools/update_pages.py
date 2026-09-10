#!/usr/bin/env python3
"""同步 GitHub Pages 演示站 (docs/ 目录)。

Pages 站点源目录为 docs/, 需要 packer 前端三件套的副本:
  packer.html / packer_logos.js / versions.json
前端有改动后重跑本脚本, 再提交推送即可更新在线演示。

用法: python tools/update_pages.py
"""
import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DOCS = BASE / "docs"
# (源文件, docs/ 内文件名): packer.html 引用的是相对路径 logos.js/versions.json
FILES = [
    ("packer.html", "packer.html"),
    ("packer_logos.js", "logos.js"),   # 本地由 packer.py 服务在 /logos.js, 静态站需同名副本
    ("versions.json", "versions.json"),
]

DOCS.mkdir(exist_ok=True)
for src, dst in FILES:
    shutil.copy2(BASE / src, DOCS / dst)
print("已同步到 docs/:", ", ".join(dst for _, dst in FILES))
print("下一步: git add docs && git commit && git push")

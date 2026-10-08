#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
packer.html 构建脚本: 由 webui/ 源文件(template.html + style.css + app.js)拼装单文件 UI。

为什么单文件: packer.html 需要离线双击/本地 HTTP 直接打开, 单文件是交付形态;
为什么拆源文件: UI 已 2500+ 行, 样式/脚本/结构分文件便于维护与 diff。
源文件里两行占位标记(/*__INLINE:style.css__*/ 与 //__INLINE:app.js__)
在构建时被替换为对应源文件内容。

用法:
  python build_webui.py            # 构建, 覆盖 packer.html 与 docs/packer.html
  python build_webui.py --check    # 只校验 webui/ 源与 packer.html 是否同步(不写文件)

改 UI 的流程: 改 webui/ 下源文件 -> python build_webui.py -> 提交(测试会拦截忘记构建)。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
WEBUI = BASE / "webui"
MARKERS = {
    "/*__INLINE:style.css__*/": WEBUI / "style.css",
    "//__INLINE:app.js__": WEBUI / "app.js",
}
TARGETS = [BASE / "packer.html", BASE / "docs" / "packer.html"]


def build_html():
    """拼装 packer.html 字节串; 标记行必须独占一行且与源文件一一对应"""
    tpl = (WEBUI / "template.html").read_bytes()
    out, hit = [], set()
    for line in tpl.splitlines(keepends=True):
        key = line.rstrip(b"\r\n").decode("utf-8")
        if key in MARKERS:
            out.append(MARKERS[key].read_bytes())
            hit.add(key)
        else:
            out.append(line)
    missed = set(MARKERS) - hit
    if missed:
        raise SystemExit("template.html 缺少占位标记: %s" % ", ".join(sorted(missed)))
    return b"".join(out)


def main():
    check_only = "--check" in sys.argv[1:]
    html = build_html()
    stale = [str(t.relative_to(BASE)) for t in TARGETS if t.read_bytes() != html]
    if check_only:
        if stale:
            print("不同步: %s (请运行 python build_webui.py)" % ", ".join(stale))
            return 1
        print("packer.html 与 webui/ 源文件同步")
        return 0
    for t in TARGETS:
        t.write_bytes(html)
        print("已写入 %s" % t.relative_to(BASE))
    return 0


if __name__ == "__main__":
    sys.exit(main())

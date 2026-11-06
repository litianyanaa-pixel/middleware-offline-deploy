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
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
WEBUI = BASE / "webui"
MARKERS = {
    "/*__INLINE:style.css__*/": WEBUI / "style.css",
    "//__INLINE:app.js__": WEBUI / "app.js",
}
TARGETS = [BASE / "packer.html", BASE / "docs" / "packer.html"]
VERSIONS_JS_TARGETS = [BASE / "versions.js", BASE / "docs" / "versions.js"]


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


def build_versions_js():
    """生成 versions.js: file:// 双击直开时 fetch 被浏览器拦截, 由 <script> 加载静态目录。
    数据源取 docs/versions.json(--dump-catalog 的插件合并产物, 测试校验其与插件同步)。"""
    src = BASE / "docs" / "versions.json"
    if not src.is_file():
        raise SystemExit("缺少 docs/versions.json, 无法生成 versions.js; "
                         "请先运行: python packer.py --dump-catalog docs/versions.json")
    data = json.loads(src.read_text(encoding="utf-8"))
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return ("/* 由 build_webui.py 自动生成 — 静态演示目录(file:// 双击兜底); "
            "改目录请跑 --dump-catalog 后重新构建 */\nwindow.PACKER_VERSIONS=" + body + ";\n").encode("utf-8")


def main():
    check_only = "--check" in sys.argv[1:]
    html = build_html()
    vjs = build_versions_js()
    stale = [str(t.relative_to(BASE)) for t in TARGETS if t.read_bytes() != html]
    stale += [str(t.relative_to(BASE)) for t in VERSIONS_JS_TARGETS
              if not t.exists() or t.read_bytes() != vjs]
    if check_only:
        if stale:
            print("不同步: %s (请运行 python build_webui.py)" % ", ".join(stale))
            return 1
        print("packer.html / versions.js 与 webui/ 源文件同步")
        return 0
    for t in TARGETS:
        t.write_bytes(html)
        print("已写入 %s" % t.relative_to(BASE))
    for t in VERSIONS_JS_TARGETS:
        t.write_bytes(vjs)
        print("已写入 %s" % t.relative_to(BASE))
    return 0


if __name__ == "__main__":
    sys.exit(main())

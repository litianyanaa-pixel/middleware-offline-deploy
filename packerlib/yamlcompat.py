# -*- coding: utf-8 -*-
"""YAML 兼容层。打包器要能在任意 Python 3.9+ 上跑，不能因为没装 PyYAML 就起不来。

以前 `validate.py` 在模块顶层 `import yaml` 校验 K8s CNI values，`compose.py` 和
`cluster.py` 还各挂着一个没用到的 `import yaml`，没装这个库的解释器会在导入阶段直接
闪退，命令行窗口一闪而过看不到报错。

装了 PyYAML 就用它做严格解析；没装就走本模块内置的 YAML 子集解析器，只用来判断
这段 values 覆盖是不是合法 mapping，覆盖 Helm values 的常见写法（嵌套 mapping / list /
标量 / 引号 / 注释 / 块标量 / flow 集合），不追求完整 YAML 1.2 语义。

业务代码统一调 safe_load()，不要直接 import yaml。回归测试看护这条约定。
"""
from __future__ import annotations

import re

try:
    import yaml as _pyyaml
except ImportError:  # 未安装 PyYAML 的正常路径
    _pyyaml = None


class YamlError(ValueError):
    """内置解析器发现语法问题(消息为简短英文技术描述, 由调用方包成双语错误)"""


# 支持带引号的 key; 值部分允许为空(后续缩进行是子块)
_KEY_RE = re.compile(r'^("(?:[^"\\]|\\.)*"|\'[^\']*\'|[A-Za-z0-9_.\-/]+)\s*:(?:\s+(.*))?$')


def _strip_comment(line):
    """去掉行尾注释(引号内的 # 不算注释)"""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote and (i == 0 or line[i - 1] != "\\"):
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i].rstrip()
    return line.rstrip()


def _balanced(s):
    """引号 / 方括号 / 花括号是否闭合"""
    stack, quote = [], None
    for i, ch in enumerate(s):
        if quote:
            if ch == quote and (i == 0 or s[i - 1] != "\\"):
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            stack.append(ch)
        elif ch in "]}":
            if not stack or (stack.pop(), ch) not in (("[", "]"), ("{", "}")):
                return False
    return not stack and quote is None


def _scalar(rest):
    """解析内联标量; 只做语法健全性检查, 数值/布尔一律按字符串处理"""
    if not rest:
        return None
    if not _balanced(rest):
        raise YamlError("unbalanced quotes or brackets: %s" % rest[:40])
    if rest[0] in "\"'":
        if len(rest) < 2 or rest[-1] != rest[0]:
            raise YamlError("unterminated quoted scalar: %s" % rest[:40])
        return rest[1:-1]
    if rest[0] in "]}":
        raise YamlError("unexpected closing bracket")
    return rest


def _prepared_lines(text):
    """-> [(indent, content)]; 丢弃空行/注释行/文档分隔符, 校验缩进字符"""
    out = []
    for raw in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if "\t" in raw[:len(raw) - len(raw.lstrip())]:
            raise YamlError("tab indentation is not allowed")
        stripped = _strip_comment(raw.strip())
        if not stripped or stripped in ("---", "..."):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        out.append((indent, stripped))
    return out


def _parse_block(lines, idx, indent):
    """解析同缩进层级的一个 mapping 或 list, 返回 (obj, next_idx)"""
    is_list = lines[idx][1] == "-" or lines[idx][1].startswith("- ")
    obj = [] if is_list else {}
    while idx < len(lines):
        ind, text = lines[idx]
        if ind < indent:
            break
        if ind > indent:
            raise YamlError("unexpected indentation")
        if is_list:
            if not (text == "-" or text.startswith("- ")):
                raise YamlError("mixed list and mapping at the same level")
            rest = text[1:].strip()
            idx += 1
            # "- key: value" 这类内联 mapping 的键同样要合法
            if rest and ":" in rest and not _KEY_RE.match(rest):
                raise YamlError("invalid list item: %s" % rest[:40])
            obj.append(_scalar(rest))
            continue
        m = _KEY_RE.match(text)
        if not m:
            raise YamlError("expected 'key: value', got: %s" % text[:40])
        key, rest = m.group(1), (m.group(2) or "").strip()
        idx += 1
        if rest in ("|", ">") or rest.startswith(("|", ">")) and rest[1:].isdigit():
            while idx < len(lines) and lines[idx][0] > ind:  # 块标量整体消费
                idx += 1
            obj[key] = ""
        elif not rest:
            if idx < len(lines) and lines[idx][0] > ind:
                child, idx = _parse_block(lines, idx, lines[idx][0])
                obj[key] = child
            else:
                obj[key] = None
        else:
            obj[key] = _scalar(rest)
            if idx < len(lines) and lines[idx][0] > ind:
                raise YamlError("unexpected indented block after scalar")
    return obj, idx


def _mini_safe_load(text):
    """内置 YAML 子集解析: 顶层必须是非空 mapping, 否则抛 YamlError"""
    lines = _prepared_lines(text)
    if not lines:
        raise YamlError("empty document")
    if lines[0][0] != 0:
        raise YamlError("top level must not be indented")
    obj, idx = _parse_block(lines, 0, 0)
    if idx != len(lines):
        raise YamlError("unexpected trailing content")
    if not isinstance(obj, dict) or not obj:
        raise YamlError("not a YAML mapping")
    return obj


def safe_load(text):
    """与 yaml.safe_load 同签名; 有 PyYAML 时严格解析, 否则降级到内置子集"""
    if _pyyaml is not None:
        return _pyyaml.safe_load(text)
    return _mini_safe_load(text)


def has_pyyaml():
    """给诊断/测试用: 当前解释器是否装了 PyYAML"""
    return _pyyaml is not None

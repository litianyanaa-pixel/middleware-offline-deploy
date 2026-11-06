#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中间件离线包本地打包器

用法:
  python packer.py                        # 启动本地 Web 界面(推荐)
  python packer.py --port 8765            # 指定端口
  python packer.py --config proj.json     # 命令行模式: 按配置文件直接打包
  python packer.py --example              # 生成配置文件样例 example-config.json

功能:
  - 读取 versions.json 物料目录, 校验离线包是否齐全
  - 根据页面选择生成 docker-compose.yml / .env / manifest.sh / images.txt
  - 自动从 nacos 镜像 tar 中提取建表 SQL 并附加建库语句(纯 python, 不依赖本机 docker)
  - 只把本次勾选的中间件镜像 + 对应架构的 docker/compose 安装包打进离线包
  - 产物: dist/<项目名>-<架构>-<日期>-offline.tar.gz

实现已按功能拆分到 packerlib/ 包, 本文件仅保留兼容入口
(旧命令与 `import packer` 用法不受影响); 各模块职责见 packerlib/__init__.py。
"""
import sys

try:
    from packerlib import *  # noqa: F401,F403
    from packerlib.cli import main  # noqa: F401
except ImportError as _exc:
    # 导入阶段就失败时给一句能看懂的话，别甩一串裸 traceback。
    # 这里 packerlib 还没加载完，取不了项目里的 tr()，按环境变量取半边就行。
    import os
    _en = (os.environ.get("DEPLOY_LANG") or os.environ.get("LANG") or "").lower().startswith("en")
    _name = _exc.name or str(_exc)

    def _say(zh, en):
        sys.stderr.write((en if _en else zh) + "\n")

    _say("打包器没起来：缺 %s。本项目只要 Python 3.9+ 标准库，完整副本还报这个错就提个 issue。" % _name,
         "Packer failed to start: missing %s. Only Python 3.9+ stdlib is needed; "
         "please open an issue if your checkout is complete." % _name)
    _say("如果命令一点输出都没有就退出了，多半是 python 指向了 Microsoft Store 的别名占位符，"
         "改用 start-packer.bat 或写解释器完整路径。",
         "If nothing was printed at all, python is probably the Microsoft Store alias stub; "
         "use start-packer.bat or the full interpreter path.")
    sys.exit(1)

if __name__ == "__main__":
    sys.exit(main())

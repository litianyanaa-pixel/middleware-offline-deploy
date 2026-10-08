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

from packerlib import *  # noqa: F401,F403
from packerlib.cli import main  # noqa: F401

if __name__ == "__main__":
    sys.exit(main())

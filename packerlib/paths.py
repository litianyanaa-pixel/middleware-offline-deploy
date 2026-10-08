# -*- coding: utf-8 -*-
"""仓库路径常量与根 logger(打包器全局共用)"""
import logging
from pathlib import Path


# packerlib/ 的上一级 = 仓库根(原 packer.py 与 versions.json/plugins/dist 同级)
BASE_DIR = Path(__file__).resolve().parent.parent

CATALOG_FILE = BASE_DIR / "versions.json"
PLUGINS_DIR = BASE_DIR / "plugins"
LOGOS_JS = BASE_DIR / "packer_logos.js"
SERVER_SH = BASE_DIR / "server" / "deploy.sh"
CLUSTER_SH = BASE_DIR / "server" / "deploy-cluster.sh"
HTML_FILE = BASE_DIR / "packer.html"
DIST_DIR = BASE_DIR / "dist"
SQL_DIR = BASE_DIR / "sql"
TPL_DIR = BASE_DIR / "templates"
WAREHOUSE = BASE_DIR / "warehouse"

log = logging.getLogger("packer")

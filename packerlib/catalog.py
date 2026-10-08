# -*- coding: utf-8 -*-
"""服务目录加载: versions.json + plugins/ 可插拔中间件合并"""
import importlib.util
import json

from .paths import CATALOG_FILE, PLUGINS_DIR, log


# 可插拔中间件插件表: {service_key: module}, 由 load_catalog() 填充

PLUGINS = {}


def _load_plugins():
    """加载 plugins/ 目录下的中间件插件; 下划线开头的文件视为模板不加载"""
    plugins = {}
    if not PLUGINS_DIR.is_dir():
        return plugins
    for f in sorted(PLUGINS_DIR.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location("mw_plugin_%s" % f.stem, f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:
            log.error("插件加载失败 %s: %s", f.name, e)
            continue
        missing = [a for a in ("SERVICE_KEY", "META", "compose_block") if not hasattr(mod, a)]
        if missing:
            log.error("插件 %s 缺少 %s, 已跳过", f.name, "/".join(missing))
            continue
        if mod.SERVICE_KEY in plugins:
            log.error("插件 %s 的 SERVICE_KEY %s 重名, 已跳过", f.name, mod.SERVICE_KEY)
            continue
        plugins[mod.SERVICE_KEY] = mod
        log.info("已加载中间件插件: %s (%s)", mod.SERVICE_KEY, mod.META.get("label", ""))
    return plugins


def load_catalog():
    with open(CATALOG_FILE, "r", encoding="utf-8") as f:
        catalog = json.load(f)
    # 合并插件(可插拔中间件): 服务/密钥字段与内置中间件完全同构
    # 原地更新: PLUGINS 以 from-import 方式被其他模块引用, 须保持同一对象
    plugins = _load_plugins()
    PLUGINS.clear()
    PLUGINS.update(plugins)
    for key, mod in PLUGINS.items():
        meta = dict(mod.META)
        meta["is_plugin"] = True
        catalog["services"][key] = meta
        for s in meta.get("secrets", []):
            catalog["secrets"].append(s)
    return catalog


# ProxySQL 读写分离代理(可选组件, 不进中间件网格)

PROXYSQL_META = {
    "image": "proxysql/proxysql", "tag": "2.6.6",
    "images": {"amd64": "warehouse/images/proxysql/2.6.6/amd64.tar",
               "arm64": "warehouse/images/proxysql/2.6.6/arm64.tar"},
}

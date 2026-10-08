# -*- coding: utf-8 -*-
"""从 nacos 镜像 tar 中提取建表 SQL 并附加建库语句(纯 python, 不依赖本机 docker)"""
import io
import json
import os
import re
import tarfile
import tempfile

from .paths import BASE_DIR, SQL_DIR, WAREHOUSE, log
from .util import PackError, bash_quote


# ---------------------------------------------------------------- nacos SQL 提取

def extract_nacos_sql_from_tar(tar_path):
    """从 docker save 的 OCI 归档里直接找出 mysql-schema.sql(不依赖 docker)"""
    candidates = ("mysql-schema.sql", "nacos-mysql.sql")
    with tarfile.open(tar_path, "r:*") as outer:
        # 外层 manifest.json -> 层列表
        try:
            mf_member = outer.extractfile("manifest.json")
        except KeyError:
            mf_member = None
        if mf_member is None:
            raise PackError("nacos 镜像 tar 缺少 manifest.json, 格式不受支持")
        manifests = json.loads(mf_member.read().decode("utf-8"))
        layers = []
        for m in manifests:
            layers.extend(m.get("Layers", []))
        if not layers:
            raise PackError("nacos 镜像 tar 中没有镜像层")
        for layer in layers:
            lf = outer.extractfile(layer)
            if lf is None:
                continue
            try:
                # 直接在(可 seek 的)成员流上打开嵌套 tar, 避免把整层读进内存
                with tarfile.open(fileobj=lf, mode="r:*") as lt:
                    for member in lt.getmembers():
                        if not member.isfile():
                            continue
                        base = member.name.rsplit("/", 1)[-1]
                        if base in candidates:
                            data = lt.extractfile(member).read()
                            log.info("从 %s 的 %s 中提取到 %s (%d bytes)",
                                     tar_path.name, layer[:20] + "...", member.name, len(data))
                            return data.decode("utf-8")
            except tarfile.ReadError:
                continue
    raise PackError("未能从 nacos 镜像中提取 mysql-schema.sql, 请手工导出后放到 warehouse/sql/nacos-mysql.sql")


def build_nacos_sql(cfg, catalog, prog=None):
    """返回最终 nacos.sql 内容(带建库语句); 原始 sql 缓存到 warehouse/sql/"""
    cache_dir = WAREHOUSE / "sql"
    cache = cache_dir / "nacos-mysql.sql"
    if cache.is_file():
        log.info("使用缓存的 nacos 建表 SQL: %s", cache)
        raw = cache.read_text(encoding="utf-8")
    else:
        arch = cfg["arch"]
        tar_path = BASE_DIR / catalog["services"]["nacos"]["images"][arch]
        log.info("首次打包: 从 %s 提取 nacos 建表 SQL(之后会缓存, 不再重复提取)", tar_path.name)
        if prog is not None:
            prog.stage("提取 nacos 建表 SQL(%s, 仅首次)" % tar_path.name)
        raw = extract_nacos_sql_from_tar(tar_path)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(raw, encoding="utf-8")
        log.info("nacos 建表 SQL 已缓存到 %s", cache)
    # 去掉源文件自带的建库/USE 语句, 统一由我们生成
    lines = [l for l in raw.splitlines()
             if not re.match(r"(?i)^\s*(CREATE\s+DATABASE|USE\s+`?nacos`?)", l)]
    header = ("CREATE DATABASE IF NOT EXISTS `nacos` DEFAULT CHARACTER SET utf8mb4 "
              "COLLATE utf8mb4_unicode_ci;\nUSE `nacos`;\n\n")
    return header + "\n".join(lines).strip() + "\n"

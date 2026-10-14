#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PackProgress 单元测试: 缓存命中统计 + 逐物料下载子进度(unit) + 生命周期语义"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from packerlib.progress import PackProgress  # noqa: E402


def test_cached_stats_accumulate_across_phases():
    p = PackProgress()
    p.start()
    p.cached_plan(5)          # 集群二进制
    p.cached_add("etcd/v3.5.6")
    p.cached_plan(2)          # chart
    p.cached_add("flannel chart")
    p.cached_plan(1)          # registry tar(未命中)
    p.cached_plan(3)          # 中间件镜像
    p.cached_add("redis:7")   # 同名重复登记只计一次
    p.cached_add("redis:7")
    s = p.snapshot()
    assert s["cached"]["total"] == 11
    assert s["cached"]["n"] == 3
    assert s["cached"]["names"] == ["etcd/v3.5.6", "flannel chart", "redis:7"]


def test_download_rows_units_and_lifecycle():
    p = PackProgress()
    p.download("etcd tgz", 100, 1000, "bytes")
    p.download("拉取镜像", 1, 2, "count")
    p.download("pause:3.9", 0, 1, "indeterminate")
    rows = {d["name"]: d for d in p.snapshot()["downloads"]}
    assert rows["etcd tgz"]["unit"] == "bytes" and rows["etcd tgz"]["done"] == 100
    assert rows["拉取镜像"]["unit"] == "count" and rows["拉取镜像"]["total"] == 2
    assert rows["pause:3.9"]["unit"] == "indeterminate"
    p.download_done("pause:3.9")
    assert "pause:3.9" not in {d["name"] for d in p.snapshot()["downloads"]}
    p.downloads_reset()
    assert p.snapshot()["downloads"] == []
    # 兼容旧形态 2 元素条目(若外部直构): unit 缺省 bytes
    p._downloads["legacy"] = [1, 2]
    assert p.snapshot()["downloads"][0]["unit"] == "bytes"


def test_status_vs_file_stage_semantics():
    p = PackProgress()
    p.stage("下载缺失集群物料")
    p.status("拉取镜像 3/38")     # 不得污染阶段标签(file() 会强改「压缩打包」)
    s = p.snapshot()
    assert s["stage"] == "下载缺失集群物料" and s["current"] == "拉取镜像 3/38"
    p.file("mysql8-amd64.tar", 100)
    assert p.snapshot()["stage"] == "压缩打包"


def test_begin_keeps_cached_clears_downloads():
    p = PackProgress()
    p.start()
    p.cached_plan(4)
    p.cached_add("etcd")
    p.download("helm tgz", 10, 100, "bytes")
    p.begin(999)   # pack() 在收集之后才 begin: 缓存统计要撑到打包完成
    s = p.snapshot()
    assert s["cached"] == {"n": 1, "total": 4, "names": ["etcd"]}
    assert s["downloads"] == []
    assert s["total"] == 999 and s["done"] == 0


def test_start_resets_between_runs():
    p = PackProgress()
    p.cached_plan(4)
    p.cached_add("etcd")
    p.download("x", 1, 2, "count")
    p.start()   # 新一轮打包: 全部清零
    s = p.snapshot()
    assert s["cached"] == {"n": 0, "total": 0, "names": []}
    assert s["downloads"] == []


def test_pending_and_downloaded_lifecycle():
    """待下载/已下载物料的登记与生命周期: 排队离队、begin 保留已下载清待下载、start 整轮重置"""
    p = PackProgress()
    p.start()
    p.pending_add("etcd tgz")
    p.pending_add("helm tgz")
    p.pending_remove("etcd tgz")   # 下载行出现即离队
    assert p.snapshot()["pending"] == ["helm tgz"]
    p.downloaded_add("helm tgz")
    p.pending_remove("helm tgz")
    s = p.snapshot()
    assert s["downloaded"] == {"n": 1, "names": ["helm tgz"]}
    assert s["pending"] == []
    p.pending_add("registry tar")
    p.begin(100)   # 收集结束后 begin: 已下载保留(显示到打包完成), 待下载清空(残留即失败项)
    s = p.snapshot()
    assert s["pending"] == [] and s["downloaded"]["n"] == 1
    p.downloads_reset()
    assert p.snapshot()["downloaded"]["n"] == 1   # downloads_reset 只清行/队列, 不清已下载登记
    p.start()      # 新一轮打包: 全部清零
    s = p.snapshot()
    assert s["downloaded"] == {"n": 0, "names": []} and s["pending"] == []


def test_snapshot_shape_stable():
    """Web 前端消费的字段不可悄悄改名/缺失"""
    p = PackProgress()
    s = p.snapshot()
    for key in ("running", "stage", "current", "total", "done", "percent", "speed",
                "elapsed", "eta", "result", "downloads", "cached", "downloaded",
                "pending", "error"):
        assert key in s, "snapshot 缺字段: %s" % key

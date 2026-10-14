# -*- coding: utf-8 -*-
"""打包进度回调: 控制台进度条与纯日志两种实现"""
import sys
import threading
import time

from .paths import log
from .util import PackError, tr


# ---------------------------------------------------------------- 打包进度

class PackProgress:
    """线程安全的打包进度(供 Web 轮询 / CLI 日志); 内部状态用 _ 前缀, 避免与方法重名"""

    def __init__(self):
        self._lock = threading.Lock()
        self._total = 0
        self._done = 0
        self._stage = "准备"
        self._current = ""
        self._cancelled = False
        self._running = False
        self._result = None
        self._error = None
        self._t0 = None
        self._t1 = None
        self._downloads = {}   # 组件下载进度: {显示名: [done, total, unit]}(打包时自动补料展示)
        self._cached = {}      # 仓库缓存命中名单(有序去重): warehouse 已有、跳过下载的物料
        self._cached_total = 0  # 本轮打包涉及的物料总数(各收集阶段 plan 事件累加)

    @property
    def running(self):
        return self._running

    def start(self):
        """打包线程启动即标记 running: begin() 之前的校验/物料收集阶段
        不再出现 running=False 的窗口(否则 Web 轮询会误判为已结束)"""
        with self._lock:
            self._running = True
            if self._t0 is None:
                self._t0 = time.time()
            self._downloads.clear()
            self._cached.clear()
            self._cached_total = 0

    def begin(self, total):
        with self._lock:
            self._total = total; self._done = 0
            self._running = True; self._cancelled = False
            self._result = None; self._error = None
            self._t0 = time.time(); self._t1 = None
            # 下载子进度此时应已随 done 事件清空, 兜底再清一次;
            # 缓存命中统计保留 —— 「已缓存 N/M」要一直显示到打包完成
            self._downloads.clear()

    def stage(self, s):
        with self._lock:
            self._stage = s

    def file(self, name, size):
        with self._lock:
            self._current = name; self._stage = "压缩打包"

    def status(self, name):
        """仅更新状态行文本, 不改阶段标签(下载/收集阶段用, 区别于 file() 的压缩打包语义)"""
        with self._lock:
            self._current = name

    # ---- 物料缓存命中(warehouse 已有则跳过下载) ----
    def cached_plan(self, total):
        """登记一个收集阶段的物料总数(多次调用累加: 集群二进制/chart/镜像包/中间件镜像/Java)"""
        with self._lock:
            self._cached_total += total

    def cached_add(self, name):
        """登记一个命中仓库缓存的物料(跳过下载)"""
        with self._lock:
            self._cached[name] = True

    # ---- 逐物料下载子进度 ----
    def download(self, name, done, total, unit="bytes"):
        """设置/更新一个物料的下载子进度; unit: bytes|count|indeterminate"""
        with self._lock:
            self._downloads[name] = [done, total, unit]

    def download_done(self, name):
        with self._lock:
            self._downloads.pop(name, None)

    def downloads_reset(self):
        with self._lock:
            self._downloads.clear()

    def add(self, n):
        with self._lock:
            self._done += n

    def set_total(self, total):
        with self._lock:
            self._total = total

    def cancel(self):
        with self._lock:
            self._cancelled = True

    def check(self):
        with self._lock:
            if self._cancelled:
                raise PackError("打包已取消")

    def finish(self, result):
        with self._lock:
            self._running = False; self._result = result; self._t1 = time.time()

    def fail(self, err):
        with self._lock:
            self._running = False; self._error = str(err); self._t1 = time.time()

    def snapshot(self):
        with self._lock:
            elapsed = (self._t1 or time.time()) - self._t0 if self._t0 else 0
            speed = self._done / elapsed if elapsed > 0.5 else 0
            return {
                "running": self._running,
                "stage": self._stage,
                "current": self._current,
                "total": self._total,
                "done": self._done,
                "percent": round(self._done * 100 / self._total) if self._total else 0,
                "speed": round(speed / 1048576, 1),
                "elapsed": round(elapsed),
                "eta": round((self._total - self._done) / speed) if speed > 1048576 else 0,
                "result": self._result,
                "downloads": [{"name": k, "done": v[0], "total": v[1], "unit": v[2] if len(v) > 2 else "bytes"}
                              for k, v in self._downloads.items()],
                "cached": {"n": len(self._cached), "total": self._cached_total,
                           "names": list(self._cached.keys())},
                "error": self._error,
            }


class LogProgress(PackProgress):
    """CLI 模式: 阶段/大文件变化时打日志"""

    def stage(self, s):
        super().stage(s)
        log.info("[进度] %s", s)

    def file(self, name, size):
        super().file(name, size)
        log.info("打包 %s (%d MB)", name, size // 1048576)

    def cached_add(self, name):
        super().cached_add(name)
        log.info("已缓存(跳过下载) %s", name)

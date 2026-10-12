# -*- coding: utf-8 -*-
"""pytest 全局钩子 — 修复 check() 风格测试的「虚假绿灯」(上线前全检 QA 发现的三层失守)。

tests/test_cluster_lifecycle.py 与 tests/test_offline_images.py 用 check() 记录断言结果
(仅打印+列表), 从不抛异常 —— pytest 判定「函数执行完没抛异常 = PASSED」, 失败被吞。
此钩子在**每个测试结束后**检查本模块 CHECKS 中新增部分, 有失败即让该测试变红;
只查新增区间, 兼容 CHECKS 跨用例累积的直跑(main())语义。
"""
import pytest


@pytest.fixture(autouse=True)
def _check_results_asserted(request):
    checks = getattr(request.module, "CHECKS", None)
    start = len(checks) if isinstance(checks, list) else 0
    yield
    if isinstance(checks, list) and len(checks) > start:
        failed = [name for name, ok in checks[start:] if not ok]
        if failed:
            raise AssertionError(
                "check() 断言失败(%d 项): %s" % (len(failed), failed))

# -*- coding: utf-8 -*-
"""前端视图 / 流程浏览器测试 (Playwright + 系统 Edge/Chrome)。

真实启动本地打包器 Web 服务, 用系统浏览器跑完整 5 步向导并截图核对视图。
浏览器用系统已装的 Edge/Chrome, 不需要 `playwright install` 下载浏览器。

运行:
  TMPDIR='E:\\tmp\\ui' TMP='E:\\tmp\\ui' TEMP='E:\\tmp\\ui' \\
    python -m pytest tests/test_ui_browser.py -q -s
截图目录: 环境变量 UI_SHOT_DIR, 缺省为系统临时目录/packer-ui-shots。
无 playwright 或找不到浏览器时自动 skip; 本机跑请务必把 TMPDIR 指到 E 盘。
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="未安装 playwright, 跳过浏览器测试")
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SHOT_DIR = Path(os.environ.get("UI_SHOT_DIR") or (Path(tempfile.gettempdir()) / "packer-ui-shots"))
BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def _browser_exe():
    for p in BROWSERS:
        if Path(p).is_file():
            return p
    pytest.skip("未找到系统 Edge/Chrome, 跳过浏览器测试")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def app_server():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "packer.py"), "--no-browser", "--port", str(port)],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/api/catalog", timeout=1).read()
                break
            except Exception:
                time.sleep(0.25)
        else:
            pytest.skip("本地打包器服务未能启动, 跳过浏览器测试")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def playwright_engine():
    with sync_playwright() as p:
        yield p


@pytest.fixture
def page(app_server, playwright_engine):
    browser = playwright_engine.chromium.launch(
        executable_path=_browser_exe(), headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
    pg.on("console", lambda m: errors.append("console: " + m.text) if m.type == "error" else None)
    pg.goto(app_server, wait_until="domcontentloaded", timeout=30000)
    pg.wait_for_selector(".wtab", timeout=15000)
    pg.wait_for_timeout(1200)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    yield pg, errors, ctx
    ctx.close()
    browser.close()


def _shot(pg, name):
    pg.screenshot(path=str(SHOT_DIR / name), full_page=False)


def _step(pg, n):
    pg.locator('button.wtab[data-v="%d"]' % n).click()
    pg.wait_for_timeout(600)


def test_ui_full_wizard_flow(page):
    """视图 + 流程: 5 步向导、服务卡片、长表单、密码批量、预览、配置导出"""
    pg, errors, ctx = page
    assert pg.locator(".wtab").count() == 5
    assert pg.locator("#statLine").count() == 1
    assert pg.locator("#statLine").inner_text().strip()
    _shot(pg, "01-basic.png")

    # 步骤 2: 套件轻分组 + 服务卡主选择单元 + 就绪度联动
    _step(pg, 2)
    assert pg.locator("#suiteBox .suite-group").count() >= 1, "套件应为轻分组"
    cards = pg.locator("#svcCats .svc")
    assert cards.count() >= 5
    before = pg.locator("#svcCats .svc.on").count()
    cards.first.click()
    pg.wait_for_timeout(400)
    assert pg.locator("#svcCats .svc.on").count() != before
    assert pg.locator("#statLine .rd-state").count() == 1, "底部状态条应给出就绪度"
    _shot(pg, "02-services.png")

    # 步骤 3: 长表单左侧粘性锚点 + 端口输入
    _step(pg, 3)
    assert pg.locator("#portBox .lf-nav").count() == 1, "端口页应有服务锚点导航"
    ports = pg.locator("#portBox input[data-port]")
    assert ports.count() >= 2
    _shot(pg, "03-ports.png")

    # 步骤 4: 批量随机 + 凭据导出(只下载, 不回显)
    _step(pg, 4)
    assert pg.locator("#btnGenAll").count() == 1 and pg.locator("#btnExportCreds").count() == 1
    pg.locator("#btnGenAll").click()
    pg.wait_for_timeout(400)
    with pg.expect_download(timeout=15000) as dl:
        pg.locator("#btnExportCreds").click()
    creds = json.loads(Path(dl.value.path()).read_text(encoding="utf-8"))
    assert "credentials" in creds and isinstance(creds["credentials"], list)
    _shot(pg, "04-secrets.png")

    # 步骤 5: 预览(不触发真实打包) — 代码块折叠工具 + 三级提示分组
    _step(pg, 5)
    pg.locator("#btnPreview").click()
    pg.wait_for_function("document.querySelector('#previewPre') && document.querySelector('#previewPre').innerText.length > 50", timeout=30000)
    assert pg.locator("#previewPre .code-tools").count() == 1, "代码块应有行号/折叠工具"
    assert pg.locator("#msg .sum-box").count() == 1, "提示应分错误/警告/信息三级"
    assert pg.locator("#step5Empty").is_hidden(), "预览生成后应收起空状态提示"
    pg.locator("#btnDiff").click()
    pg.wait_for_timeout(500)
    _shot(pg, "05-preview.png")

    # 导出配置: 必须带 schema_version(契约版本化)
    with pg.expect_download(timeout=15000) as dl2:
        pg.locator("#btnExport").click()
    cfg = json.loads(Path(dl2.value.path()).read_text(encoding="utf-8"))
    assert cfg.get("schema_version") == 1
    assert "Change_Me" not in pg.locator("body").inner_text(), "密码不得回显到页面"
    assert not errors, "页面存在 JS 错误: %s" % errors[:5]


def test_ui_readiness_reports_blockers(page):
    """制造端口冲突 -> 底部就绪度与步骤条应显示阻塞项"""
    pg, errors, ctx = page
    _step(pg, 3)
    ports = pg.locator("#portBox input[data-port]")
    if ports.count() >= 2:
        ports.nth(0).fill("18080")
        ports.nth(1).fill("18080")
        pg.wait_for_timeout(700)
        assert pg.locator("#statLine .rd-state.bad, #statLine .rd-chip.bad").count() >= 1, \
            "端口冲突应在就绪度里报阻塞"
        assert pg.locator(".wbadge.stepstat.bad").count() >= 1, "步骤条应有错误态"
        _shot(pg, "06-blocked.png")
    assert not errors, "页面存在 JS 错误: %s" % errors[:5]


def test_ui_i18n_switch(page):
    """中英切换: 关键导航文案应变为英文, 再切回中文"""
    pg, errors, ctx = page
    zh = pg.locator(".wtab").first.inner_text()
    pg.locator("#btnLang").click()
    pg.wait_for_timeout(900)
    en = pg.locator(".wtab").first.inner_text()
    assert en != zh and any(c.isalpha() and ord(c) < 128 for c in en), \
        "切换后步骤条应为英文: %r" % en
    _shot(pg, "07-en.png")
    pg.locator("#btnLang").click()
    pg.wait_for_timeout(900)
    assert pg.locator(".wtab").first.inner_text() == zh
    assert not errors, "页面存在 JS 错误: %s" % errors[:5]


def test_ui_dark_mode_and_responsive(app_server, playwright_engine):
    """深色模式 + 900px 断点响应式: 无横向溢出, 深色背景生效"""
    browser = playwright_engine.chromium.launch(
        executable_path=_browser_exe(), headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"])
    try:
        dark = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
        pg = dark.new_page()
        pg.goto(app_server, wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(1200)
        bg = pg.evaluate("getComputedStyle(document.body).backgroundColor")
        nums = [int(x) for x in bg.replace("rgba(", "").replace("rgb(", "").replace(")", "").split(",")[:3] if x.strip().isdigit()]
        assert nums and sum(nums) / len(nums) < 110, "深色模式背景应偏暗: %s" % bg
        repeat = pg.evaluate("getComputedStyle(document.querySelector('select')).backgroundRepeat")
        assert repeat == "no-repeat", "深色模式下拉箭头不得平铺: %s" % repeat
        _shot(pg, "08-dark.png")
        dark.close()

        small = browser.new_context(viewport={"width": 800, "height": 900})
        pg2 = small.new_page()
        pg2.goto(app_server, wait_until="domcontentloaded", timeout=30000)
        pg2.wait_for_timeout(1200)
        assert pg2.locator(".wtab").count() == 5
        overflow = pg2.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 30, "窄屏不应出现横向溢出: %s px" % overflow
        _shot(pg2, "09-narrow.png")
        small.close()
    finally:
        browser.close()


def test_ui_clear_button_always_visible(page):
    """清空选择按钮默认就在(禁用态), 选中后可用, 点击后真的清空"""
    pg, errors, ctx = page
    _step(pg, 2)
    clear = pg.locator("#catChips .chip-f.clear")
    assert clear.count() == 1, "清空选择按钮应始终存在, 不能等选中才出现"
    if pg.locator("#svcCats .svc.on").count():
        clear.click()          #  fixture 可能从 localStorage 恢复了上次的勾选
        pg.wait_for_timeout(300)
        clear = pg.locator("#catChips .chip-f.clear")
    assert clear.is_disabled(), "未选中时应为禁用态"
    pg.locator("#svcCats .svc").first.click()
    pg.wait_for_timeout(350)
    assert pg.locator("#svcCats .svc.on").count() >= 1
    assert not pg.locator("#catChips .chip-f.clear").is_disabled(), "有勾选时应可点击"
    pg.locator("#catChips .chip-f.clear").click()
    pg.wait_for_timeout(350)
    assert pg.locator("#svcCats .svc.on").count() == 0, "点击后应清空全部选择"
    assert pg.locator("#catChips .chip-f.clear").is_disabled()
    assert not errors, "页面存在 JS 错误: %s" % errors[:5]


def test_ui_k8s_missing_material_is_info_not_problem(page):
    """缺料会自动补齐 -> 信息级, 不报红色问题; 点开明细能定位到具体缺料项"""
    pg, errors, ctx = page
    # 固定成「kk 二进制缺失」场景, 不依赖本机物料是否恰好齐全
    pg.evaluate("""() => {
      if (typeof CATALOG !== 'undefined' && CATALOG.files_present)
        CATALOG.files_present['cluster_kk_amd64'] = false;
      if (typeof state !== 'undefined'){
        state.cluster.enabled = true;
        state.cluster.roles = {'0': 'control-plane'};   // 排除配置类提醒, 只留缺料
        state.servers = [{name:'node1', user:'root', ip:'10.0.0.1', ssh:22, pass:''}];
      }
      if (typeof renderClusterCard === 'function') renderClusterCard();
      if (typeof refreshReadiness === 'function') refreshReadiness();
    }""")
    pg.wait_for_timeout(500)
    badge1 = pg.locator("#stb1")
    cls = (badge1.get_attribute("class") or "").split()
    assert "bad" not in cls and "notice" not in cls, \
        "单纯缺料不该有红色/黄色徽标: %r %r" % (badge1.inner_text(), cls)
    assert "问题" not in badge1.inner_text(), "单纯缺料不该报『问题』: %r" % badge1.inner_text()
    assert pg.locator("#statLine .rd-state.ok").count() == 1, "缺料不应阻断打包"
    info = pg.locator("#statLine .rd-chip.info")
    assert info.count() == 1, "应以信息级 chip 告知会自动补齐"
    assert "自动补齐" in info.inner_text()

    info.click()
    pg.wait_for_timeout(350)
    panel = pg.locator("#rdPanel")
    assert panel.is_visible(), "点击 chip 应展开可核对的明细面板"
    assert panel.locator(".rd-item").count() >= 1
    assert panel.locator(".rd-tag.info").count() >= 1, "明细需标注『自动补齐』而非阻断"
    assert "kk 二进制" in panel.inner_text(), "明细应写明具体缺什么"
    _shot(pg, "10-k8s-info-panel.png")

    panel.locator(".rd-item").first.click()
    pg.wait_for_timeout(450)
    assert pg.locator("#k8sMatBadge.rd-target").count() == 1, "定位应高亮卡片上的缺料徽章"
    # 缺料说明只在徽章里出现一次, 不再重复一条信息条
    assert pg.locator("#k8sSvcCard .warnline.is-info").count() == 0, "卡片内不该再重复缺料说明"
    assert pg.locator("#k8sSvcCard .warnline").is_hidden(), "仅 ARM64 阻断时才显示 warnline"
    card_text = pg.locator("#k8sSvcCard").inner_text()
    assert card_text.count("kk 二进制") == 1, "同一缺料信息在卡片内只能出现一次: %r" % card_text
    _shot(pg, "11-k8s-info-located.png")

    # 步骤徽标本身也可点开明细, hover title 已列出具体条目
    badge1.click()
    pg.wait_for_timeout(300)
    assert pg.locator("#rdPanel").is_visible()
    assert not errors, "页面存在 JS 错误: %s" % errors[:5]


# ---------------------------------------------------------------- 方案 A: Docker 引导弹窗
# 用请求拦截构造「需要/不需要本机 Docker」的预览与环境探测结果, 不触碰真实打包线程。
DOCKER_PREVIEW_NEED = {
    "compose": "", "env": "",
    "warnings": ["物料缺失(打包时缺失镜像将自动拉取, 需打包机 Docker 可用): nginx:1.25"
                 "|Missing materials (missing image tars auto-pull at pack time, Docker required): nginx:1.25"],
    "materials": {"rows": [
        {"group": "中间件镜像", "name": "nginx:1.25", "cached": False,
         "sources": ["docker pull(国内源候选, 需打包机 Docker)"], "note": ""}],
        "cached_n": 0, "total": 1},
}
DOCKER_PREVIEW_CLEAN = {
    "compose": "", "env": "", "warnings": [],
    "materials": {"rows": [
        {"group": "中间件镜像", "name": "nginx:1.25", "cached": True,
         "sources": [], "note": ""}], "cached_n": 1, "total": 1},
}
DOCKER_ENV_MISSING = {"platform": "Windows", "docker": {
    "found": False, "path": "", "version": "", "compose": False, "daemon": False}}
DOCKER_ENV_READY = {"platform": "Windows", "docker": {
    "found": True, "path": "C:/Program Files/Docker/docker.exe",
    "version": "Docker version 26.1.4", "compose": True, "daemon": True}}
DOCKER_PROGRESS_DONE = {
    "running": False, "percent": 100, "done": 1048576, "total": 1048576,
    "elapsed": 1, "speed": 0, "downloads": [], "stage": "", "current": "",
    "result": {"path": "mock.tar.gz", "size_mb": 1, "images": 0,
               "sha256": "", "warnings": []},
}


def _docker_routes(pg, holder):
    """holder: {'preview':..., 'env':..., 'calls': {'env':0,'pack':0}}

    全部返回 200(400 会被浏览器记成 console error); /api/pack 只计次,
    /api/progress 立刻给完成态, 不启动真实打包线程也不留轮询定时器。
    """
    def handler(route):
        url = route.request.url
        if url.endswith("/api/env"):
            holder["calls"]["env"] += 1
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(holder["env"], ensure_ascii=False))
        elif url.endswith("/api/preview"):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(holder["preview"], ensure_ascii=False))
        elif url.endswith("/api/pack"):
            holder["calls"]["pack"] += 1
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True}))
        elif url.endswith("/api/progress"):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(DOCKER_PROGRESS_DONE))
        else:
            route.continue_()
    pg.route("**/api/*", handler)


def _reset_pack_modal(pg):
    """上一轮打包完成会留下遮罩, 复位后再点「开始打包」才能命中按钮"""
    pg.evaluate("""() => {
      if (typeof pollTimer !== 'undefined' && pollTimer){ clearInterval(pollTimer); pollTimer = null; }
      const m = document.querySelector('#modal'); if (m) m.classList.add('hidden');
      if (typeof msg === 'function') msg('');
    }""")


def test_ui_docker_guide_blocks_pack_until_choice(page):
    """视图+流程: 需要本机 Docker 时弹窗; 取消不打包, 跳过并继续才放行"""
    pg, errors, ctx = page
    holder = {"preview": DOCKER_PREVIEW_NEED, "env": DOCKER_ENV_MISSING,
              "calls": {"env": 0, "pack": 0}}
    _docker_routes(pg, holder)
    _step(pg, 5)
    dlg = pg.locator("#dockerHelp")

    pg.locator("#btnPack").click()
    dlg.wait_for(state="visible", timeout=10000)
    box = dlg.bounding_box()
    assert box and box["x"] > 150 and box["y"] > 20, \
        "弹窗应居中显示, 不能贴左上角: %r" % (box,)
    assert "未找到 docker" in pg.locator("#dhDetect").inner_text(), "应展示只读检测结果"
    assert pg.locator("#dhPlatforms .dh-plat").count() == 3, "应给出三平台安装方式"
    assert pg.locator("#dhPlatforms .dh-plat.folded").count() == 2, \
        "非当前平台应折叠成标题, 三个平台一屏可见"
    assert "当前系统" in pg.locator("#dhPlatforms").inner_text(), "应标出当前平台"
    assert "winget" in pg.locator("#dhPlatforms").inner_text(), "应给出可复制的安装命令"
    pg.locator('[data-dh-fold="Darwin"]').click()
    pg.wait_for_timeout(200)
    assert pg.locator('[data-dh-plat="Darwin"] .dh-cmd').is_visible(), "点标题应展开该平台命令"
    pg.locator('[data-dh-fold="Darwin"]').click()
    pg.wait_for_timeout(200)
    assert "不会替你安装" in pg.locator("#dhNote").inner_text(), "须明确声明不自动安装"
    dbox = dlg.bounding_box()
    for sel in ("#dhNote", "#dhMute"):
        bb = pg.locator(sel).bounding_box()
        assert bb and bb["y"] + bb["height"] <= dbox["y"] + dbox["height"] + 2, \
            "%s 应固定在弹窗可见区, 不能藏在滚动列表下面" % sel
    assert holder["calls"]["pack"] == 0, "弹窗期间不得开始打包"
    _shot(pg, "12-docker-guide.png")

    pg.locator("#dhCancel").click()
    pg.wait_for_timeout(300)
    assert not dlg.is_visible(), "取消应关闭弹窗"
    assert holder["calls"]["pack"] == 0, "取消后不得继续打包"

    pg.locator("#btnPack").click()
    dlg.wait_for(state="visible", timeout=10000)
    pg.locator("#dhSkip").click()
    pg.wait_for_timeout(500)
    assert not dlg.is_visible()
    assert holder["calls"]["pack"] == 1, "「跳过并继续」应放行打包"
    assert not errors, "页面存在 JS 错误: %s" % errors[:5]


def test_ui_docker_guide_copy_mute_recheck(page):
    """复制命令 / 静音可撤销 / Docker 就绪后重新检测自动放行 / 深色视图"""
    pg, errors, ctx = page
    holder = {"preview": DOCKER_PREVIEW_NEED, "env": DOCKER_ENV_MISSING,
              "calls": {"env": 0, "pack": 0}}
    _docker_routes(pg, holder)
    ctx.grant_permissions(["clipboard-read", "clipboard-write"])
    pg.emulate_media(color_scheme="dark")

    pg.evaluate("""(p) => {
      window.__dhDone = null;
      ensureDockerReady(p).then(v => { window.__dhDone = v; });
    }""", DOCKER_PREVIEW_NEED)
    dlg = pg.locator("#dockerHelp")
    dlg.wait_for(state="visible", timeout=10000)

    # 复制按钮真的把命令写进剪贴板, 而不是执行它
    pg.locator('[data-dh-copy="Windows"]').click()
    pg.wait_for_timeout(300)
    clip = pg.evaluate("navigator.clipboard.readText()")
    assert "winget" in clip and "Docker.DockerDesktop" in clip, "复制内容应为安装命令: %r" % clip
    _shot(pg, "13-docker-dark.png")

    # 「本次不再提示」勾选后要能撤销(曾出现取消勾选仍静音的 bug)
    pg.locator("#dhMute").check()
    assert pg.evaluate("sessionStorage.getItem('packer-docker-help-muted')") == "1"
    pg.locator("#dhMute").uncheck()
    assert pg.evaluate("sessionStorage.getItem('packer-docker-help-muted')") is None, \
        "取消勾选应撤销静音"
    pg.locator("#dhMute").check()
    pg.locator("#dhSkip").click()
    pg.wait_for_timeout(300)
    assert pg.evaluate("window.__dhDone") is True, "跳过应放行"
    n = holder["calls"]["env"]
    assert pg.evaluate("async (p) => await ensureDockerReady(p)", DOCKER_PREVIEW_NEED) is True, \
        "静音后应完全无感"
    assert holder["calls"]["env"] == n, "静音后不应再请求 /api/env"

    # 重新检测: 环境变为就绪时自动关闭并放行
    pg.evaluate("sessionStorage.removeItem('packer-docker-help-muted')")
    holder["env"] = DOCKER_ENV_READY
    pg.evaluate("""(p) => {
      window.__dhDone2 = null;
      ensureDockerReady(p).then(v => { window.__dhDone2 = v; });
    }""", DOCKER_PREVIEW_NEED)
    # 首次探测已是 ready -> 不该弹窗, 直接放行
    pg.wait_for_timeout(400)
    assert not dlg.is_visible(), "Docker 已就绪不该弹窗"
    assert pg.evaluate("window.__dhDone2") is True

    # 手动打开弹窗核对「守护进程已就绪」这条正向检测结果
    pg.evaluate("""(env) => { showDockerHelp(env); }""", DOCKER_ENV_READY)
    dlg.wait_for(state="visible", timeout=5000)
    assert "守护进程已就绪" in pg.locator("#dhDetect").inner_text(), \
        "就绪时应显示正向检测结果, 不能固定显示守护进程未运行"
    assert pg.locator("#dhDetect .dh-ok").count() == 1
    pg.locator("#dhClose").click()
    assert not errors, "页面存在 JS 错误: %s" % errors[:5]


def test_ui_docker_guide_does_not_disturb(page):
    """不需要本机 Docker / Docker 已就绪时完全无感: 不弹窗、不探测、直接打包"""
    pg, errors, ctx = page
    holder = {"preview": DOCKER_PREVIEW_CLEAN, "env": DOCKER_ENV_MISSING,
              "calls": {"env": 0, "pack": 0}}
    _docker_routes(pg, holder)
    _step(pg, 5)
    pg.locator("#btnPack").click()
    pg.wait_for_timeout(900)
    assert not pg.locator("#dockerHelp").is_visible(), "物料齐全不该弹 Docker 引导"
    assert holder["calls"]["env"] == 0, "不需要 Docker 时连探测都不该发, 避免打扰"
    assert holder["calls"]["pack"] == 1, "应直接开始打包"
    assert pg.locator("#dockerHint").is_hidden(), "预览提示条不该出现"

    # 需要 Docker 但守护进程可用 -> 探测一次, 不弹窗
    _reset_pack_modal(pg)
    holder["preview"] = DOCKER_PREVIEW_NEED
    holder["env"] = DOCKER_ENV_READY
    pg.locator("#btnPack").click()
    pg.wait_for_timeout(900)
    assert not pg.locator("#dockerHelp").is_visible()
    assert holder["calls"]["env"] == 1
    assert holder["calls"]["pack"] == 2
    assert not errors, "页面存在 JS 错误: %s" % errors[:5]


# ---------------------------------------------------------------- 交互走查回归
CJK = re.compile(r"[\u4e00-\u9fff]")


def test_ui_english_no_chinese_residue(page):
    """英文模式下动态渲染的物料抽屉 / Java 卡片不得残留中文或半吊子翻译"""
    pg, errors, ctx = page
    pg.locator("#btnLang").click()
    pg.wait_for_timeout(1000)
    # textContent 覆盖折叠区与隐藏抽屉; 必须排除 <script>, 否则会读进源码里的双语词条
    txt = pg.evaluate("""() => {
      const c = document.body.cloneNode(true);
      c.querySelectorAll('script,style,noscript,template').forEach(n => n.remove());
      return c.textContent;
    }""")
    assert "按台分发" not in txt, "Java 卡片 JDK 说明未翻译"
    assert "runtime包" not in txt, "物料抽屉出现半吊子翻译(runtime包)"
    assert "IP未填" not in txt
    pg.locator("#btnWh").click()
    pg.wait_for_timeout(500)
    rows = pg.locator("#whBody tr")
    assert rows.count() > 20, "物料抽屉应列出物料"
    bad = [t for t in rows.all_inner_texts() if CJK.search(t)]
    assert not bad, "英文模式物料抽屉残留中文: %s" % bad[:3]
    assert "Java runtime bundle" in pg.locator("#whBody").inner_text()
    assert not errors, "页面存在 JS 错误: %s" % errors[:5]


def test_ui_step5_empty_state_matches_context(page):
    """空态文案随状态变化; 预览失败给明确引导而不是一屏空白"""
    pg, errors, ctx = page
    # 1) 未选组件
    _step(pg, 2)
    clr = pg.locator("#catChips .chip-f.clear")
    if not clr.is_disabled():
        clr.click()
        pg.wait_for_timeout(300)
    _step(pg, 5)
    assert "请先到" in pg.locator("#step5EmptyText").inner_text(), \
        "未选组件时应引导去步骤2, 而不是让用户点不可用的预览按钮"
    assert pg.locator("#btnPreview").is_disabled()

    # 2) 已选组件但尚未预览
    _step(pg, 2)
    pg.locator("#svcCats .svc").first.click()
    pg.wait_for_timeout(300)
    _step(pg, 5)
    assert "已选 1 项" in pg.locator("#step5EmptyText").inner_text()

    # 3) 端口非法 -> 预览失败
    _step(pg, 3)
    ports = pg.locator("#portBox input[data-port]")
    assert ports.count() >= 1
    ports.first.fill("0")
    pg.wait_for_timeout(300)
    _step(pg, 5)
    pg.locator("#btnPreview").click()
    pg.wait_for_timeout(2200)
    msgtext = pg.locator("#msg").inner_text()
    assert "预览未通过" in msgtext, "预览失败应给出明确标题: %r" % msgtext
    assert "超出允许端口范围 1-65535" in msgtext, "应透出后端校验原因"
    assert "端口 HTTP 端口" not in msgtext, "端口号文案不得重复拼接"
    assert pg.locator("#step5Empty").is_visible(), "失败时不能只留一屏空白"
    assert "上次预览未通过" in pg.locator("#step5EmptyText").inner_text(), \
        "失败态应引导修正后重试, 而不是提示勾选组件"
    assert pg.locator("#previewCard").is_hidden()
    assert pg.locator("#step5Fix").is_visible(), "失败态应给「去修正配置」入口"
    _shot(pg, "14-preview-failed.png")
    pg.locator("#step5Fix").click()
    pg.wait_for_timeout(500)
    assert pg.locator("#rdPanel").is_visible(), "点击应展开可读的问题明细"
    assert pg.locator('button.wtab[data-v="3"].on').count() == 1, "应跳到出问题的步骤"

    # 4) 修正后重新预览 -> 恢复
    _step(pg, 3)
    pg.locator("#portBox input[data-port]").first.fill("80")
    pg.wait_for_timeout(300)
    _step(pg, 5)
    pg.locator("#btnPreview").click()
    pg.wait_for_function("document.querySelector('#previewCard').style.display !== 'none'",
                         timeout=30000)
    assert pg.locator("#step5Empty").is_hidden(), "预览成功后应收起空态"
    assert "预览未通过" not in pg.locator("#msg").inner_text(), "成功后应清掉失败提示"
    _shot(pg, "15-preview-recovered.png")
    # 后端对非法端口返回 400 会被浏览器记成资源错误, 属预期; 其余 JS 错误不接受
    real = [e for e in errors if "400 (Bad Request)" not in e]
    assert not real, "页面存在 JS 错误: %s" % real[:5]


def test_ui_blank_extra_port_does_not_block_preview(page):
    """点「+」后还没填的空白自定义端口行, 不该让预览直接 400"""
    pg, errors, ctx = page
    _step(pg, 3)
    add = pg.locator("#portBox .addport")
    assert add.count() >= 1
    add.first.click()
    pg.wait_for_timeout(350)
    assert pg.locator("#portBox .map-row.extra").count() >= 1, "应新增一行待填的自定义映射"

    # 导出配置不应带出空白行
    with pg.expect_download(timeout=10000) as dl:
        pg.locator("#btnExport").click()
    cfg = json.loads(Path(dl.value.path()).read_text(encoding="utf-8"))
    blank = [(k, r) for k, rows in (cfg.get("extra_ports") or {}).items() for r in rows
             if not str(r.get("host", "")).strip() and not str(r.get("container", "")).strip()]
    assert not blank, "导出的配置含空白占位行: %s" % blank[:2]

    _step(pg, 5)
    pg.locator("#btnPreview").click()
    pg.wait_for_function("document.querySelector('#previewCard').style.display !== 'none'",
                         timeout=30000)
    assert "预览未通过" not in pg.locator("#msg").inner_text(), \
        "空白占位行不应阻断预览: %s" % pg.locator("#msg").inner_text()[:200]
    assert not errors, "页面存在 JS 错误: %s" % errors[:5]

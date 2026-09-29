# -*- coding: utf-8 -*-
"""
用 Edge headless + CDP 重新截取 README 用的 UI 截图 (docs/images/ui-*.png)

前置: packer.py 已在某端口运行 (默认 8766)
运行: python tools/update_readme_shots.py [port]
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websocket

# 本机调试端口必须绕过系统代理, 否则 urllib 会把 127.0.0.1 请求发给代理
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"
_proxy_handler = urllib.request.ProxyHandler({})
_opener = urllib.request.build_opener(_proxy_handler)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
PORT = sys.argv[1] if len(sys.argv) > 1 else "8766"
BASE = "http://127.0.0.1:%s" % PORT
CDP_PORT = 9223

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def start_edge():
    exe = next((p for p in EDGE_CANDIDATES if Path(p).is_file()), None)
    if not exe:
        raise SystemExit("未找到 Edge, 请把 msedge.exe 路径加入 EDGE_CANDIDATES")
    return subprocess.Popen([
        exe,
        "--headless=new",
        "--remote-debugging-port=%d" % CDP_PORT,
        "--window-size=1500,1040",
        "--disable-gpu",
        "--no-first-run",
        "--no-proxy-server",
        "--user-data-dir=%s" % (ROOT / ".workbuddy" / "tmp-edge-profile"),
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cdp_connect():
    for _ in range(30):
        try:
            with _opener.open("http://127.0.0.1:%d/json" % CDP_PORT, timeout=2) as f:
                tabs = json.load(f)
            page = next(t for t in tabs if t["type"] == "page")
            # DevTools (Chrome 111+) 拒绝带 Origin 的 WS 握手, suppress_origin 绕过
            ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=30,
                                             suppress_origin=True)
            return ws
        except Exception:
            time.sleep(1)
    raise SystemExit("CDP 连接失败")


class Cdp:
    def __init__(self, ws):
        self.ws, self.mid = ws, 0

    def send(self, method, **params):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.mid:
                if "error" in msg:
                    raise RuntimeError("%s: %s" % (method, msg["error"]))
                return msg.get("result", {})

    def eval_js(self, expr, await_promise=False):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=await_promise)
        if r.get("exceptionDetails"):
            raise RuntimeError("JS 异常: %s" % json.dumps(r["exceptionDetails"])[:500])
        return r.get("result", {}).get("value")

    def shot(self, path):
        data = self.send("Page.captureScreenshot", format="png")
        Path(path).write_bytes(base64.b64decode(data["data"]))
        print("[shot] %s" % Path(path).name)


PICK = ["nginx", "mysql8", "postgres", "mongodb", "redis",
        "node-exporter", "prometheus", "grafana"]


def pick_services(c):
    """选择有代表性的服务组合 (避免重复添加)"""
    c.eval_js("""
      (function(){
        [%s].forEach(function(k){
          var el = document.querySelector('.svc[data-key="'+k+'"]');
          if (el && !el.classList.contains('on')) el.click();
        });
      })()
    """ % ",".join('"%s"' % k for k in PICK))
    time.sleep(0.6)


def goto(c, step):
    """通过点击向导 tab 切换步骤 (函数在 IIFE 内, 不能直接调用)"""
    c.eval_js("""
      (function(){
        var b = document.querySelector('.wtab[data-v="%s"]');
        if (b) b.click();
        window.scrollTo(0,0);
      })()
    """ % step)
    time.sleep(0.6)


def main():
    try:
        ws = cdp_connect()   # 复用已在运行的 Edge 实例
        edge = None
    except SystemExit:
        edge = start_edge()
        ws = cdp_connect()
    try:
        c = Cdp(ws)
        c.send("Page.enable")
        c.send("Emulation.setDeviceMetricsOverride", width=1500, height=1040,
               deviceScaleFactor=2, mobile=False)   # 2x 高清
        c.send("Page.navigate", url=BASE)
        time.sleep(4)   # 等 catalog fetch + 渲染

        pick_services(c)

        # 1. 基础设置 + 选择中间件: step1 基础设置, step2 中间件网格
        goto(c, "2")
        c.eval_js("document.getElementById('sec-svc').scrollIntoView()")
        time.sleep(0.4)
        c.shot(OUT / "ui-basic.png")

        # 2. 端口映射 (step 3 上半)
        goto(c, "3")
        c.eval_js("document.getElementById('sec-port').scrollIntoView()")
        time.sleep(0.4)
        c.shot(OUT / "ui-port.png")

        # 3. 反向代理向导 (step 3 下半)
        c.eval_js("document.getElementById('sec-proxy').scrollIntoView()")
        time.sleep(0.4)
        c.shot(OUT / "ui-proxy.png")

        # 4. 账号密码 (step 4 上半)
        goto(c, "4")
        c.eval_js("document.getElementById('sec-pwd').scrollIntoView()")
        time.sleep(0.4)
        c.shot(OUT / "ui-secrets.png")

        # 5. 数据库来源 + 备份 (step 4 下半)
        c.eval_js("document.getElementById('sec-db').scrollIntoView()")
        time.sleep(0.4)
        c.shot(OUT / "ui-db-backup.png")

        # 6. 物料仓库抽屉
        c.eval_js("document.getElementById('btnWh').click(); window.scrollTo(0,0)")
        time.sleep(1.0)
        c.shot(OUT / "ui-warehouse.png")
        c.eval_js("document.querySelector('[data-dwclose]').click()")

        # 7. 生成预览 (step 5): 点击预览按钮, 触发 gotoStep(5)+doPreview()
        c.eval_js("document.getElementById('btnPreview').click()")
        time.sleep(2.0)
        c.shot(OUT / "ui-preview.png")

        print("全部截图完成 ->", OUT)
    finally:
        if edge:
            edge.terminate()


if __name__ == "__main__":
    main()

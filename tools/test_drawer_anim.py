# -*- coding: utf-8 -*-
"""验证物料仓库/历史产物抽屉的出入场动画: 点击打开截入场帧, 关闭截出场帧"""
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

import websocket  # noqa: E402

CDP_PORT = 9223
_opener = urlopen

from update_readme_shots import Cdp, cdp_connect, start_edge  # noqa: E402

OUT = ROOT / ".workbuddy"
OUT.mkdir(exist_ok=True)


def wait_visible(c, sel, timeout=8000):
    c.eval_js("""
    (async () => {
      const t0 = Date.now();
      while (Date.now() - t0 < %d) {
        const el = document.querySelector('%s');
        if (el && !el.classList.contains('hidden')) return true;
        await new Promise(r => setTimeout(r, 60));
      }
      return false;
    })()
    """ % (timeout, sel), await_promise=True)


def main():
    proc = start_edge()
    try:
        c = Cdp(cdp_connect())
        c.send("Page.enable")
        c.send("Page.navigate", url="http://127.0.0.1:8766/")
        time.sleep(3)

        ok = True

        # --- 物料仓库: 打开 -> 入场帧 -> 关闭 -> 出场帧 ---
        c.eval_js("document.getElementById('btnWh').click()")
        time.sleep(0.12)   # 动画进行中(0.38s)
        c.shot(OUT / "dw_enter.png")
        state1 = c.eval_js(
            "(()=>{const w=document.getElementById('dw-wh');"
            "return w.classList.contains('hidden')||w.classList.contains('closing');})()")
        wait_visible(c, "#dw-wh")

        c.eval_js("closeDrawers()")
        mid = c.eval_js(
            "(()=>{const w=document.getElementById('dw-wh');"
            "return w.classList.contains('closing') && !w.classList.contains('hidden');})()")
        c.shot(OUT / "dw_exit.png")   # 截图耗时较长, 状态检查先行
        time.sleep(0.5)    # 动画结束后应彻底 hidden
        done = c.eval_js(
            "(()=>{const w=document.getElementById('dw-wh');"
            "return w.classList.contains('hidden') && !w.classList.contains('closing');})()")
        print("open-state OK:", not state1, "| closing-mid OK:", mid, "| hidden-after OK:", done)
        ok = not state1 and mid and done

        # --- 历史产物: 快速开关不残留 closing 状态 ---
        c.eval_js("document.getElementById('btnOut').click()")
        wait_visible(c, "#dw-out")
        c.eval_js("document.getElementById('btnOut').click()")  # 立即切换到 out(重开)
        reopen = c.eval_js(
            "(()=>{const o=document.getElementById('dw-out'),h=document.getElementById('dw-wh');"
            "return !o.classList.contains('hidden') && !o.classList.contains('closing')"
            " && h.classList.contains('hidden');})()")
        print("quick-reopen OK:", reopen)
        ok = ok and reopen
        c.shot(OUT / "dw_out_open.png")

        # Esc 关闭
        c.eval_js("document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape'}))")
        time.sleep(0.5)
        esc = c.eval_js("document.getElementById('dw-out').classList.contains('hidden')")
        print("esc-close OK:", esc)
        ok = ok and esc

        print("ALL PASS" if ok else "SOME FAILED")
        sys.exit(0 if ok else 1)
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()

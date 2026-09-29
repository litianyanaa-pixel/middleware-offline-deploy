# -*- coding: utf-8 -*-
"""截图验证部署服务器池排版: 多行节点 + 密码列 + 添加按钮"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
OUT = ROOT / ".workbuddy"
OUT.mkdir(exist_ok=True)

from update_readme_shots import Cdp, cdp_connect, start_edge  # noqa: E402

URL = "http://127.0.0.1:8765/"


def main():
    proc = start_edge()
    try:
        c = Cdp(cdp_connect())
        c.send("Page.enable")
        c.send("Page.navigate", url=URL)
        time.sleep(3)

        # 勾选 kafka + mysql8 + redis, 触发服务器池显示
        c.eval_js("""
        (() => {
          localStorage.removeItem('mw_packer_state');
          for (const k of ['kafka','mysql8','redis']) {
            const el = document.querySelector('.svc[data-key="'+k+'"]');
            if (el && !el.classList.contains('on')) el.click();
          }
          return true;
        })()
        """)
        time.sleep(1)
        # 切到第 2 步 (选择中间件), 拓扑面板在此 (gotoStep 在 IIFE 内, 需点击 tab)
        c.eval_js("(function(){var b=document.querySelector('.wtab[data-v=\"2\"]');if(b)b.click();})()")
        time.sleep(0.8)
        # 添加 2 台服务器, 填入示例数据
        c.eval_js("""
        (() => {
          document.querySelector('[data-svadd]').click();
          document.querySelector('[data-svadd]').click();
          const set = (i, f, v) => {
            const el = document.querySelector('[data-sv="'+i+':'+f+'"]');
            el.value = v; el.dispatchEvent(new Event('input'));
          };
          set(0,'name','node1'); set(0,'user','root'); set(0,'pass','S3cret~!'); set(0,'ip','10.0.0.11'); set(0,'ssh','22');
          set(1,'name','mysql-node'); set(1,'user','deploy'); set(1,'pass',''); set(1,'ip','192.168.100.200'); set(1,'ssh','2222');
          return document.querySelectorAll('.map-row.svrow').length;
        })()
        """)
        time.sleep(0.6)
        n_rows = c.eval_js("document.querySelectorAll('.map-row.svrow').length")
        del_inline = c.eval_js("""
        (() => {
          const r = document.querySelector('.map-row.svrow');
          const btn = r.querySelector('.delrow').getBoundingClientRect();
          const inp = r.querySelector('input').getBoundingClientRect();
          return { same_line: Math.abs(btn.top - inp.top) < 8,
                   ip_w: r.querySelectorAll('input')[3].getBoundingClientRect().width,
                   user_w: r.querySelectorAll('input')[1].getBoundingClientRect().width,
                   pass_w: r.querySelectorAll('input')[2].getBoundingClientRect().width };
        })()
        """)
        print("rows:", n_rows, "| del same line:", del_inline["same_line"],
              "| widths user/pass/ip: %.0f/%.0f/%.0f" % (del_inline["user_w"], del_inline["pass_w"], del_inline["ip_w"]))
        # 滚动到服务器池, 整屏截图目检
        c.eval_js("document.getElementById('topoBox').scrollIntoView({block:'start'})")
        time.sleep(0.5)
        c.shot(OUT / "server_pool.png")
        print("saved:", OUT / "server_pool.png")
    finally:
        proc.kill()


if __name__ == "__main__":
    main()

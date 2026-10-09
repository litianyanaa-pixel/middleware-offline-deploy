# -*- coding: utf-8 -*-
"""nginx 反向代理配置生成(站点/上游/证书/RealIP)"""
import re

from .util import tr


# ---------------------------------------------------------------- nginx 反代配置生成

_PX_PROXY_HEADERS = """        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;"""


def px_ssl_rel(i, site):
    """站点证书在 nginx/ssl 下的相对目录(容器内 /etc/nginx/ssl/<rel>)"""
    name = re.sub(r"[^A-Za-z0-9.-]", "_", site["server_name"].split()[0])
    return "wizard/%d_%s" % (i + 1, name)


def _px_real_ip_lines(s):
    if not s["trusted_proxies"]:
        return []
    out = ["    # 前面还有 CDN/负载均衡: 从 X-Forwarded-For 还原真实客户端 IP($remote_addr)"]
    out += ["    set_real_ip_from %s;" % c for c in s["trusted_proxies"]]
    out += ["    real_ip_header X-Forwarded-For;", "    real_ip_recursive on;"]
    return out


def _px_locations(s, up):
    out = []
    if s["api_prefix"] and s["api_prefix"] != "/":
        out.append("    location %s {" % s["api_prefix"])
        out.append("        proxy_pass http://%s%s;" % (up, "/" if s["strip"] else ""))
        out.append("        proxy_http_version 1.1;")
        out.append(_PX_PROXY_HEADERS)
        out.append('        proxy_set_header Connection        "";   # 复用 upstream 长连接')
        out += ["        proxy_connect_timeout 5s;",
                "        proxy_send_timeout   60s;",
                "        proxy_read_timeout   60s;",
                "        proxy_buffer_size    16k;",
                "        proxy_buffers        8 32k;",
                "    }"]
    if s["ws"]:
        out.append("    location %s {" % s["ws_path"])
        out.append("        proxy_pass http://%s;" % up)
        out.append("        proxy_http_version 1.1;")
        out.append(_PX_PROXY_HEADERS)
        out.append("        proxy_set_header Upgrade    $http_upgrade;")
        out.append("        proxy_set_header Connection $connection_upgrade;  # nginx.conf 内置 map, 兼容普通请求")
        out += ["        proxy_read_timeout 3600s;   # 长连接不被空闲切断",
                "        proxy_send_timeout 3600s;",
                "    }"]
    if s["mode"] == "proxy":
        out.append("    location / {")
        out.append("        proxy_pass http://%s;" % up)
        out.append("        proxy_http_version 1.1;")
        out.append(_PX_PROXY_HEADERS)
        out.append('        proxy_set_header Connection        "";')
        out += ["        proxy_connect_timeout 5s;",
                "        proxy_send_timeout   60s;",
                "        proxy_read_timeout   60s;",
                "        proxy_buffer_size    16k;",
                "        proxy_buffers        8 32k;",
                "    }"]
    else:
        if s["root"]:
            out.append("    root %s;" % s["root"])
        out.append("    location / {")
        out.append("        index index.html index.htm;")
        out.append("        try_files $uri $uri/ %s;" % ("/index.html" if s["spa"] else "=404"))
        out.append("    }")
    return out


def _px_server_block(s, up, no, listen, ssl=False, ssl_rel=""):
    out = ["server {"]
    out.append("    listen       %s;" % ("443 ssl" if ssl else listen))
    out.append("    server_name  %s;" % s["server_name"])
    out.append("    client_max_body_size %s;" % s["body_size"])
    out += _px_real_ip_lines(s)
    if ssl:
        base = "/etc/nginx/ssl/%s" % ssl_rel
        out += [
            "    ssl_certificate     %s/fullchain.pem;" % base,
            "    ssl_certificate_key %s/privkey.pem;" % base,
            "    ssl_protocols       TLSv1.2 TLSv1.3;",
            "    ssl_ciphers         HIGH:!aNULL:!MD5;",
            "    ssl_session_cache   shared:SSL_%d:10m;" % no,
            "    ssl_session_timeout 1d;",
        ]
    out += _px_locations(s, up)
    out.append("}")
    return out


def gen_nginx_confs(cfg, catalog):
    """反代向导: 生成 conf.d/default.conf 内容(默认站点 + 全部站点)。
    未配置站点时返回 None, 沿用模板 default.conf。"""
    sites = cfg.get("proxies") or []
    if not sites:
        return None

    out = [
        "# 由打包器生成 — 站点/反代规则请回到本地打包器修改后重新打包, 手工修改会在重新部署时被覆盖",
        "",
    ]
    # 默认站点(直接访问 IP 时展示欢迎页); 若某站点已用 _ 占位默认名则跳过, 避免 server_name 冲突
    if not any(s["server_name"].strip() == "_" for s in sites):
        out += [
            "# ---- 默认站点: 直接访问 IP 时展示欢迎页 ----",
            "server {",
            "    listen 80 default_server;",
            "    server_name _;",
            "    root   /usr/share/nginx/html;",
            "    index  index.html index.htm;",
            "    location / { try_files $uri $uri/ =404; }",
            "}",
        ]

    for i, s in enumerate(sites):
        no = i + 1
        up = "px_%d" % no
        out.append("")
        mode_txt = "整站反代" if s["mode"] == "proxy" else "静态+接口"
        ssl_txt = ", HTTPS" if s["ssl"] else ""
        out.append("# ---- 站点 %d: %s (listen %d, %s%s) ----" % (no, s["server_name"], s["listen"], mode_txt, ssl_txt))
        # 整站反代(proxy)的 location / 直接引用 px_<n>: 即使无接口前缀也必须生成 upstream
        # (validate_config 会把 proxy 模式的 api_prefix 归一为 "/", 这里兜底防绕过校验直调)
        if s["api_prefix"] or s["mode"] == "proxy":
            out.append("upstream %s {" % up)
            out.append("    server %s:%d;" % (s["target_host"], s["target_port"]))
            out.append("    keepalive 32;          # upstream 长连接池, 配合 proxy_set_header Connection \"\" 复用")
            out.append("}")
        if s["ssl"]:
            rel = px_ssl_rel(i, s)
            out += _px_server_block(s, up, no, listen=443, ssl=True, ssl_rel=rel)
            if s["listen"] != 443:
                if s["redirect"]:
                    out += ["# HTTP 全部 301 跳转到 HTTPS",
                            "server {",
                            "    listen       %d;" % s["listen"],
                            "    server_name  %s;" % s["server_name"],
                            "    return 301 https://$host$request_uri;",
                            "}"]
                else:
                    out += _px_server_block(s, up, no, listen=s["listen"], ssl=False)
        else:
            out += _px_server_block(s, up, no, listen=s["listen"], ssl=False)

    return "\n".join(out) + "\n"

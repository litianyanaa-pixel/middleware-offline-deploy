# -*- coding: utf-8 -*-
"""命令行入口: Web 界面 / 配置打包 / 样例与目录导出"""
import argparse
import json
import logging
import sys
from pathlib import Path

from .catalog import load_catalog
from .builder import pack
from .paths import log
from .progress import LogProgress
from .util import PackError, tr
from .web import run_web


# ---------------------------------------------------------------- CLI

EXAMPLE_CONFIG = {
    "project": "demo",
    "arch": "amd64",
    "services": ["nginx", "mysql8", "redis", "xxljob"],
    "ports": {"nginx_http": 80, "nginx_https": 443, "nginx_extra": 9000,
              "mysql8": 13307, "redis": 16379, "xxljob": 18081},
    "secrets": {
        "MYSQL_ROOT_PASSWORD": "Change_Me_123",
        "XXL_JOB_ADMIN_PASSWORD": "Change_Me_123",
        "XXL_JOB_ACCESS_TOKEN": "Change_Me_123",
        "REDIS_PASSWORD": "Change_Me_123",
    },
    "deploy_dir": "/data/middleware",
    "docker_data_root": "/data/docker",
    "registry_mirrors": [],
    "extra_ports": {},
    "backup": {"enabled": True, "days": [1, 2, 3, 4, 5, 6, 7], "hour": 3, "keep": 7, "dir": "/data/backup/db"},
    "db": {
        "nacos": None,
        "xxljob": {"mode": "local", "schema": "xxl_job"},
    },
}


def main():
    parser = argparse.ArgumentParser(description="中间件离线包本地打包器|Local offline bundle packer")
    parser.add_argument("--config", help="按配置文件打包(JSON), 不启动 Web 界面|Pack from a JSON config file (no Web UI)")
    parser.add_argument("--out", default=None, help="打包输出目录, 默认 dist/|Output directory, default dist/")
    parser.add_argument("--example", action="store_true", help="生成配置文件样例 example-config.json|Write sample config example-config.json")
    parser.add_argument("--dump-catalog", metavar="PATH", default=None,
                        help="导出合并后的服务目录(内置+插件)为 JSON, 供静态演示页(Pages)使用; 常用: --dump-catalog docs/versions.json|"
                             "Dump the merged catalog (builtin + plugins) as JSON for the static demo page; e.g. --dump-catalog docs/versions.json")
    parser.add_argument("--port", type=int, default=8765, help="Web 界面端口, 默认 8765|Web UI port, default 8765")
    parser.add_argument("--no-browser", action="store_true", help="启动 Web 时不自动打开浏览器|Do not open the browser automatically")
    parser.add_argument("--lang", default="zh", choices=("zh", "en"), help="CLI 消息语言|CLI message language")
    parser.add_argument("--log-file", default=None, help="日志文件路径|Log file path")
    args = parser.parse_args()

    handlers = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=handlers)

    try:
        if args.example:
            Path("example-config.json").write_text(
                json.dumps(EXAMPLE_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
            log.info("已生成 example-config.json, 编辑后执行: python packer.py --config example-config.json|"
                     "example-config.json written; edit it then run: python packer.py --config example-config.json")
            return 0
        if args.dump_catalog:
            cat = load_catalog()
            Path(args.dump_catalog).write_bytes(
                (json.dumps(cat, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            log.info("已导出合并服务目录(%d 个服务)到 %s|Merged catalog (%d services) dumped to %s",
                     len(cat["services"]), args.dump_catalog, len(cat["services"]), args.dump_catalog)
            return 0
        if args.config:
            cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
            catalog = load_catalog()
            result = pack(cfg, catalog, out_dir=args.out, progress=LogProgress())
            for w in result["warnings"]:
                log.warning("%s", tr(w, args.lang))
            log.info("产物: %s (%s MB)|Output: %s (%s MB)", result["path"], result["size_mb"], result["path"], result["size_mb"])
            return 0
        run_web(args.port, no_browser=args.no_browser)
        return 0
    except PackError as e:
        log.error("%s", tr(str(e), args.lang))
        return 1
    except Exception:
        log.exception("失败")
        return 1


if __name__ == "__main__":
    sys.exit(main())

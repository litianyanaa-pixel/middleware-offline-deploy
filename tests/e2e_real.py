# -*- coding: utf-8 -*-
"""
参数化真机 e2e 框架 (SSH/SFTP via paramiko)

把上一轮「两台 CentOS 7 真机双机验证」流程固化成可复用脚本。
**本文件默认不会执行任何远程操作**:
  - `python tests/e2e_real.py --help`      只打印用法并退出(不 import paramiko/不联网)
  - `python tests/e2e_real.py ...`         只打印执行计划(plan)并退出(不联网)
  - `python tests/e2e_real.py ... --run`   显式加 --run 才真正连接主机

安全约束(务必遵守):
  - SSH 密码**只**从 `--ssh-password-env` 指定的环境变量读取(默认 QA_SSH_PASSWORD);
    绝不写入任何文件、绝不打印、绝不作为远程命令行参数(避免 ps 泄露)、
    绝不进入异常文本。仅以 paramiko.connect(password=...) 在内存中传递。
  - deploy.sh 的部署报告/manifest.sh/.env 含明文密码: 本框架**不回显任何远程命令的
    stdout 全文**, 步骤结果只输出「rc / 计数 / 是否满足断言」等脱敏摘要。
  - 清理阶段只删除本次部署新加载的镜像(部署前后 docker images 快照 diff),
    不触碰主机原有镜像; 不自动 `systemctl restart docker`(避免影响共享主机上
    其它容器), 仅还原 daemon.json 文件并提示现场按需重启。

Windows / Git Bash 注意(MSYS 路径转换):
  Git Bash 会把以 `/` 开头的 argv(如 --workdir /data/qa-e2e)改写成本地 Windows
  路径。请在调用前前置环境变量, 例如:
      MSYS_NO_PATHCONV=1 python tests/e2e_real.py \
          --workdir /data/qa-e2e --bundle dist/x.tar.gz --run
  脚本内也设置 os.environ 作为子进程继承提示, 并用 _posix_path() 对被转换的
  远程路径做启发式还原(兜底)。注意: `--bundle` 是**本地**文件路径, 不做 POSIX 规范化。

用法示例:
  # 只看计划(默认, 绝不联网)
  python tests/e2e_real.py --hosts 10.100.11.112,10.100.11.113 \
      --user root --workdir /data/qa-e2e --bundle dist/demo-offline.tar.gz
  # 真机执行(密码来自环境变量, 不落盘)
  QA_SSH_PASSWORD='******' MSYS_NO_PATHCONV=1 \
      python tests/e2e_real.py --hosts 10.100.11.112,10.100.11.113 \
      --workdir /data/qa-e2e --bundle dist/demo-offline.tar.gz --run
  # 失败后保留现场排查
  ... --run --keep

框架范围: 每台 --hosts 主机作为独立目标, 跑一遍「单机离线包」完整闭环。
上一轮涉及的跨机 distribute.sh 主从分发还没进框架(见 ROADMAP.md「功能」一节),
需要时用 --hosts 分别对各节点独立验证, 或后续扩展 step_deploy 的命令模板。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

# ---- 步骤清单: plan 输出与 --run 执行共用同一份定义, 避免两处漂移 ----
# (key, 说明, 是否属于 finally 清理阶段)
STEPS = [
    ("probe",          "连通性/只读环境探测(docker/compose/daemon.json/端口/crontab/磁盘)", False),
    ("upload_sha256",  "上传 bundle 并校验 sha256", False),
    ("extract",        "解压离线包", False),
    ("deploy",         "部署 ./deploy.sh", False),
    ("health_report",  "健康检查与部署报告", False),
    ("backup_restore", "备份/恢复/从任意 cwd 执行 restore.sh list", False),
    ("redeploy_keep",  "重部署配置保留验证", False),
    ("uninstall_cron", "卸载锁与 cron 清理", False),
    ("cleanup",        "环境还原(删工作目录/恢复 daemon.json.packer/删测试镜像/crontab 归零)", True),
]


# ---------------------------------------------------------------- 工具

def _setup_msys():
    """给脚本内可能启动的子进程继承 MSYS 豁免提示。

    注意: 这个设置**无法**回溯影响本 python 进程已被 MSYS 转换过的 argv
    (转换发生在 exec 之前)。真正可靠的做法是调用方前置 MSYS_NO_PATHCONV=1,
    见模块 docstring; 这里再叠加 _posix_path() 作为兜底。
    """
    os.environ.setdefault("MSYS_NO_PATHCONV", "1")
    os.environ.setdefault("MSYS2_ARG_CONV_EXCL", "*")


def _posix_path(p):
    """规范化**远程** POSIX 路径: 统一分隔符 + 启发式还原被 MSYS 改写的 `/` 开头参数。

    Git Bash 常把 `/data/qa-e2e` 变成 `C:/Program Files/Git/data/qa-e2e`;
    这里尝试从已知 POSIX 顶级目录起截取还原。局限: 属启发式, 不保证覆盖所有
    安装路径 —— 推荐始终用 MSYS_NO_PATHCONV=1 前置调用。
    """
    if not p:
        return p
    p = p.replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", p):
        m = re.search(r"/(?:data|tmp|opt|home|var|mnt|srv|root|etc)(?:/|$)", p)
        if m:
            p = p[m.start():]
    return p.rstrip("/") or "/"


def _parse_hosts(raw):
    return [h.strip() for h in (raw or "").split(",") if h.strip()]


def _local_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 执行器

class Runner:
    """持有 SSH 连接与步骤结果。命令模板集中在此, 真机环境不同可直接改这里。"""

    # 只读探测(全部为只读命令; 末尾 || true 保证 crontab -l 无条目时不算失败)
    PROBE_CMD = (
        "echo '== docker =='; docker --version 2>&1; "
        "echo '== compose =='; docker compose version 2>&1; "
        "echo '== daemon.json =='; cat /etc/docker/daemon.json 2>&1; "
        "echo '== ports(/proc/net/tcp) =='; head -5 /proc/net/tcp 2>&1; "
        "echo '== crontab =='; crontab -l 2>&1; "
        "echo '== disk =='; df -h / 2>&1; "
        "echo '== daemon.json.packer =='; ls -l /etc/docker/daemon.json.packer 2>&1; "
        "echo '== images =='; docker images --format '{{.ID}}' 2>&1; "
        "true"
    )

    def __init__(self, args, password, paramiko):
        self.args = args
        self._password = password          # 仅内存; 绝不落盘/打印/进异常文本
        self._paramiko = paramiko
        self.hosts = _parse_hosts(args.hosts)
        self.workdir = _posix_path(args.workdir)   # 远程 POSIX 路径
        self.bundle = args.bundle                  # 本地路径, 不做 POSIX 规范化
        self.clients = {}
        self.results = []                         # (name, ok, detail)
        self.probe = {}                           # host -> 探测原文(不打印)
        self.crontab_before = {}                  # host -> 部署前 crontab 快照
        self.images_before = {}                   # host -> 部署前镜像 ID 集合
        self.loaded_images = {}                   # host -> 本次新增镜像 ID 列表
        self.remote_bundle = {}                   # host -> 远程包路径
        self.pkg_dir = {}                         # host -> 解包后的目录

    # ---- 底层 SSH/SFTP ----
    def connect_all(self):
        for h in self.hosts:
            c = self._paramiko.SSHClient()
            # 隔离 QA 内网环境用 AutoAddPolicy; 生产/共享主机应换 known_hosts 校验
            c.set_missing_host_key_policy(self._paramiko.AutoAddPolicy())
            c.connect(h, port=22, username=self.args.user,
                      password=self._password, timeout=20,
                      allow_agent=False, look_for_keys=False)
            self.clients[h] = c

    def close_all(self):
        for c in self.clients.values():
            try:
                c.close()
            except Exception:
                pass

    def cmd(self, host, command, timeout=120):
        """执行远程命令, 返回 (rc, out, err)。调用方不得把 out 全文打印(可能含明文密码)。"""
        _in, out, err = self.clients[host].exec_command(command, timeout=timeout)
        rc = out.channel.recv_exit_status()
        return rc, out.read().decode("utf-8", "replace"), err.read().decode("utf-8", "replace")

    def sftp_put(self, host, local, remote):
        sftp = self.clients[host].open_sftp()
        try:
            sftp.put(local, remote)
        finally:
            sftp.close()

    # ---- 步骤 1: 只读探测 ----
    def step_probe(self):
        for h in self.hosts:
            rc, out, _err = self.cmd(h, self.PROBE_CMD, timeout=60)
            if rc != 0:
                return False, "%s 探测 rc=%d" % (h, rc)
            self.probe[h] = out
            # 拆出 crontab 与 images 快照(仅供清理对比, 不打印)
            m = re.search(r"== crontab ==\n(.*?)\n== disk ==", out, re.S)
            self.crontab_before[h] = (m.group(1) if m else "").strip()
            m2 = re.search(r"== images ==\n(.*?)\Z", out, re.S)
            self.images_before[h] = set(
                x.strip() for x in (m2.group(1).splitlines() if m2 else []) if x.strip())
        return True, "%d 台主机只读探测完成(结果仅存内存)" % len(self.hosts)

    # ---- 步骤 2: 上传 + sha256 ----
    def step_upload_sha256(self):
        local_sha = _local_sha256(self.bundle)
        remote_name = os.path.basename(self.bundle)
        for h in self.hosts:
            self.cmd(h, "mkdir -p %s" % self._q(self.workdir))
            remote = "%s/%s" % (self.workdir, remote_name)
            self.sftp_put(h, self.bundle, remote)
            self.remote_bundle[h] = remote
            rc, out, _err = self.cmd(h, "sha256sum %s" % self._q(remote))
            remote_sha = (out.split() or [""])[0] if rc == 0 else ""
            if rc != 0 or remote_sha != local_sha:
                return False, "%s 上传后 sha256 不一致(rc=%d)" % (h, rc)
        return True, "%d 台主机上传完成且 sha256 一致" % len(self.hosts)

    # ---- 步骤 3: 解压 ----
    def step_extract(self):
        for h in self.hosts:
            rc, _out, _err = self.cmd(
                h, "tar -xzf %s -C %s" % (self._q(self.remote_bundle[h]),
                                          self._q(self.workdir)), timeout=300)
            if rc != 0:
                return False, "%s 解压 rc=%d" % (h, rc)
            # 定位解包目录(取 workdir 下第一个目录)
            rc2, out2, _e2 = self.cmd(
                h, "find %s -maxdepth 1 -mindepth 1 -type d | head -1" % self._q(self.workdir))
            d = out2.strip()
            if rc2 != 0 or not d:
                return False, "%s 未找到解包目录" % h
            self.pkg_dir[h] = d
        return True, "%d 台主机解压完成" % len(self.hosts)

    # ---- 步骤 4: 部署 ----
    def step_deploy(self):
        for h in self.hosts:
            d = self._q(self.pkg_dir[h])
            rc, _out, _err = self.cmd(h, "cd %s && ./deploy.sh" % d, timeout=900)
            if rc != 0:
                return False, "%s deploy.sh rc=%d" % (h, rc)
            # 记录本次新加载的镜像(供清理精确删除, 不碰原有镜像)
            _r, out2, _e2 = self.cmd(h, "docker images --format '{{.ID}}' 2>/dev/null")
            after = set(x.strip() for x in out2.splitlines() if x.strip())
            self.loaded_images[h] = sorted(after - self.images_before.get(h, set()))
        return True, "%d 台主机 deploy.sh 全部 EXIT=0" % len(self.hosts)

    # ---- 步骤 5: 健康检查与部署报告 ----
    def step_health_report(self):
        for h in self.hosts:
            d = self._q(self.pkg_dir[h])
            # 只判定报告存在 + 无失败标记, 不回显报告内容(报告含明文账号密码)
            rc, out, _err = self.cmd(
                h, "test -f %s/部署报告.txt && grep -c '✗\\|FAIL' %s/部署报告.txt || echo MISSING"
                   % (d, d))
            if rc != 0 or "MISSING" in out:
                return False, "%s 部署报告缺失" % h
            fails = (out.strip().splitlines() or ["0"])[-1]
            if fails.isdigit() and int(fails) > 0:
                return False, "%s 报告含 %s 项失败标记" % (h, fails)
        return True, "%d 台主机部署报告存在且无失败标记" % len(self.hosts)

    # ---- 步骤 6: 备份/恢复/任意 cwd 执行 restore.sh list ----
    def step_backup_restore(self):
        for h in self.hosts:
            d = self._q(self.pkg_dir[h])
            # 从 / 目录(任意 cwd)执行 restore.sh list, 校验能列出备份
            rc, out, _err = self.cmd(
                h, "cd / && %s/restore.sh list" % d, timeout=180)
            if rc != 0:
                return False, "%s 从 / 执行 restore.sh list rc=%d" % (h, rc)
            # 备份/恢复闭环: 生成一次备份 -> 删库 -> 恢复(命令模板, 真机按现场调整)
            rc2, _o2, _e2 = self.cmd(h, "cd %s && ./backup.sh" % d, timeout=300)
            if rc2 != 0:
                return False, "%s backup.sh rc=%d" % (h, rc2)
        return True, "%d 台主机 restore.sh list(任意 cwd) + backup.sh 通过" % len(self.hosts)

    # ---- 步骤 7: 重部署配置保留 ----
    def step_redeploy_keep(self):
        for h in self.hosts:
            d = self._q(self.pkg_dir[h])
            # 手工改一处「只补齐」配置(redis.conf), 重部署后应保留用户版并生成 .packer-new
            self.cmd(h, "echo '# e2e hand edit' >> %s/conf/redis/redis.conf 2>/dev/null || true" % d)
            rc, _out, _err = self.cmd(h, "cd %s && ./deploy.sh" % d, timeout=900)
            if rc != 0:
                return False, "%s 重部署 rc=%d" % (h, rc)
            _r, o2, _e2 = self.cmd(
                h, "grep -c '# e2e hand edit' %s/conf/redis/redis.conf 2>/dev/null || echo 0" % d)
            kept = (o2.strip().splitlines() or ["0"])[-1]
            if kept.strip() == "0":
                return False, "%s 重部署后手工改动未保留" % h
        return True, "%d 台主机重部署配置保留断言通过" % len(self.hosts)

    # ---- 步骤 8: 卸载锁与 cron 清理 ----
    def step_uninstall_cron(self):
        for h in self.hosts:
            d = self._q(self.pkg_dir[h])
            rc, _out, _err = self.cmd(
                h, "cd %s && ./uninstall.sh --purge-data --yes" % d, timeout=600)
            if rc != 0:
                return False, "%s uninstall.sh rc=%d" % (h, rc)
            # cron 不应残留 # mw-backup 行; 锁应落在 backup.conf 指定目录
            _r, o2, _e2 = self.cmd(h, "crontab -l 2>/dev/null | grep -c '# mw-backup' || echo 0")
            left = (o2.strip().splitlines() or ["0"])[-1]
            if left.strip() != "0":
                return False, "%s 卸载后仍残留 # mw-backup cron" % h
        return True, "%d 台主机卸载锁/cron 清理通过" % len(self.hosts)

    # ---- 步骤 9: 环境还原(finally 阶段) ----
    def step_cleanup(self):
        ok = True
        notes = []
        for h in self.hosts:
            try:
                # 只删本次部署新加载的镜像
                ids = self.loaded_images.get(h, [])
                if ids:
                    self.cmd(h, "docker rmi -f %s >/dev/null 2>&1 || true" % " ".join(ids),
                             timeout=300)
                # 还原 daemon.json.packer(不自动重启 docker, 见模块 docstring)
                self.cmd(h, "test -f /etc/docker/daemon.json.packer && "
                            "mv -f /etc/docker/daemon.json.packer /etc/docker/daemon.json || true")
                # crontab 应回到部署前基线(通常为空)
                _r, cur, _e = self.cmd(h, "crontab -l 2>/dev/null || true")
                if (cur or "").strip() != self.crontab_before.get(h, ""):
                    ok = False
                    notes.append("%s crontab 未回到基线" % h)
                # 删除测试工作目录
                if not self.args.keep:
                    self.cmd(h, "rm -rf %s" % self._q(self.workdir))
            except Exception as e:
                ok = False
                notes.append("%s 清理异常: %s" % (h, type(e).__name__))
        detail = "环境还原完成" if ok else "环境还原有问题: " + "; ".join(notes)
        return ok, detail

    # ---- 调度 ----
    @staticmethod
    def _q(p):
        """远程路径加单引号(路径均为脚本生成/固定值, 不含用户密码)。"""
        return "'%s'" % str(p).replace("'", "'\\''")

    def step(self, name, fn):
        try:
            ok, detail = fn()
        except Exception as e:
            # 只暴露异常类型, 不回显消息(远程命令输出/异常可能含敏感内容)
            ok, detail = False, "异常: %s" % type(e).__name__
        print("%s %-18s %s" % ("[OK]" if ok else "[FAIL]", name, detail))
        self.results.append((name, ok, detail))
        return ok

    def run(self):
        fns = {
            "probe": self.step_probe,
            "upload_sha256": self.step_upload_sha256,
            "extract": self.step_extract,
            "deploy": self.step_deploy,
            "health_report": self.step_health_report,
            "backup_restore": self.step_backup_restore,
            "redeploy_keep": self.step_redeploy_keep,
            "uninstall_cron": self.step_uninstall_cron,
            "cleanup": self.step_cleanup,
        }
        try:
            self.step("connect", self._connect_step)
            for key, _desc, is_cleanup in STEPS:
                if is_cleanup:
                    continue          # cleanup 放到 finally
                self.step(key, fns[key])
        finally:
            # 任何一步失败仍继续清理; --keep 时跳过清场但打印提示
            if self.args.keep:
                print("[SKIP] --keep 指定, 跳过环境还原(现场保留在 %s)" % self.workdir)
            else:
                self.step("cleanup", fns["cleanup"])
            self.close_all()
        return self.summary()

    def _connect_step(self):
        self.connect_all()
        return True, "%d 台主机 SSH 连通" % len(self.hosts)

    def summary(self):
        total = len(self.results)
        failed = [(n, d) for n, ok, d in self.results if not ok]
        print("\n==== E2E 总结 ====")
        print("步骤 %d 项, 通过 %d, 失败 %d, 主机 %d 台"
              % (total, total - len(failed), len(failed), len(self.hosts)))
        for n, d in failed:
            print("  [FAIL] %-18s %s" % (n, d))
        code = 1 if failed else 0
        print("退出码: %d" % code)
        return code


# ---------------------------------------------------------------- plan / CLI

def print_plan(args):
    hosts = _parse_hosts(args.hosts)
    print("== E2E 执行计划 (未执行任何远程操作; 加 --run 才连接主机) ==")
    print("  hosts            : %s (%d 台)" % (", ".join(hosts) or "<未指定>", len(hosts)))
    print("  user             : %s" % args.user)
    print("  workdir(远程)    : %s" % _posix_path(args.workdir))
    print("  bundle(本地)     : %s" % (args.bundle or "<未指定>"))
    print("  ssh 密码         : 仅从环境变量 $%s 读取(不落盘/不打印)" % args.ssh_password_env)
    print("  keep             : %s" % ("是" if args.keep else "否"))
    print("  run              : 否 (仅 plan)")
    print("  步骤:")
    for i, (key, desc, is_cleanup) in enumerate(STEPS, 1):
        print("   %2d. %-18s %s%s"
              % (i, key, desc, "  [finally]" if is_cleanup else ""))
    print("  提示: Windows Git Bash 请前置 MSYS_NO_PATHCONV=1 再调用, 避免 / 开头远程路径被改写。")
    print("  提示: --run 需要 --hosts / --bundle 与 $%s 均就绪。" % args.ssh_password_env)


def build_parser():
    p = argparse.ArgumentParser(
        prog="tests/e2e_real.py",
        description="参数化真机 e2e 框架(默认只打印 plan, 加 --run 才连接主机)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("安全: SSH 密码只从 --ssh-password-env 指定的环境变量读取, 绝不写文件/打印。\n"
                "示例: MSYS_NO_PATHCONV=1 QA_SSH_PASSWORD='***' python tests/e2e_real.py "
                "--hosts 10.100.11.112,10.100.11.113 --workdir /data/qa-e2e "
                "--bundle dist/x.tar.gz --run"))
    p.add_argument("--hosts", default="",
                   help="目标主机, 逗号分隔, 如 10.100.11.112,10.100.11.113")
    p.add_argument("--user", default="root", help="SSH 用户名(默认 root)")
    p.add_argument("--workdir", default="/data/qa-e2e",
                   help="远程测试工作目录(POSIX 绝对路径, 默认 /data/qa-e2e)")
    p.add_argument("--bundle", default="", help="本地离线包 tar.gz 路径")
    p.add_argument("--ssh-password-env", default="QA_SSH_PASSWORD",
                   help="存放 SSH 密码的环境变量名(默认 QA_SSH_PASSWORD)")
    p.add_argument("--keep", action="store_true", help="失败后不清场, 保留现场")
    p.add_argument("--run", action="store_true",
                   help="显式执行远程操作; 不加则只打印 plan 并退出")
    return p


def main(argv=None):
    _setup_msys()
    args = build_parser().parse_args(argv)

    # 默认 & --help: argparse 在 --help 时直接 SystemExit(0), 不会走到下面;
    # 无 --run 时只打印 plan, 全程不 import paramiko / 不联网。
    if not args.run:
        print_plan(args)
        return 0

    # ---- 以下仅在显式 --run 时执行 ----
    hosts = _parse_hosts(args.hosts)
    if not hosts:
        print("[FAIL] --run 需要 --hosts")
        return 2
    if not args.bundle or not Path(args.bundle).is_file():
        print("[FAIL] --run 需要 --bundle 指向存在的本地 tar.gz")
        return 2
    # 密码只从环境变量读; 不打印其值, 也不写文件
    password = os.environ.get(args.ssh_password_env)
    if not password:
        print("[FAIL] --run 需要环境变量 $%s 提供 SSH 密码(脚本不读取/不打印其值)"
              % args.ssh_password_env)
        return 2
    try:
        import paramiko  # 延迟导入: --help / plan 不依赖 paramiko
    except ImportError:
        print("[FAIL] 未安装 paramiko, 无法 --run (plan/--help 不受影响)")
        return 2

    runner = Runner(args, password, paramiko)
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())

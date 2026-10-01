#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机操作小工具(paramiko)。密码经环境变量 KK_PW 传入, 不入库。

用法:
  python tests/rt.py cmd  <ip> "<命令>"                    # 执行命令(打印输出)
  python tests/rt.py put  <ip> <本地文件> <远端路径>        # 上传文件
  python tests/rt.py get  <ip> <远端路径> <本地文件>        # 下载文件
"""
import os
import sys
import time

import paramiko

HOSTS = {"112": "10.100.11.112", "113": "10.100.11.113"}


def client(ip):
    pw = os.environ.get("KK_PW")
    if not pw:
        sys.exit("缺少环境变量 KK_PW")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, username="root", password=pw, timeout=15, look_for_keys=False, allow_agent=False)
    return c


def run(ip, cmd, timeout=1800):
    c = client(ip)
    try:
        _, out, err = c.exec_command(cmd, timeout=timeout)
        out.channel.recv_exit_status()
        o = out.read().decode("utf-8", "replace")
        e = err.read().decode("utf-8", "replace")
        print(o)
        if e.strip():
            print("[STDERR]", e[-2000:])
        return o
    finally:
        c.close()


def put(ip, local, remote):
    c = client(ip)
    try:
        s = c.open_sftp()
        t0 = time.time()
        s.put(local, remote)
        st = s.stat(remote)
        print("OK %s -> %s:%s (%.1f MB/s, %d bytes)"
              % (local, ip, remote, st.st_size / 1048576 / max(time.time() - t0, 0.1), st.st_size))
        s.close()
    finally:
        c.close()


def get(ip, remote, local):
    c = client(ip)
    try:
        s = c.open_sftp()
        s.get(remote, local)
        print("OK %s:%s -> %s" % (ip, remote, local))
        s.close()
    finally:
        c.close()


def put_stream(ip, local, remote):
    """SFTP 不可用时的替代: exec 通道 cat 流式写入远端。"""
    c = client(ip)
    try:
        t0 = time.time()
        _, out, err = c.exec_command("cat > '%s'" % remote, timeout=7200)
        total = 0
        with open(local, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                out.channel.sendall(chunk)
                total += len(chunk)
        out.channel.shutdown_write()
        rc = out.channel.recv_exit_status()
        e = err.read().decode("utf-8", "replace")
        if rc != 0:
            sys.exit("stream put 失败 rc=%d %s" % (rc, e[-500:]))
        print("OK(stream) %s -> %s:%s (%.1f MB/s, %d bytes)"
              % (local, ip, remote, total / 1048576 / max(time.time() - t0, 0.1), total))
    finally:
        c.close()


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) < 2 or a[0] not in ("cmd", "put", "get", "cat"):
        sys.exit(__doc__)
    ip = HOSTS.get(a[1], a[1])
    if a[0] == "cmd":
        run(ip, a[2], int(a[3]) if len(a) > 3 else 1800)
    elif a[0] == "put":
        put(ip, a[2], a[3])
    elif a[0] == "cat":
        put_stream(ip, a[2], a[3])
    else:
        get(ip, a[2], a[3])

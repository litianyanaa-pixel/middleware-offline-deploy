#!/bin/bash
# 节点容器入口: 按环境变量设置 root 密码后启动 sshd
echo "root:${ROOT_PASS:-root123}" | chpasswd
exec /usr/sbin/sshd -D

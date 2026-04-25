#!/bin/bash

set -e

NODE_NAME=${NODE_NAME:-unknown}

SRV6_LOCATOR=${SRV6_LOCATOR:-fc00:0::1/128}

echo "===== FRR+SRv6 Node Starting ====="

echo "Node Name: $NODE_NAME"

echo "SRv6 Locator: $SRV6_LOCATOR"

echo

# 启用IPv6和SRv6

sysctl -w net.ipv6.conf.all.forwarding=1 > /dev/null

sysctl -w net.ipv6.conf.all.seg6_enabled=1 > /dev/null

sysctl -w net.ipv6.conf.default.seg6_enabled=1 > /dev/null

sysctl -w net.ipv6.conf.all.accept_ra=0 > /dev/null

# 配置SRv6 locator地址到lo接口

echo "Configuring SRv6 locator on lo..."

ip -6 addr add $SRV6_LOCATOR dev lo

ip link set lo up

# 确保FRR配置文件存在

if [ ! -f /etc/frr/frr.conf ]; then

    echo "Error: /etc/frr/frr.conf not found!"

    echo "Container must be started with FRR configuration mounted."

    exit 1

fi

# 启动FRR守护进程

echo "Starting FRR daemons..."

# 确保日志目录存在

mkdir -p /var/log/frr

touch /var/log/frr/zebra.log /var/log/frr/isisd.log

chown -R frr:frr /var/log/frr

# 使用service命令启动FRR (更可靠)

if [ -f /usr/lib/frr/frrinit.sh ]; then

    /usr/lib/frr/frrinit.sh start

elif [ -f /etc/init.d/frr ]; then

    /etc/init.d/frr start

else

    # 手动启动守护进程

    echo "Starting zebra..."

    /usr/lib/frr/zebra -d -A 127.0.0.1 -s 90000000

    echo "Starting isisd..."

    /usr/lib/frr/isisd -d -A ::1

fi

# 等待FRR完全启动

sleep 8

# 验证FRR进程

echo

echo "FRR Processes:"

ps aux | grep -E 'zebra|isisd' | grep -v grep

echo

echo "Node $NODE_NAME is ready"

echo "=========================="

echo

# 保持容器运行并输出日志

# 如果日志文件不存在就只sleep

if [ -f /var/log/frr/zebra.log ]; then

    exec tail -f /var/log/frr/zebra.log /var/log/frr/isisd.log

else

    echo "Logs not found, keeping container alive..."

    exec sleep infinity

fi

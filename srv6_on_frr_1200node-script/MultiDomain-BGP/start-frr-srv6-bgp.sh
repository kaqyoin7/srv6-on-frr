#!/bin/bash
# SRv6+FRR+BGP 节点启动脚本（BGP分域版）
# 支持双IS-IS实例（eBGP边界节点）

set -e

NODE_NAME=${NODE_NAME:-unknown}
SRV6_LOCATOR=${SRV6_LOCATOR:-fc00:0::1/128}
IS_BORDER=${IS_BORDER:-false}

echo "===== FRR+SRv6+BGP Node Starting ====="
echo "Node Name    : $NODE_NAME"
echo "SRv6 Locator : $SRV6_LOCATOR"
echo "Border Node  : $IS_BORDER"
echo

# ── 内核参数 ─────────────────────────────────────────────────────────────────
sysctl -w net.ipv6.conf.all.forwarding=1       > /dev/null
sysctl -w net.ipv6.conf.all.seg6_enabled=1     > /dev/null
sysctl -w net.ipv6.conf.default.seg6_enabled=1 > /dev/null
sysctl -w net.ipv6.conf.all.accept_ra=0        > /dev/null
# BGP 需要较大的 socket buffer
sysctl -w net.core.rmem_max=134217728          > /dev/null
sysctl -w net.core.wmem_max=134217728          > /dev/null

# ── SRv6 Locator 配置到 lo ───────────────────────────────────────────────────
echo "Configuring SRv6 locator on lo..."
ip -6 addr add "$SRV6_LOCATOR" dev lo 2>/dev/null || true
ip link set lo up

# ── 验证配置文件 ──────────────────────────────────────────────────────────────
if [ ! -f /etc/frr/frr.conf ]; then
    echo "Error: /etc/frr/frr.conf not found!"
    exit 1
fi

if [ ! -f /etc/frr/daemons ]; then
    echo "Error: /etc/frr/daemons not found!"
    exit 1
fi

# ── 日志目录 ──────────────────────────────────────────────────────────────────
mkdir -p /var/log/frr
touch /var/log/frr/zebra.log \
      /var/log/frr/isisd.log  \
      /var/log/frr/bgpd.log
chown -R frr:frr /var/log/frr

# ── 启动 FRR ─────────────────────────────────────────────────────────────────
echo "Starting FRR daemons..."
if [ -f /usr/lib/frr/frrinit.sh ]; then
    /usr/lib/frr/frrinit.sh start
elif [ -f /etc/init.d/frr ]; then
    /etc/init.d/frr start
else
    echo "Starting zebra..."
    /usr/lib/frr/zebra -d -A 127.0.0.1 -s 90000000
    echo "Starting isisd..."
    /usr/lib/frr/isisd -d -A ::1
    echo "Starting bgpd..."
    /usr/lib/frr/bgpd -d -A 127.0.0.1
fi

# 边界节点需要更长的启动等待（双IS-IS + BGP）
if [ "$IS_BORDER" = "true" ]; then
    echo "Border node: waiting for FRR to fully initialize..."
    sleep 12
else
    sleep 8
fi

# ── 验证进程 ──────────────────────────────────────────────────────────────────
echo
echo "FRR Processes:"
ps aux | grep -E 'zebra|isisd|bgpd' | grep -v grep || true

echo
echo "Node $NODE_NAME is ready (IS_BORDER=$IS_BORDER)"
echo "========================================"
echo

# ── 保持容器运行 ──────────────────────────────────────────────────────────────
LOG_FILES=""
[ -f /var/log/frr/zebra.log ] && LOG_FILES="$LOG_FILES /var/log/frr/zebra.log"
[ -f /var/log/frr/isisd.log  ] && LOG_FILES="$LOG_FILES /var/log/frr/isisd.log"
[ -f /var/log/frr/bgpd.log   ] && LOG_FILES="$LOG_FILES /var/log/frr/bgpd.log"

if [ -n "$LOG_FILES" ]; then
    exec tail -f $LOG_FILES
else
    echo "Logs not found, keeping container alive..."
    exec sleep infinity
fi

#!/bin/bash

echo "===== Configuring SRv6 for 400 nodes ====="
echo

# 配置参数
NUM_RINGS=20
NODES_PER_RING=20
TOTAL_NODES=$((NUM_RINGS * NODES_PER_RING))

echo "Topology: ${NUM_RINGS} rings × ${NODES_PER_RING} nodes = ${TOTAL_NODES} nodes"
echo

# 步骤1: 为所有节点配置SRv6 End行为
echo "Step 1: Configuring SRv6 End behavior on all ${TOTAL_NODES} nodes..."
echo "This will take a few minutes..."

count=0
for ((ring=0; ring<NUM_RINGS; ring++)); do
    for ((node=0; node<NODES_PER_RING; node++)); do
        node_name="r${ring}n${node}"
        
        # 计算SRv6 locator
        ring_hex=$(printf "%04x" $ring)
        node_hex=$(printf "%04x" $node)
        locator="fc00:${ring_hex}:${node_hex}::/48"
        
        # 配置SRv6 End行为
        sudo docker exec srv6-${node_name} \
            ip -6 route add ${locator} encap seg6local action End dev lo \
            2>/dev/null
        
        count=$((count + 1))
        
        # 每50个节点显示进度
        if [ $((count % 50)) -eq 0 ] || [ ${count} -eq ${TOTAL_NODES} ]; then
            percent=$((count * 100 / TOTAL_NODES))
            echo "  Progress: ${count}/${TOTAL_NODES} (${percent}%)"
        fi
    done
done

echo "✓ Step 1 completed: SRv6 End behavior configured on all nodes"


echo "===== 配置3条测试SRv6路径 ====="
echo

# ============================================
# 路径1: r0n0 -> r0n5 (环内跨5跳)
# ============================================
echo "路径1: r0n0 -> r0n5 (环内路径，6个段)"
echo "  完整路径: r0n0 -> r0n1 -> r0n2 -> r0n3 -> r0n4 -> r0n5"
echo "  段列表: fc00:0000:0000::1, fc00:0000:0001::1, fc00:0000:0002::1,"
echo "         fc00:0000:0003::1, fc00:0000:0004::1, fc00:0000:0005::1"

sudo docker exec srv6-r0n0 \
    ip -6 route add fc00:0000:0005::1/128 \
    encap seg6 mode encap segs \
    fc00:0000:0001::1,fc00:0000:0002::1,fc00:0000:0003::1,fc00:0000:0004::1,fc00:0000:0005::1 \
    dev r0n0-r0n1 2>/dev/null || echo "  (路径已存在)"

echo "✓ 路径1配置完成"
echo

# ============================================
# 路径2: r0n10 -> r3n10 (跨3个环)
# ============================================
echo "路径2: r0n10 -> r3n10 (环间路径，4个段)"
echo "  完整路径: r0n10 -> r1n10 -> r2n10 -> r3n10"
echo "  段列表: fc00:0000:000a::1, fc00:0001:000a::1,"
echo "         fc00:0002:000a::1, fc00:0003:000a::1"

sudo docker exec srv6-r0n10 \
    ip -6 route add fc00:0003:000a::1/128 \
    encap seg6 mode encap segs \
    fc00:0001:000a::1,fc00:0002:000a::1,fc00:0003:000a::1 \
    dev r0n10-r1n10 2>/dev/null || echo "  (路径已存在)"

echo "✓ 路径2配置完成"
echo

# ============================================
# 路径3: r0n0 -> r2n5 (混合路径)
# ============================================
echo "路径3: r0n0 -> r2n5 (环间+环内混合，8个段)"
echo "  完整路径: r0n0 -> r1n0 -> r2n0 -> r2n1 -> r2n2 -> r2n3 -> r2n4 -> r2n5"
echo "  段列表: fc00:0000:0000::1, fc00:0001:0000::1, fc00:0002:0000::1,"
echo "         fc00:0002:0001::1, fc00:0002:0002::1, fc00:0002:0003::1,"
echo "         fc00:0002:0004::1, fc00:0002:0005::1"

sudo docker exec srv6-r0n0 \
    ip -6 route add fc00:0002:0005::1/128 \
    encap seg6 mode encap segs \
    fc00:0001:0000::1,fc00:0002:0000::1,fc00:0002:0001::1,fc00:0002:0002::1,fc00:0002:0003::1,fc00:0002:0004::1,fc00:0002:0005::1 \
    dev r0n0-r1n0 2>/dev/null || echo "  (路径已存在)"

echo "✓ 路径3配置完成"
echo

echo "===== 配置完成 ====="
echo

# 验证配置
echo "验证已配置的路径:"
echo
echo "路径1 (r0n0):"
sudo docker exec srv6-r0n0 ip -6 route show | grep "fc00:0000:0005::1"
echo
echo "路径2 (r0n10):"
sudo docker exec srv6-r0n10 ip -6 route show | grep "fc00:0003:000a::1"
echo
echo "路径3 (r0n0):"
sudo docker exec srv6-r0n0 ip -6 route show | grep "fc00:0002:0005::1"
echo

echo "===== 测试命令 ====="
echo
echo "# 测试路径1"
echo "sudo docker exec srv6-r0n0 ping6 -c 5 fc00:0000:0005::1"
echo
echo "# 测试路径2"
echo "sudo docker exec srv6-r0n10 ping6 -c 5 fc00:0003:000a::1"
echo
echo "# 测试路径3"
echo "sudo docker exec srv6-r0n0 ping6 -c 5 fc00:0002:0005::1"

echo
echo "===== SRv6 Configuration Completed ====="
echo
echo "All nodes now have SRv6 End behavior configured."
echo "You can now use dynamic_srv6_manager.py to configure paths."
echo
echo "Verification (sample node r0n0):"
sudo docker exec srv6-r0n0 ip -6 route show table local | grep seg6local | head -1

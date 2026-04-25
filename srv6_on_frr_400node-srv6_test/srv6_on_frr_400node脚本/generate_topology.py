#!/usr/bin/env python3
"""
生成400节点拓扑定义
20个环，每个环20个节点
环内：每个节点与相邻节点连接形成环
环间：相邻环的同位置节点连接
"""

import json

def generate_400node_topology():
    """
    生成400节点拓扑
    拓扑结构：20个环 x 20个节点/环 = 400节点
    """
    
    NUM_RINGS = 20  # 环数量
    NODES_PER_RING = 20  # 每个环的节点数
    
    nodes = []
    links = []
    
    print(f"Generating topology: {NUM_RINGS} rings × {NODES_PER_RING} nodes/ring = {NUM_RINGS * NODES_PER_RING} nodes")
    
    # 生成节点
    node_id = 1
    for ring_idx in range(NUM_RINGS):
        for node_idx in range(NODES_PER_RING):
            # 节点名称: r0n0, r0n1, ..., r19n19
            node_name = f"r{ring_idx}n{node_idx}"
            
            # SRv6地址分配
            # 使用紧凑的地址空间：fc00:RRRR:NNNN::1
            # RRRR = ring_idx (16进制)
            # NNNN = node_idx (16进制)
            ring_hex = f"{ring_idx:04x}"
            node_hex = f"{node_idx:04x}"
            srv6_locator = f"fc00:{ring_hex}:{node_hex}::1/128"
            srv6_prefix = f"fc00:{ring_hex}:{node_hex}::/48"
            
            # ISIS NET: 49.RRRR.RRRR.NNNN.00
            isis_net = f"49.{ring_idx:04x}.{ring_idx:04x}.{node_idx:04x}.00"
            
            nodes.append({
                "name": node_name,
                "id": node_id,
                "ring": ring_idx,
                "node_in_ring": node_idx,
                "srv6_locator": srv6_locator,
                "srv6_prefix": srv6_prefix,
                "isis_net": isis_net
            })
            
            node_id += 1
    
    print(f"Generated {len(nodes)} nodes")
    
    # 生成链路
    link_count = 0
    
    # 1. 环内链路
    print("Generating intra-ring links...")
    for ring_idx in range(NUM_RINGS):
        for node_idx in range(NODES_PER_RING):
            # 当前节点
            node1 = f"r{ring_idx}n{node_idx}"
            
            # 下一个节点（环形）
            next_node_idx = (node_idx + 1) % NODES_PER_RING
            node2 = f"r{ring_idx}n{next_node_idx}"
            
            # 子网分配：fc00:RRRR:NNNN:MMMM::/64
            # MMMM = next_node_idx
            subnet = f"fc00:{ring_idx:04x}:{node_idx:04x}:{next_node_idx:04x}::/64"
            
            links.append({
                "node1": node1,
                "node2": node2,
                "subnet": subnet,
                "type": "intra-ring"
            })
            link_count += 1
    
    print(f"  Generated {link_count} intra-ring links")
    
    # 2. 环间链路
    print("Generating inter-ring links...")
    inter_ring_count = 0
    for ring_idx in range(NUM_RINGS - 1):  # 0-18，不包括19（19不连接0）
        for node_idx in range(NODES_PER_RING):
            # 当前环的节点
            node1 = f"r{ring_idx}n{node_idx}"
            # 下一个环的同位置节点
            node2 = f"r{ring_idx + 1}n{node_idx}"
            
            # 子网分配：fc00:9000:RRRR:NNNN::/64
            # 9000前缀表示环间链路
            subnet = f"fc00:9000:{ring_idx:04x}:{node_idx:04x}::/64"
            
            links.append({
                "node1": node1,
                "node2": node2,
                "subnet": subnet,
                "type": "inter-ring"
            })
            inter_ring_count += 1
    
    print(f"  Generated {inter_ring_count} inter-ring links")
    print(f"Total links: {len(links)}")
    
    # 构建拓扑定义
    topology = {
        "network_name": "srv6-400node-net",
        "description": "400-node topology: 20 rings × 20 nodes/ring",
        "num_rings": NUM_RINGS,
        "nodes_per_ring": NODES_PER_RING,
        "total_nodes": len(nodes),
        "total_links": len(links),
        "nodes": nodes,
        "links": links
    }
    
    return topology


def save_topology(topology, filename="topology_400nodes.json"):
    """保存拓扑到文件"""
    with open(filename, 'w') as f:
        json.dump(topology, f, indent=2)
    print(f"\n✓ Topology saved to {filename}")
    
    # 打印统计信息
    print("\n" + "=" * 60)
    print("Topology Statistics:")
    print("=" * 60)
    print(f"Total nodes: {topology['total_nodes']}")
    print(f"Total links: {topology['total_links']}")
    print(f"Rings: {topology['num_rings']}")
    print(f"Nodes per ring: {topology['nodes_per_ring']}")
    
    # 计算链路类型统计
    intra_links = sum(1 for link in topology['links'] if link['type'] == 'intra-ring')
    inter_links = sum(1 for link in topology['links'] if link['type'] == 'inter-ring')
    print(f"Intra-ring links: {intra_links}")
    print(f"Inter-ring links: {inter_links}")
    
    # 示例节点
    print("\nExample nodes:")
    for i in [0, 19, 20, 39, 399]:
        if i < len(topology['nodes']):
            node = topology['nodes'][i]
            print(f"  {node['name']}: {node['srv6_locator']}")


if __name__ == "__main__":
    print("Generating 400-node ring topology...")
    print()
    
    topology = generate_400node_topology()
    save_topology(topology)
    
    print("\nNext steps:")
    print("  1. python3 generate_configs.py topology_400nodes.json")
    print("  2. sudo python3 deploy.py topology_400nodes.json")
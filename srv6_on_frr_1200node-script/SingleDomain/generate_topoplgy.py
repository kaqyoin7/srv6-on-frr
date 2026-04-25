# !/usr/bin/env python3
"""
生成1200节点拓扑定义
60个环，每个环20个节点
环内：每个节点与相邻节点连接形成环
环间：相邻环的同位置节点连接，环间链路不闭合（ring59与ring0无直连）
"""

import json

def generate_1200node_topology():
    """
    生成1200节点拓扑
    拓扑结构：60个环 x 20个节点/环 = 1200节点
    环间链路不闭合（ring59与ring0无直连）
    """

    NUM_RINGS = 60       # 环数量
    NODES_PER_RING = 20  # 每个环的节点数

    nodes = []
    links = []

    print(f"Generating topology: {NUM_RINGS} rings × {NODES_PER_RING} nodes/ring = {NUM_RINGS * NODES_PER_RING} nodes")

    # 生成节点
    node_id = 1
    for ring_idx in range(NUM_RINGS):
        for node_idx in range(NODES_PER_RING):
            # 节点名称: r0n0, r0n1, ..., r59n19
            node_name = f"r{ring_idx}n{node_idx}"

            # SRv6地址分配：fc00:RRRR:NNNN::1
            ring_hex = f"{ring_idx:04x}"
            node_hex = f"{node_idx:04x}"
            srv6_locator = f"fc00:{ring_hex}:{node_hex}::1/128"
            srv6_prefix  = f"fc00:{ring_hex}:{node_hex}::/48"

            # ISIS NET: 49.0000.RRRR.NNNN.00
            isis_net = f"49.0000.{ring_idx:04x}.{node_idx:04x}.00"

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

    # 1. 环内链路（每环20条，形成闭合环，共 60×20 = 1200条）
    print("Generating intra-ring links...")
    intra_count = 0
    for ring_idx in range(NUM_RINGS):
        for node_idx in range(NODES_PER_RING):
            node1 = f"r{ring_idx}n{node_idx}"
            next_node_idx = (node_idx + 1) % NODES_PER_RING
            node2 = f"r{ring_idx}n{next_node_idx}"

            subnet = f"fc00:{ring_idx:04x}:{node_idx:04x}:{next_node_idx:04x}::/64"

            links.append({
                "node1": node1,
                "node2": node2,
                "subnet": subnet,
                "type": "intra-ring"
            })
            intra_count += 1

    print(f"  Generated {intra_count} intra-ring links")

    # ── 环间链路（不闭合：ring0-ring1 ... ring58-ring59，共59x20=1180条）──
    print("Generating inter-ring links (closed, ring59 -> ring0 included)...")
    inter_count = 0
    for ring_idx in range(NUM_RINGS - 1):
        next_ring_idx = (ring_idx + 1) % NUM_RINGS   # ring59的下一个为ring0
        for node_idx in range(NODES_PER_RING):
            node1 = f"r{ring_idx}n{node_idx}"
            node2 = f"r{next_ring_idx}n{node_idx}"

            # fc00:9000:RRRR:NNNN::/64，9000前缀标识环间链路
            subnet = f"fc00:9000:{ring_idx:04x}:{node_idx:04x}::/64"

            links.append({
                "node1": node1,
                "node2": node2,
                "subnet": subnet,
                "type": "inter-ring"
            })
            inter_count += 1

    print(f"  Generated {inter_count} inter-ring links (including ring59<->ring0)")
    print(f"Total links: {len(links)}")

    topology = {
        "network_name": "srv6-1200node-net",
        "description": "1200-node topology: 60 rings × 20 nodes/ring, fully closed inter-ring",
        "num_rings": NUM_RINGS,
        "nodes_per_ring": NODES_PER_RING,
        "total_nodes": len(nodes),
        "total_links": len(links),
        "nodes": nodes,
        "links": links
    }

    return topology


def save_topology(topology, filename="topology_1200nodes.json"):
    with open(filename, 'w') as f:
        json.dump(topology, f, indent=2)
    print(f"\n✓ Topology saved to {filename}")

    print("\n" + "=" * 60)
    print("Topology Statistics:")
    print("=" * 60)
    print(f"Total nodes : {topology['total_nodes']}")
    print(f"Total links : {topology['total_links']}")
    print(f"Rings       : {topology['num_rings']}")
    print(f"Nodes/ring  : {topology['nodes_per_ring']}")

    intra = sum(1 for l in topology['links'] if l['type'] == 'intra-ring')
    inter = sum(1 for l in topology['links'] if l['type'] == 'inter-ring')
    print(f"Intra-ring links : {intra}")
    print(f"Inter-ring links : {inter}  (closed torus, ring59<->ring0 included)")

    print("\nExample nodes:")
    for i in [0, 19, 20, 39, 600, 1199]:
        if i < len(topology['nodes']):
            node = topology['nodes'][i]
            print(f"  {node['name']}: {node['srv6_locator']}")


if __name__ == "__main__":
    print("Generating 1200-node ring topology (60 rings × 20 nodes, closed)...")
    print()

    topology = generate_1200node_topology()
    save_topology(topology)

    print("\nNext steps:")
    print("  1. python3 generate_configs.py topology_1200nodes.json")
    print("  2. sudo python3 deploy.py topology_1200nodes.json")



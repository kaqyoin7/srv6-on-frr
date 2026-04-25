#!/usr/bin/env python3
"""
生成1200节点拓扑定义（BGP分域版本）
拓扑结构：60个环 × 20个节点/环 = 1200节点
分为4个BGP域，每域15个环（300节点）

域划分：
  域A (AS65001): ring0  - ring14
  域B (AS65002): ring15 - ring29
  域C (AS65003): ring30 - ring44
  域D (AS65004): ring45 - ring59

边界节点（eBGP对等点）：
  r14n0 <-> r15n0  (A-B域间)
  r29n0 <-> r30n0  (B-C域间)
  r44n0 <-> r45n0  (C-D域间)

域内边界节点对（iBGP）：
  r15n0 <-> r29n0  (B域两端)
  r30n0 <-> r44n0  (C域两端)
"""

import json

# ── 域定义 ────────────────────────────────────────────────────────────────────
DOMAIN_CONFIG = {
    'A': {'asn': 65001, 'rings': (0,  14), 'area': '49.0001'},
    'B': {'asn': 65002, 'rings': (15, 29), 'area': '49.0002'},
    'C': {'asn': 65003, 'rings': (30, 44), 'area': '49.0003'},
    'D': {'asn': 65004, 'rings': (45, 59), 'area': '49.0004'},
}

# eBGP对等关系（跨域直连链路端点，node_in_ring=0）
EBGP_PEERS = [
    ('r14n0', 'r15n0'),   # A-B
    ('r29n0', 'r30n0'),   # B-C
    ('r44n0', 'r45n0'),   # C-D
]

# iBGP对等关系（同域两端边界节点）
IBGP_PEERS = [
    ('r15n0', 'r29n0'),   # B域内
    ('r30n0', 'r44n0'),   # C域内
]

NUM_RINGS      = 60
NODES_PER_RING = 20


def get_domain(ring_idx):
    """根据环索引返回域名称"""
    for domain, cfg in DOMAIN_CONFIG.items():
        start, end = cfg['rings']
        if start <= ring_idx <= end:
            return domain
    raise ValueError(f"ring {ring_idx} not in any domain")


def is_border_node(node_name):
    """判断节点是否是边界节点（eBGP或iBGP端点）"""
    all_border = set()
    for a, b in EBGP_PEERS:
        all_border.add(a)
        all_border.add(b)
    for a, b in IBGP_PEERS:
        all_border.add(a)
        all_border.add(b)
    return node_name in all_border


def is_dual_isis_node(node_name, ring_idx, node_idx):
    """
    判断节点是否需要双IS-IS实例。
    规则：边界环（14,15,29,30,44,45）上的 n0 节点运行双IS-IS：
      - n0 of ring14: IS-IS A + IS-IS B（通过跨域链路与ring15邻接）
      - n0 of ring15: IS-IS B + IS-IS A（通过跨域链路与ring14邻接）
      - 依此类推
    """
    DUAL_ISIS_NODES = {
        'r14n0', 'r15n0',
        'r29n0', 'r30n0',
        'r44n0', 'r45n0',
    }
    return node_name in DUAL_ISIS_NODES


def get_bgp_ring_aggregates(domain):
    """返回该域需要在BGP中发布的per-ring聚合前缀列表"""
    start, end = DOMAIN_CONFIG[domain]['rings']
    return [f'fc00:{ring:04x}::/32' for ring in range(start, end + 1)]


def generate_topology():
    nodes = []
    links = []

    print(f"Generating topology: {NUM_RINGS} rings × {NODES_PER_RING} nodes/ring = "
          f"{NUM_RINGS * NODES_PER_RING} nodes")

    # ── 节点生成 ──────────────────────────────────────────────────────────────
    node_id = 1
    for ring_idx in range(NUM_RINGS):
        domain = get_domain(ring_idx)
        domain_cfg = DOMAIN_CONFIG[domain]

        for node_idx in range(NODES_PER_RING):
            node_name   = f"r{ring_idx}n{node_idx}"
            ring_hex    = f"{ring_idx:04x}"
            node_hex    = f"{node_idx:04x}"
            srv6_locator = f"fc00:{ring_hex}:{node_hex}::1/128"
            srv6_prefix  = f"fc00:{ring_hex}:{node_hex}::/48"

            # ISIS NET 使用域专属 area
            isis_net_base = f"{ring_idx:04x}.{node_idx:04x}"
            isis_net      = f"{domain_cfg['area']}.{isis_net_base}.00"

            dual_isis = is_dual_isis_node(node_name, ring_idx, node_idx)

            # 双IS-IS节点需要两个 ISIS NET（两个area各一个）
            secondary_domain = None
            secondary_isis_net = None
            if dual_isis:
                # 找对端域
                for a, b in EBGP_PEERS:
                    if node_name == a:
                        # 本节点在边界左侧，secondary是右侧域
                        peer_ring = int(b[1:b.index('n')])
                        secondary_domain = get_domain(peer_ring)
                    elif node_name == b:
                        peer_ring = int(a[1:a.index('n')])
                        secondary_domain = get_domain(peer_ring)
                if secondary_domain:
                    sec_area = DOMAIN_CONFIG[secondary_domain]['area']
                    secondary_isis_net = f"{sec_area}.{isis_net_base}.00"

            node_entry = {
                "name":              node_name,
                "id":                node_id,
                "ring":              ring_idx,
                "node_in_ring":      node_idx,
                "domain":            domain,
                "asn":               domain_cfg['asn'],
                "isis_area":         domain_cfg['area'],
                "isis_net":          isis_net,
                "srv6_locator":      srv6_locator,
                "srv6_prefix":       srv6_prefix,
                "dual_isis":         dual_isis,
                "secondary_domain":  secondary_domain,
                "secondary_isis_net": secondary_isis_net,
                "is_border":         is_border_node(node_name),
            }
            nodes.append(node_entry)
            node_id += 1

    print(f"Generated {len(nodes)} nodes")

    # ── 环内链路 ──────────────────────────────────────────────────────────────
    print("Generating intra-ring links...")
    intra_count = 0
    for ring_idx in range(NUM_RINGS):
        domain = get_domain(ring_idx)
        for node_idx in range(NODES_PER_RING):
            node1 = f"r{ring_idx}n{node_idx}"
            next_node_idx = (node_idx + 1) % NODES_PER_RING
            node2 = f"r{ring_idx}n{next_node_idx}"
            subnet = f"fc00:{ring_idx:04x}:{node_idx:04x}:{next_node_idx:04x}::/64"
            links.append({
                "node1":       node1,
                "node2":       node2,
                "subnet":      subnet,
                "type":        "intra-ring",
                "cross_domain": False,
                "domain":      domain,
            })
            intra_count += 1
    print(f"  Generated {intra_count} intra-ring links")

    # ── 环间链路 ──────────────────────────────────────────────────────────────
    print("Generating inter-ring links...")
    inter_count = 0
    cross_count = 0
    for ring_idx in range(NUM_RINGS - 1):
        next_ring_idx = ring_idx + 1
        domain1 = get_domain(ring_idx)
        domain2 = get_domain(next_ring_idx)
        cross = (domain1 != domain2)

        for node_idx in range(NODES_PER_RING):
            node1  = f"r{ring_idx}n{node_idx}"
            node2  = f"r{next_ring_idx}n{node_idx}"
            subnet = f"fc00:9000:{ring_idx:04x}:{node_idx:04x}::/64"
            links.append({
                "node1":        node1,
                "node2":        node2,
                "subnet":       subnet,
                "type":         "inter-ring",
                "cross_domain": cross,
                "domain":       domain1 if not cross else f"{domain1}-{domain2}",
            })
            inter_count += 1
            if cross:
                cross_count += 1

    print(f"  Generated {inter_count} inter-ring links ({cross_count} cross-domain)")
    print(f"Total links: {len(links)}")

    # ── 域元数据 ──────────────────────────────────────────────────────────────
    domain_metadata = {}
    for domain, cfg in DOMAIN_CONFIG.items():
        start, end = cfg['rings']
        domain_metadata[domain] = {
            "asn":          cfg['asn'],
            "rings":        [start, end],
            "node_count":   (end - start + 1) * NODES_PER_RING,
            "isis_area":    cfg['area'],
            "bgp_aggregates": get_bgp_ring_aggregates(domain),
            "border_nodes": [],
        }

    # 填充边界节点信息
    for name in ['r14n0','r15n0','r29n0','r30n0','r44n0','r45n0']:
        ring_idx = int(name[1:name.index('n')])
        domain   = get_domain(ring_idx)
        domain_metadata[domain]["border_nodes"].append(name)

    topology = {
        "network_name":    "srv6-1200node-bgp-net",
        "description":     "1200-node topology: 60 rings × 20 nodes/ring, BGP 4-domain",
        "num_rings":       NUM_RINGS,
        "nodes_per_ring":  NODES_PER_RING,
        "total_nodes":     len(nodes),
        "total_links":     len(links),
        "domains":         domain_metadata,
        "ebgp_peers":      [{"node1": a, "node2": b} for a, b in EBGP_PEERS],
        "ibgp_peers":      [{"node1": a, "node2": b} for a, b in IBGP_PEERS],
        "nodes":           nodes,
        "links":           links,
    }
    return topology


def save_topology(topology, filename="topology_1200nodes_bgp.json"):
    with open(filename, 'w') as f:
        json.dump(topology, f, indent=2)
    print(f"\n✓ Topology saved to {filename}")

    print("\n" + "=" * 60)
    print("Topology Statistics:")
    print("=" * 60)
    print(f"Total nodes : {topology['total_nodes']}")
    print(f"Total links : {topology['total_links']}")

    intra  = sum(1 for l in topology['links'] if l['type'] == 'intra-ring')
    inter  = sum(1 for l in topology['links'] if l['type'] == 'inter-ring')
    cross  = sum(1 for l in topology['links'] if l.get('cross_domain'))
    border = sum(1 for n in topology['nodes'] if n['is_border'])
    dual   = sum(1 for n in topology['nodes'] if n['dual_isis'])

    print(f"Intra-ring links  : {intra}")
    print(f"Inter-ring links  : {inter}  (cross-domain: {cross})")
    print(f"Border nodes      : {border}  (dual-ISIS: {dual})")

    print("\nDomain Summary:")
    for domain, meta in topology['domains'].items():
        print(f"  Domain {domain} (AS{meta['asn']}): "
              f"ring{meta['rings'][0]}-ring{meta['rings'][1]}, "
              f"{meta['node_count']} nodes, "
              f"border={meta['border_nodes']}")

    print("\neBGP Sessions:")
    for peer in topology['ebgp_peers']:
        print(f"  {peer['node1']} <-> {peer['node2']}")

    print("\niBGP Sessions:")
    for peer in topology['ibgp_peers']:
        print(f"  {peer['node1']} <-> {peer['node2']}")


if __name__ == "__main__":
    print("Generating 1200-node BGP-domain topology...")
    print()
    topology = generate_topology()
    save_topology(topology)

    print("\nNext steps:")
    print("  1. python3 generate_configs_bgp.py topology_1200nodes_bgp.json")
    print("  2. sudo python3 deploy_5Network.py topology_1200nodes_bgp.json")

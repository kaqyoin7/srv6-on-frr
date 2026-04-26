#!/usr/bin/env python3
"""
Generate a 1200-node SRv6-over-FRR topology with 4 IS-IS areas.
"""

import ipaddress
import json

NUM_RINGS = 60
NODES_PER_RING = 20
AREA_RANGES = [
    (0, 14),
    (15, 29),
    (30, 44),
    (45, 59),
]
BOUNDARY_RINGS = {14, 15, 29, 30, 44, 45}


def get_area_id(ring_idx):
    """Return the 1-based area ID for a ring index."""
    for area_id, (start, end) in enumerate(AREA_RANGES, start=1):
        if start <= ring_idx <= end:
            return area_id
    raise ValueError(f"ring_idx {ring_idx} out of range")


def get_is_type(ring_idx):
    """Boundary rings are L1/L2, everything else is L1-only."""
    return "level-1-2" if ring_idx in BOUNDARY_RINGS else "level-1"


def get_ring_summary_prefix(ring_idx):
    """
    Return a summary prefix that covers all per-node /48 locator prefixes in a ring.

    Node IDs use the third hextet and span 0x0000-0x0013. A /43 covers 0x0000-0x001f.
    """
    return ipaddress.IPv6Network(f"fc00:{ring_idx:04x}::/43")


def build_area_summaries():
    """Build collapsed IPv6 summaries for every area."""
    summaries = []
    for area_id, (start, end) in enumerate(AREA_RANGES, start=1):
        ring_prefixes = [get_ring_summary_prefix(ring) for ring in range(start, end + 1)]
        collapsed = list(ipaddress.collapse_addresses(ring_prefixes))
        summaries.append({
            "area_id": area_id,
            "ring_start": start,
            "ring_end": end,
            "summary_prefixes": [str(prefix) for prefix in collapsed],
            "ring_summary_prefixes": [str(prefix) for prefix in ring_prefixes],
        })
    return summaries


def generate_1200node_topology():
    nodes = []
    links = []

    print(
        f"Generating topology: {NUM_RINGS} rings x {NODES_PER_RING} nodes/ring "
        f"= {NUM_RINGS * NODES_PER_RING} nodes"
    )
    print("IS-IS areas: 4  (rings 0-14 / 15-29 / 30-44 / 45-59)")
    print(f"Boundary rings (L1/L2): {sorted(BOUNDARY_RINGS)}")
    print("Inter-ring links: open chain only (ring59 <-> ring0 disconnected)")
    print()

    node_id = 1
    for ring_idx in range(NUM_RINGS):
        area_id = get_area_id(ring_idx)
        is_type = get_is_type(ring_idx)

        for node_idx in range(NODES_PER_RING):
            node_name = f"r{ring_idx}n{node_idx}"
            ring_hex = f"{ring_idx:04x}"
            node_hex = f"{node_idx:04x}"

            srv6_locator = f"fc00:{ring_hex}:{node_hex}::1/128"
            srv6_prefix = f"fc00:{ring_hex}:{node_hex}::/48"
            isis_net = f"49.{area_id:04d}.0000.{ring_hex}.{node_hex}.00"

            nodes.append({
                "name": node_name,
                "id": node_id,
                "ring": ring_idx,
                "node_in_ring": node_idx,
                "area_id": area_id,
                "is_type": is_type,
                "srv6_locator": srv6_locator,
                "srv6_prefix": srv6_prefix,
                "isis_net": isis_net,
            })
            node_id += 1

    print(f"Generated {len(nodes)} nodes")

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
                "type": "intra-ring",
            })
            intra_count += 1

    print(f"  Generated {intra_count} intra-ring links")

    print("Generating inter-ring links (ring59 -> ring0 excluded)...")
    inter_count = 0
    for ring_idx in range(NUM_RINGS - 1):
        next_ring_idx = ring_idx + 1
        for node_idx in range(NODES_PER_RING):
            node1 = f"r{ring_idx}n{node_idx}"
            node2 = f"r{next_ring_idx}n{node_idx}"
            subnet = f"fc00:9000:{ring_idx:04x}:{node_idx:04x}::/64"

            links.append({
                "node1": node1,
                "node2": node2,
                "subnet": subnet,
                "type": "inter-ring",
            })
            inter_count += 1

    print(f"  Generated {inter_count} inter-ring links")
    print(f"Total links: {len(links)}")

    topology = {
        "network_name": "srv6-1200node-net",
        "description": (
            "1200-node topology: 60 rings x 20 nodes/ring, 4 IS-IS areas, "
            "inter-ring open (no ring59<->ring0)"
        ),
        "num_rings": NUM_RINGS,
        "nodes_per_ring": NODES_PER_RING,
        "total_nodes": len(nodes),
        "total_links": len(links),
        "area_ranges": [
            {"area_id": i + 1, "ring_start": start, "ring_end": end}
            for i, (start, end) in enumerate(AREA_RANGES)
        ],
        "area_summaries": build_area_summaries(),
        "boundary_rings": sorted(BOUNDARY_RINGS),
        "nodes": nodes,
        "links": links,
    }
    return topology


def save_topology(topology, filename="topology_1200nodes.json"):
    with open(filename, "w") as f:
        json.dump(topology, f, indent=2)
    print(f"\nTopology saved to {filename}")

    print("\n" + "=" * 60)
    print("Topology Statistics:")
    print("=" * 60)
    print(f"Total nodes  : {topology['total_nodes']}")
    print(f"Total links  : {topology['total_links']}")
    print(f"Rings        : {topology['num_rings']}")
    print(f"Nodes/ring   : {topology['nodes_per_ring']}")

    intra = sum(1 for link in topology["links"] if link["type"] == "intra-ring")
    inter = sum(1 for link in topology["links"] if link["type"] == "inter-ring")
    l1l2 = sum(1 for node in topology["nodes"] if node["is_type"] == "level-1-2")
    l1 = sum(1 for node in topology["nodes"] if node["is_type"] == "level-1")
    print(f"Intra-ring links : {intra}")
    print(f"Inter-ring links : {inter}  (open, ring59<->ring0 disconnected)")
    print(f"L1 nodes         : {l1}")
    print(f"L1/L2 nodes      : {l1l2}  (boundary rings: {topology['boundary_rings']})")

    print("\nArea breakdown:")
    for area in topology["area_ranges"]:
        num_nodes = (area["ring_end"] - area["ring_start"] + 1) * topology["nodes_per_ring"]
        print(
            f"  Area {area['area_id']}: ring{area['ring_start']:02d} ~ "
            f"ring{area['ring_end']:02d}  ({num_nodes} nodes)"
        )

    print("\nArea summaries:")
    for area in topology["area_summaries"]:
        prefixes = ", ".join(area["summary_prefixes"])
        print(f"  Area {area['area_id']}: {prefixes}")

    print("\nExample nodes:")
    for i in [0, 19, 280, 299, 300, 319, 1180, 1199]:
        if i < len(topology["nodes"]):
            node = topology["nodes"][i]
            print(
                f"  {node['name']:10s}: area={node['area_id']}  "
                f"is_type={node['is_type']:12s}  locator={node['srv6_locator']}"
            )


if __name__ == "__main__":
    print("Generating 1200-node topology (60x20, 4 IS-IS areas, open inter-ring)...")
    print()
    topology = generate_1200node_topology()
    save_topology(topology)
    print("\nNext steps:")
    print("  1. python3 generate_configs.py topology_1200nodes.json")
    print("  2. sudo python3 deploy_5Network.py topology_1200nodes.json")

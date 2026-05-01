#!/usr/bin/env python3
"""
Generate a multi-area SRv6-over-FRR topology for HOST 1.

Host 1 deploys rings 0-29 (3 areas, 10 rings each).

Cross-host boundary:
  - Ring 29 is an EXTRA boundary ring toward host2's ring 30.
  - r29n0 is boundary-backbone; its r29n0<->r30n0 link is level-2-only.
  - r29n1..71 <-> r30n1..71 links are level-1.
  - All r29nx<->r30nx interfaces are created on the r29 side (inside containers).
  - r30nx<->r29nx stubs are left on the host1 hypervisor for bridging to host2.
  - Ring21n0 through Ring28n0 are transit-backbone (L2-only) nodes, extending
    the backbone column from the intra-host backbone (ring9-20) out to ring29.

IS-IS area layout:
  Area 1: rings  0- 9
  Area 2: rings 10-19
  Area 3: rings 20-29

Intra-host boundary rings (L1/L2): {9, 10, 19, 20}
Cross-host boundary ring  (L1/L2): {29}   <- new, toward host2
Backbone column (node_in_ring=0):  rings 9-29
"""

import ipaddress
import json
import math

# ---------------------------------------------------------------------------
# Scale
# ---------------------------------------------------------------------------
NUM_RINGS = 30          # rings 0-29
NODES_PER_RING = 72

# ---------------------------------------------------------------------------
# IS-IS area layout  (intra-host)
# ---------------------------------------------------------------------------
AREA_RANGES = [
    (0, 9),
    (10, 19),
    (20, 29),
]

# Intra-host boundary rings (L1/L2 nodes inside host1)
INTRA_BOUNDARY_RINGS = {9, 10, 19, 20}

# Cross-host boundary ring: ring29 peers with host2's ring30
CROSS_HOST_BOUNDARY_RING = 29      # on host1 side
CROSS_HOST_PEER_RING     = 30      # on host2 side (not deployed here)

# All rings that carry L1/L2 nodes
BOUNDARY_RINGS = INTRA_BOUNDARY_RINGS | {CROSS_HOST_BOUNDARY_RING}

# ---------------------------------------------------------------------------
# Backbone column (node_in_ring index 0)
# Extended from intra-host backbone (9-20) all the way to cross-host edge (29)
# ---------------------------------------------------------------------------
BACKBONE_NODE_IN_RING = 0
BACKBONE_RING_START   = 9     # first intra-host backbone ring
BACKBONE_RING_END     = 29    # extended to cross-host boundary


def bits_needed(count):
    if count <= 1:
        return 0
    return math.ceil(math.log2(count))


def get_area_id(ring_idx):
    for area_id, (start, end) in enumerate(AREA_RANGES, start=1):
        if start <= ring_idx <= end:
            return area_id
    raise ValueError(f"ring_idx {ring_idx} out of range")


def get_is_type(ring_idx):
    """Boundary rings run L1/L2; everything else is L1-only."""
    return "level-1-2" if ring_idx in BOUNDARY_RINGS else "level-1"


def get_node_role(ring_idx, node_idx):
    """
    Classify every node.

    boundary-backbone : L1/L2 node on the backbone column (n0)
    transit-backbone  : L2-only node on the backbone column between boundary rings
    area              : regular L1 node
    """
    if node_idx != BACKBONE_NODE_IN_RING:
        return "area"

    if ring_idx in BOUNDARY_RINGS:
        return "boundary-backbone"

    # Extended backbone: rings 21-28 (between intra-host boundary ring20 and
    # cross-host boundary ring29) are transit-backbone L2-only nodes.
    if BACKBONE_RING_START < ring_idx < BACKBONE_RING_END:
        return "transit-backbone"

    return "area"


# ---------------------------------------------------------------------------
# Address helpers
# ---------------------------------------------------------------------------

def get_ring_summary_prefix(ring_idx):
    node_bits = bits_needed(NODES_PER_RING)
    prefix_len = 48 - node_bits
    return ipaddress.IPv6Network(f"fc00:{ring_idx:04x}::/{prefix_len}")


def get_inter_ring_summary_prefix(ring_idx):
    node_bits = bits_needed(NODES_PER_RING)
    prefix_len = 64 - node_bits
    return ipaddress.IPv6Network(f"fc00:9000:{ring_idx:04x}::/{prefix_len}")


def build_area_summaries():
    summaries = []
    for area_id, (start, end) in enumerate(AREA_RANGES, start=1):
        ring_prefixes = [get_ring_summary_prefix(r) for r in range(start, end + 1)]
        inter_ring_prefixes = [get_inter_ring_summary_prefix(r) for r in range(start, end + 1)]
        collapsed = list(ipaddress.collapse_addresses(ring_prefixes + inter_ring_prefixes))
        summaries.append(
            {
                "area_id": area_id,
                "ring_start": start,
                "ring_end": end,
                "summary_prefixes": [str(p) for p in collapsed],
                "ring_summary_prefixes": [str(p) for p in ring_prefixes],
                "inter_ring_summary_prefixes": [str(p) for p in inter_ring_prefixes],
            }
        )
    return summaries


# ---------------------------------------------------------------------------
# Topology generation
# ---------------------------------------------------------------------------

def generate_topology():
    nodes = []
    links = []

    print(
        f"[Host1] Generating topology: {NUM_RINGS} rings x {NODES_PER_RING} nodes/ring "
        f"= {NUM_RINGS * NODES_PER_RING} nodes"
    )
    print(f"IS-IS areas: {len(AREA_RANGES)}")
    for area_id, (start, end) in enumerate(AREA_RANGES, start=1):
        print(f"  Area {area_id}: rings {start}-{end}")
    print(f"Intra-host boundary rings (L1/L2): {sorted(INTRA_BOUNDARY_RINGS)}")
    print(f"Cross-host boundary ring  (L1/L2): {CROSS_HOST_BOUNDARY_RING} (peers with ring {CROSS_HOST_PEER_RING} on host2)")
    print(f"Backbone column: node_in_ring={BACKBONE_NODE_IN_RING}, rings {BACKBONE_RING_START}-{BACKBONE_RING_END}")
    print()

    node_id = 1
    for ring_idx in range(NUM_RINGS):
        area_id = get_area_id(ring_idx)
        is_type = get_is_type(ring_idx)

        for node_idx in range(NODES_PER_RING):
            node_name = f"r{ring_idx}n{node_idx}"
            ring_hex  = f"{ring_idx:04x}"
            node_hex  = f"{node_idx:04x}"

            srv6_locator = f"fc00:{ring_hex}:{node_hex}::1/128"
            srv6_prefix  = f"fc00:{ring_hex}:{node_hex}::/48"
            isis_net     = f"49.{area_id:04d}.0000.{ring_hex}.{node_hex}.00"

            nodes.append(
                {
                    "name":        node_name,
                    "id":          node_id,
                    "ring":        ring_idx,
                    "node_in_ring": node_idx,
                    "area_id":     area_id,
                    "is_type":     is_type,
                    "node_role":   get_node_role(ring_idx, node_idx),
                    "srv6_locator": srv6_locator,
                    "srv6_prefix": srv6_prefix,
                    "isis_net":    isis_net,
                }
            )
            node_id += 1

    print(f"Generated {len(nodes)} nodes")

    # ------------------------------------------------------------------
    # Intra-ring links (full rings for rings 0-29)
    # ------------------------------------------------------------------
    print("Generating intra-ring links...")
    intra_count = 0
    for ring_idx in range(NUM_RINGS):
        for node_idx in range(NODES_PER_RING):
            node1 = f"r{ring_idx}n{node_idx}"
            next_node_idx = (node_idx + 1) % NODES_PER_RING
            node2 = f"r{ring_idx}n{next_node_idx}"
            subnet = f"fc00:{ring_idx:04x}:{node_idx:04x}:{next_node_idx:04x}::/64"
            links.append({"node1": node1, "node2": node2, "subnet": subnet, "type": "intra-ring"})
            intra_count += 1
    print(f"  Generated {intra_count} intra-ring links")

    # ------------------------------------------------------------------
    # Inter-ring links: rings 0-28 -> 1-29  (normal intra-host chain)
    # ------------------------------------------------------------------
    print("Generating intra-host inter-ring links (rings 0-28 -> 1-29)...")
    inter_intra_count = 0
    for ring_idx in range(NUM_RINGS - 1):          # 0..28
        next_ring_idx = ring_idx + 1
        for node_idx in range(NODES_PER_RING):
            node1  = f"r{ring_idx}n{node_idx}"
            node2  = f"r{next_ring_idx}n{node_idx}"
            subnet = f"fc00:9000:{ring_idx:04x}:{node_idx:04x}::/64"
            links.append({"node1": node1, "node2": node2, "subnet": subnet, "type": "inter-ring"})
            inter_intra_count += 1
    print(f"  Generated {inter_intra_count} intra-host inter-ring links")

    # ------------------------------------------------------------------
    # Cross-host inter-ring links: ring29 -> ring30
    #
    # These links are REAL on the r29 side (interfaces go into r29 containers).
    # The r30 stub interfaces are left on the host1 hypervisor for bridging.
    # Mark with type="cross-host-inter-ring" so the deploy script can treat
    # them differently (r29 end -> container, r30 end -> hypervisor).
    # ------------------------------------------------------------------
    print(f"Generating cross-host inter-ring links (ring{CROSS_HOST_BOUNDARY_RING} -> ring{CROSS_HOST_PEER_RING})...")
    cross_count = 0
    for node_idx in range(NODES_PER_RING):
        node1  = f"r{CROSS_HOST_BOUNDARY_RING}n{node_idx}"   # lives on host1
        node2  = f"r{CROSS_HOST_PEER_RING}n{node_idx}"       # lives on host2
        subnet = f"fc00:9000:{CROSS_HOST_BOUNDARY_RING:04x}:{node_idx:04x}::/64"
        links.append(
            {
                "node1":  node1,
                "node2":  node2,
                "subnet": subnet,
                "type":   "cross-host-inter-ring",   # special marker
                # deployment hint: node1 iface -> container, node2 iface -> hypervisor
                "deploy_node1": "container",
                "deploy_node2": "hypervisor",
            }
        )
        cross_count += 1
    print(f"  Generated {cross_count} cross-host inter-ring links")
    print(f"  r29nx->r30nx : deployed inside r29 containers")
    print(f"  r30nx->r29nx : left on host1 hypervisor (to be bridged to host2)")

    print(f"Total links: {len(links)}")

    topology = {
        "host":         "host1",
        "network_name": f"srv6-host1-{len(nodes)}node-net",
        "description": (
            f"Host1: {len(nodes)}-node topology, rings 0-29, 3 IS-IS areas. "
            f"Cross-host boundary at ring29 <-> ring30 (host2)."
        ),
        "num_rings":       NUM_RINGS,
        "nodes_per_ring":  NODES_PER_RING,
        "total_nodes":     len(nodes),
        "total_links":     len(links),
        "area_ranges": [
            {"area_id": i + 1, "ring_start": start, "ring_end": end}
            for i, (start, end) in enumerate(AREA_RANGES)
        ],
        "area_summaries":  build_area_summaries(),
        "boundary_rings":  sorted(BOUNDARY_RINGS),
        "intra_boundary_rings": sorted(INTRA_BOUNDARY_RINGS),
        "cross_host_boundary": {
            "local_ring":  CROSS_HOST_BOUNDARY_RING,
            "remote_ring": CROSS_HOST_PEER_RING,
            "remote_host": "host2",
        },
        "backbone": {
            "node_in_ring": BACKBONE_NODE_IN_RING,
            "ring_start":   BACKBONE_RING_START,
            "ring_end":     BACKBONE_RING_END,
        },
        "nodes": nodes,
        "links": links,
    }
    return topology


def save_topology(topology, filename=None):
    if filename is None:
        filename = f"topology_host1_{topology['total_nodes']}nodes.json"

    with open(filename, "w") as f:
        json.dump(topology, f, indent=2)
    print(f"\nTopology saved to {filename}")

    print("\n" + "=" * 60)
    print("Host1 Topology Statistics:")
    print("=" * 60)
    print(f"Total nodes  : {topology['total_nodes']}")
    print(f"Total links  : {topology['total_links']}")
    print(f"Rings        : {topology['num_rings']} (0-29)")

    intra      = sum(1 for l in topology["links"] if l["type"] == "intra-ring")
    inter      = sum(1 for l in topology["links"] if l["type"] == "inter-ring")
    cross      = sum(1 for l in topology["links"] if l["type"] == "cross-host-inter-ring")
    l1l2_nodes = sum(1 for n in topology["nodes"] if n["is_type"] == "level-1-2")
    l1_nodes   = sum(1 for n in topology["nodes"] if n["is_type"] == "level-1")
    bb_bound   = sum(1 for n in topology["nodes"] if n["node_role"] == "boundary-backbone")
    bb_transit = sum(1 for n in topology["nodes"] if n["node_role"] == "transit-backbone")

    print(f"Intra-ring links       : {intra}")
    print(f"Intra-host inter-ring  : {inter}")
    print(f"Cross-host inter-ring  : {cross}  (ring29 <-> ring30)")
    print(f"L1 nodes               : {l1_nodes}")
    print(f"L1/L2 nodes            : {l1l2_nodes}  (boundary: {sorted(topology['boundary_rings'])})")
    print(f"Backbone boundary nodes: {bb_bound}")
    print(f"Backbone transit nodes : {bb_transit}  (rings 21-28, n0 only)")

    print("\nArea breakdown:")
    for area in topology["area_ranges"]:
        n = (area["ring_end"] - area["ring_start"] + 1) * topology["nodes_per_ring"]
        print(f"  Area {area['area_id']}: ring{area['ring_start']:02d}~ring{area['ring_end']:02d}  ({n} nodes)")

    print("\nArea summaries:")
    for area in topology["area_summaries"]:
        prefixes = ", ".join(area["summary_prefixes"])
        print(f"  Area {area['area_id']}: {prefixes}")

    print("\nBackbone column (n0) roles:")
    for ring_idx in range(BACKBONE_RING_START, BACKBONE_RING_END + 1):
        node = f"r{ring_idx}n0"
        role = get_node_role(ring_idx, 0)
        is_t = get_is_type(ring_idx)
        print(f"  {node:10s}: is_type={is_t:12s}  role={role}")

    return filename


if __name__ == "__main__":
    print("=" * 60)
    print("HOST 1 topology generator (rings 0-29)")
    print("=" * 60)
    print()
    topology = generate_topology()
    output_file = save_topology(topology)
    print("\nNext steps:")
    print(f"  1. python3 generate_configs.py {output_file}")
    print(f"  2. sudo python3 deploy_host1.py {output_file}")

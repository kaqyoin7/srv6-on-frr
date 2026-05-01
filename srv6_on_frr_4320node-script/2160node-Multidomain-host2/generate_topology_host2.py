#!/usr/bin/env python3
"""
Generate a multi-area SRv6-over-FRR topology for HOST 2.

Host 2 deploys rings 30-59 (3 areas, 10 rings each).

Cross-host boundary:
  - Ring 30 is an EXTRA boundary ring toward host1's ring 29.
  - r30n0 is boundary-backbone; its r30n0<->r29n0 link is level-2-only.
  - r30n1..71 <-> r29n1..71 links are level-1.
  - All r30nx<->r29nx interfaces are created on the r30 side (inside containers).
  - r29nx<->r30nx stubs are left on the host2 hypervisor for bridging to host1.
  - Ring31n0 through Ring38n0 are transit-backbone (L2-only) nodes, extending
    the backbone column from the cross-host boundary (ring30) to the first
    intra-host boundary ring (ring39).

IS-IS area layout:
  Area 4: rings 30-39
  Area 5: rings 40-49
  Area 6: rings 50-59

Intra-host boundary rings (L1/L2): {39, 40, 49, 50}
Cross-host boundary ring  (L1/L2): {30}   <- new, toward host1
Backbone column (node_in_ring=0):  rings 30-50
"""

import ipaddress
import json
import math

# ---------------------------------------------------------------------------
# Scale
# ---------------------------------------------------------------------------
NUM_RINGS      = 30         # rings 30-59
NODES_PER_RING = 72
RING_OFFSET    = 30         # global ring index of the first ring on this host

# ---------------------------------------------------------------------------
# IS-IS area layout  (intra-host, global ring indices)
# ---------------------------------------------------------------------------
AREA_RANGES = [
    (30, 39),
    (40, 49),
    (50, 59),
]

# Intra-host boundary rings
INTRA_BOUNDARY_RINGS = {39, 40, 49, 50}

# Cross-host boundary ring: ring30 peers with host1's ring29
CROSS_HOST_BOUNDARY_RING = 30      # on host2 side
CROSS_HOST_PEER_RING     = 29      # on host1 side (not deployed here)

# All rings that carry L1/L2 nodes
BOUNDARY_RINGS = INTRA_BOUNDARY_RINGS | {CROSS_HOST_BOUNDARY_RING}

# ---------------------------------------------------------------------------
# Backbone column (node_in_ring index 0)
# Extended from cross-host edge (ring30) through intra-host backbone (39-50)
# ---------------------------------------------------------------------------
BACKBONE_NODE_IN_RING = 0
BACKBONE_RING_START   = 30    # cross-host boundary, also start of backbone
BACKBONE_RING_END     = 50    # last intra-host backbone boundary ring


# Area IDs for this host start at 4 to be globally unique
AREA_ID_OFFSET = 3   # area_id = local_area_index (1-based) + AREA_ID_OFFSET


def bits_needed(count):
    if count <= 1:
        return 0
    return math.ceil(math.log2(count))


def get_area_id(ring_idx):
    for local_id, (start, end) in enumerate(AREA_RANGES, start=1):
        if start <= ring_idx <= end:
            return local_id + AREA_ID_OFFSET   # 4, 5, or 6
    raise ValueError(f"ring_idx {ring_idx} out of range")


def get_is_type(ring_idx):
    return "level-1-2" if ring_idx in BOUNDARY_RINGS else "level-1"


def get_node_role(ring_idx, node_idx):
    """
    boundary-backbone : L1/L2 node on backbone column (n0) at a boundary ring
    transit-backbone  : L2-only node on backbone column between boundary rings
    area              : regular L1 node
    """
    if node_idx != BACKBONE_NODE_IN_RING:
        return "area"

    if ring_idx in BOUNDARY_RINGS:
        return "boundary-backbone"

    # Transit backbone spans rings 31-38 (between cross-host boundary ring30
    # and first intra-host boundary ring39).
    if BACKBONE_RING_START < ring_idx < BACKBONE_RING_END:
        return "transit-backbone"

    return "area"


# ---------------------------------------------------------------------------
# Address helpers  (global ring indices -> consistent with host1 addressing)
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
    for local_id, (start, end) in enumerate(AREA_RANGES, start=1):
        area_id = local_id + AREA_ID_OFFSET
        ring_prefixes       = [get_ring_summary_prefix(r)       for r in range(start, end + 1)]
        inter_ring_prefixes = [get_inter_ring_summary_prefix(r) for r in range(start, end + 1)]
        collapsed = list(ipaddress.collapse_addresses(ring_prefixes + inter_ring_prefixes))
        summaries.append(
            {
                "area_id":   area_id,
                "ring_start": start,
                "ring_end":   end,
                "summary_prefixes":            [str(p) for p in collapsed],
                "ring_summary_prefixes":       [str(p) for p in ring_prefixes],
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

    ring_range = range(RING_OFFSET, RING_OFFSET + NUM_RINGS)   # 30..59

    print(
        f"[Host2] Generating topology: {NUM_RINGS} rings x {NODES_PER_RING} nodes/ring "
        f"= {NUM_RINGS * NODES_PER_RING} nodes  (global rings {RING_OFFSET}-{RING_OFFSET+NUM_RINGS-1})"
    )
    print(f"IS-IS areas: {len(AREA_RANGES)}")
    for local_id, (start, end) in enumerate(AREA_RANGES, start=1):
        print(f"  Area {local_id + AREA_ID_OFFSET}: rings {start}-{end}")
    print(f"Intra-host boundary rings (L1/L2): {sorted(INTRA_BOUNDARY_RINGS)}")
    print(f"Cross-host boundary ring  (L1/L2): {CROSS_HOST_BOUNDARY_RING} (peers with ring {CROSS_HOST_PEER_RING} on host1)")
    print(f"Backbone column: node_in_ring={BACKBONE_NODE_IN_RING}, rings {BACKBONE_RING_START}-{BACKBONE_RING_END}")
    print()

    # Node IDs start at 1 on host2 (self-contained; global uniqueness is not
    # required for FRR but is preserved via the globally unique ISIS NET addr).
    node_id = 1
    for ring_idx in ring_range:
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
                    "name":         node_name,
                    "id":           node_id,
                    "ring":         ring_idx,
                    "node_in_ring": node_idx,
                    "area_id":      area_id,
                    "is_type":      is_type,
                    "node_role":    get_node_role(ring_idx, node_idx),
                    "srv6_locator": srv6_locator,
                    "srv6_prefix":  srv6_prefix,
                    "isis_net":     isis_net,
                }
            )
            node_id += 1

    print(f"Generated {len(nodes)} nodes")

    # ------------------------------------------------------------------
    # Intra-ring links (rings 30-59)
    # ------------------------------------------------------------------
    print("Generating intra-ring links...")
    intra_count = 0
    for ring_idx in ring_range:
        for node_idx in range(NODES_PER_RING):
            node1 = f"r{ring_idx}n{node_idx}"
            next_node_idx = (node_idx + 1) % NODES_PER_RING
            node2  = f"r{ring_idx}n{next_node_idx}"
            subnet = f"fc00:{ring_idx:04x}:{node_idx:04x}:{next_node_idx:04x}::/64"
            links.append({"node1": node1, "node2": node2, "subnet": subnet, "type": "intra-ring"})
            intra_count += 1
    print(f"  Generated {intra_count} intra-ring links")

    # ------------------------------------------------------------------
    # Cross-host inter-ring links: ring29 <-> ring30
    #
    # r30nx interfaces are created inside r30 containers on host2.
    # r29nx stubs are left on the host2 hypervisor for bridging to host1.
    # ------------------------------------------------------------------
    print(f"Generating cross-host inter-ring links (ring{CROSS_HOST_PEER_RING} <-> ring{CROSS_HOST_BOUNDARY_RING})...")
    cross_count = 0
    for node_idx in range(NODES_PER_RING):
        node1  = f"r{CROSS_HOST_PEER_RING}n{node_idx}"      # lives on host1
        node2  = f"r{CROSS_HOST_BOUNDARY_RING}n{node_idx}"  # lives on host2
        subnet = f"fc00:9000:{CROSS_HOST_PEER_RING:04x}:{node_idx:04x}::/64"
        links.append(
            {
                "node1":  node1,
                "node2":  node2,
                "subnet": subnet,
                "type":   "cross-host-inter-ring",
                # deployment hint: node2 iface -> container, node1 iface -> hypervisor
                "deploy_node1": "hypervisor",
                "deploy_node2": "container",
            }
        )
        cross_count += 1
    print(f"  Generated {cross_count} cross-host inter-ring links")
    print(f"  r30nx->r29nx : deployed inside r30 containers")
    print(f"  r29nx->r30nx : left on host2 hypervisor (to be bridged to host1)")

    # ------------------------------------------------------------------
    # Intra-host inter-ring links: rings 30-58 -> 31-59
    # ------------------------------------------------------------------
    print("Generating intra-host inter-ring links (rings 30-58 -> 31-59)...")
    inter_intra_count = 0
    for ring_idx in range(RING_OFFSET, RING_OFFSET + NUM_RINGS - 1):   # 30..58
        next_ring_idx = ring_idx + 1
        for node_idx in range(NODES_PER_RING):
            node1  = f"r{ring_idx}n{node_idx}"
            node2  = f"r{next_ring_idx}n{node_idx}"
            subnet = f"fc00:9000:{ring_idx:04x}:{node_idx:04x}::/64"
            links.append({"node1": node1, "node2": node2, "subnet": subnet, "type": "inter-ring"})
            inter_intra_count += 1
    print(f"  Generated {inter_intra_count} intra-host inter-ring links")

    print(f"Total links: {len(links)}")

    topology = {
        "host":         "host2",
        "network_name": f"srv6-host2-{len(nodes)}node-net",
        "description": (
            f"Host2: {len(nodes)}-node topology, rings 30-59, 3 IS-IS areas. "
            f"Cross-host boundary at ring30 <-> ring29 (host1)."
        ),
        "num_rings":      NUM_RINGS,
        "nodes_per_ring": NODES_PER_RING,
        "ring_offset":    RING_OFFSET,
        "total_nodes":    len(nodes),
        "total_links":    len(links),
        "area_ranges": [
            {"area_id": i + 1 + AREA_ID_OFFSET, "ring_start": start, "ring_end": end}
            for i, (start, end) in enumerate(AREA_RANGES)
        ],
        "area_summaries":  build_area_summaries(),
        "boundary_rings":  sorted(BOUNDARY_RINGS),
        "intra_boundary_rings": sorted(INTRA_BOUNDARY_RINGS),
        "cross_host_boundary": {
            "local_ring":  CROSS_HOST_BOUNDARY_RING,
            "remote_ring": CROSS_HOST_PEER_RING,
            "remote_host": "host1",
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
        filename = f"topology_host2_{topology['total_nodes']}nodes.json"

    with open(filename, "w") as f:
        json.dump(topology, f, indent=2)
    print(f"\nTopology saved to {filename}")

    print("\n" + "=" * 60)
    print("Host2 Topology Statistics:")
    print("=" * 60)
    print(f"Total nodes  : {topology['total_nodes']}")
    print(f"Total links  : {topology['total_links']}")
    print(f"Rings        : {topology['num_rings']} (30-59)")

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
    print(f"Backbone transit nodes : {bb_transit}  (rings 31-38, n0 only)")

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
    print("HOST 2 topology generator (rings 30-59)")
    print("=" * 60)
    print()
    topology = generate_topology()
    output_file = save_topology(topology)
    print("\nNext steps:")
    print(f"  1. python3 generate_configs.py {output_file}")
    print(f"  2. sudo python3 deploy_host2.py {output_file}")

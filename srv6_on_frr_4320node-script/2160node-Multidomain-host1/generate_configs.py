#!/usr/bin/env python3
"""
Generate FRR configs for a distributed multi-area SRv6 topology.

Supports both host1 (rings 0-29) and host2 (rings 30-59) topology files.

Cross-host boundary handling:
  Host1: ring29 is a boundary ring toward host2's ring30.
         - r29n0  <-> r30n0 : level-2-only  (backbone link)
         - r29n1+ <-> r30nx : level-1       (regular inter-ring, different area)
         - Cross-host links are generated in r29 FRR configs; r30 side stubs
           are configured as plain IPv6 interfaces (no ISIS) since r30 containers
           do not exist on host1.

  Host2: ring30 is a boundary ring toward host1's ring29.
         - r30n0  <-> r29n0 : level-2-only  (backbone link)
         - r30n1+ <-> r29nx : level-1       (regular inter-ring, different area)
         - Cross-host links are generated in r30 FRR configs; r29 side stubs
           are configured as plain IPv6 interfaces (no ISIS) since r29 containers
           do not exist on host2.

IS-IS design (unchanged from original):
  - intra-area links: L1
  - backbone column (n0): L2
  - non-backbone cross-area links: no ISIS
  - transit-backbone nodes: no area L1 participation
"""

import json
import sys
from pathlib import Path


class MultiAreaFRRConfigGenerator:
    def __init__(self, topology_file):
        print(f"Loading topology from {topology_file}...")
        with open(topology_file, "r") as f:
            self.topology = json.load(f)

        self.nodes_by_name = {node["name"]: node for node in self.topology["nodes"]}
        self.area_summaries = {
            area["area_id"]: area for area in self.topology.get("area_summaries", [])
        }

        backbone = self.topology.get("backbone", {})
        self.backbone_node_in_ring = backbone.get("node_in_ring", 0)
        self.backbone_ring_start   = backbone.get("ring_start")
        self.backbone_ring_end     = backbone.get("ring_end")

        # Cross-host boundary info (present only in distributed topology files)
        cross = self.topology.get("cross_host_boundary", {})
        self.cross_host_local_ring  = cross.get("local_ring")   # e.g. 29 on host1, 30 on host2
        self.cross_host_remote_ring = cross.get("remote_ring")  # e.g. 30 on host1, 29 on host2
        self.host_id = self.topology.get("host", "unknown")     # "host1" or "host2"

        print(f"  Host         : {self.host_id}")
        print(f"  Nodes        : {len(self.topology['nodes'])}")
        print(f"  Links        : {len(self.topology['links'])}")

        if "area_ranges" in self.topology:
            print("  IS-IS areas:")
            for area in self.topology["area_ranges"]:
                print(
                    f"    Area {area['area_id']}: "
                    f"ring{area['ring_start']} ~ ring{area['ring_end']}"
                )
        if "boundary_rings" in self.topology:
            print(f"  Boundary rings (L1/L2): {self.topology['boundary_rings']}")
        if self.cross_host_local_ring is not None:
            print(
                f"  Cross-host boundary: ring{self.cross_host_local_ring} "
                f"<-> ring{self.cross_host_remote_ring} ({cross.get('remote_host', '?')})"
            )
        if backbone:
            print(
                f"  Backbone: node_in_ring={self.backbone_node_in_ring}, "
                f"rings {self.backbone_ring_start}~{self.backbone_ring_end}"
            )

        print("Building interface map...")
        self.node_interfaces = self._build_interface_map()
        print("  Interface map built")

    # ------------------------------------------------------------------
    # Link classification helpers
    # ------------------------------------------------------------------

    def _is_backbone_vertical_link(self, node1_name, node2_name):
        """True for the single-node-per-ring L2 backbone chain."""
        if self.backbone_ring_start is None or self.backbone_ring_end is None:
            return False

        n1 = self.nodes_by_name.get(node1_name)
        n2 = self.nodes_by_name.get(node2_name)
        if n1 is None or n2 is None:
            return False

        if n1["node_in_ring"] != self.backbone_node_in_ring:
            return False
        if n2["node_in_ring"] != self.backbone_node_in_ring:
            return False
        if abs(n1["ring"] - n2["ring"]) != 1:
            return False

        lo = min(n1["ring"], n2["ring"])
        hi = max(n1["ring"], n2["ring"])
        return self.backbone_ring_start <= lo and hi <= self.backbone_ring_end

    def _is_cross_host_backbone_link(self, node1_name, node2_name):
        """
        True when exactly one node is in the cross-host local ring and the other
        is in the cross-host remote ring, AND both are node_in_ring == 0.
        This link must be level-2-only on both sides.
        """
        if self.cross_host_local_ring is None:
            return False

        n1 = self.nodes_by_name.get(node1_name)
        n2 = self.nodes_by_name.get(node2_name)
        if n1 is None or n2 is None:
            # One end is a remote stub (not in our node list) — check by name
            # e.g. node2_name == "r30n0" when only r29 nodes are in self.nodes_by_name
            def ring_of(name):
                try:
                    return int(name.split("n")[0][1:])
                except Exception:
                    return -1
            def nidx_of(name):
                try:
                    return int(name.split("n")[1])
                except Exception:
                    return -1

            r1, i1 = (n1["ring"], n1["node_in_ring"]) if n1 else (ring_of(node1_name), nidx_of(node1_name))
            r2, i2 = (n2["ring"], n2["node_in_ring"]) if n2 else (ring_of(node2_name), nidx_of(node2_name))

            rings = {r1, r2}
            if rings == {self.cross_host_local_ring, self.cross_host_remote_ring}:
                return i1 == 0 and i2 == 0
            return False

        rings = {n1["ring"], n2["ring"]}
        if rings == {self.cross_host_local_ring, self.cross_host_remote_ring}:
            return n1["node_in_ring"] == 0 and n2["node_in_ring"] == 0
        return False

    def _classify_interface(self, local_node, peer_node_name, link):
        """
        Decide whether ISIS should run on a link and at which level.

        Returns (isis_enabled: bool, circuit_type: str | None)

        Rules (in priority order):
        1. Intra-host backbone vertical links -> L2-only
        2. Cross-host backbone link (n0 <-> n0 across the host boundary) -> L2-only
        3. Cross-host non-backbone links (nx <-> nx, n>0) ->
              local node is in cross_host_local_ring:
                if local area == remote area conceptually -> run L1
                otherwise -> run L1 (they are adjacent rings, treated as same area edge)
              Actually: these links cross area boundaries (area3 ring29 <-> area4 ring30),
              so they are cross-area. Non-backbone cross-area = no ISIS.
              Exception: if the local node IS on the cross-host boundary ring AND
              n>0, we still want IS-IS L1 adjacency toward the peer for the
              attached-bit default route to propagate — but the peer is in a different
              area, so FRR would not form a L1 adjacency anyway. We disable ISIS here
              and rely on the default route from the L2 backbone.
              -> no ISIS  (same as regular cross-area non-backbone links)
        4. Regular cross-area links (non-backbone) -> no ISIS
        5. Transit-backbone role -> no L1 participation -> no ISIS for area links
        6. Default intra-area -> L1
        """
        local_role = local_node.get("node_role", "area")
        peer_node  = self.nodes_by_name.get(peer_node_name)

        # --- Rule 1: intra-host backbone ---
        if self._is_backbone_vertical_link(local_node["name"], peer_node_name):
            return True, "level-2-only"

        # --- Rule 2: cross-host backbone link (n0 <-> n0) ---
        if self._is_cross_host_backbone_link(local_node["name"], peer_node_name):
            return True, "level-2-only"

        # --- For remaining rules, determine cross-area status ---
        if peer_node is not None:
            cross_area = local_node["area_id"] != peer_node["area_id"]
        else:
            # Peer is a remote node (not in our topology). Determine by ring.
            def ring_of(name):
                try:
                    return int(name.split("n")[0][1:])
                except Exception:
                    return -1
            peer_ring = ring_of(peer_node_name)
            # Cross-host remote ring is always a different area
            cross_area = (peer_ring == self.cross_host_remote_ring)

        # --- Rule 3 & 4: cross-area non-backbone -> no ISIS ---
        if cross_area:
            return False, None

        # --- Rule 5: transit-backbone role -> no ISIS for area links ---
        if local_role == "transit-backbone":
            return False, None

        # --- Rule 6: default intra-area -> L1 ---
        return True, "level-1"

    def _build_interface_map(self):
        """Build interface metadata for every node in this topology."""
        interface_map = {node["name"]: [] for node in self.topology["nodes"]}

        for link in self.topology["links"]:
            node1_name = link["node1"]
            node2_name = link["node2"]
            subnet     = link["subnet"]
            link_type  = link["type"]

            iface1 = f"{node1_name}-{node2_name}"
            iface2 = f"{node2_name}-{node1_name}"
            ip1    = subnet.replace("::/64", "::1/64")
            ip2    = subnet.replace("::/64", "::2/64")

            node1_data = self.nodes_by_name.get(node1_name)
            node2_data = self.nodes_by_name.get(node2_name)

            # ----------------------------------------------------------
            # Cross-host links: one end may not be in our node list.
            # We only generate config for the local side.
            # ----------------------------------------------------------
            if link_type == "cross-host-inter-ring":
                deploy_n1 = link.get("deploy_node1", "container")
                deploy_n2 = link.get("deploy_node2", "container")

                # Determine which end is local (container) vs remote (hypervisor)
                if node1_data is not None and deploy_n1 == "container":
                    # node1 is our local node; add iface1 to node1's interface list
                    n1_isis, n1_circuit = self._classify_interface(node1_data, node2_name, link)
                    interface_map[node1_name].append(
                        {
                            "name":         iface1,
                            "peer":         node2_name,
                            "ipv6":         ip1,
                            "link_type":    link_type,
                            "cross_area":   True,
                            "cross_host":   True,
                            "isis_enabled": n1_isis,
                            "circuit_type": n1_circuit,
                        }
                    )

                if node2_data is not None and deploy_n2 == "container":
                    # node2 is our local node; add iface2 to node2's interface list
                    n2_isis, n2_circuit = self._classify_interface(node2_data, node1_name, link)
                    interface_map[node2_name].append(
                        {
                            "name":         iface2,
                            "peer":         node1_name,
                            "ipv6":         ip2,
                            "link_type":    link_type,
                            "cross_area":   True,
                            "cross_host":   True,
                            "isis_enabled": n2_isis,
                            "circuit_type": n2_circuit,
                        }
                    )
                # The hypervisor-side stubs are NOT added to any node config here.
                continue

            # ----------------------------------------------------------
            # Normal (intra-host) links: both ends are local nodes
            # ----------------------------------------------------------
            if node1_data is None or node2_data is None:
                # Shouldn't happen for non-cross-host links; skip defensively
                continue

            cross_area = node1_data["area_id"] != node2_data["area_id"]
            n1_isis, n1_circuit = self._classify_interface(node1_data, node2_name, link)
            n2_isis, n2_circuit = self._classify_interface(node2_data, node1_name, link)

            interface_map[node1_name].append(
                {
                    "name":         iface1,
                    "peer":         node2_name,
                    "ipv6":         ip1,
                    "link_type":    link_type,
                    "cross_area":   cross_area,
                    "cross_host":   False,
                    "isis_enabled": n1_isis,
                    "circuit_type": n1_circuit,
                }
            )
            interface_map[node2_name].append(
                {
                    "name":         iface2,
                    "peer":         node1_name,
                    "ipv6":         ip2,
                    "link_type":    link_type,
                    "cross_area":   cross_area,
                    "cross_host":   False,
                    "isis_enabled": n2_isis,
                    "circuit_type": n2_circuit,
                }
            )

        return interface_map

    # ------------------------------------------------------------------
    # Area summary config
    # ------------------------------------------------------------------

    def get_area_summary_config(self, node):
        """Return summary policy config for boundary-backbone nodes."""
        area_id      = node["area_id"]
        summary_info = self.area_summaries.get(area_id)
        if node.get("node_role") != "boundary-backbone" or not summary_info:
            return "", "", ""

        static_routes = []
        prefix_list   = [
            f"ipv6 prefix-list AREA{area_id}-SUMMARY seq 10 description Area {area_id} summaries"
        ]
        seq = 20
        for prefix in summary_info["summary_prefixes"]:
            static_routes.append(f"ipv6 route {prefix} Null0")
            prefix_list.append(
                f"ipv6 prefix-list AREA{area_id}-SUMMARY seq {seq} permit {prefix}"
            )
            seq += 10

        route_map   = (
            f"route-map AREA{area_id}-TO-L2 permit 10\n"
            f" match ipv6 address prefix-list AREA{area_id}-SUMMARY\n"
        )
        redistribute = f" redistribute ipv6 static level-2 route-map AREA{area_id}-TO-L2\n"
        return (
            "\n".join(static_routes) + "\n",
            "\n".join(prefix_list) + "\n" + route_map + "!\n",
            redistribute,
        )

    # ------------------------------------------------------------------
    # FRR config generation
    # ------------------------------------------------------------------

    def generate_frr_conf(self, node):
        """Generate FRR config for a single node."""
        node_name    = node["name"]
        isis_net     = node["isis_net"]
        srv6_locator = node["srv6_locator"]
        interfaces   = self.node_interfaces[node_name]
        node_role    = node.get("node_role", "area")

        if node_role == "boundary-backbone":
            is_type = "level-1-2"
        elif node_role == "transit-backbone":
            is_type = "level-2-only"
        else:
            is_type = "level-1"

        static_routes, summary_policy, l2_redistribute = self.get_area_summary_config(node)
        loopback_circuit = "level-2-only" if is_type == "level-2-only" else "level-1"

        config = f"""!
! FRR configuration for {node_name}
! Host: {self.host_id}
! IS-IS area: {node.get('area_id', '?')}  is-type: {is_type}  role: {node_role}
!
frr version 10.5.1
frr defaults traditional
hostname {node_name}
log file /var/log/frr/frr.log
service integrated-vtysh-config
!
ipv6 forwarding
!
interface lo
 description SRv6 Locator Interface
 ipv6 address {srv6_locator}
 ipv6 router isis SRv6
 isis passive
 isis circuit-type {loopback_circuit}
!
"""

        for iface in interfaces:
            config += f"""interface {iface['name']}
 ipv6 address {iface['ipv6']}
"""
            if iface["isis_enabled"]:
                config += f""" ipv6 router isis SRv6
 isis circuit-type {iface['circuit_type']}
 isis network point-to-point
 isis hello-interval 3
 isis hello-multiplier 3
"""
            if iface.get("cross_host"):
                config += f" description cross-host-link to {iface['peer']}\n"
            config += "!\n"

        if static_routes:
            config += f"{static_routes}!\n"
        if summary_policy:
            config += summary_policy

        attached_recv = " attached-bit receive\n" if is_type != "level-2-only" else ""
        attached_send = " attached-bit send\n"    if node_role == "boundary-backbone" else ""

        config += f"""router isis SRv6
 net {isis_net}
 is-type {is_type}
 metric-style wide
 topology ipv6-unicast
{attached_recv}{attached_send}{l2_redistribute} log-adjacency-changes
!
line vty
!
end
"""
        return config

    def generate_daemons_conf(self):
        return """zebra=yes
isisd=yes
staticd=yes
bgpd=no
ospfd=no
ospf6d=no
ripd=no
ripngd=no
pimd=no
ldpd=no
nhrpd=no
eigrpd=no
babeld=no
sharpd=no
pbrd=no
bfdd=no
fabricd=no
vrrpd=no
zebra_options=" -s 90000000"
isisd_options=" -A ::1"
"""

    def generate_all(self, output_dir="configs"):
        """Generate all FRR configs and the shared daemons file."""
        output_path = Path(output_dir)
        output_path.mkdir(exist_ok=True)

        total_nodes  = len(self.topology["nodes"])
        l1l2_count   = sum(1 for n in self.topology["nodes"] if n.get("is_type") == "level-1-2")
        l2only_count = sum(1 for n in self.topology["nodes"] if n.get("node_role") == "transit-backbone")
        l1_count     = total_nodes - l1l2_count - l2only_count

        print("\n" + "=" * 60)
        print(f"Generating FRR configurations for {total_nodes} nodes ({self.host_id})...")
        print(f"  L1 nodes      : {l1_count}")
        print(f"  L1/L2 nodes   : {l1l2_count}")
        print(f"  L2-only nodes : {l2only_count}")
        if self.cross_host_local_ring is not None:
            print(f"  Cross-host boundary: ring{self.cross_host_local_ring} "
                  f"<-> ring{self.cross_host_remote_ring}")
        print("=" * 60)

        batch_size = 20
        for i, node in enumerate(self.topology["nodes"], 1):
            config      = self.generate_frr_conf(node)
            config_file = output_path / f"frr-{node['name']}.conf"
            with open(config_file, "w") as f:
                f.write(config)

            if i % batch_size == 0 or i == total_nodes:
                percent = (i / total_nodes) * 100
                print(
                    f"  Progress: {i}/{total_nodes} ({percent:.1f}%)"
                    f" - Last: {node['name']} [{node.get('node_role', '?')}]"
                )

        with open(output_path / "daemons", "w") as f:
            f.write(self.generate_daemons_conf())

        # Generate a summary of cross-host interface stubs for the deploy script
        if self.cross_host_local_ring is not None:
            self._write_cross_host_stub_list(output_path)

        print("\n" + "=" * 60)
        print(f"Generated {total_nodes} node configurations -> {output_dir}/")
        print("=" * 60)

    def _write_cross_host_stub_list(self, output_path):
        """
        Write a JSON file listing all hypervisor-side stub interfaces for cross-host links.
        The deploy script uses this to create veth stubs on the hypervisor.
        """
        stubs = []
        for link in self.topology["links"]:
            if link["type"] != "cross-host-inter-ring":
                continue
            node1_name = link["node1"]
            node2_name = link["node2"]
            subnet     = link["subnet"]
            ip1        = subnet.replace("::/64", "::1/64")
            ip2        = subnet.replace("::/64", "::2/64")

            deploy_n1 = link.get("deploy_node1", "container")
            deploy_n2 = link.get("deploy_node2", "container")

            # The hypervisor stub is the end marked "hypervisor"
            if deploy_n1 == "hypervisor":
                stubs.append({
                    "veth_name":    f"{node1_name}-{node2_name}",
                    "peer_veth":    f"{node2_name}-{node1_name}",
                    "ipv6":         ip1,
                    "local_node":   node1_name,
                    "remote_node":  node2_name,
                    "note":         f"hypervisor stub; bridge to {self.topology['cross_host_boundary']['remote_host']}",
                })
            elif deploy_n2 == "hypervisor":
                stubs.append({
                    "veth_name":    f"{node2_name}-{node1_name}",
                    "peer_veth":    f"{node1_name}-{node2_name}",
                    "ipv6":         ip2,
                    "local_node":   node2_name,
                    "remote_node":  node1_name,
                    "note":         f"hypervisor stub; bridge to {self.topology['cross_host_boundary']['remote_host']}",
                })

        stub_file = output_path / "cross_host_stubs.json"
        with open(stub_file, "w") as f:
            json.dump({"host": self.topology.get("host"), "stubs": stubs}, f, indent=2)
        print(f"\nCross-host stub interface list -> {stub_file}")
        print(f"  {len(stubs)} hypervisor-side stubs documented for bridging")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 generate_configs.py <topology.json>")
        print("\nExamples:")
        print("  python3 generate_configs.py topology_host1_2160nodes.json")
        print("  python3 generate_configs.py topology_host2_2160nodes.json")
        sys.exit(1)

    topology_file = sys.argv[1]
    if not Path(topology_file).exists():
        print(f"Error: Topology file '{topology_file}' not found!")
        sys.exit(1)

    try:
        generator = MultiAreaFRRConfigGenerator(topology_file)
        generator.generate_all()
        print("\nConfiguration generation completed!")

        host = generator.host_id
        deploy_script = f"deploy_{host}.py" if host in ("host1", "host2") else "deploy_hostX.py"
        print(f"\nNext step:")
        print(f"  sudo python3 {deploy_script} {topology_file}")
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

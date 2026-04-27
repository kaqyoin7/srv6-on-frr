#!/usr/bin/env python3
"""
Generate FRR configs for the 1200-node multi-area SRv6 topology.

Design goals:
- keep node-internal visibility scoped to a single IS-IS L1 area
- keep cross-area links in the L2 backbone only
- advertise only area summaries into L2
- let L1 routers use the attached-bit default route for remote-area reachability
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

        print(f"  Nodes : {len(self.topology['nodes'])}")
        print(f"  Links : {len(self.topology['links'])}")

        if "area_ranges" in self.topology:
            print("  IS-IS areas:")
            for area in self.topology["area_ranges"]:
                print(
                    f"    Area {area['area_id']}: "
                    f"ring{area['ring_start']} ~ ring{area['ring_end']}"
                )
        if "boundary_rings" in self.topology:
            print(f"  Boundary rings (L1/L2): {self.topology['boundary_rings']}")

        print("Building interface map...")
        self.node_interfaces = self._build_interface_map()
        print("  Interface map built")

    def _is_backbone_vertical_link(self, node1, node2):
        """Return True for the single-node-per-ring L2 backbone chain."""
        n1 = self.nodes_by_name[node1]
        n2 = self.nodes_by_name[node2]
        if n1["node_in_ring"] != 0 or n2["node_in_ring"] != 0:
            return False
        if abs(n1["ring"] - n2["ring"]) != 1:
            return False
        return 14 <= min(n1["ring"], n2["ring"]) and max(n1["ring"], n2["ring"]) <= 45

    def _classify_interface(self, local_node, peer_node, link):
        """
        Decide whether ISIS should run on a link and at which level.

        Design:
        - regular intra-area links are L1
        - the n0 column from ring14 to ring45 is the only L2 backbone
        - non-backbone cross-area links do not run ISIS
        - transit L2-only nodes do not participate in area L1
        """
        local_role = local_node.get("node_role", "area")
        cross_area = local_node["area_id"] != peer_node["area_id"]

        if self._is_backbone_vertical_link(local_node["name"], peer_node["name"]):
            return True, "level-2-only"

        if cross_area:
            return False, None

        if local_role == "transit-backbone":
            return False, None

        return True, "level-1"

    def _build_interface_map(self):
        """Build interface metadata for every node."""
        interface_map = {node["name"]: [] for node in self.topology["nodes"]}

        for link in self.topology["links"]:
            node1 = link["node1"]
            node2 = link["node2"]
            subnet = link["subnet"]

            iface1 = f"{node1}-{node2}"
            iface2 = f"{node2}-{node1}"
            ip1 = subnet.replace("::/64", "::1/64")
            ip2 = subnet.replace("::/64", "::2/64")

            node1_data = self.nodes_by_name[node1]
            node2_data = self.nodes_by_name[node2]
            node1_enabled, node1_circuit = self._classify_interface(node1_data, node2_data, link)
            node2_enabled, node2_circuit = self._classify_interface(node2_data, node1_data, link)
            cross_area = node1_data["area_id"] != node2_data["area_id"]

            interface_map[node1].append({
                "name": iface1,
                "peer": node2,
                "ipv6": ip1,
                "link_type": link["type"],
                "cross_area": cross_area,
                "isis_enabled": node1_enabled,
                "circuit_type": node1_circuit,
            })
            interface_map[node2].append({
                "name": iface2,
                "peer": node1,
                "ipv6": ip2,
                "link_type": link["type"],
                "cross_area": cross_area,
                "isis_enabled": node2_enabled,
                "circuit_type": node2_circuit,
            })

        return interface_map

    def get_area_summary_config(self, node):
        """Return summary policy config for boundary backbone nodes."""
        area_id = node["area_id"]
        summary_info = self.area_summaries.get(area_id)
        if node.get("node_role") != "boundary-backbone" or not summary_info:
            return "", "", ""

        static_routes = []
        prefix_list = [f"ipv6 prefix-list AREA{area_id}-SUMMARY seq 10 description Area {area_id} summaries"]
        seq = 20
        for prefix in summary_info["summary_prefixes"]:
            static_routes.append(f"ipv6 route {prefix} Null0")
            prefix_list.append(f"ipv6 prefix-list AREA{area_id}-SUMMARY seq {seq} permit {prefix}")
            seq += 10

        route_map = (
            f"route-map AREA{area_id}-TO-L2 permit 10\n"
            f" match ipv6 address prefix-list AREA{area_id}-SUMMARY\n"
        )
        redistribute = f" redistribute ipv6 static level-2 route-map AREA{area_id}-TO-L2\n"
        return "\n".join(static_routes) + "\n", "\n".join(prefix_list) + "\n" + route_map + "!\n", redistribute

    def generate_frr_conf(self, node):
        """Generate FRR config for a single node."""
        node_name = node["name"]
        isis_net = node["isis_net"]
        srv6_locator = node["srv6_locator"]
        interfaces = self.node_interfaces[node_name]
        node_role = node.get("node_role", "area")
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
            config += """!
"""

        if static_routes:
            config += f"{static_routes}!\n"
        if summary_policy:
            config += summary_policy

        attached_recv = " attached-bit receive\n" if is_type != "level-2-only" else ""
        attached_send = " attached-bit send\n" if node_role == "boundary-backbone" else ""

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
        output_path = Path(output_dir)
        output_path.mkdir(exist_ok=True)

        total_nodes = len(self.topology["nodes"])
        l1l2_count = sum(
            1 for node in self.topology["nodes"] if node.get("is_type") == "level-1-2"
        )
        l1_count = total_nodes - l1l2_count

        print("\n" + "=" * 60)
        print(f"Generating FRR configurations for {total_nodes} nodes...")
        print(f"  L1 nodes    : {l1_count}")
        print(f"  L1/L2 nodes : {l1l2_count}")
        print("=" * 60)

        batch_size = 20
        for i, node in enumerate(self.topology["nodes"], 1):
            config = self.generate_frr_conf(node)
            config_file = output_path / f"frr-{node['name']}.conf"

            with open(config_file, "w") as f:
                f.write(config)

            if i % batch_size == 0 or i == total_nodes:
                percent = (i / total_nodes) * 100
                print(
                    f"  Progress: {i}/{total_nodes} ({percent:.1f}%)"
                    f" - Last: {node['name']} [{node.get('is_type', '?')}]"
                )

        with open(output_path / "daemons", "w") as f:
            f.write(self.generate_daemons_conf())

        print("\n" + "=" * 60)
        print(f"Generated {total_nodes} node configurations -> {output_dir}/")
        print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 generate_configs.py <topology.json>")
        print("\nExample:")
        print("  python3 generate_configs.py topology_1200nodes.json")
        sys.exit(1)

    topology_file = sys.argv[1]

    if not Path(topology_file).exists():
        print(f"Error: Topology file '{topology_file}' not found!")
        sys.exit(1)

    try:
        generator = MultiAreaFRRConfigGenerator(topology_file)
        generator.generate_all()
        print("\nConfiguration generation completed!")
        print("\nNext step:")
        print(f"  sudo python3 deploy_5Network.py {topology_file}")
    except Exception as e:
        print(f"\nError: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

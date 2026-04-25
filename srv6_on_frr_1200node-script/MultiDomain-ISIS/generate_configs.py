#!/usr/bin/env python3
"""
FRR配置生成器 - 1200节点 / IS-IS四域版
读取topology JSON中的 is_type 与 area_id 字段，
为L1节点生成 is-type level-1，为边界节点生成 is-type level-1-2。
"""

import json
import sys
from pathlib import Path


class MultiAreaFRRConfigGenerator:
    def __init__(self, topology_file):
        print(f"Loading topology from {topology_file}...")
        with open(topology_file, 'r') as f:
            self.topology = json.load(f)

        print(f"  Nodes : {len(self.topology['nodes'])}")
        print(f"  Links : {len(self.topology['links'])}")

        # 打印域信息（若topology中有）
        if 'area_ranges' in self.topology:
            print("  IS-IS areas:")
            for area in self.topology['area_ranges']:
                print(f"    Area {area['area_id']}: "
                      f"ring{area['ring_start']} ~ ring{area['ring_end']}")
        if 'boundary_rings' in self.topology:
            print(f"  Boundary rings (L1/L2): {self.topology['boundary_rings']}")

        print("Building interface map...")
        self.node_interfaces = self._build_interface_map()
        print("  Interface map built")

    def _build_interface_map(self):
        """构建每个节点的接口映射"""
        interface_map = {node['name']: [] for node in self.topology['nodes']}

        for link in self.topology['links']:
            node1   = link['node1']
            node2   = link['node2']
            subnet  = link['subnet']

            iface1 = f"{node1}-{node2}"
            ip1    = subnet.replace('::/64', '::1/64')
            interface_map[node1].append({'name': iface1, 'ipv6': ip1})

            iface2 = f"{node2}-{node1}"
            ip2    = subnet.replace('::/64', '::2/64')
            interface_map[node2].append({'name': iface2, 'ipv6': ip2})

        return interface_map

    def generate_frr_conf(self, node):
        """生成单个节点的FRR配置"""
        node_name    = node['name']
        isis_net     = node['isis_net']
        srv6_locator = node['srv6_locator']
        interfaces   = self.node_interfaces[node_name]

        # 读取IS-IS类型；若旧topology无此字段则默认level-1
        is_type = node.get('is_type', 'level-1')

        config = f"""!
! FRR configuration for {node_name}
! IS-IS area: {node.get('area_id', '?')}  is-type: {is_type}
!
frr version 8.1
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
!
"""
        # 配置各网口
        for iface in interfaces:
            config += f"""interface {iface['name']}
 ipv6 address {iface['ipv6']}
 ipv6 router isis SRv6
 isis network point-to-point
 isis hello-interval 3
 isis hello-multiplier 3
!
"""

        # ISIS进程配置
        # L1/L2节点额外开启 redistribute isis level-1 into level-2，
        # 使域内SRv6前缀通过L2骨干全网可达
        isis_extra = ""
        if is_type == "level-1-2":
            isis_extra = " redistribute isis level-1 into level-2\n"

        config += f"""!
router isis SRv6
 net {isis_net}
 is-type {is_type}
 topology ipv6-unicast
 log-adjacency-changes
{isis_extra}!
line vty
!
end
"""
        return config

    def generate_daemons_conf(self):
        return """zebra=yes
isisd=yes
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

    def generate_all(self, output_dir='configs'):
        output_path = Path(output_dir)
        output_path.mkdir(exist_ok=True)

        total_nodes = len(self.topology['nodes'])
        l1l2_count  = sum(1 for n in self.topology['nodes']
                          if n.get('is_type') == 'level-1-2')
        l1_count    = total_nodes - l1l2_count

        print("\n" + "=" * 60)
        print(f"Generating FRR configurations for {total_nodes} nodes...")
        print(f"  L1 nodes    : {l1_count}")
        print(f"  L1/L2 nodes : {l1l2_count}")
        print("=" * 60)

        batch_size = 20
        for i, node in enumerate(self.topology['nodes'], 1):
            config      = self.generate_frr_conf(node)
            config_file = output_path / f"frr-{node['name']}.conf"

            with open(config_file, 'w') as f:
                f.write(config)

            if i % batch_size == 0 or i == total_nodes:
                percent = (i / total_nodes) * 100
                print(f"  Progress: {i}/{total_nodes} ({percent:.1f}%)"
                      f" - Last: {node['name']} [{node.get('is_type','?')}]")

        # daemons配置
        daemons_file = output_path / "daemons"
        with open(daemons_file, 'w') as f:
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
        print(f"  sudo python3 deploy.py {topology_file}")
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

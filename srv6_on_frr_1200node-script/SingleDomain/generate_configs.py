#!/usr/bin/env python3
"""
FRR配置生成器 - 支持大规模拓扑（400+节点）
优化：减少内存占用，增加进度显示
"""

import json
import sys
from pathlib import Path


class LargeScaleFRRConfigGenerator:
    def __init__(self, topology_file):
        print(f"Loading topology from {topology_file}...")
        with open(topology_file, 'r') as f:
            self.topology = json.load(f)

        print(f"  Nodes: {len(self.topology['nodes'])}")
        print(f"  Links: {len(self.topology['links'])}")

        print("Building interface map...")
        self.node_interfaces = self._build_interface_map()
        print("  ✓ Interface map built")

    def _build_interface_map(self):
        """构建每个节点的接口映射"""
        interface_map = {node['name']: [] for node in self.topology['nodes']}

        for link in self.topology['links']:
            node1 = link['node1']
            node2 = link['node2']
            subnet = link['subnet']

            iface1 = f"{node1}-{node2}"
            ip1 = subnet.replace('::/64', '::1/64')
            interface_map[node1].append({'name': iface1, 'ipv6': ip1})

            iface2 = f"{node2}-{node1}"
            ip2 = subnet.replace('::/64', '::2/64')
            interface_map[node2].append({'name': iface2, 'ipv6': ip2})

        return interface_map

    def generate_frr_conf(self, node):
        """生成单个节点的FRR配置"""
        node_name = node['name']
        isis_net = node['isis_net']
        srv6_locator = node['srv6_locator']
        interfaces = self.node_interfaces[node_name]

        config = f"""!
! FRR configuration for {node_name}
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

        # 配置所有接口
        for iface in interfaces:
            config += f"""interface {iface['name']}
 ipv6 address {iface['ipv6']}
 ipv6 router isis SRv6
 isis network point-to-point
 isis hello-interval 3
 isis hello-multiplier 3
!
"""

        # ISIS配置
        config += f"""!
router isis SRv6
 net {isis_net}
 is-type level-2-only
 topology ipv6-unicast
 log-adjacency-changes
!
line vty
!
end
"""
        return config

    def generate_daemons_conf(self):
        """生成守护进程配置"""
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
        """生成所有配置"""
        output_path = Path(output_dir)
        output_path.mkdir(exist_ok=True)

        total_nodes = len(self.topology['nodes'])

        print("\n" + "=" * 60)
        print(f"Generating FRR configurations for {total_nodes} nodes...")
        print("=" * 60)

        # 分批生成配置，显示进度
        batch_size = 20
        for i, node in enumerate(self.topology['nodes'], 1):
            config = self.generate_frr_conf(node)
            config_file = output_path / f"frr-{node['name']}.conf"

            with open(config_file, 'w') as f:
                f.write(config)

            # 每20个节点显示一次进度
            if i % batch_size == 0 or i == total_nodes:
                percent = (i / total_nodes) * 100
                print(f"  Progress: {i}/{total_nodes} ({percent:.1f}%) - Last: {node['name']}")

        # 生成daemons配置
        daemons_file = output_path / "daemons"
        with open(daemons_file, 'w') as f:
            f.write(self.generate_daemons_conf())

        print("\n" + "=" * 60)
        print(f"✓ Generated {total_nodes} node configurations")
        print(f"✓ Output directory: {output_dir}/")
        print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 generate_configs.py <topology.json>")
        print("\nExample:")
        print("  python3 generate_configs.py topology_400nodes.json")
        sys.exit(1)

    topology_file = sys.argv[1]

    if not Path(topology_file).exists():
        print(f"Error: Topology file '{topology_file}' not found!")
        sys.exit(1)

    try:
        generator = LargeScaleFRRConfigGenerator(topology_file)
        generator.generate_all()
        print("\n✓ Configuration generation completed!")
        print("\nNext step:")
        print(f"  sudo python3 deploy.py {topology_file}")
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)




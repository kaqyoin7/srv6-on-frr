#!/usr/bin/env python3
"""
FRR配置生成器 - BGP分域版本（1200节点，4个AS域）

节点类型：
  1. 普通节点       - 单IS-IS实例，无BGP
  2. iBGP边界节点   - 单IS-IS实例 + iBGP（同域两端互联，B域/C域内部）
  3. eBGP边界节点   - 双IS-IS实例 + eBGP + iBGP（域间对等，r14n0/r15n0 等）

路由策略：
  - 域内：IS-IS 承载所有 /128 locator 和链路子网
  - 边界：BGP 通过 aggregate-address 将本域每个ring聚合为 /32 发布
  - 域外路由：BGP /32 聚合前缀经 redistribute 注入域内 IS-IS
  - 结果：每节点路由表 ~300条IS-IS + ~45条BGP聚合 ≈ 345条（原来 ~3500条）

IS-IS实例命名：
  - 主实例：'SRv6_{DOMAIN}'  e.g. 'SRv6_A'
  - 双IS-IS节点次实例：'SRv6_{SECONDARY_DOMAIN}'  e.g. 'SRv6_B'
"""

import json
import sys
from pathlib import Path

# eBGP直连对：node -> peer (通过跨域veth直连)
EBGP_DIRECT_PEERS = {
    'r14n0': 'r15n0',
    'r15n0': 'r14n0',
    'r29n0': 'r30n0',
    'r30n0': 'r29n0',
    'r44n0': 'r45n0',
    'r45n0': 'r44n0',
}

# iBGP对：同域两端边界节点互为iBGP邻居（需通过IS-IS路由可达）
IBGP_PAIRS = [
    ('r15n0', 'r29n0'),
    ('r30n0', 'r44n0'),
]

# 每个域的 BGP 聚合前缀（per-ring /32，覆盖该ring所有20个节点/128）
DOMAIN_CONFIG = {
    'A': {'asn': 65001, 'rings': (0,  14), 'area': '49.0001'},
    'B': {'asn': 65002, 'rings': (15, 29), 'area': '49.0002'},
    'C': {'asn': 65003, 'rings': (30, 44), 'area': '49.0003'},
    'D': {'asn': 65004, 'rings': (45, 59), 'area': '49.0004'},
}


class BGPDomainFRRConfigGenerator:
    def __init__(self, topology_file):
        print(f"Loading topology from {topology_file}...")
        with open(topology_file, 'r') as f:
            self.topology = json.load(f)

        # 验证是否是BGP版拓扑
        if 'domains' not in self.topology:
            print("ERROR: This topology file does not contain BGP domain info.")
            print("Please run generate_topology_bgp.py first.")
            sys.exit(1)

        print(f"  Nodes  : {len(self.topology['nodes'])}")
        print(f"  Links  : {len(self.topology['links'])}")
        print(f"  Domains: {list(self.topology['domains'].keys())}")

        # 节点名->节点对象的快速查找
        self.node_map = {n['name']: n for n in self.topology['nodes']}

        # 构建接口映射
        print("Building interface map...")
        self.node_interfaces = self._build_interface_map()
        print("  ✓ Interface map built")

        # 构建 BGP 邻居信息（需要知道对端 loopback IP 用于iBGP）
        self._build_bgp_neighbor_map()

    # ── 接口映射 ──────────────────────────────────────────────────────────────

    def _build_interface_map(self):
        """构建每个节点的接口映射，标记是否跨域"""
        interface_map = {node['name']: [] for node in self.topology['nodes']}

        for link in self.topology['links']:
            node1   = link['node1']
            node2   = link['node2']
            subnet  = link['subnet']
            cross   = link.get('cross_domain', False)

            iface1 = f"{node1}-{node2}"
            ip1    = subnet.replace('::/64', '::1/64')
            interface_map[node1].append({
                'name':         iface1,
                'ipv6':         ip1,
                'cross_domain': cross,
                'peer':         node2,
                'link_type':    link['type'],
            })

            iface2 = f"{node2}-{node1}"
            ip2    = subnet.replace('::/64', '::2/64')
            interface_map[node2].append({
                'name':         iface2,
                'ipv6':         ip2,
                'cross_domain': cross,
                'peer':         node1,
                'link_type':    link['type'],
            })

        return interface_map

    def _build_bgp_neighbor_map(self):
        """
        构建 iBGP 邻居的 loopback IPv6 地址映射。
        iBGP session 使用对端 loopback（SRv6 locator 主机地址）建立，
        需要通过 IS-IS 路由可达。
        """
        self.ibgp_neighbor_ip = {}   # node_name -> {peer_name -> peer_loopback_ip}
        for a, b in IBGP_PAIRS:
            node_a = self.node_map[a]
            node_b = self.node_map[b]
            # SRv6 locator 格式：fc00:RRRR:NNNN::1/128，取主机地址
            ip_a = node_a['srv6_locator'].split('/')[0]
            ip_b = node_b['srv6_locator'].split('/')[0]
            self.ibgp_neighbor_ip.setdefault(a, {})[b] = ip_b
            self.ibgp_neighbor_ip.setdefault(b, {})[a] = ip_a

        # eBGP 邻居使用直连接口 IP（::2 on cross-domain link）
        self.ebgp_neighbor_ip = {}   # node_name -> peer_ipv6 (link-local on cross-domain iface)
        for link in self.topology['links']:
            if not link.get('cross_domain'):
                continue
            n1, n2 = link['node1'], link['node2']
            subnet = link['subnet']   # fc00:9000:RRRR:NNNN::/64
            ip1 = subnet.replace('::/64', '::1')
            ip2 = subnet.replace('::/64', '::2')
            # n1 的 eBGP peer IP 是 n2 的接口地址
            if n1 in EBGP_DIRECT_PEERS and EBGP_DIRECT_PEERS[n1] == n2:
                self.ebgp_neighbor_ip[n1] = ip2
                self.ebgp_neighbor_ip[n2] = ip1

    # ── 普通节点配置 ──────────────────────────────────────────────────────────

    def _gen_normal_node(self, node):
        """单IS-IS，无BGP"""
        name       = node['name']
        domain     = node['domain']
        isis_net   = node['isis_net']
        isis_inst  = f"SRv6_{domain}"
        srv6_locator = node['srv6_locator']
        interfaces = self.node_interfaces[name]

        cfg = f"""!
! FRR config for {name}  [Domain {domain} AS{node['asn']}]
!
frr version 8.1
frr defaults traditional
hostname {name}
log file /var/log/frr/frr.log
service integrated-vtysh-config
!
ipv6 forwarding
!
interface lo
 description SRv6 Locator
 ipv6 address {srv6_locator}
 ipv6 router isis {isis_inst}
!
"""
        for iface in interfaces:
            cfg += f"""interface {iface['name']}
 ipv6 address {iface['ipv6']}
 ipv6 router isis {isis_inst}
 isis network point-to-point
 isis hello-interval 3
 isis hello-multiplier 3
!
"""

        cfg += f"""router isis {isis_inst}
 net {isis_net}
 is-type level-2-only
 topology ipv6-unicast
 log-adjacency-changes
!
line vty
!
end
"""
        return cfg

    # ── iBGP边界节点配置（B域r15n0/r29n0，C域r30n0/r44n0）─────────────────

    def _gen_ibgp_border_node(self, node):
        """
        单IS-IS + iBGP + 路由聚合/再分发。
        这类节点不直接跨越域边界，但需要：
          1. 接收来自 eBGP 边界节点（via iBGP）的域外聚合路由
          2. 将域外聚合路由注入 IS-IS，使域内节点可达
          3. 向 iBGP peer 通告本域聚合前缀
        """
        name        = node['name']
        domain      = node['domain']
        asn         = node['asn']
        isis_net    = node['isis_net']
        isis_inst   = f"SRv6_{domain}"
        srv6_locator = node['srv6_locator']
        loopback_ip  = srv6_locator.split('/')[0]
        interfaces  = self.node_interfaces[name]

        # 本域 BGP 聚合前缀
        start_ring, end_ring = DOMAIN_CONFIG[domain]['rings']
        aggregates = [f'fc00:{r:04x}::/32' for r in range(start_ring, end_ring + 1)]

        # iBGP peer
        ibgp_peers = self.ibgp_neighbor_ip.get(name, {})

        cfg = f"""!
! FRR config for {name}  [Domain {domain} AS{asn} - iBGP border]
!
frr version 8.1
frr defaults traditional
hostname {name}
log file /var/log/frr/frr.log
service integrated-vtysh-config
!
ipv6 forwarding
!
interface lo
 description SRv6 Locator
 ipv6 address {srv6_locator}
 ipv6 router isis {isis_inst}
!
"""
        for iface in interfaces:
            cfg += f"""interface {iface['name']}
 ipv6 address {iface['ipv6']}
 ipv6 router isis {isis_inst}
 isis network point-to-point
 isis hello-interval 3
 isis hello-multiplier 3
!
"""

        cfg += f"""router isis {isis_inst}
 net {isis_net}
 is-type level-2-only
 topology ipv6-unicast
 log-adjacency-changes
 redistribute bgp level-2
!
"""

        # BGP 配置
        cfg += f"""router bgp {asn}
 bgp router-id {loopback_ip}
 no bgp ebgp-requires-policy
 bgp log-neighbor-changes
!
 address-family ipv6 unicast
"""
        # 本域聚合前缀（summary-only 不向上游泄漏明细）
        for agg in aggregates:
            cfg += f"  aggregate-address {agg} summary-only\n"

        # iBGP 邻居
        for peer_name, peer_ip in ibgp_peers.items():
            peer_asn = self.node_map[peer_name]['asn']
            cfg += f"""  neighbor {peer_ip} remote-as {peer_asn}
  neighbor {peer_ip} update-source lo
  neighbor {peer_ip} activate
  neighbor {peer_ip} soft-reconfiguration inbound
"""

        cfg += """ exit-address-family
!
line vty
!
end
"""
        return cfg

    # ── eBGP边界节点配置（r14n0, r15n0, r29n0, r30n0, r44n0, r45n0）─────────

    def _gen_ebgp_border_node(self, node):
        """
        双IS-IS实例 + eBGP + iBGP + 路由聚合/再分发。

        两个IS-IS实例：
          - 主实例 SRv6_{domain}：与本域所有邻居运行
          - 次实例 SRv6_{secondary_domain}：仅与对端跨域节点运行（跨域链路）

        BGP：
          - eBGP：与对端域的 eBGP 边界节点（跨域直连）
          - iBGP：与同域另一端边界节点（如存在，B/C域）
          - 本域聚合前缀通过 aggregate-address 发布
          - 接收到的域外聚合前缀 redistribute 进两个IS-IS实例
        """
        name             = node['name']
        domain           = node['domain']
        asn              = node['asn']
        isis_net         = node['isis_net']
        isis_inst        = f"SRv6_{domain}"
        srv6_locator     = node['srv6_locator']
        loopback_ip      = srv6_locator.split('/')[0]
        interfaces       = self.node_interfaces[name]

        secondary_domain   = node.get('secondary_domain')
        secondary_isis_net = node.get('secondary_isis_net')
        secondary_inst     = f"SRv6_{secondary_domain}" if secondary_domain else None

        # 本域 BGP 聚合前缀
        start_ring, end_ring = DOMAIN_CONFIG[domain]['rings']
        aggregates = [f'fc00:{r:04x}::/32' for r in range(start_ring, end_ring + 1)]

        # eBGP peer IP（对端直连接口地址）
        ebgp_peer_ip  = self.ebgp_neighbor_ip.get(name)
        ebgp_peer_name = EBGP_DIRECT_PEERS.get(name)
        ebgp_peer_asn  = self.node_map[ebgp_peer_name]['asn'] if ebgp_peer_name else None

        # iBGP peers（同域另端，可能为空）
        ibgp_peers = self.ibgp_neighbor_ip.get(name, {})

        cfg = f"""!
! FRR config for {name}  [Domain {domain} AS{asn} - eBGP border, dual-ISIS]
!
frr version 8.1
frr defaults traditional
hostname {name}
log file /var/log/frr/frr.log
service integrated-vtysh-config
!
ipv6 forwarding
!
interface lo
 description SRv6 Locator
 ipv6 address {srv6_locator}
 ipv6 router isis {isis_inst}
!
"""
        # 接口配置：根据是否跨域链路决定加入哪个IS-IS实例
        for iface in interfaces:
            is_cross = iface.get('cross_domain', False)
            peer     = iface.get('peer', '')

            if is_cross and secondary_inst:
                # 跨域链路：加入次IS-IS实例
                isis_for_iface = secondary_inst
            else:
                # 域内链路：加入主IS-IS实例
                isis_for_iface = isis_inst

            cfg += f"""interface {iface['name']}
 ipv6 address {iface['ipv6']}
 ipv6 router isis {isis_for_iface}
 isis network point-to-point
 isis hello-interval 3
 isis hello-multiplier 3
!
"""

        # 主IS-IS实例（本域）
        cfg += f"""router isis {isis_inst}
 net {isis_net}
 is-type level-2-only
 topology ipv6-unicast
 log-adjacency-changes
 redistribute bgp level-2
!
"""

        # 次IS-IS实例（对端域，仅跨域链路）
        if secondary_inst and secondary_isis_net:
            cfg += f"""router isis {secondary_inst}
 net {secondary_isis_net}
 is-type level-2-only
 topology ipv6-unicast
 log-adjacency-changes
 redistribute bgp level-2
!
"""

        # BGP 配置
        cfg += f"""router bgp {asn}
 bgp router-id {loopback_ip}
 no bgp ebgp-requires-policy
 bgp log-neighbor-changes
!
 address-family ipv6 unicast
"""
        # 本域聚合前缀
        for agg in aggregates:
            cfg += f"  aggregate-address {agg} summary-only\n"

        # eBGP 邻居（直连，使用对端接口地址）
        if ebgp_peer_ip and ebgp_peer_asn:
            cfg += f"""  neighbor {ebgp_peer_ip} remote-as {ebgp_peer_asn}
  neighbor {ebgp_peer_ip} activate
  neighbor {ebgp_peer_ip} soft-reconfiguration inbound
"""

        # iBGP 邻居（loopback，需IS-IS可达）
        for peer_name, peer_ip in ibgp_peers.items():
            peer_asn = self.node_map[peer_name]['asn']
            cfg += f"""  neighbor {peer_ip} remote-as {peer_asn}
  neighbor {peer_ip} update-source lo
  neighbor {peer_ip} activate
  neighbor {peer_ip} soft-reconfiguration inbound
"""

        cfg += """ exit-address-family
!
line vty
!
end
"""
        return cfg

    # ── daemons 配置 ─────────────────────────────────────────────────────────

    def generate_daemons_conf(self):
        return """zebra=yes
bgpd=yes
isisd=yes
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
bgpd_options=""
isisd_options=" -A ::1"
"""

    # ── 批量生成 ──────────────────────────────────────────────────────────────

    def generate_all(self, output_dir='configs'):
        output_path = Path(output_dir)
        output_path.mkdir(exist_ok=True)

        total_nodes = len(self.topology['nodes'])
        normal_count = ebgp_count = ibgp_count = 0

        print("\n" + "=" * 60)
        print(f"Generating FRR+BGP configurations for {total_nodes} nodes...")
        print("=" * 60)

        batch_size = 20
        for i, node in enumerate(self.topology['nodes'], 1):
            name = node['name']

            # 判断节点类型
            if name in EBGP_DIRECT_PEERS:
                config = self._gen_ebgp_border_node(node)
                ebgp_count += 1
            elif name in self.ibgp_neighbor_ip:
                config = self._gen_ibgp_border_node(node)
                ibgp_count += 1
            else:
                config = self._gen_normal_node(node)
                normal_count += 1

            config_file = output_path / f"frr-{name}.conf"
            with open(config_file, 'w') as f:
                f.write(config)

            if i % batch_size == 0 or i == total_nodes:
                pct = (i / total_nodes) * 100
                print(f"  Progress: {i}/{total_nodes} ({pct:.1f}%)")

        # daemons 配置（所有节点共用，bgpd=yes）
        daemons_file = output_path / "daemons"
        with open(daemons_file, 'w') as f:
            f.write(self.generate_daemons_conf())

        print("\n" + "=" * 60)
        print(f"✓ Generated {total_nodes} node configurations")
        print(f"  Normal nodes  : {normal_count}")
        print(f"  iBGP borders  : {ibgp_count}  {list(self.ibgp_neighbor_ip.keys())}")
        print(f"  eBGP borders  : {ebgp_count}  {list(EBGP_DIRECT_PEERS.keys())}")
        print(f"✓ Output directory: {output_dir}/")
        print("=" * 60)

        self._print_route_estimate()

    def _print_route_estimate(self):
        """打印预估路由表规模"""
        domains = self.topology['domains']
        print("\n--- Route Table Size Estimate ---")
        for domain, meta in domains.items():
            n = meta['node_count']
            rings = meta['rings'][1] - meta['rings'][0] + 1
            # 域内IS-IS：n个/128 locator + n个intra-ring链路子网 + inter-ring链路
            intra_isis = n + n + (rings * NODES_PER_RING)
            # 域外BGP聚合：其余3个域，每域15条/32
            other_rings = 60 - rings
            bgp_routes = other_rings   # 每个外部ring 1条/32
            total = intra_isis + bgp_routes
            print(f"  Domain {domain}: ~{intra_isis} IS-IS + {bgp_routes} BGP = ~{total} routes/node")
        print(f"  (vs original full-mesh IS-IS: ~3500 routes/node)")


NODES_PER_RING = 20   # module-level constant for estimate


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 generate_configs_bgp.py <topology_bgp.json>")
        print("\nExample:")
        print("  python3 generate_configs_bgp.py topology_1200nodes_bgp.json")
        sys.exit(1)

    topology_file = sys.argv[1]

    if not Path(topology_file).exists():
        print(f"Error: Topology file '{topology_file}' not found!")
        sys.exit(1)

    try:
        generator = BGPDomainFRRConfigGenerator(topology_file)
        generator.generate_all()
        print("\n✓ Configuration generation completed!")
        print("\nNext step:")
        print(f"  sudo python3 deploy_5Network.py {topology_file}")
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

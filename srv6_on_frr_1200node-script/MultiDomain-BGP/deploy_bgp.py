#!/usr/bin/env python3
"""
SRv6+FRR+BGP 部署编排器（BGP分域版，1200节点）

相对原版 deploy_5Network.py 的主要变化：
  1. 边界节点容器添加 IS_BORDER=true 环境变量（启动脚本据此延长等待）
  2. 收敛等待分两阶段：IS-IS 收敛 + BGP 收敛
  3. 验证增加 BGP 邻居和跨域路由检查
  4. Docker network 分配不变（仍按ring范围分5个network，与BGP域无关）
"""

import json
import os
import sys
import subprocess
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# eBGP边界节点集合（部署时需设置 IS_BORDER=true）
EBGP_BORDER_NODES = {'r14n0', 'r15n0', 'r29n0', 'r30n0', 'r44n0', 'r45n0'}
# iBGP边界节点集合
IBGP_BORDER_NODES = {'r15n0', 'r29n0', 'r30n0', 'r44n0'}
ALL_BORDER_NODES  = EBGP_BORDER_NODES | IBGP_BORDER_NODES


class BGPSRv6Deployer:
    def __init__(self, topology_file, config_dir='configs'):
        with open(topology_file, 'r') as f:
            self.topology = json.load(f)

        if 'domains' not in self.topology:
            print("ERROR: Not a BGP topology file. Run generate_topology_bgp.py first.")
            sys.exit(1)

        self.config_dir        = Path(config_dir)
        self.base_network_name = self.topology.get('network_name', 'srv6-bgp-net')
        self.image_name        = 'frr-srv6-node:latest'
        self.max_workers       = 30

        # 5个Docker bridge network，按ring索引分配（与原版逻辑相同）
        self.num_networks   = 5
        self.network_names  = [f"{self.base_network_name}-{i}" for i in range(self.num_networks)]
        nodes_per_ring = self.topology.get('nodes_per_ring', 20)
        num_rings      = self.topology.get('num_rings', 60)
        rings_per_net  = num_rings // self.num_networks

        self._node_network = {}
        for node in self.topology['nodes']:
            ring_idx = node['ring']
            net_idx  = min(ring_idx // rings_per_net, self.num_networks - 1)
            self._node_network[node['name']] = self.network_names[net_idx]

        self._pid_cache = {}

    # ── 工具方法 ──────────────────────────────────────────────────────────────

    def run_cmd(self, cmd, check=True, quiet=False):
        if not quiet:
            print(f"  $ {cmd[:100]}...")
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if check and result.returncode != 0:
            if not quiet:
                print(f"  ✗ Error: {result.stderr}")
            raise RuntimeError(f"Command failed: {cmd}")
        return result

    def docker_exec(self, node, cmd, quiet=False):
        return self.run_cmd(
            f"docker exec srv6-{node} {cmd}",
            check=False, quiet=quiet
        )

    # ── Step 1: 清理 ──────────────────────────────────────────────────────────

    def cleanup(self):
        print("\n" + "=" * 60)
        print("Step 1: Cleanup old deployment")
        print("=" * 60)

        total = len(self.topology['nodes'])
        node_names = [f"srv6-{n['name']}" for n in self.topology['nodes']]
        batch_size = 100
        for i in range(0, len(node_names), batch_size):
            batch = node_names[i:i+batch_size]
            self.run_cmd(f"docker rm -f {' '.join(batch)}", check=False, quiet=True)
            done = min(i + batch_size, len(node_names))
            if done % 400 == 0 or done >= len(node_names):
                print(f"  Containers removed: {done}/{len(node_names)}")

        for net_name in self.network_names:
            self.run_cmd(f"docker network rm {net_name}", check=False, quiet=True)

        total_links = len(self.topology['links'])
        for i, link in enumerate(self.topology['links']):
            if i % 500 == 0:
                print(f"  veth cleanup: {i}/{total_links}")
            self.run_cmd(
                f"ip link delete {link['node1']}-{link['node2']}",
                check=False, quiet=True
            )
        print("✓ Cleanup completed")

    # ── Step 2: 网络 ──────────────────────────────────────────────────────────

    def create_network(self):
        print("\n" + "=" * 60)
        print("Step 2: Create Docker networks")
        print("=" * 60)
        for net_name in self.network_names:
            self.run_cmd(f"docker network create {net_name}", quiet=True)
            print(f"  ✓ {net_name}")

    # ── Step 3: 启动容器 ──────────────────────────────────────────────────────

    def start_single_container(self, node):
        name         = node['name']
        srv6_locator = node['srv6_locator']
        is_border    = 'true' if name in ALL_BORDER_NODES else 'false'

        frr_conf   = self.config_dir / f"frr-{name}.conf"
        daemons_conf = self.config_dir / "daemons"

        if not frr_conf.exists():
            raise FileNotFoundError(f"Config not found: {frr_conf}")

        node_network = self._node_network[name]
        cmd = f"""
            docker run -d \
              --name srv6-{name} \
              --hostname {name} \
              --network {node_network} \
              --privileged \
              --cap-add NET_ADMIN \
              --sysctl net.ipv6.conf.all.disable_ipv6=0 \
              --sysctl net.ipv6.conf.all.forwarding=1 \
              -v {frr_conf.absolute()}:/etc/frr/frr.conf \
              -v {daemons_conf.absolute()}:/etc/frr/daemons:ro \
              -e NODE_NAME={name} \
              -e SRV6_LOCATOR={srv6_locator} \
              -e IS_BORDER={is_border} \
              {self.image_name}
        """
        self.run_cmd(cmd, quiet=True)
        return name

    def start_containers(self):
        print("\n" + "=" * 60)
        print("Step 3: Start FRR+BGP containers")
        print("=" * 60)

        total = len(self.topology['nodes'])
        print(f"Starting {total} containers ({len(ALL_BORDER_NODES)} border nodes)...")

        completed = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self.start_single_container, n): n
                       for n in self.topology['nodes']}
            for future in as_completed(futures):
                try:
                    future.result()
                    completed += 1
                    if completed % 100 == 0 or completed == total:
                        print(f"  Progress: {completed}/{total} ({completed/total*100:.1f}%)")
                except Exception as e:
                    node = futures[future]
                    print(f"  ✗ {node['name']}: {e}")

        print(f"✓ Started {completed}/{total} containers")
        print("Waiting 15s for container initialization...")
        time.sleep(15)

    # ── Step 4: PID 预取 ──────────────────────────────────────────────────────

    def prefetch_all_pids(self):
        print("\n" + "=" * 60)
        print("Step 4: Pre-fetching container PIDs")
        print("=" * 60)

        total      = len(self.topology['nodes'])
        node_names = [n['name'] for n in self.topology['nodes']]
        batch_size = 200
        fetched    = 0

        def fetch_pid(name):
            result = self.run_cmd(
                f"docker inspect -f '{{{{.State.Pid}}}}' srv6-{name}",
                quiet=True
            )
            return name, result.stdout.strip()

        for i in range(0, len(node_names), batch_size):
            batch = node_names[i:i+batch_size]
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                for name, pid in executor.map(fetch_pid, batch):
                    if pid:
                        self._pid_cache[name] = pid
                        fetched += 1
            if fetched % 400 == 0 or fetched >= total:
                print(f"  Progress: {fetched}/{total}")

        print(f"✓ Cached {len(self._pid_cache)} PIDs")

    # ── Step 5: 链路 ──────────────────────────────────────────────────────────

    def create_single_link(self, link):
        node1  = link['node1']
        node2  = link['node2']
        subnet = link['subnet']

        veth1 = f"{node1}-{node2}"
        veth2 = f"{node2}-{node1}"

        pid1 = self._pid_cache.get(node1)
        pid2 = self._pid_cache.get(node2)
        if not pid1 or not pid2:
            raise RuntimeError(f"PID not found for {node1} or {node2}")

        ip1 = subnet.replace('::/64', '::1/64')
        ip2 = subnet.replace('::/64', '::2/64')

        self.run_cmd(f"ip link add {veth1} type veth peer name {veth2}", quiet=True)

        # 移入容器网络命名空间
        self.run_cmd(f"ip link set {veth1} netns {pid1}", quiet=True)
        self.run_cmd(f"ip link set {veth2} netns {pid2}", quiet=True)

        # 配置接口名和IPv6地址
        self.run_cmd(
            f"nsenter -t {pid1} -n ip link set {veth1} name {veth1} up",
            quiet=True)
        self.run_cmd(
            f"nsenter -t {pid1} -n ip -6 addr add {ip1} dev {veth1}",
            quiet=True)
        self.run_cmd(
            f"nsenter -t {pid2} -n ip link set {veth2} name {veth2} up",
            quiet=True)
        self.run_cmd(
            f"nsenter -t {pid2} -n ip -6 addr add {ip2} dev {veth2}",
            quiet=True)

    def create_links(self):
        print("\n" + "=" * 60)
        print("Step 5: Create inter-node links")
        print("=" * 60)

        total     = len(self.topology['links'])
        cross_cnt = sum(1 for l in self.topology['links'] if l.get('cross_domain'))
        print(f"Creating {total} links ({cross_cnt} cross-domain)...")

        completed = failed = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self.create_single_link, l): l
                       for l in self.topology['links']}
            for future in as_completed(futures):
                try:
                    future.result()
                    completed += 1
                    if completed % 200 == 0 or completed == total:
                        print(f"  Progress: {completed}/{total} ({completed/total*100:.1f}%)")
                except Exception as e:
                    failed += 1
                    link = futures[future]
                    print(f"  ✗ {link['node1']}-{link['node2']}: {e}")

        print(f"✓ Created {completed}/{total} links" +
              (f" ({failed} failed)" if failed else ""))

    # ── Step 6: 等待收敛 ──────────────────────────────────────────────────────

    def wait_for_convergence(self):
        """两阶段等待：IS-IS收敛 + BGP收敛"""
        print("\n" + "=" * 60)
        print("Step 6: Wait for IS-IS + BGP convergence")
        print("=" * 60)

        total_nodes = len(self.topology['nodes'])

        # 阶段一：IS-IS收敛（域内300节点，比原来快很多）
        isis_wait = min(300, 30 + total_nodes // 10)
        print(f"Phase 1: IS-IS convergence ({isis_wait}s)...")
        for i in range(isis_wait):
            if i % 30 == 0:
                print(f"  {i}/{isis_wait}s", flush=True)
            time.sleep(1)

        # 阶段二：BGP收敛（eBGP建立 + iBGP传播）
        bgp_wait = 60
        print(f"\nPhase 2: BGP convergence ({bgp_wait}s)...")
        for i in range(bgp_wait):
            if i % 15 == 0:
                print(f"  {i}/{bgp_wait}s", flush=True)
            time.sleep(1)

        print(f"\n✓ Convergence wait completed")

    # ── Step 7: 验证 ──────────────────────────────────────────────────────────

    def verify_deployment(self):
        print("\n" + "=" * 60)
        print("Step 7: Verify deployment")
        print("=" * 60)

        nodes     = self.topology['nodes']
        domains   = self.topology['domains']

        # 每个域抽样一个普通节点 + 所有边界节点
        sample_names = list(ALL_BORDER_NODES)
        for domain, meta in domains.items():
            start_ring, end_ring = meta['rings']
            mid_ring = (start_ring + end_ring) // 2
            mid_node = f"r{mid_ring}n5"
            sample_names.append(mid_node)

        print(f"\nSampling {len(sample_names)} nodes...")

        for name in sample_names:
            is_border = name in ALL_BORDER_NODES
            tag = "[BORDER]" if is_border else "[NORMAL]"
            print(f"\n--- {name} {tag} ---")

            # IS-IS 邻居
            result = self.docker_exec(
                name,
                "vtysh -c 'show isis neighbor' | grep -c Up",
                quiet=True)
            print(f"  IS-IS neighbors (Up): {result.stdout.strip() or '0'}")

            # IPv6 路由数
            result = self.docker_exec(name, "ip -6 route show | wc -l", quiet=True)
            print(f"  IPv6 routes total   : {result.stdout.strip()}")

            if is_border:
                # BGP 邻居状态
                result = self.docker_exec(
                    name,
                    "vtysh -c 'show bgp ipv6 unicast summary' 2>/dev/null | grep -E 'Establ|Active|Idle'",
                    quiet=True)
                if result.stdout.strip():
                    print(f"  BGP neighbors:\n{result.stdout.rstrip()}")

                # 跨域路由数
                result = self.docker_exec(
                    name,
                    "vtysh -c 'show bgp ipv6 unicast' 2>/dev/null | grep -c fc00",
                    quiet=True)
                print(f"  BGP routes (fc00:): {result.stdout.strip() or '0'}")

    # ── Summary ──────────────────────────────────────────────────────────────

    def print_summary(self):
        print("\n" + "=" * 60)
        print("DEPLOYMENT COMPLETED")
        print("=" * 60)

        nodes   = self.topology['nodes']
        domains = self.topology['domains']

        print(f"\nTopology: {len(nodes)} nodes, {len(self.topology['links'])} links")
        print(f"Domains : {len(domains)} × BGP AS")
        print(f"Routing : IS-IS (intra-domain) + BGP (inter-domain)")

        print("\n" + "-" * 60)
        print("Quick verification commands:")
        print("-" * 60)
        print("""
# 检查边界节点BGP邻居
docker exec srv6-r14n0 vtysh -c 'show bgp ipv6 unicast summary'
docker exec srv6-r15n0 vtysh -c 'show bgp ipv6 unicast summary'

# 检查边界节点路由表（IS-IS域内 + BGP域外聚合）
docker exec srv6-r14n0 vtysh -c 'show bgp ipv6 unicast'
docker exec srv6-r14n0 ip -6 route show | wc -l

# 检查普通节点路由（应包含域外/32聚合路由）
docker exec srv6-r7n10 ip -6 route show | grep -E 'fc00:.*/32'

# 测试域内连通性（域A内部）
docker exec srv6-r0n0 ping6 -c 3 fc00:000e:0000::1

# 测试跨域连通性（域A -> 域B）
docker exec srv6-r0n0 ping6 -c 3 fc00:001d:0000::1

# 测试跨域连通性（域A -> 域C，需经过B中转）
docker exec srv6-r0n0 ping6 -c 3 fc00:002c:0000::1

# 测试跨域连通性（域A -> 域D，跨3个域）
docker exec srv6-r0n0 ping6 -c 3 fc00:003b:0000::1

# 检查IS-IS拓扑数据库（确认双实例正常）
docker exec srv6-r14n0 vtysh -c 'show isis topology ipv6-unicast'
docker exec srv6-r15n0 vtysh -c 'show isis topology ipv6-unicast'
""")

        print("-" * 60)
        print("Cleanup:")
        print("  python3 deploy_bgp.py --cleanup topology_1200nodes_bgp.json")

    # ── 主流程 ────────────────────────────────────────────────────────────────

    def deploy(self):
        print("\n" + "=" * 60)
        print("SRv6+FRR+BGP DEPLOYMENT (1200 NODES, 4 DOMAINS)")
        print("=" * 60)
        print(f"Nodes          : {len(self.topology['nodes'])}")
        print(f"Links          : {len(self.topology['links'])}")
        print(f"Border nodes   : {sorted(ALL_BORDER_NODES)}")
        print(f"Parallel workers: {self.max_workers}")

        start_time = time.time()
        try:
            self.cleanup()
            self.create_network()
            self.start_containers()
            self.prefetch_all_pids()
            self.create_links()
            self.wait_for_convergence()
            self.verify_deployment()
            self.print_summary()

            elapsed = int(time.time() - start_time)
            print(f"\n✓ Total deployment time: {elapsed // 60}m {elapsed % 60}s")
            return True

        except Exception as e:
            print(f"\n✗ Deployment failed: {e}")
            import traceback
            traceback.print_exc()
            return False


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description='Deploy SRv6+FRR+BGP network (1200 nodes, 4 domains)'
    )
    parser.add_argument('topology', nargs='?',
                        default='topology_1200nodes_bgp.json')
    parser.add_argument('--cleanup', action='store_true')
    parser.add_argument('--config-dir', default='configs')
    parser.add_argument('--workers', type=int, default=30)
    args = parser.parse_args()

    if not os.path.exists(args.topology):
        print(f"Error: {args.topology} not found!")
        sys.exit(1)

    deployer = BGPSRv6Deployer(args.topology, args.config_dir)
    deployer.max_workers = args.workers

    if args.cleanup:
        deployer.cleanup()
        print("✓ Cleanup completed")
        sys.exit(0)

    if not deployer.config_dir.exists():
        print(f"Error: config dir '{deployer.config_dir}' not found!")
        print(f"  Run: python3 generate_configs_bgp.py {args.topology}")
        sys.exit(1)

    success = deployer.deploy()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

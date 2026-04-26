#!/usr/bin/env python3
"""
SRv6+FRR部署Orchestrator - 4000节点版
支持大规模部署（4000节点）
优化：并行化、进度显示、资源管理、批量PID预取
"""

import json
import os
import sys
import subprocess
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed


class LargeScaleSRv6Deployer:
    def __init__(self, topology_file, config_dir='configs'):
        with open(topology_file, 'r') as f:
            self.topology = json.load(f)

        self.config_dir = Path(config_dir)
        self.base_network_name = self.topology.get('network_name', 'srv6-net')
        self.image_name = 'frr-srv6-node:latest'
        self.max_workers = 30  # 4000节点需要更高并行度

        # 5个独立network，每个bridge承载800个容器，规避bridge FDB 1024上限
        self.num_networks = 5
        self.network_names = [
            f"{self.base_network_name}-{i}" for i in range(self.num_networks)
        ]
        # 按ring范围划分network：每个network负责 80/5=16 个ring
        nodes_per_ring = self.topology.get('nodes_per_ring', 50)
        num_rings = self.topology.get('num_rings', 80)
        rings_per_net = num_rings // self.num_networks  # 16
        # node_id -> network_name 映射，按ring索引分配
        self._node_network = {}
        for node in self.topology['nodes']:
            ring_idx = node['ring']
            net_idx = min(ring_idx // rings_per_net, self.num_networks - 1)
            self._node_network[node['name']] = self.network_names[net_idx]

        # 缓存容器PID，避免link创建时重复docker inspect
        self._pid_cache = {}

    def run_cmd(self, cmd, check=True, quiet=False):
        """执行shell命令"""
        if not quiet:
            print(f"  $ {cmd[:100]}...")

        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True
        )

        if check and result.returncode != 0:
            if not quiet:
                print(f"  ✗ Error: {result.stderr}")
            raise RuntimeError(f"Command failed: {cmd}")

        return result

    def docker_exec(self, node, cmd, quiet=False):
        """在容器中执行命令"""
        return self.run_cmd(
            f"docker exec srv6-{node} {cmd}",
            check=False,
            quiet=quiet
        )

    def cleanup(self):
        """清理旧环境"""
        print("\n" + "=" * 60)
        print("Step 1: Cleanup old deployment")
        print("=" * 60)

        total = len(self.topology['nodes'])
        print(f"Removing {total} containers...")

        # 批量删除容器，batch_size提升为100减少调用次数
        node_names = [f"srv6-{node['name']}" for node in self.topology['nodes']]
        batch_size = 100

        for i in range(0, len(node_names), batch_size):
            batch = node_names[i:i+batch_size]
            cmd = f"docker rm -f {' '.join(batch)}"
            self.run_cmd(cmd, check=False, quiet=True)

            done = min(i + batch_size, len(node_names))
            if done % 500 == 0 or done >= len(node_names):
                print(f"  Progress: {done}/{len(node_names)}")

        # 删除5个network
        for net_name in self.network_names:
            self.run_cmd(f"docker network rm {net_name}", check=False, quiet=True)

        # 批量删除veth接口
        total_links = len(self.topology['links'])
        print(f"Removing {total_links} veth pairs...")
        for i, link in enumerate(self.topology['links']):
            if i % 500 == 0:
                print(f"  Progress: {i}/{total_links}")
            self.run_cmd(
                f"ip link delete {link['node1']}-{link['node2']}",
                check=False,
                quiet=True
            )

        print("✓ Cleanup completed")

    def create_network(self):
        """创建5个独立Docker网络，每个bridge承载800容器，规避FDB 1024上限"""
        print("\n" + "=" * 60)
        print("Step 2: Create Docker networks (5 networks × 800 containers)")
        print("=" * 60)

        for net_name in self.network_names:
            self.run_cmd(f"docker network create {net_name}", quiet=True)
            print(f"✓ Network '{net_name}' created")

    def start_single_container(self, node):
        """启动单个容器（用于并行化）"""
        node_name = node['name']
        srv6_locator = node['srv6_locator']

        frr_conf = self.config_dir / f"frr-{node_name}.conf"
        daemons_conf = self.config_dir / "daemons"

        if not frr_conf.exists():
            raise FileNotFoundError(f"Configuration file {frr_conf} not found!")

        # 按ring范围选择对应network，每个bridge只挂800个容器
        node_network = self._node_network[node_name]
        cmd = f"""
            docker run -d \
              --name srv6-{node_name} \
              --hostname {node_name} \
              --network {node_network} \
              --privileged \
              --cap-add NET_ADMIN \
              --sysctl net.ipv6.conf.all.disable_ipv6=0 \
              --sysctl net.ipv6.conf.all.forwarding=1 \
              -v {frr_conf.absolute()}:/etc/frr/frr.conf \
              -v {daemons_conf.absolute()}:/etc/frr/daemons:ro \
              -e NODE_NAME={node_name} \
              -e SRV6_LOCATOR={srv6_locator} \
              {self.image_name}
        """

        self.run_cmd(cmd, quiet=True)
        return node_name

    def start_containers(self):
        """并行启动所有容器"""
        print("\n" + "=" * 60)
        print("Step 3: Start FRR containers")
        print("=" * 60)

        total = len(self.topology['nodes'])
        print(f"Starting {total} containers (parallel: {self.max_workers} workers)...")

        completed = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self.start_single_container, node): node
                      for node in self.topology['nodes']}

            for future in as_completed(futures):
                try:
                    future.result()
                    completed += 1

                    if completed % 100 == 0 or completed == total:
                        percent = (completed / total) * 100
                        print(f"  Progress: {completed}/{total} ({percent:.1f}%)")

                except Exception as e:
                    node = futures[future]
                    print(f"  ✗ Error starting {node['name']}: {e}")

        print(f"✓ Started {completed}/{total} containers")
        print("Waiting for containers to initialize...")
        time.sleep(15)

    def prefetch_all_pids(self):
        """
        批量预取所有容器PID，避免link创建时逐个docker inspect。
        4000节点 × 2次inspect/link = 大量重复调用，预取后直接查字典。
        """
        print("\n" + "=" * 60)
        print("Step 4: Pre-fetching container PIDs")
        print("=" * 60)

        total = len(self.topology['nodes'])
        print(f"Fetching PIDs for {total} containers...")

        # 使用Go模板一次性批量获取，每批200个减少命令数量
        node_names = [node['name'] for node in self.topology['nodes']]
        batch_size = 200
        fetched = 0

        for i in range(0, len(node_names), batch_size):
            batch = node_names[i:i+batch_size]

            # 并行获取这批节点的PID
            def fetch_pid(name):
                result = self.run_cmd(
                    f"docker inspect -f '{{{{.State.Pid}}}}' srv6-{name}",
                    quiet=True
                )
                return name, result.stdout.strip()

            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                for name, pid in executor.map(fetch_pid, batch):
                    if pid:
                        self._pid_cache[name] = pid
                        fetched += 1

            if fetched % 500 == 0 or fetched >= total:
                print(f"  Progress: {fetched}/{total}")

        print(f"✓ Cached {len(self._pid_cache)} PIDs")

    def create_single_link(self, link):
        """创建单个链路，使用预缓存的PID"""
        node1 = link['node1']
        node2 = link['node2']
        subnet = link['subnet']

        veth1 = f"{node1}-{node2}"
        veth2 = f"{node2}-{node1}"

        # 创建veth对
        self.run_cmd(f"ip link add {veth1} type veth peer name {veth2}", quiet=True)

        # 从缓存获取PID，避免重复docker inspect
        pid1 = self._pid_cache.get(node1)
        pid2 = self._pid_cache.get(node2)

        if not pid1:
            pid1 = self.run_cmd(
                f"docker inspect -f '{{{{.State.Pid}}}}' srv6-{node1}",
                quiet=True
            ).stdout.strip()

        if not pid2:
            pid2 = self.run_cmd(
                f"docker inspect -f '{{{{.State.Pid}}}}' srv6-{node2}",
                quiet=True
            ).stdout.strip()

        # 移动到容器命名空间
        self.run_cmd(f"ip link set {veth1} netns {pid1}", quiet=True)
        self.run_cmd(f"ip link set {veth2} netns {pid2}", quiet=True)

        # 启用接口
        self.docker_exec(node1, f"ip link set {veth1} up", quiet=True)
        self.docker_exec(node2, f"ip link set {veth2} up", quiet=True)

        # 配置IPv6地址
        ip1 = subnet.replace('::/64', '::1/64')
        ip2 = subnet.replace('::/64', '::2/64')
        self.docker_exec(node1, f"ip -6 addr add {ip1} dev {veth1}", quiet=True)
        self.docker_exec(node2, f"ip -6 addr add {ip2} dev {veth2}", quiet=True)

    def create_links(self):
        """并行创建节点间链路"""
        print("\n" + "=" * 60)
        print("Step 5: Create inter-node links")
        print("=" * 60)

        total = len(self.topology['links'])
        print(f"Creating {total} links (parallel: {self.max_workers} workers)...")

        completed = 0
        failed = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self.create_single_link, link): link
                      for link in self.topology['links']}

            for future in as_completed(futures):
                try:
                    future.result()
                    completed += 1

                    if completed % 200 == 0 or completed == total:
                        percent = (completed / total) * 100
                        print(f"  Progress: {completed}/{total} ({percent:.1f}%)")

                except Exception as e:
                    failed += 1
                    link = futures[future]
                    print(f"  ✗ Error creating link {link['node1']}-{link['node2']}: {e}")

        print(f"✓ Created {completed}/{total} links" + (f" ({failed} failed)" if failed else ""))

    def wait_for_isis_convergence(self):
        """等待ISIS路由收敛"""
        print("\n" + "=" * 60)
        print("Step 6: Wait for ISIS convergence")
        print("=" * 60)

        total_nodes = len(self.topology['nodes'])
        # 4000节点需要10-15分钟收敛，上限设为900秒
        wait_time = min(900, 60 + total_nodes // 4)

        print(f"Waiting {wait_time} seconds for ISIS to converge...")
        print(f"(Large network: {total_nodes} nodes, estimated 10-15 minutes)")

        for i in range(wait_time):
            if i % 30 == 0:
                print(f"  {i}/{wait_time}s elapsed", flush=True)
            time.sleep(1)

        print(f"\n✓ ISIS convergence wait completed ({wait_time}s)")

    def verify_deployment(self):
        """验证部署，采样覆盖首环、中间环、末环"""
        print("\n" + "=" * 60)
        print("Step 7: Verify deployment")
        print("=" * 60)

        nodes = self.topology['nodes']
        nodes_per_ring = self.topology.get('nodes_per_ring', 50)

        # 采样节点：首环首节点、首环末节点、第二环首节点、中间节点、末节点
        sample_indices = [
            0,                          # r0n0  - 首环首节点
            nodes_per_ring - 1,         # r0n49 - 首环末节点
            nodes_per_ring,             # r1n0  - 第二环首节点
            len(nodes) // 2,            # 中间节点
            len(nodes) - 1              # r79n49 - 最后节点
        ]

        sample_nodes = [nodes[i]['name'] for i in sample_indices if i < len(nodes)]
        print(f"\nSampling {len(sample_nodes)} nodes for verification...")

        for node_name in sample_nodes:
            print(f"\n--- Node: {node_name} ---")

            # 检查ISIS邻居数量
            result = self.docker_exec(
                node_name,
                "vtysh -c 'show isis neighbor' | grep -c Up",
                quiet=True
            )
            if result.stdout.strip():
                print(f"  ISIS neighbors (Up): {result.stdout.strip()}")

            # 检查IPv6路由数量
            result = self.docker_exec(node_name, "ip -6 route show | wc -l", quiet=True)
            if result.stdout.strip():
                print(f"  IPv6 routes: {result.stdout.strip()}")

            # 检查SRv6 locator
            result = self.docker_exec(
                node_name,
                "ip -6 route show table local | grep seg6local | wc -l",
                quiet=True
            )
            if result.stdout.strip():
                print(f"  SRv6 seg6local entries: {result.stdout.strip()}")

    def print_summary(self):
        """打印部署摘要"""
        print("\n" + "=" * 60)
        print("DEPLOYMENT COMPLETED")
        print("=" * 60)

        nodes = self.topology['nodes']
        print(f"\nTopology: {len(nodes)} nodes, {len(self.topology['links'])} links")
        print(f"Networks: {self.num_networks} × bridge ({len(nodes)//self.num_networks} containers each)")
        print(f"Routing: ISIS")

        node1 = nodes[0]['name']
        node_mid = nodes[len(nodes) // 2]['name']
        node_last = nodes[-1]['name']

        print("\n" + "-" * 60)
        print("Quick verification commands:")
        print("-" * 60)
        print(f"""
# 查看样本节点ISIS邻居
docker exec srv6-{node1} vtysh -c 'show isis neighbor'
docker exec srv6-{node1} ip -6 route show | head -20

# 测试环内连通性（r0n0 -> r0n25，跨25跳）
docker exec srv6-{node1} ping6 -c 3 fc00:0000:0019::1

# 测试环间连通性（r0n0 -> r40n0，跨40个环）
docker exec srv6-{node1} ping6 -c 3 fc00:0028:0000::1

# 测试对角连通性（r0n0 -> r79n49）
docker exec srv6-{node1} ping6 -c 3 {nodes[-1]['srv6_locator'].split('/')[0]}
""")

        print("-" * 60)
        print("Next step - Configure SRv6 End behavior:")
        print("-" * 60)
        print("  chmod +x configure_srv6.sh && ./configure_srv6.sh")

        print("\n" + "-" * 60)
        print("Cleanup:")
        print("-" * 60)
        print(f"  python3 deploy.py --cleanup topology_4000nodes.json")

    def deploy(self):
        """执行完整部署"""
        print("\n" + "=" * 60)
        print("SRv6+FRR LARGE-SCALE DEPLOYMENT (4000 NODES)")
        print("=" * 60)
        print(f"Nodes: {len(self.topology['nodes'])}")
        print(f"Links: {len(self.topology['links'])}")
        print(f"Parallel workers: {self.max_workers}")
        print(f"Estimated time: 80-120 minutes")

        start_time = time.time()

        try:
            self.cleanup()
            self.create_network()
            self.start_containers()
            self.prefetch_all_pids()   # 批量预取PID，加速link创建
            self.create_links()
            self.wait_for_isis_convergence()
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
        description='Deploy large-scale SRv6+FRR network (4000 nodes)'
    )
    parser.add_argument(
        'topology',
        nargs='?',
        default='topology_4000nodes.json',
        help='Topology file (default: topology_4000nodes.json)'
    )
    parser.add_argument(
        '--cleanup',
        action='store_true',
        help='Only cleanup existing deployment'
    )
    parser.add_argument(
        '--config-dir',
        default='configs',
        help='Configuration directory (default: configs)'
    )
    parser.add_argument(
        '--workers',
        type=int,
        default=30,
        help='Number of parallel workers (default: 30)'
    )

    args = parser.parse_args()

    if not os.path.exists(args.topology):
        print(f"Error: Topology file '{args.topology}' not found!")
        sys.exit(1)

    deployer = LargeScaleSRv6Deployer(args.topology, args.config_dir)
    deployer.max_workers = args.workers

    if args.cleanup:
        deployer.cleanup()
        print("✓ Cleanup completed")
        sys.exit(0)

    if not deployer.config_dir.exists():
        print(f"Error: Configuration directory '{deployer.config_dir}' not found!")
        print("\nPlease generate configurations first:")
        print(f"  python3 generate_configs.py {args.topology}")
        sys.exit(1)

    success = deployer.deploy()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
     main()






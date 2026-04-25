#!/usr/bin/env python3
"""
SRv6+FRR部署Orchestrator - 优化版
支持大规模部署（400+节点）
优化：并行化、进度显示、资源管理
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
        self.network_name = self.topology.get('network_name', 'srv6-net')
        self.image_name = 'frr-srv6-node:latest'
        self.max_workers = 10  # 并行工作线程数
    
    def run_cmd(self, cmd, check=True, quiet=False):
        """执行shell命令"""
        if not quiet:
            print(f"  $ {cmd[:100]}...")  # 只显示前100个字符
        
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
            f"sudo docker exec srv6-{node} {cmd}",
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
        
        # 批量删除容器
        node_names = [f"srv6-{node['name']}" for node in self.topology['nodes']]
        batch_size = 50
        
        for i in range(0, len(node_names), batch_size):
            batch = node_names[i:i+batch_size]
            cmd = f"sudo docker rm -f {' '.join(batch)}"
            self.run_cmd(cmd, check=False, quiet=True)
            
            if (i + batch_size) % 100 == 0 or (i + batch_size) >= len(node_names):
                print(f"  Progress: {min(i + batch_size, len(node_names))}/{len(node_names)}")
        
        # 删除网络
        self.run_cmd(f"sudo docker network rm {self.network_name}", check=False, quiet=True)
        
        # 批量删除veth接口
        print(f"Removing {len(self.topology['links'])} veth pairs...")
        for i, link in enumerate(self.topology['links']):
            if i % 100 == 0:
                print(f"  Progress: {i}/{len(self.topology['links'])}")
            self.run_cmd(
                f"sudo ip link delete {link['node1']}-{link['node2']}",
                check=False,
                quiet=True
            )
        
        print("✓ Cleanup completed")
    
    def create_network(self):
        """创建Docker网络"""
        print("\n" + "=" * 60)
        print("Step 2: Create Docker network")
        print("=" * 60)
        
        self.run_cmd(f"sudo docker network create {self.network_name}", quiet=True)
        print(f"✓ Network '{self.network_name}' created")
    
    def start_single_container(self, node):
        """启动单个容器（用于并行化）"""
        node_name = node['name']
        srv6_locator = node['srv6_locator']
        
        frr_conf = self.config_dir / f"frr-{node_name}.conf"
        daemons_conf = self.config_dir / "daemons"
        
        if not frr_conf.exists():
            raise FileNotFoundError(f"Configuration file {frr_conf} not found!")
        
        cmd = f"""
            sudo docker run -d \
              --name srv6-{node_name} \
              --hostname {node_name} \
              --network {self.network_name} \
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
                    node_name = future.result()
                    completed += 1
                    
                    if completed % 20 == 0 or completed == total:
                        percent = (completed / total) * 100
                        print(f"  Progress: {completed}/{total} ({percent:.1f}%)")
                
                except Exception as e:
                    node = futures[future]
                    print(f"  ✗ Error starting {node['name']}: {e}")
        
        print(f"✓ Started {completed}/{total} containers")
        print("Waiting for containers to initialize...")
        time.sleep(10)
    
    def create_single_link(self, link, idx, total):
        """创建单个链路（用于并行化）"""
        node1 = link['node1']
        node2 = link['node2']
        subnet = link['subnet']
        
        veth1 = f"{node1}-{node2}"
        veth2 = f"{node2}-{node1}"
        
        # 创建veth对
        self.run_cmd(f"sudo ip link add {veth1} type veth peer name {veth2}", quiet=True)
        
        # 获取容器PID
        pid1 = self.run_cmd(
            f"docker inspect -f '{{{{.State.Pid}}}}' srv6-{node1}",
            quiet=True
        ).stdout.strip()
        
        pid2 = self.run_cmd(
            f"docker inspect -f '{{{{.State.Pid}}}}' srv6-{node2}",
            quiet=True
        ).stdout.strip()
        
        # 移动到容器命名空间
        self.run_cmd(f"sudo ip link set {veth1} netns {pid1}", quiet=True)
        self.run_cmd(f"sudo ip link set {veth2} netns {pid2}", quiet=True)
        
        # 启用接口
        self.docker_exec(node1, f"ip link set {veth1} up", quiet=True)
        self.docker_exec(node2, f"ip link set {veth2} up", quiet=True)
        
        # 配置IPv6地址
        ip1 = subnet.replace('::/64', '::1/64')
        ip2 = subnet.replace('::/64', '::2/64')
        self.docker_exec(node1, f"ip -6 addr add {ip1} dev {veth1}", quiet=True)
        self.docker_exec(node2, f"ip -6 addr add {ip2} dev {veth2}", quiet=True)
        
        return idx
    
    def create_links(self):
        """并行创建节点间链路"""
        print("\n" + "=" * 60)
        print("Step 4: Create inter-node links")
        print("=" * 60)
        
        total = len(self.topology['links'])
        print(f"Creating {total} links (parallel: {self.max_workers} workers)...")
        
        completed = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self.create_single_link, link, idx, total): idx 
                      for idx, link in enumerate(self.topology['links'])}
            
            for future in as_completed(futures):
                try:
                    future.result()
                    completed += 1
                    
                    if completed % 50 == 0 or completed == total:
                        percent = (completed / total) * 100
                        print(f"  Progress: {completed}/{total} ({percent:.1f}%)")
                
                except Exception as e:
                    print(f"  ✗ Error creating link: {e}")
        
        print(f"✓ Created {completed}/{total} links")
    
    def wait_for_isis_convergence(self):
        """等待ISIS路由收敛"""
        print("\n" + "=" * 60)
        print("Step 5: Wait for ISIS convergence")
        print("=" * 60)
        
        total_nodes = len(self.topology['nodes'])
        # 400节点约需2-3分钟收敛
        wait_time = min(180, 30 + total_nodes // 2)
        
        print(f"Waiting {wait_time} seconds for ISIS to converge...")
        print(f"(Large network: {total_nodes} nodes)")
        
        for i in range(wait_time):
            if i % 10 == 0:
                print(f"  {i}/{wait_time}s", end="", flush=True)
            else:
                print(".", end="", flush=True)
            time.sleep(1)
        
        print(f"\n✓ ISIS should have converged")
    
    def verify_deployment(self):
        """验证部署"""
        print("\n" + "=" * 60)
        print("Step 6: Verify deployment")
        print("=" * 60)
        
        # 验证几个样本节点
        sample_nodes = [
            self.topology['nodes'][0]['name'],      # 第1个节点
            self.topology['nodes'][19]['name'],     # 第1个环的最后节点
            self.topology['nodes'][20]['name'],     # 第2个环的第1个节点
            self.topology['nodes'][-1]['name']      # 最后一个节点
        ]
        
        print(f"\nSampling {len(sample_nodes)} nodes for verification...")
        
        for node_name in sample_nodes:
            print(f"\n--- Node: {node_name} ---")
            
            # 检查ISIS邻居数量
            result = self.docker_exec(node_name, "vtysh -c 'show isis neighbor' | grep -c Up", quiet=True)
            if result.stdout:
                neighbor_count = result.stdout.strip()
                print(f"  ISIS neighbors: {neighbor_count}")
            
            # 检查IPv6路由数量
            result = self.docker_exec(node_name, "ip -6 route show | wc -l", quiet=True)
            if result.stdout:
                route_count = result.stdout.strip()
                print(f"  IPv6 routes: {route_count}")
    
    def print_summary(self):
        """打印部署摘要"""
        print("\n" + "=" * 60)
        print("DEPLOYMENT COMPLETED")
        print("=" * 60)
        
        print(f"\nTopology: {len(self.topology['nodes'])} nodes, {len(self.topology['links'])} links")
        print(f"Network: {self.network_name}")
        print(f"Routing: ISIS")
        
        node1 = self.topology['nodes'][0]['name']
        node_last = self.topology['nodes'][-1]['name']
        
        print("\n" + "-" * 60)
        print("Quick verification commands:")
        print("-" * 60)
        print(f"""
# Check sample node
sudo docker exec srv6-{node1} vtysh -c 'show isis neighbor'
sudo docker exec srv6-{node1} ip -6 route show | head -20

# Test connectivity
sudo docker exec srv6-{node1} ping6 -c 3 {self.topology['nodes'][-1]['srv6_locator'].split('/')[0]}

# Configure SRv6 (required)
./configure_srv6_bulk.sh
""")
        
        print("-" * 60)
        print("Cleanup:")
        print("-" * 60)
        print(f"python3 deploy.py --cleanup topology_400nodes.json")
    
    def deploy(self):
        """执行完整部署"""
        print("\n" + "=" * 60)
        print("SRv6+FRR LARGE-SCALE DEPLOYMENT")
        print("=" * 60)
        print(f"Nodes: {len(self.topology['nodes'])}")
        print(f"Links: {len(self.topology['links'])}")
        
        try:
            self.cleanup()
            self.create_network()
            self.start_containers()
            self.create_links()
            self.wait_for_isis_convergence()
            self.verify_deployment()
            self.print_summary()
            
            return True
        
        except Exception as e:
            print(f"\n✗ Deployment failed: {e}")
            import traceback
            traceback.print_exc()
            return False


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Deploy large-scale SRv6+FRR network'
    )
    parser.add_argument(
        'topology',
        nargs='?',
        default='topology_400nodes.json',
        help='Topology file (default: topology_400nodes.json)'
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
    
    args = parser.parse_args()
    
    if not os.path.exists(args.topology):
        print(f"Error: Topology file '{args.topology}' not found!")
        sys.exit(1)
    
    deployer = LargeScaleSRv6Deployer(args.topology, args.config_dir)
    
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
#!/usr/bin/env python3
"""
SRv6+FRR閮ㄧ讲Orchestrator - 4000鑺傜偣鐗?
鏀寔澶ц妯￠儴缃诧紙4000鑺傜偣锛?
浼樺寲锛氬苟琛屽寲銆佽繘搴︽樉绀恒€佽祫婧愮鐞嗐€佹壒閲廝ID棰勫彇
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
        self.nodes_by_name = {node['name']: node for node in self.topology['nodes']}
        self.base_network_name = self.topology.get('network_name', 'srv6-net')
        self.image_name = 'frr-srv6-node:latest'
        self.max_workers = 30  # 4000鑺傜偣闇€瑕佹洿楂樺苟琛屽害

        # 5涓嫭绔媙etwork锛屾瘡涓猙ridge鎵胯浇800涓鍣紝瑙勯伩bridge FDB 1024涓婇檺
        self.num_networks = 5
        self.network_names = [
            f"{self.base_network_name}-{i}" for i in range(self.num_networks)
        ]
        # 鎸塺ing鑼冨洿鍒掑垎network锛氭瘡涓猲etwork璐熻矗 80/5=16 涓猺ing
        nodes_per_ring = self.topology.get('nodes_per_ring', 50)
        num_rings = self.topology.get('num_rings', 80)
        rings_per_net = num_rings // self.num_networks  # 16
        # node_id -> network_name 鏄犲皠锛屾寜ring绱㈠紩鍒嗛厤
        self._node_network = {}
        for node in self.topology['nodes']:
            ring_idx = node['ring']
            net_idx = min(ring_idx // rings_per_net, self.num_networks - 1)
            self._node_network[node['name']] = self.network_names[net_idx]

        # 缂撳瓨瀹瑰櫒PID锛岄伩鍏峫ink鍒涘缓鏃堕噸澶峝ocker inspect
        self._pid_cache = {}

    def run_cmd(self, cmd, check=True, quiet=False):
        """鎵цshell鍛戒护"""
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
                print(f"  鉁?Error: {result.stderr}")
            raise RuntimeError(f"Command failed: {cmd}")

        return result

    def docker_exec(self, node, cmd, quiet=False):
        """鍦ㄥ鍣ㄤ腑鎵ц鍛戒护"""
        return self.run_cmd(
            f"docker exec srv6-{node} {cmd}",
            check=False,
            quiet=quiet
        )

    def cleanup(self):
        """Clean up old deployment."""
        print("\n" + "=" * 60)
        print("Step 1: Cleanup old deployment")
        print("=" * 60)

        total = len(self.topology['nodes'])
        print(f"Removing {total} containers...")

        # 鎵归噺鍒犻櫎瀹瑰櫒锛宐atch_size鎻愬崌涓?00鍑忓皯璋冪敤娆℃暟
        node_names = [f"srv6-{node['name']}" for node in self.topology['nodes']]
        batch_size = 100

        for i in range(0, len(node_names), batch_size):
            batch = node_names[i:i+batch_size]
            cmd = f"docker rm -f {' '.join(batch)}"
            self.run_cmd(cmd, check=False, quiet=True)

            done = min(i + batch_size, len(node_names))
            if done % 500 == 0 or done >= len(node_names):
                print(f"  Progress: {done}/{len(node_names)}")

        # 鍒犻櫎5涓猲etwork
        for net_name in self.network_names:
            self.run_cmd(f"docker network rm {net_name}", check=False, quiet=True)

        # 鎵归噺鍒犻櫎veth鎺ュ彛
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

        print("鉁?Cleanup completed")

    def create_network(self):
        """Create Docker bridge networks."""
        print("\n" + "=" * 60)
        print("Step 2: Create Docker networks (5 networks 脳 800 containers)")
        print("=" * 60)

        for net_name in self.network_names:
            self.run_cmd(f"docker network create {net_name}", quiet=True)
            print(f"鉁?Network '{net_name}' created")

    def start_single_container(self, node):
        """Start a single FRR container."""
        node_name = node['name']
        srv6_locator = node['srv6_locator']

        frr_conf = self.config_dir / f"frr-{node_name}.conf"
        daemons_conf = self.config_dir / "daemons"

        if not frr_conf.exists():
            raise FileNotFoundError(f"Configuration file {frr_conf} not found!")

        # 鎸塺ing鑼冨洿閫夋嫨瀵瑰簲network锛屾瘡涓猙ridge鍙寕800涓鍣?
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
        """Start all FRR containers in parallel."""
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
                    print(f"  鉁?Error starting {node['name']}: {e}")

        print(f"鉁?Started {completed}/{total} containers")
        print("Waiting for containers to initialize...")
        time.sleep(15)

    def prefetch_all_pids(self):
        """
        鎵归噺棰勫彇鎵€鏈夊鍣≒ID锛岄伩鍏峫ink鍒涘缓鏃堕€愪釜docker inspect銆?
        4000鑺傜偣 脳 2娆nspect/link = 澶ч噺閲嶅璋冪敤锛岄鍙栧悗鐩存帴鏌ュ瓧鍏搞€?
        """
        print("\n" + "=" * 60)
        print("Step 4: Pre-fetching container PIDs")
        print("=" * 60)

        total = len(self.topology['nodes'])
        print(f"Fetching PIDs for {total} containers...")

        # 浣跨敤Go妯℃澘涓€娆℃€ф壒閲忚幏鍙栵紝姣忔壒200涓噺灏戝懡浠ゆ暟閲?
        node_names = [node['name'] for node in self.topology['nodes']]
        batch_size = 200
        fetched = 0

        for i in range(0, len(node_names), batch_size):
            batch = node_names[i:i+batch_size]

            # 骞惰鑾峰彇杩欐壒鑺傜偣鐨凱ID
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

        print(f"鉁?Cached {len(self._pid_cache)} PIDs")

    def create_single_link(self, link):
        """鍒涘缓鍗曚釜閾捐矾锛屼娇鐢ㄩ缂撳瓨鐨凱ID"""
        node1 = link['node1']
        node2 = link['node2']
        subnet = link['subnet']

        veth1 = f"{node1}-{node2}"
        veth2 = f"{node2}-{node1}"

        # 鍒涘缓veth瀵?
        self.run_cmd(f"ip link add {veth1} type veth peer name {veth2}", quiet=True)

        # 浠庣紦瀛樿幏鍙朠ID锛岄伩鍏嶉噸澶峝ocker inspect
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

        # 绉诲姩鍒板鍣ㄥ懡鍚嶇┖闂?
        self.run_cmd(f"ip link set {veth1} netns {pid1}", quiet=True)
        self.run_cmd(f"ip link set {veth2} netns {pid2}", quiet=True)

        # 鍚敤鎺ュ彛
        self.docker_exec(node1, f"ip link set {veth1} up", quiet=True)
        self.docker_exec(node2, f"ip link set {veth2} up", quiet=True)

        # 閰嶇疆IPv6鍦板潃
        ip1 = subnet.replace('::/64', '::1/64')
        ip2 = subnet.replace('::/64', '::2/64')
        self.docker_exec(node1, f"ip -6 addr add {ip1} dev {veth1}", quiet=True)
        self.docker_exec(node2, f"ip -6 addr add {ip2} dev {veth2}", quiet=True)

    def _group_links_by_area(self):
        """
        鎸?area 缁勭粐閾捐矾鍒涘缓椤哄簭锛?        - 姣忎釜 area 鍏堝缓绔嬬幆鍐呴摼璺?        - 鍐嶅缓绔嬭 area 鍐呴儴鐨勭幆闂撮摼璺?        - 鏈€鍚庣粺涓€寤虹珛璺?area 閾捐矾
        """
        area_ranges = self.topology.get('area_ranges', [])
        area_links = {
            area['area_id']: {
                'intra-ring': [],
                'inter-ring': [],
            }
            for area in area_ranges
        }
        cross_area_links = []

        for link in self.topology['links']:
            node1 = self.nodes_by_name[link['node1']]
            node2 = self.nodes_by_name[link['node2']]

            if node1['area_id'] != node2['area_id']:
                cross_area_links.append(link)
                continue

            area_links[node1['area_id']][link['type']].append(link)

        batches = []
        for area in area_ranges:
            area_id = area['area_id']
            intra_ring_links = area_links[area_id]['intra-ring']
            inter_ring_links = area_links[area_id]['inter-ring']

            if intra_ring_links:
                batches.append((f"Area {area_id} intra-ring links", intra_ring_links))
            if inter_ring_links:
                batches.append((f"Area {area_id} inter-ring links", inter_ring_links))

        if cross_area_links:
            batches.append(("Cross-area backbone links", cross_area_links))

        return batches

    def _create_link_batch(self, batch_name, links, completed_so_far, total_links):
        """Create one link batch in parallel."""
        print(f"\n  -> {batch_name}: {len(links)} links")

        batch_completed = 0
        batch_failed = 0

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self.create_single_link, link): link for link in links}

            for future in as_completed(futures):
                try:
                    future.result()
                    batch_completed += 1

                    global_completed = completed_so_far + batch_completed
                    if batch_completed % 200 == 0 or batch_completed == len(links):
                        percent = (global_completed / total_links) * 100
                        print(
                            f"     Progress: {global_completed}/{total_links} "
                            f"({percent:.1f}%)"
                        )

                except Exception as e:
                    batch_failed += 1
                    link = futures[future]
                    print(f"     閴?Error creating link {link['node1']}-{link['node2']}: {e}")

        print(
            f"     Batch done: {batch_completed}/{len(links)}"
            + (f" ({batch_failed} failed)" if batch_failed else "")
        )
        return batch_completed, batch_failed

    def create_links(self):
        """Create inter-node links in ordered batches."""
        print("\n" + "=" * 60)
        print("Step 5: Create inter-node links")
        print("=" * 60)

        total = len(self.topology['links'])
        print(f"Creating {total} links (parallel: {self.max_workers} workers)...")

        print("Order: Area 1 -> Area 2 -> Area 3 -> Area 4 -> cross-area backbone")

        completed = 0
        failed = 0
        for batch_name, links in self._group_links_by_area():
            batch_completed, batch_failed = self._create_link_batch(
                batch_name, links, completed, total
            )
            completed += batch_completed
            failed += batch_failed

        print(f"鉁?Created {completed}/{total} links" + (f" ({failed} failed)" if failed else ""))

    def wait_for_isis_convergence(self):
        """绛夊緟ISIS璺敱鏀舵暃"""
        print("\n" + "=" * 60)
        print("Step 6: Wait for ISIS convergence")
        print("=" * 60)

        total_nodes = len(self.topology['nodes'])
        # 4000鑺傜偣闇€瑕?0-15鍒嗛挓鏀舵暃锛屼笂闄愯涓?00绉?
        wait_time = min(900, 60 + total_nodes // 4)

        print(f"Waiting {wait_time} seconds for ISIS to converge...")
        print(f"(Large network: {total_nodes} nodes, estimated 10-15 minutes)")

        for i in range(wait_time):
            if i % 30 == 0:
                print(f"  {i}/{wait_time}s elapsed", flush=True)
            time.sleep(1)

        print(f"\n鉁?ISIS convergence wait completed ({wait_time}s)")

    def verify_deployment(self):
        """Verify deployment with sampled nodes."""
        print("\n" + "=" * 60)
        print("Step 7: Verify deployment")
        print("=" * 60)

        nodes = self.topology['nodes']
        nodes_per_ring = self.topology.get('nodes_per_ring', 50)

        # 閲囨牱鑺傜偣锛氶鐜鑺傜偣銆侀鐜湯鑺傜偣銆佺浜岀幆棣栬妭鐐广€佷腑闂磋妭鐐广€佹湯鑺傜偣
        sample_indices = [
            0,                          # r0n0  - 棣栫幆棣栬妭鐐?
            nodes_per_ring - 1,         # r0n49 - 棣栫幆鏈妭鐐?
            nodes_per_ring,             # r1n0  - 绗簩鐜鑺傜偣
            len(nodes) // 2,            # 涓棿鑺傜偣
            len(nodes) - 1              # r79n49 - 鏈€鍚庤妭鐐?
        ]

        sample_nodes = [nodes[i]['name'] for i in sample_indices if i < len(nodes)]
        print(f"\nSampling {len(sample_nodes)} nodes for verification...")

        for node_name in sample_nodes:
            print(f"\n--- Node: {node_name} ---")

            # 妫€鏌SIS閭诲眳鏁伴噺
            result = self.docker_exec(
                node_name,
                "vtysh -c 'show isis neighbor' | grep -c Up",
                quiet=True
            )
            if result.stdout.strip():
                print(f"  ISIS neighbors (Up): {result.stdout.strip()}")

            # 妫€鏌Pv6璺敱鏁伴噺
            result = self.docker_exec(node_name, "ip -6 route show | wc -l", quiet=True)
            if result.stdout.strip():
                print(f"  IPv6 routes: {result.stdout.strip()}")

            # 妫€鏌Rv6 locator
            result = self.docker_exec(
                node_name,
                "ip -6 route show table local | grep seg6local | wc -l",
                quiet=True
            )
            if result.stdout.strip():
                print(f"  SRv6 seg6local entries: {result.stdout.strip()}")

    def print_summary(self):
        """鎵撳嵃閮ㄧ讲鎽樿"""
        print("\n" + "=" * 60)
        print("DEPLOYMENT COMPLETED")
        print("=" * 60)

        nodes = self.topology['nodes']
        print(f"\nTopology: {len(nodes)} nodes, {len(self.topology['links'])} links")
        print(f"Networks: {self.num_networks} 脳 bridge ({len(nodes)//self.num_networks} containers each)")
        print(f"Routing: ISIS")

        node1 = nodes[0]['name']
        node_mid = nodes[len(nodes) // 2]['name']
        node_last = nodes[-1]['name']

        print("\n" + "-" * 60)
        print("Quick verification commands:")
        print("-" * 60)
        print(f"""
# 鏌ョ湅鏍锋湰鑺傜偣ISIS閭诲眳
docker exec srv6-{node1} vtysh -c 'show isis neighbor'
docker exec srv6-{node1} ip -6 route show | head -20

# 娴嬭瘯鐜唴杩為€氭€э紙r0n0 -> r0n25锛岃法25璺筹級
docker exec srv6-{node1} ping6 -c 3 fc00:0000:0019::1

# 娴嬭瘯鐜棿杩為€氭€э紙r0n0 -> r40n0锛岃法40涓幆锛?
docker exec srv6-{node1} ping6 -c 3 fc00:0028:0000::1

# 娴嬭瘯瀵硅杩為€氭€э紙r0n0 -> r79n49锛?
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
        """鎵ц瀹屾暣閮ㄧ讲"""
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
            self.prefetch_all_pids()   # 鎵归噺棰勫彇PID锛屽姞閫焞ink鍒涘缓
            self.create_links()
            self.wait_for_isis_convergence()
            self.verify_deployment()
            self.print_summary()

            elapsed = int(time.time() - start_time)
            print(f"\n鉁?Total deployment time: {elapsed // 60}m {elapsed % 60}s")
            return True

        except Exception as e:
            print(f"\n鉁?Deployment failed: {e}")
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
        print("鉁?Cleanup completed")
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








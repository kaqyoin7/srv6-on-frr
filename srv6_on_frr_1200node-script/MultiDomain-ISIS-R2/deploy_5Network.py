#!/usr/bin/env python3
"""
SRv6+FRR deployment orchestrator.

Features:
- parallel container startup
- batched PID prefetch
- ordered link creation by area
- progress reporting
"""

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


class LargeScaleSRv6Deployer:
    def __init__(self, topology_file, config_dir="configs"):
        with open(topology_file, "r") as f:
            self.topology = json.load(f)

        self.config_dir = Path(config_dir)
        self.nodes_by_name = {node["name"]: node for node in self.topology["nodes"]}
        self.base_network_name = self.topology.get("network_name", "srv6-net")
        self.image_name = "frr-srv6-node:latest"
        self.max_workers = 30

        # Use multiple Docker bridge networks to spread container load.
        self.num_networks = 5
        self.network_names = [
            f"{self.base_network_name}-{i}" for i in range(self.num_networks)
        ]

        # Assign nodes to Docker bridges by ring range.
        num_rings = self.topology.get("num_rings", 80)
        rings_per_net = max(1, num_rings // self.num_networks)
        self._node_network = {}
        for node in self.topology["nodes"]:
            ring_idx = node["ring"]
            net_idx = min(ring_idx // rings_per_net, self.num_networks - 1)
            self._node_network[node["name"]] = self.network_names[net_idx]

        # Cache container PIDs to avoid repeated docker inspect calls.
        self._pid_cache = {}

    def run_cmd(self, cmd, check=True, quiet=False):
        """Run a shell command."""
        if not quiet:
            print(f"  $ {cmd[:100]}...")

        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
        )

        if check and result.returncode != 0:
            if not quiet:
                print(f"  Error: {result.stderr}")
            raise RuntimeError(f"Command failed: {cmd}")

        return result

    def docker_exec(self, node, cmd, quiet=False):
        """Run a command inside a container."""
        return self.run_cmd(
            f"docker exec srv6-{node} {cmd}",
            check=False,
            quiet=quiet,
        )

    def cleanup(self):
        """Clean up old deployment."""
        print("\n" + "=" * 60)
        print("Step 1: Cleanup old deployment")
        print("=" * 60)

        total = len(self.topology["nodes"])
        print(f"Removing {total} containers...")

        # Remove containers in batches to reduce command overhead.
        node_names = [f"srv6-{node['name']}" for node in self.topology["nodes"]]
        batch_size = 100

        for i in range(0, len(node_names), batch_size):
            batch = node_names[i : i + batch_size]
            cmd = f"docker rm -f {' '.join(batch)}"
            self.run_cmd(cmd, check=False, quiet=True)

            done = min(i + batch_size, len(node_names))
            if done % 500 == 0 or done >= len(node_names):
                print(f"  Progress: {done}/{len(node_names)}")

        # Remove Docker bridge networks.
        for net_name in self.network_names:
            self.run_cmd(f"docker network rm {net_name}", check=False, quiet=True)

        # Remove veth pairs.
        total_links = len(self.topology["links"])
        print(f"Removing {total_links} veth pairs...")
        for i, link in enumerate(self.topology["links"]):
            if i % 500 == 0:
                print(f"  Progress: {i}/{total_links}")
            self.run_cmd(
                f"ip link delete {link['node1']}-{link['node2']}",
                check=False,
                quiet=True,
            )

        print("Cleanup completed")

    def create_network(self):
        """Create Docker bridge networks."""
        print("\n" + "=" * 60)
        print("Step 2: Create Docker networks (5 networks x 800 containers)")
        print("=" * 60)

        for net_name in self.network_names:
            self.run_cmd(f"docker network create {net_name}", quiet=True)
            print(f"Network '{net_name}' created")

    def start_single_container(self, node):
        """Start a single FRR container."""
        node_name = node["name"]
        srv6_locator = node["srv6_locator"]

        frr_conf = self.config_dir / f"frr-{node_name}.conf"
        daemons_conf = self.config_dir / "daemons"

        if not frr_conf.exists():
            raise FileNotFoundError(f"Configuration file {frr_conf} not found!")

        # Select the Docker bridge for this node by ring range.
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

        total = len(self.topology["nodes"])
        print(f"Starting {total} containers (parallel: {self.max_workers} workers)...")

        completed = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self.start_single_container, node): node
                for node in self.topology["nodes"]
            }

            for future in as_completed(futures):
                try:
                    future.result()
                    completed += 1

                    if completed % 100 == 0 or completed == total:
                        percent = (completed / total) * 100
                        print(f"  Progress: {completed}/{total} ({percent:.1f}%)")

                except Exception as e:
                    node = futures[future]
                    print(f"  Error starting {node['name']}: {e}")

        print(f"Started {completed}/{total} containers")
        print("Waiting for containers to initialize...")
        time.sleep(15)

    def prefetch_all_pids(self):
        """
        Pre-fetch all container PIDs.

        This avoids repeated docker inspect calls during link creation.
        """
        print("\n" + "=" * 60)
        print("Step 4: Pre-fetching container PIDs")
        print("=" * 60)

        total = len(self.topology["nodes"])
        print(f"Fetching PIDs for {total} containers...")

        # Fetch PIDs in batches to reduce command count.
        node_names = [node["name"] for node in self.topology["nodes"]]
        batch_size = 200
        fetched = 0

        for i in range(0, len(node_names), batch_size):
            batch = node_names[i : i + batch_size]

            def fetch_pid(name):
                result = self.run_cmd(
                    f"docker inspect -f '{{{{.State.Pid}}}}' srv6-{name}",
                    quiet=True,
                )
                return name, result.stdout.strip()

            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                for name, pid in executor.map(fetch_pid, batch):
                    if pid:
                        self._pid_cache[name] = pid
                        fetched += 1

            if fetched % 500 == 0 or fetched >= total:
                print(f"  Progress: {fetched}/{total}")

        print(f"Cached {len(self._pid_cache)} PIDs")

    def create_single_link(self, link):
        """Create a single veth link using cached container PIDs."""
        node1 = link["node1"]
        node2 = link["node2"]
        subnet = link["subnet"]

        veth1 = f"{node1}-{node2}"
        veth2 = f"{node2}-{node1}"

        # Create the veth pair.
        self.run_cmd(f"ip link add {veth1} type veth peer name {veth2}", quiet=True)

        # Use cached PIDs when available.
        pid1 = self._pid_cache.get(node1)
        pid2 = self._pid_cache.get(node2)

        if not pid1:
            pid1 = self.run_cmd(
                f"docker inspect -f '{{{{.State.Pid}}}}' srv6-{node1}",
                quiet=True,
            ).stdout.strip()

        if not pid2:
            pid2 = self.run_cmd(
                f"docker inspect -f '{{{{.State.Pid}}}}' srv6-{node2}",
                quiet=True,
            ).stdout.strip()

        # Move each veth end into the target container namespace.
        self.run_cmd(f"ip link set {veth1} netns {pid1}", quiet=True)
        self.run_cmd(f"ip link set {veth2} netns {pid2}", quiet=True)

        # Bring the interfaces up.
        self.docker_exec(node1, f"ip link set {veth1} up", quiet=True)
        self.docker_exec(node2, f"ip link set {veth2} up", quiet=True)

        # Configure IPv6 addresses.
        ip1 = subnet.replace("::/64", "::1/64")
        ip2 = subnet.replace("::/64", "::2/64")
        self.docker_exec(node1, f"ip -6 addr add {ip1} dev {veth1}", quiet=True)
        self.docker_exec(node2, f"ip -6 addr add {ip2} dev {veth2}", quiet=True)

    def _group_links_by_area(self):
        """
        Group links by deployment order:
        - create intra-ring links for one area first
        - then create that area's inter-ring links
        - after all areas, create cross-area backbone links
        """
        area_ranges = self.topology.get("area_ranges", [])
        area_links = {
            area["area_id"]: {
                "intra-ring": [],
                "inter-ring": [],
            }
            for area in area_ranges
        }
        cross_area_links = []

        for link in self.topology["links"]:
            node1 = self.nodes_by_name[link["node1"]]
            node2 = self.nodes_by_name[link["node2"]]

            if node1["area_id"] != node2["area_id"]:
                cross_area_links.append(link)
                continue

            area_links[node1["area_id"]][link["type"]].append(link)

        batches = []
        for area in area_ranges:
            area_id = area["area_id"]
            intra_ring_links = area_links[area_id]["intra-ring"]
            inter_ring_links = area_links[area_id]["inter-ring"]

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
            futures = {
                executor.submit(self.create_single_link, link): link for link in links
            }

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
                    print(f"     Error creating link {link['node1']}-{link['node2']}: {e}")

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

        total = len(self.topology["links"])
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

        print(f"Created {completed}/{total} links" + (f" ({failed} failed)" if failed else ""))

    def wait_for_isis_convergence(self):
        """Wait for IS-IS convergence."""
        print("\n" + "=" * 60)
        print("Step 6: Wait for ISIS convergence")
        print("=" * 60)

        total_nodes = len(self.topology["nodes"])
        # For a large topology, convergence may take several minutes.
        wait_time = min(900, 60 + total_nodes // 4)

        print(f"Waiting {wait_time} seconds for ISIS to converge...")
        print(f"(Large network: {total_nodes} nodes, estimated 10-15 minutes)")

        for i in range(wait_time):
            if i % 30 == 0:
                print(f"  {i}/{wait_time}s elapsed", flush=True)
            time.sleep(1)

        print(f"\nISIS convergence wait completed ({wait_time}s)")

    def verify_deployment(self):
        """Verify deployment with sampled nodes."""
        print("\n" + "=" * 60)
        print("Step 7: Verify deployment")
        print("=" * 60)

        nodes = self.topology["nodes"]
        nodes_per_ring = self.topology.get("nodes_per_ring", 50)

        # Sample nodes from the start, middle, and end of the topology.
        sample_indices = [
            0,
            nodes_per_ring - 1,
            nodes_per_ring,
            len(nodes) // 2,
            len(nodes) - 1,
        ]

        sample_nodes = [nodes[i]["name"] for i in sample_indices if i < len(nodes)]
        print(f"\nSampling {len(sample_nodes)} nodes for verification...")

        for node_name in sample_nodes:
            print(f"\n--- Node: {node_name} ---")

            # Check the number of Up IS-IS neighbors.
            result = self.docker_exec(
                node_name,
                "vtysh -c 'show isis neighbor' | grep -c Up",
                quiet=True,
            )
            if result.stdout.strip():
                print(f"  ISIS neighbors (Up): {result.stdout.strip()}")

            # Check IPv6 route count.
            result = self.docker_exec(node_name, "ip -6 route show | wc -l", quiet=True)
            if result.stdout.strip():
                print(f"  IPv6 routes: {result.stdout.strip()}")

            # Check SRv6 seg6local entries.
            result = self.docker_exec(
                node_name,
                "ip -6 route show table local | grep seg6local | wc -l",
                quiet=True,
            )
            if result.stdout.strip():
                print(f"  SRv6 seg6local entries: {result.stdout.strip()}")

    def print_summary(self):
        """Print a deployment summary."""
        print("\n" + "=" * 60)
        print("DEPLOYMENT COMPLETED")
        print("=" * 60)

        nodes = self.topology["nodes"]
        print(f"\nTopology: {len(nodes)} nodes, {len(self.topology['links'])} links")
        print(
            f"Networks: {self.num_networks} x bridge "
            f"({len(nodes) // self.num_networks} containers each)"
        )
        print("Routing: ISIS")

        node1 = nodes[0]["name"]

        print("\n" + "-" * 60)
        print("Quick verification commands:")
        print("-" * 60)
        print(
            f"""
# Check IS-IS neighbors on a sample node
docker exec srv6-{node1} vtysh -c 'show isis neighbor'
docker exec srv6-{node1} ip -6 route show | head -20

# Test connectivity within a ring (r0n0 -> r0n25)
docker exec srv6-{node1} ping6 -c 3 fc00:0000:0019::1

# Test connectivity across rings (r0n0 -> r40n0)
docker exec srv6-{node1} ping6 -c 3 fc00:0028:0000::1

# Test long-distance end-to-end connectivity
docker exec srv6-{node1} ping6 -c 3 {nodes[-1]['srv6_locator'].split('/')[0]}
"""
        )

        print("-" * 60)
        print("Next step - Configure SRv6 End behavior:")
        print("-" * 60)
        print("  chmod +x configure_srv6.sh && ./configure_srv6.sh")

        print("\n" + "-" * 60)
        print("Cleanup:")
        print("-" * 60)
        print("  python3 deploy.py --cleanup topology_4000nodes.json")

    def deploy(self):
        """Run the full deployment workflow."""
        print("\n" + "=" * 60)
        print("SRv6+FRR LARGE-SCALE DEPLOYMENT (4000 NODES)")
        print("=" * 60)
        print(f"Nodes: {len(self.topology['nodes'])}")
        print(f"Links: {len(self.topology['links'])}")
        print(f"Parallel workers: {self.max_workers}")
        print("Estimated time: 80-120 minutes")

        start_time = time.time()

        try:
            self.cleanup()
            self.create_network()
            self.start_containers()
            self.prefetch_all_pids()
            self.create_links()
            self.wait_for_isis_convergence()
            self.verify_deployment()
            self.print_summary()

            elapsed = int(time.time() - start_time)
            print(f"\nTotal deployment time: {elapsed // 60}m {elapsed % 60}s")
            return True

        except Exception as e:
            print(f"\nDeployment failed: {e}")
            import traceback

            traceback.print_exc()
            return False


def main():
    parser = argparse.ArgumentParser(
        description="Deploy large-scale SRv6+FRR network (4000 nodes)"
    )
    parser.add_argument(
        "topology",
        nargs="?",
        default="topology_4000nodes.json",
        help="Topology file (default: topology_4000nodes.json)",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Only cleanup existing deployment",
    )
    parser.add_argument(
        "--config-dir",
        default="configs",
        help="Configuration directory (default: configs)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=30,
        help="Number of parallel workers (default: 30)",
    )

    args = parser.parse_args()

    if not os.path.exists(args.topology):
        print(f"Error: Topology file '{args.topology}' not found!")
        sys.exit(1)

    deployer = LargeScaleSRv6Deployer(args.topology, args.config_dir)
    deployer.max_workers = args.workers

    if args.cleanup:
        deployer.cleanup()
        print("Cleanup completed")
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

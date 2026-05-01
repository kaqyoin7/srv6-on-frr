#!/usr/bin/env python3
"""
SRv6+FRR deployment orchestrator for HOST 1 (rings 0-29).

Cross-host boundary handling (ring29 <-> ring30):
  - r29nx containers are started and configured normally.
  - For each cross-host link:
      * veth pair  r29nx-r30nx / r30nx-r29nx  is created on the HOST hypervisor.
      * The r29nx-r30nx end is moved into the srv6-r29nx container namespace
        and assigned its IPv6 address.
      * The r30nx-r29nx end is left on the hypervisor (no container move),
        to be bridged by your existing cross-host mechanism.
  - No containers are created for ring30 nodes on this host.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


class Host1SRv6Deployer:
    def __init__(self, topology_file, config_dir="configs"):
        with open(topology_file, "r") as f:
            self.topology = json.load(f)

        self.config_dir   = Path(config_dir)
        self.nodes_by_name = {node["name"]: node for node in self.topology["nodes"]}
        self.base_network_name = self.topology.get("network_name", "srv6-host1-net")
        self.image_name   = "frr-srv6-node:latest"
        self.max_workers  = 30

        # Cross-host boundary metadata
        cross = self.topology.get("cross_host_boundary", {})
        self.cross_host_local_ring  = cross.get("local_ring",  29)   # ring29 on host1
        self.cross_host_remote_ring = cross.get("remote_ring", 30)   # ring30 on host2

        # Separate cross-host links from normal links
        self.normal_links     = [l for l in self.topology["links"]
                                  if l["type"] != "cross-host-inter-ring"]
        self.cross_host_links = [l for l in self.topology["links"]
                                  if l["type"] == "cross-host-inter-ring"]

        # Docker bridge networks: spread containers across 5 bridges
        self.num_networks   = 5
        self.network_names  = [f"{self.base_network_name}-{i}" for i in range(self.num_networks)]
        num_rings           = self.topology.get("num_rings", 30)
        rings_per_net       = max(1, num_rings // self.num_networks)
        self._node_network  = {}
        for node in self.topology["nodes"]:
            ring_idx  = node["ring"]
            net_idx   = min(ring_idx // rings_per_net, self.num_networks - 1)
            self._node_network[node["name"]] = self.network_names[net_idx]

        self._pid_cache = {}

    # ------------------------------------------------------------------
    # Shell / Docker helpers
    # ------------------------------------------------------------------

    def run_cmd(self, cmd, check=True, quiet=False):
        if not quiet:
            print(f"  $ {cmd[:120]}...")
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if check and result.returncode != 0:
            if not quiet:
                print(f"  Error: {result.stderr.strip()}")
            raise RuntimeError(f"Command failed: {cmd}")
        return result

    def docker_exec(self, node, cmd, quiet=False):
        return self.run_cmd(f"docker exec srv6-{node} {cmd}", check=False, quiet=quiet)

    # ------------------------------------------------------------------
    # Step 1: Cleanup
    # ------------------------------------------------------------------

    def cleanup(self):
        print("\n" + "=" * 60)
        print("Step 1: Cleanup old deployment")
        print("=" * 60)

        node_names = [f"srv6-{n['name']}" for n in self.topology["nodes"]]
        print(f"Removing {len(node_names)} containers...")
        batch_size = 100
        for i in range(0, len(node_names), batch_size):
            batch = node_names[i:i + batch_size]
            self.run_cmd(f"docker rm -f {' '.join(batch)}", check=False, quiet=True)
            done = min(i + batch_size, len(node_names))
            if done % 500 == 0 or done >= len(node_names):
                print(f"  Progress: {done}/{len(node_names)}")

        for net in self.network_names:
            self.run_cmd(f"docker network rm {net}", check=False, quiet=True)

        # Remove all veth pairs (both normal and cross-host)
        all_links = self.topology["links"]
        print(f"Removing {len(all_links)} veth pairs...")
        for i, link in enumerate(all_links):
            if i % 500 == 0:
                print(f"  Progress: {i}/{len(all_links)}")
            self.run_cmd(
                f"ip link delete {link['node1']}-{link['node2']}",
                check=False, quiet=True,
            )

        # Also clean up any leftover cross-host stub veths on the hypervisor
        print(f"Cleaning up cross-host hypervisor stubs (ring{self.cross_host_remote_ring})...")
        for node_idx in range(self.topology["nodes_per_ring"]):
            stub_name = f"r{self.cross_host_remote_ring}n{node_idx}-r{self.cross_host_local_ring}n{node_idx}"
            self.run_cmd(f"ip link delete {stub_name}", check=False, quiet=True)

        print("Cleanup completed")

    # ------------------------------------------------------------------
    # Step 2: Create Docker networks
    # ------------------------------------------------------------------

    def create_network(self):
        print("\n" + "=" * 60)
        print(f"Step 2: Create {self.num_networks} Docker bridge networks")
        print("=" * 60)
        for net in self.network_names:
            self.run_cmd(f"docker network create {net}", quiet=True)
            print(f"  Network '{net}' created")

    # ------------------------------------------------------------------
    # Step 3: Start containers  (only local nodes, no ring30)
    # ------------------------------------------------------------------

    def start_single_container(self, node):
        node_name    = node["name"]
        srv6_locator = node["srv6_locator"]
        frr_conf     = self.config_dir / f"frr-{node_name}.conf"
        daemons_conf = self.config_dir / "daemons"

        if not frr_conf.exists():
            raise FileNotFoundError(f"Config not found: {frr_conf}")

        node_network = self._node_network[node_name]
        cmd = (
            f"docker run -d "
            f"--name srv6-{node_name} "
            f"--hostname {node_name} "
            f"--network {node_network} "
            f"--privileged "
            f"--cap-add NET_ADMIN "
            f"--sysctl net.ipv6.conf.all.disable_ipv6=0 "
            f"--sysctl net.ipv6.conf.all.forwarding=1 "
            f"-v {frr_conf.absolute()}:/etc/frr/frr.conf "
            f"-v {daemons_conf.absolute()}:/etc/frr/daemons:ro "
            f"-e NODE_NAME={node_name} "
            f"-e SRV6_LOCATOR={srv6_locator} "
            f"{self.image_name}"
        )
        self.run_cmd(cmd, quiet=True)
        return node_name

    def start_containers(self):
        print("\n" + "=" * 60)
        print("Step 3: Start FRR containers (ring0-29 only)")
        print("=" * 60)
        total = len(self.topology["nodes"])
        print(f"Starting {total} containers (workers={self.max_workers})...")

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
                        print(f"  Progress: {completed}/{total} ({100*completed/total:.1f}%)")
                except Exception as e:
                    node = futures[future]
                    print(f"  Error starting {node['name']}: {e}")

        print(f"Started {completed}/{total} containers")
        print("Waiting 15s for containers to initialize...")
        time.sleep(15)

    # ------------------------------------------------------------------
    # Step 4: Pre-fetch PIDs
    # ------------------------------------------------------------------

    def prefetch_all_pids(self):
        print("\n" + "=" * 60)
        print("Step 4: Pre-fetching container PIDs")
        print("=" * 60)
        total      = len(self.topology["nodes"])
        node_names = [n["name"] for n in self.topology["nodes"]]
        batch_size = 200
        fetched    = 0

        for i in range(0, len(node_names), batch_size):
            batch = node_names[i:i + batch_size]

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

    # ------------------------------------------------------------------
    # Step 5a: Create normal (intra-host) links
    # ------------------------------------------------------------------

    def create_single_link(self, link):
        """Create a veth pair and move both ends into their containers."""
        node1  = link["node1"]
        node2  = link["node2"]
        subnet = link["subnet"]
        veth1  = f"{node1}-{node2}"
        veth2  = f"{node2}-{node1}"

        self.run_cmd(f"ip link add {veth1} type veth peer name {veth2}", quiet=True)

        pid1 = self._pid_cache.get(node1) or self.run_cmd(
            f"docker inspect -f '{{{{.State.Pid}}}}' srv6-{node1}", quiet=True
        ).stdout.strip()
        pid2 = self._pid_cache.get(node2) or self.run_cmd(
            f"docker inspect -f '{{{{.State.Pid}}}}' srv6-{node2}", quiet=True
        ).stdout.strip()

        self.run_cmd(f"ip link set {veth1} netns {pid1}", quiet=True)
        self.run_cmd(f"ip link set {veth2} netns {pid2}", quiet=True)

        self.docker_exec(node1, f"ip link set {veth1} up", quiet=True)
        self.docker_exec(node2, f"ip link set {veth2} up", quiet=True)

        ip1 = subnet.replace("::/64", "::1/64")
        ip2 = subnet.replace("::/64", "::2/64")
        self.docker_exec(node1, f"ip -6 addr add {ip1} dev {veth1}", quiet=True)
        self.docker_exec(node2, f"ip -6 addr add {ip2} dev {veth2}", quiet=True)

    # ------------------------------------------------------------------
    # Step 5b: Create cross-host links
    #
    #  Topology (host1 perspective):
    #    veth pair:  r29nx-r30nx  (local end)  /  r30nx-r29nx  (hypervisor stub)
    #
    #  Actions:
    #    1. Create veth pair on hypervisor.
    #    2. Move r29nx-r30nx into the srv6-r29nx container namespace.
    #    3. Bring the interface up inside the container.
    #    4. Assign IPv6 address inside the container.
    #    5. Leave r30nx-r29nx on the hypervisor (bring it up but no IP).
    #       Your bridging mechanism will connect it to host2's r30nx-r29nx stub.
    # ------------------------------------------------------------------

    def create_single_cross_host_link(self, link):
        """
        Create one cross-host veth pair.

        node1 = r29nx  -> deploy_node1 = "container"  -> moved into container
        node2 = r30nx  -> deploy_node2 = "hypervisor" -> stays on hypervisor
        """
        node1  = link["node1"]   # r29nx  (local)
        node2  = link["node2"]   # r30nx  (remote / hypervisor stub)
        subnet = link["subnet"]

        local_node  = node1 if link.get("deploy_node1") == "container" else node2
        remote_stub = node2 if link.get("deploy_node1") == "container" else node1

        local_veth  = f"{local_node}-{remote_stub}"   # goes into container
        remote_veth = f"{remote_stub}-{local_node}"   # stays on hypervisor

        # 1. Create the veth pair on the hypervisor
        self.run_cmd(
            f"ip link add {local_veth} type veth peer name {remote_veth}",
            quiet=True,
        )

        # 2. Move local_veth into the container's network namespace
        pid = self._pid_cache.get(local_node) or self.run_cmd(
            f"docker inspect -f '{{{{.State.Pid}}}}' srv6-{local_node}", quiet=True
        ).stdout.strip()
        self.run_cmd(f"ip link set {local_veth} netns {pid}", quiet=True)

        # 3. Bring local_veth up inside the container
        self.docker_exec(local_node, f"ip link set {local_veth} up", quiet=True)

        # 4. Assign the correct IPv6 address inside the container
        #    node1 always gets ::1, node2 gets ::2 (by topology convention)
        if local_node == node1:
            local_ip = subnet.replace("::/64", "::1/64")
        else:
            local_ip = subnet.replace("::/64", "::2/64")
        self.docker_exec(local_node, f"ip -6 addr add {local_ip} dev {local_veth}", quiet=True)

        # 5. Bring remote_veth up on the hypervisor (no IP; bridging will attach it)
        self.run_cmd(f"ip link set {remote_veth} up", quiet=True)

    def _group_links_by_area(self):
        """
        Group NORMAL (intra-host) links by area for ordered deployment.
        Cross-host links are handled separately.
        """
        area_ranges = self.topology.get("area_ranges", [])
        area_links  = {
            area["area_id"]: {"intra-ring": [], "inter-ring": []}
            for area in area_ranges
        }
        cross_area_links = []

        for link in self.normal_links:
            node1 = self.nodes_by_name[link["node1"]]
            node2 = self.nodes_by_name[link["node2"]]
            if node1["area_id"] != node2["area_id"]:
                cross_area_links.append(link)
                continue
            area_links[node1["area_id"]][link["type"]].append(link)

        batches = []
        for area in area_ranges:
            aid = area["area_id"]
            if area_links[aid]["intra-ring"]:
                batches.append((f"Area {aid} intra-ring links", area_links[aid]["intra-ring"]))
            if area_links[aid]["inter-ring"]:
                batches.append((f"Area {aid} inter-ring links", area_links[aid]["inter-ring"]))
        if cross_area_links:
            batches.append(("Cross-area backbone links (intra-host)", cross_area_links))

        return batches

    def _create_link_batch(self, batch_name, links, completed_so_far, total_links):
        print(f"\n  -> {batch_name}: {len(links)} links")
        batch_completed = 0
        batch_failed    = 0

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self.create_single_link, l): l for l in links}
            for future in as_completed(futures):
                try:
                    future.result()
                    batch_completed += 1
                    global_done = completed_so_far + batch_completed
                    if batch_completed % 200 == 0 or batch_completed == len(links):
                        pct = global_done / total_links * 100
                        print(f"     Progress: {global_done}/{total_links} ({pct:.1f}%)")
                except Exception as e:
                    batch_failed += 1
                    link = futures[future]
                    print(f"     Error: {link['node1']}-{link['node2']}: {e}")

        print(
            f"     Batch done: {batch_completed}/{len(links)}"
            + (f" ({batch_failed} failed)" if batch_failed else "")
        )
        return batch_completed, batch_failed

    def create_links(self):
        print("\n" + "=" * 60)
        print("Step 5: Create inter-node links")
        print("=" * 60)

        normal_total = len(self.normal_links)
        cross_total  = len(self.cross_host_links)
        print(f"Normal (intra-host) links : {normal_total}")
        print(f"Cross-host links          : {cross_total}")

        # --- 5a: Normal links in area order ---
        completed = 0
        failed    = 0
        for batch_name, links in self._group_links_by_area():
            bc, bf = self._create_link_batch(batch_name, links, completed, normal_total)
            completed += bc
            failed    += bf
        print(f"Intra-host links done: {completed}/{normal_total}"
              + (f" ({failed} failed)" if failed else ""))

        # --- 5b: Cross-host links ---
        print(f"\n  -> Cross-host boundary links (ring{self.cross_host_local_ring}"
              f" -> ring{self.cross_host_remote_ring}): {cross_total} links")
        print(f"     Local end  (r{self.cross_host_local_ring}nx-r{self.cross_host_remote_ring}nx) -> container")
        print(f"     Remote end (r{self.cross_host_remote_ring}nx-r{self.cross_host_local_ring}nx) -> hypervisor stub")

        cross_done   = 0
        cross_failed = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self.create_single_cross_host_link, l): l
                for l in self.cross_host_links
            }
            for future in as_completed(futures):
                try:
                    future.result()
                    cross_done += 1
                    if cross_done % 72 == 0 or cross_done == cross_total:
                        pct = cross_done / cross_total * 100
                        print(f"     Progress: {cross_done}/{cross_total} ({pct:.1f}%)")
                except Exception as e:
                    cross_failed += 1
                    link = futures[future]
                    print(f"     Error: {link['node1']}-{link['node2']}: {e}")

        print(f"     Cross-host links done: {cross_done}/{cross_total}"
              + (f" ({cross_failed} failed)" if cross_failed else ""))

    # ------------------------------------------------------------------
    # Step 6: Wait for IS-IS convergence
    # ------------------------------------------------------------------

    def wait_for_isis_convergence(self):
        print("\n" + "=" * 60)
        print("Step 6: Wait for ISIS convergence")
        print("=" * 60)
        total_nodes = len(self.topology["nodes"])
        wait_time   = min(900, 60 + total_nodes // 4)
        print(f"Waiting {wait_time}s for ISIS to converge ({total_nodes} nodes)...")
        for i in range(wait_time):
            if i % 30 == 0:
                print(f"  {i}/{wait_time}s elapsed", flush=True)
            time.sleep(1)
        print(f"ISIS convergence wait done ({wait_time}s)")

    # ------------------------------------------------------------------
    # Step 7: Verify
    # ------------------------------------------------------------------

    def verify_deployment(self):
        print("\n" + "=" * 60)
        print("Step 7: Verify deployment")
        print("=" * 60)
        nodes = self.topology["nodes"]
        npr   = self.topology.get("nodes_per_ring", 72)
        sample_indices = [0, npr - 1, npr, len(nodes) // 2, len(nodes) - 1]
        sample_nodes   = [nodes[i]["name"] for i in sample_indices if i < len(nodes)]

        print(f"Sampling {len(sample_nodes)} nodes...")
        for node_name in sample_nodes:
            print(f"\n--- {node_name} ---")
            r = self.docker_exec(node_name, "vtysh -c 'show isis neighbor' | grep -c Up", quiet=True)
            if r.stdout.strip():
                print(f"  ISIS neighbors (Up): {r.stdout.strip()}")
            r = self.docker_exec(node_name, "ip -6 route show | wc -l", quiet=True)
            if r.stdout.strip():
                print(f"  IPv6 routes: {r.stdout.strip()}")

        # Verify cross-host stubs are up on the hypervisor
        print("\n--- Cross-host hypervisor stubs ---")
        up_count = 0
        for node_idx in range(min(5, self.topology["nodes_per_ring"])):
            stub = f"r{self.cross_host_remote_ring}n{node_idx}-r{self.cross_host_local_ring}n{node_idx}"
            r    = self.run_cmd(f"ip link show {stub}", check=False, quiet=True)
            state = "UP" if "UP" in r.stdout else "DOWN/missing"
            print(f"  {stub}: {state}")
            if state == "UP":
                up_count += 1
        print(f"  (showing first 5 of {self.topology['nodes_per_ring']} stubs)")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def print_summary(self):
        print("\n" + "=" * 60)
        print("HOST1 DEPLOYMENT COMPLETED")
        print("=" * 60)
        nodes = self.topology["nodes"]
        print(f"Nodes deployed : {len(nodes)} (rings 0-{self.cross_host_local_ring})")
        print(f"Links created  : {len(self.normal_links)} intra-host + {len(self.cross_host_links)} cross-host stubs")

        node1 = nodes[0]["name"]
        lr    = self.cross_host_local_ring
        rr    = self.cross_host_remote_ring
        print(f"""
Cross-host stub interfaces on this hypervisor:
  r{rr}nx-r{lr}nx  (one per node in ring{lr}, total {self.topology['nodes_per_ring']})
  -> Connect these to your bridge / VXLAN / GRE toward host2.

Quick checks:
  docker exec srv6-{node1} vtysh -c 'show isis neighbor'
  docker exec srv6-r{lr}n0 vtysh -c 'show isis neighbor'
  ip link show r{rr}n0-r{lr}n0    # hypervisor stub should be UP

Cleanup:
  sudo python3 deploy_host1.py --cleanup <topology_file>
""")

    # ------------------------------------------------------------------
    # Main deploy workflow
    # ------------------------------------------------------------------

    def deploy(self):
        print("\n" + "=" * 60)
        print("SRv6+FRR HOST1 DEPLOYMENT (rings 0-29, 2160 nodes)")
        print("=" * 60)
        print(f"Nodes          : {len(self.topology['nodes'])}")
        print(f"Normal links   : {len(self.normal_links)}")
        print(f"Cross-host links: {len(self.cross_host_links)} (ring{self.cross_host_local_ring} -> ring{self.cross_host_remote_ring})")
        print(f"Workers        : {self.max_workers}")

        start = time.time()
        try:
            self.cleanup()
            self.create_network()
            self.start_containers()
            self.prefetch_all_pids()
            self.create_links()
            self.wait_for_isis_convergence()
            self.verify_deployment()
            self.print_summary()
            elapsed = int(time.time() - start)
            print(f"\nTotal time: {elapsed // 60}m {elapsed % 60}s")
            return True
        except Exception as e:
            print(f"\nDeployment failed: {e}")
            import traceback
            traceback.print_exc()
            return False


def main():
    parser = argparse.ArgumentParser(description="Deploy Host1 SRv6+FRR network (rings 0-29)")
    parser.add_argument("topology", nargs="?",
                        default="topology_host1_2160nodes.json",
                        help="Topology JSON file")
    parser.add_argument("--cleanup",    action="store_true", help="Only cleanup")
    parser.add_argument("--config-dir", default="configs",   help="Config directory")
    parser.add_argument("--workers",    type=int, default=30, help="Parallel workers")
    args = parser.parse_args()

    if not os.path.exists(args.topology):
        print(f"Error: {args.topology} not found")
        sys.exit(1)

    deployer = Host1SRv6Deployer(args.topology, args.config_dir)
    deployer.max_workers = args.workers

    if args.cleanup:
        deployer.cleanup()
        print("Cleanup completed")
        sys.exit(0)

    if not deployer.config_dir.exists():
        print(f"Error: config dir '{deployer.config_dir}' not found")
        print(f"  Run: python3 generate_configs.py {args.topology}")
        sys.exit(1)

    success = deployer.deploy()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

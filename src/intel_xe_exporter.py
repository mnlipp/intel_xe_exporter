#!/usr/bin/env python3

import glob
import re
import time

from prometheus_client import start_http_server, Gauge


# ---------------- Metrics ----------------

gpu_vram = Gauge(
    "intel_xe_vram_resident_bytes",
    "Resident VRAM used by Intel Xe GPUs"
)

gpu_system = Gauge(
    "intel_xe_system_resident_bytes",
    "Resident system memory used by Intel Xe GPUs"
)

process_vram = Gauge(
    "intel_xe_process_vram_resident_bytes",
    "Resident VRAM per DRM client",
    ["pid", "process", "card", "tile"]
)

process_system = Gauge(
    "intel_xe_process_system_resident_bytes",
    "Resident system memory per DRM client",
    ["pid", "process", "card"]
)

engine_cycles = Gauge(
    "intel_xe_engine_cycles_total",
    "Total Intel Xe engine cycles",
    ["engine", "pid", "process"]
)

engine_active_cycles = Gauge(
    "intel_xe_engine_active_cycles_total",
    "Active Intel Xe engine cycles",
    ["engine"]
)

engine_capacity = Gauge(
    "intel_xe_engine_capacity",
    "Intel Xe engine capacity",
    ["engine"]
)

engine_util = Gauge(
    "intel_xe_engine_busy_ratio",
    "Intel Xe engine utilization ratio",
    ["engine", "pid", "process"]
)


# ---------------- Helpers ----------------

def parse_size(value):
    """Parse human-readable size strings like '16248 KiB' or '184 MiB' into bytes."""
    parts = value.split()
    number = int(parts[0])
    if len(parts) == 1:
        return number
    return number * {
        "KiB": 1024,
        "MiB": 1024 ** 2,
        "GiB": 1024 ** 3,
    }.get(parts[1], 1)


def process_name(pid):
    """Read the process name from /proc/<pid>/comm."""
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except Exception:
        return "unknown"


def read_xe_clients():
    """
    Walk /proc/<pid>/fdinfo/<fd> and collect data from every file descriptor
    that belongs to the Intel Xe DRM driver (identified by 'drm-driver: xe').

    Each fdinfo file exposes per-client counters:

      - drm-resident-vram<N>   : VRAM used by this client on tile N (KiB/MiB)

      - drm-resident-system    : System memory used by this client

      - drm-total-cycles-<eng> : Cumulative engine cycles since boot.
        This is a global driver value — identical for every client, so
        it must NOT be summed across clients.

      - drm-cycles-<eng>       : Active cycles consumed by this client
        on engine <eng> since boot.  This IS per-client and must be summed.

      - drm-engine-capacity-<eng> : Number of engine units (e.g. vcs=2, vecs=2).
        Used to scale the busy ratio so multi-unit engines can reach 100%.

    Fds pointing to the same underlying DRM file (same inode number) are
    deduplicated.  Remaining fds belonging to the same process are aggregated
    into a single client entry: VRAM and system memory are summed per tile,
    active cycles are summed per engine, and global counters (total cycles,
    capacity) are taken from the first fd only.
    """

    # Collect all DRM fdinfo paths grouped by PID, keyed by inode to
    # deduplicate fds that refer to the same DRM file.
    drm_fds = {}

    for path in glob.glob("/proc/[0-9]*/fdinfo/*"):

        match = re.match(r"/proc/(\d+)/fdinfo/", path)
        if not match:
            continue

        pid = match.group(1)

        try:
            with open(path) as f:
                data = f.read()
        except (PermissionError, FileNotFoundError):
            continue

        if not re.search(r"^drm-driver:\s*xe\s*$", data, re.MULTILINE):
            continue

        ino_match = re.search(r"^ino:\s+(\d+)", data, re.MULTILINE)
        ino = ino_match.group(1) if ino_match else path

        drm_fds.setdefault(pid, {})
        # Only keep the first fdinfo for each inode within this PID.
        if ino not in drm_fds[pid]:
            drm_fds[pid][ino] = data

    # Parse and aggregate all unique fds per PID into a single client entry.
    clients = []

    for pid, fd_map in drm_fds.items():
        fd_contents = list(fd_map.values())
        card = "unknown"
        vram = {}
        system = 0
        cycles = {}
        active_cycles = {}
        capacity = {}

        for data in fd_contents:
            m = re.search(r"drm-pdev:\s+([0-9a-f:.]+)", data)
            if m and card == "unknown":
                card = m.group(1)

            for line in data.splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip()

                m = re.match(r"drm-resident-vram(\d+)", key)
                if m:
                    tile = m.group(1)
                    vram[tile] = vram.get(tile, 0) + parse_size(value)
                    continue

                if key == "drm-resident-system":
                    system += parse_size(value)
                    continue

                m = re.match(r"drm-total-cycles-(\w+)", key)
                if m:
                    # Global counter — take from first fd only.
                    if m.group(1) not in cycles:
                        cycles[m.group(1)] = int(value)
                    continue

                m = re.match(r"drm-cycles-(\w+)", key)
                if m:
                    engine = m.group(1)
                    active_cycles[engine] = active_cycles.get(engine, 0) + int(value)
                    continue

                m = re.match(r"drm-engine-capacity-(\w+)", key)
                if m:
                    # Capacity is the same for all fds — take first only.
                    if m.group(1) not in capacity:
                        capacity[m.group(1)] = int(value)

        clients.append({
            "pid": pid,
            "process": process_name(pid),
            "card": card,
            "vram": vram,
            "system": system,
            "cycles": cycles,
            "active_cycles": active_cycles,
            "capacity": capacity,
        })

    return clients


# ---------------- Engine utilization ----------------

previous_total_cycles = {}
previous_active_cycles = {}
previous_process_active = {}


def update_engine_stats(clients):
    """
    Compute busy_ratio from counter deltas between polling intervals:

      busy = delta_active / (delta_total * capacity)

    Key observations from fdinfo analysis:

      drm-total-cycles-<eng> is a global driver counter — every client's fdinfo
      reports the same value.  Taking the first client's value avoids multiplying
      the denominator by the number of clients.

      drm-cycles-<eng> is per-client active cycles.  Summing across clients gives
      the total active work done on the engine during the interval.

      drm-engine-capacity-<eng> is the number of parallel engine units (e.g. 2
      VCS instances).  Dividing by capacity allows the ratio to reach 1.0 when
      all units are fully occupied.

    The first scrape after startup skips utilization (no previous delta).

    Busy ratio is reported per process.  The global delta_total serves as the
    denominator for every process's ratio.
    """

    total = {}
    active = {}
    capacities = {}

    for client in clients:

        for engine, value in client["cycles"].items():
            # Global counter — take from first client only.
            if engine not in total:
                total[engine] = value

        for engine, value in client["active_cycles"].items():
            # Per-client — sum across all clients.
            active[engine] = active.get(engine, 0) + value

        for engine, value in client["capacity"].items():
            capacities[engine] = value

    for client in clients:
        for engine, value in client["cycles"].items():
            engine_cycles.labels(engine, client["pid"], client["process"]).set(value)

    for engine, value in active.items():
        engine_active_cycles.labels(engine).set(value)

    for engine, value in capacities.items():
        engine_capacity.labels(engine).set(value)

    for engine in total:
        if engine not in previous_total_cycles:
            continue

        old_total = previous_total_cycles[engine]
        delta_total = total[engine] - old_total
        capacity = capacities.get(engine, 1)

        if delta_total <= 0:
            continue

        for client in clients:
            key = (client["pid"], client["process"], engine)
            current_active = client["active_cycles"].get(engine, 0)
            old_process_active = previous_process_active.get(key, 0)
            delta_active = current_active - old_process_active

            busy = delta_active / (delta_total * capacity)
            engine_util.labels(
                engine, client["pid"], client["process"]
            ).set(max(0, min(busy, 1)))

    previous_total_cycles.clear()
    previous_total_cycles.update(total)
    previous_active_cycles.clear()
    previous_active_cycles.update(active)

    previous_process_active.clear()
    for client in clients:
        for engine, value in client["active_cycles"].items():
            key = (client["pid"], client["process"], engine)
            previous_process_active[key] = value


# ---------------- Update metrics ----------------

def update():

    clients = read_xe_clients()

    total_vram = 0
    total_vram_capacity = {}
    total_system = 0

    process_vram.clear()
    process_system.clear()
    engine_cycles.clear()
    engine_util.clear()

    for client in clients:

        for tile, size in client["vram"].items():
            total_vram += size
            process_vram.labels(
                client["pid"],
                client["process"],
                client["card"],
                tile
            ).set(size)

        total_system += client["system"]
        process_system.labels(
            client["pid"],
            client["process"],
            client["card"]
        ).set(client["system"])

    gpu_vram.set(total_vram)
    gpu_system.set(total_system)

    update_engine_stats(clients)


# ---------------- Main ----------------

if __name__ == "__main__":

    start_http_server(9830)

    while True:
        update()
        time.sleep(5)

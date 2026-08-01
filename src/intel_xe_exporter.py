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

gpu_vram_total = Gauge(
    "intel_xe_vram_total_bytes",
    "Total VRAM capacity of Intel Xe GPUs"
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
    ["engine"]
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
    ["engine"]
)


# ---------------- Helpers ----------------

def parse_size(value):
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
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except Exception:
        return "unknown"


def read_xe_clients():

    clients = []

    for path in glob.glob("/proc/[0-9]*/fdinfo/*"):

        match = re.match(
            r"/proc/(\d+)/fdinfo/",
            path
        )

        if not match:
            continue

        pid = match.group(1)

        try:
            with open(path) as f:
                data = f.read()

        except (PermissionError, FileNotFoundError):
            continue


        if not re.search(
            r"^drm-driver:\s*xe\s*$",
            data,
            re.MULTILINE
        ):
            continue


        card = "unknown"

        m = re.search(
            r"drm-pdev:\s+([0-9a-f:.]+)",
            data
        )

        if m:
            card = m.group(1)


        values = {
            "pid": pid,
            "process": process_name(pid),
            "card": card,
            "vram": {},
            "vram_total": {},
            "system": 0,
            "cycles": {},
            "active_cycles": {},
            "capacity": {},
        }


        for line in data.splitlines():

            if ":" not in line:
                continue

            key, value = line.split(":", 1)

            key = key.strip()
            value = value.strip()


            # Resident VRAM tiles
            m = re.match(
                r"drm-resident-vram(\d+)",
                key
            )

            if m:
                values["vram"][m.group(1)] = parse_size(value)
                continue


            # Total VRAM tiles
            m = re.match(
                r"drm-total-vram(\d+)",
                key
            )

            if m:
                values["vram_total"][m.group(1)] = parse_size(value)
                continue


            if key == "drm-resident-system":

                values["system"] = parse_size(value)
                continue


            # Total engine cycles
            m = re.match(
                r"drm-total-cycles-(\w+)",
                key
            )

            if m:
                values["cycles"][m.group(1)] = int(value)
                continue


            # Active engine cycles
            m = re.match(
                r"drm-cycles-(\w+)",
                key
            )

            if m:
                values["active_cycles"][m.group(1)] = int(value)
                continue


            # Engine capacity
            m = re.match(
                r"drm-engine-capacity-(\w+)",
                key
            )

            if m:
                values["capacity"][m.group(1)] = int(value)


        clients.append(values)

    return clients


# ---------------- Engine utilization ----------------

previous_total_cycles = {}
previous_active_cycles = {}


def update_engine_stats(clients):

    total = {}
    active = {}
    capacities = {}


    for client in clients:

        for engine, value in client["cycles"].items():

            total[engine] = (
                total.get(engine, 0) +
                value
            )


        for engine, value in client["active_cycles"].items():

            active[engine] = (
                active.get(engine, 0) +
                value
            )


        for engine, value in client["capacity"].items():

            capacities[engine] = value



    for engine, value in total.items():

        engine_cycles.labels(engine).set(value)


    for engine, value in active.items():

        engine_active_cycles.labels(engine).set(value)



    for engine, value in capacities.items():

        engine_capacity.labels(engine).set(value)



    # Calculate busy ratio from counter deltas

    for engine in total:

        if engine not in previous_total_cycles:
            continue

        old_total = previous_total_cycles[engine]
        old_active = previous_active_cycles.get(engine, 0)

        delta_total = total[engine] - old_total
        delta_active = active.get(engine, 0) - old_active


        if delta_total > 0:

            busy = delta_active / delta_total

            engine_util.labels(engine).set(
                max(0, min(busy, 1))
            )


    previous_total_cycles.clear()
    previous_total_cycles.update(total)

    previous_active_cycles.clear()
    previous_active_cycles.update(active)



# ---------------- Update metrics ----------------

def update():

    clients = read_xe_clients()

    total_vram = 0
    total_vram_capacity = {}
    total_system = 0


    process_vram.clear()
    process_system.clear()


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
        ).set(
            client["system"]
        )


        for tile, size in client["vram_total"].items():

            if tile not in total_vram_capacity:
                total_vram_capacity[tile] = size


    gpu_vram.set(total_vram)
    gpu_vram_total.set(sum(total_vram_capacity.values()))
    gpu_system.set(total_system)


    update_engine_stats(clients)



# ---------------- Main ----------------

if __name__ == "__main__":

    start_http_server(9830)

    while True:

        update()

        time.sleep(5)

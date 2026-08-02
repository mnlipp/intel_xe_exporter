# Intel Xe Exporter

A lightweight Prometheus exporter for Intel Xe (Arc) GPU metrics.
Reads `/proc/*/fdinfo/` to expose per-process and aggregate GPU memory
and engine utilization as Prometheus gauges.

Runs as a DaemonSet on Kubernetes, one replica per node with an Intel Arc GPU.

Metrics that are already made available by node-exporter (hwmon...) such
as temperature, fan speed and power usage are not duplicated by this exporter. 

## Metrics

| Metric | Type | Labels |
|---|---|---|
| `intel_xe_vram_resident_bytes` | Gauge | — |
| `intel_xe_system_resident_bytes` | Gauge | — |
| `intel_xe_process_vram_resident_bytes` | Gauge |
  `pid`, `process`, `card`, `tile` |
| `intel_xe_process_system_resident_bytes` | Gauge | `pid`, `process`, `card` |
| `intel_xe_engine_cycles_total` | Gauge | `engine` |
| `intel_xe_engine_active_cycles_total` | Gauge | `engine` |
| `intel_xe_engine_capacity` | Gauge | `engine` |
| `intel_xe_engine_busy_ratio` | Gauge | `engine` |

`busy_ratio` is computed from counter deltas between polling intervals (5s).
The first scrape after startup returns no utilization data.

## Running locally

Requires root to read `/proc/*/fdinfo/`.

```bash
pip install -r src/requirements.txt
sudo python src/intel_xe_exporter.py
```

The metrics endpoint is available at `http://localhost:9830/`.

## Kubernetes deployment

### Prerequisites

- `monitoring` namespace exists
- Nodes run a Linux kernel with the `intel_xe` driver

### Deploy

```bash
kubectl apply -f kubernetes/
```

This creates:

- **DaemonSet** — runs on every node, requires `hostPID: true`
- **Headless Service** — discovers pods for scraping by Prometheus

Image: `ghcr.io/mnlipp/intel_xe_exporter:latest`

### Security

To read GPU metrics from other processes, container needs
access to `/proc/*/fdinfo/`. This requires two things:

- **`hostPID: true`** — the container must see host PIDs. Without
  this, `/proc` only contains the container's own processes.
- **`CAP_SYS_PTRACE`** — the kernel blocks reading `/proc/<pid>/fdinfo/`
  for processes that don't belong to the caller. This capability lifts
  that restriction.

All other capabilities are dropped (`drop: ALL`) to keep the
surface area minimal. The container runs as root because
`/proc/*/fdinfo/` is readable by root only.

### Testing

Deploy the manifests from above, then port-forward to a running pod:

```bash
kubectl port-forward svc/intel-xe-exporter 9830:9830 -n monitoring
```

In another terminal, fetch the metrics:

```bash
curl http://localhost:9830/
```

You should see Prometheus-formatted gauges for GPU memory and engine stats.

## Building the image

```bash
buildah build -t ghcr.io/mnlipp/intel_xe_exporter:latest \
  -f image/Containerfile src/
```

The Dockerfile expects `src/` as the build context,
not the repository root.


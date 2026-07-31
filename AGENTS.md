# intel_xe_exporter

Single-file Python Prometheus exporter for Intel Xe (Arc) GPU metrics. HTTP server on port 9830, polls `/proc` every 5 seconds.

## Layout

- `src/intel_xe_exporter.py` — entire application, single entrypoint
- `src/requirements.txt` — one dependency: `prometheus_client`
- `image/Containerfile` — build context is `src/`, **not repo root**
- `kubernetes/` — DaemonSet + headless Service, target namespace `monitoring`
- `.github/workflows/build-image.yml` — builds and pushes to `ghcr.io` on tag push or manual dispatch

## Running locally

```bash
pip install -r src/requirements.txt
python src/intel_xe_exporter.py
```

**Must run as root** — reads `/proc/*/fdinfo/` to classify DRM clients.

## Building and deploying

```bash
buildah build -t intel-xe-exporter:latest -f image/Containerfile src/
kubectl apply -f kubernetes/
```

The `monitoring` namespace must be created beforehand — `kubernetes/` does not include a Namespace resource. The DaemonSet requires `hostPID: true`.

## CI

Workflow `build-image` (`.github/workflows/build-image.yml`) pushes to `ghcr.io`. Triggers: push to a tag, or manual dispatch. No secrets needed — uses `GITHUB_TOKEN`.

## Notes

- No tests, lint, or type-check config exist.
- Engine utilization (`busy_ratio`) is computed from counter deltas between polling intervals; first scrape always skips it.
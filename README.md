# Ingress Query

A small, **read-only** web UI for browsing the Ingresses of a Kubernetes cluster:
see every Ingress at a glance, check whether it actually works, and find the
services and pods behind it.

## Features

**Ingress list**
- All Ingresses grouped by namespace (collapsible groups)
- Search by Ingress name **or host** (case-insensitive substring); matches are highlighted
- Namespace picker with its own filter field (keyboard: ↑/↓, Enter, Esc)
- Status filter (UP / DEGRADED / DOWN / UNKNOWN)
- Per-namespace counters for DOWN and DEGRADED ingresses
- Search, namespace and status filters are kept in the URL, so filtered views can be bookmarked or shared

**Health status**: derived from the cluster state, without sending HTTP requests to the hosts:

| Status | Meaning |
| --- | --- |
| `UP` | Every backend service exists and has at least one ready endpoint |
| `DEGRADED` | Some backend services are healthy, others are not |
| `DOWN` | No backend service is healthy (service missing or no ready endpoints) |
| `UNKNOWN` | Nothing to check (no service backends) or EndpointSlices are not readable |

`ExternalName` services count as healthy. Hover over a status badge to see the reason.

**Ingress detail page**
- Overview: status and reasons, ingress class, load balancer address, hosts, default backend
- Rules (host / path / path type / backend) and TLS (secret per host set)
- Every backend service: type, cluster IP, ports, selector, ready / not-ready endpoint counts
- Pods behind each service (resolved via the service selector): status (e.g. `CrashLoopBackOff`),
  ready containers, restarts, pod IP, node, owner, images, age
- Missing backend services are flagged
- Events, labels, annotations, and the full manifest as YAML (`managedFields` stripped)

The UI is a single static page with light and dark mode. It does not show pod logs.

## Security model

- **Read-only.** The ClusterRole in [`deploy/rbac.yaml`](deploy/rbac.yaml) grants only `get` and `list` on
  `ingresses`, `services`, `pods`, `events` and `endpointslices`. The app has no code paths that write.
- **No built-in authentication.** Put authentication in front of the app's own Ingress
  (e.g. oauth2-proxy, basic auth or an IP allowlist via your ingress controller's annotations).
  Anyone who can open the UI can see all Ingresses, services and pods (names, IPs, images) in the cluster.
- The container runs as a non-root user with a read-only root filesystem and all capabilities dropped.
- If the app lacks permission for events or EndpointSlices, those parts show as unavailable instead of failing.

## Quick start

1. Build and push the image:

   ```sh
   IMAGE=registry.example.com/ingress-query scripts/dev.sh push
   ```

2. Set the image in [`deploy/kustomization.yaml`](deploy/kustomization.yaml) (`newName` / `newTag`),
   and the host (and `ingressClassName` and auth annotations if needed) in [`deploy/ingress.yaml`](deploy/ingress.yaml).

3. Deploy:

   ```sh
   kubectl apply -k deploy/
   ```

The manifests create the `ingress-query` namespace, ServiceAccount, read-only ClusterRole and binding,
Deployment, Service and Ingress.

## Development

The backend is Python ([FastAPI](https://fastapi.tiangolo.com/) with the official Kubernetes client).
The frontend is plain HTML/CSS/JS in a single file with no build step.

```
app/main.py              Backend: Kubernetes queries, health logic, JSON API, static file serving
app/static/index.html    Frontend: list view, detail view, hash routing (#/, #/ingress/<ns>/<name>)
tests/test_api.py        Backend tests against a mocked Kubernetes API (pytest)
tests/ui/*.test.js       Frontend behaviour tests in jsdom
deploy/                  Kubernetes manifests (kustomize)
scripts/dev.sh           Build / test / run helper (runs everything in Docker)
```

### Helper script

All commands run in Docker, so you only need Docker locally:

| Command | Description |
| --- | --- |
| `scripts/dev.sh test` | Run backend and UI tests |
| `scripts/dev.sh test-api` | Backend tests only |
| `scripts/dev.sh test-ui` | UI tests only |
| `scripts/dev.sh build` | Build the image (`IMAGE`, `TAG` env vars; default `ingress-query:0.1.0`) |
| `scripts/dev.sh push` | Build and push the image |
| `scripts/dev.sh run-local` | Run the image on `localhost:8080` against your current kubeconfig context |

### Running without Docker

Outside a cluster the app uses your current kubeconfig context:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8080
pytest -q
```

### Configuration

| Env var | Default | Description |
| --- | --- | --- |
| `LOG_LEVEL` | `INFO` | Python log level |
| `KUBECONFIG` | – | Only used outside a cluster; in-cluster config takes precedence |

## API

| Endpoint | Description |
| --- | --- |
| `GET /api/ingresses?q=&namespace=` | Ingress summaries with health status. `q` matches the name or any host (case-insensitive) |
| `GET /api/ingresses/{namespace}/{name}` | Ingress detail with health, backend services, pods and events |
| `GET /api/ingresses/{namespace}/{name}/yaml` | Ingress manifest as YAML |
| `GET /healthz` | Liveness / readiness probe |

The list endpoint uses two cluster-wide calls (services and EndpointSlices) to compute every ingress's
health, so the number of API calls doesn't grow with the number of Ingresses.

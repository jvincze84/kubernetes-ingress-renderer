# CLAUDE.md

Working notes for Claude Code (and humans) to continue work on this project. User-facing documentation is in `README.md`.

## What this is

**Ingress Query**: a small, read-only web UI that runs inside a Kubernetes cluster and lists Ingresses by namespace.
It shows whether each Ingress works (UP/DEGRADED/DOWN/UNKNOWN) and, on a detail page, the backend services and pods behind it.

## Hard requirements (agreed with the user, don't change without asking)

- **Read-only.** Never add code that creates, updates or deletes cluster objects. RBAC (`deploy/rbac.yaml`) stays `get`/`list` only.
  If a feature needs a new resource type, add it to the ClusterRole with `get`/`list` only, and handle `403` gracefully.
- **No authentication in the app.** It runs behind its own Ingress, which handles auth.
- **UI language: English.** The user talks to Claude in **Hungarian**, so reply in Hungarian. Code, UI text, comments, commits and docs stay in English.
- **No pod logs** in the UI. The user explicitly said they're not needed.
- Search matches `metadata.name` **or** any rule host (case-insensitive substring), in both the UI and `GET /api/ingresses?q=`.

## Architecture

```
app/main.py            FastAPI backend (single module)
app/static/index.html  Entire frontend: inline CSS + vanilla JS, no build step, no external assets
tests/test_api.py      pytest; Kubernetes API is mocked by replacing m._apis with fake classes
tests/ui/*.test.js     Plain node scripts using jsdom; load index.html, mock window.fetch, assert DOM
deploy/                kustomize: namespace, rbac (SA + ClusterRole + binding), deployment, service, ingress
scripts/dev.sh         build | push | test | test-api | test-ui | run-local (everything runs in Docker)
```

### Backend (`app/main.py`)
- `apis()` lazily loads config: in-cluster first, then kubeconfig. It caches clients in the module-level dict `_apis`
  (`net`, `core`, `discovery`, `client`), and tests monkeypatch that dict.
- Error handling: `ApiException` → 502 plain text; `config.ConfigException` (no cluster) → 503; missing ingress → 404.
  Optional data (events, EndpointSlices) returns `None` on API errors, and the UI shows "not available".
- **Health logic**:
  - `service_state(found, type, ready)` returns `(up|down|unknown, reason)`.
  - `ingress_health(states)` aggregates those into `UP` (all up), `DOWN` (all down, or down plus unknown),
    `DEGRADED` (some up, some not), or `UNKNOWN` (no services, or all unknown).
  - `ExternalName` services count as up.
  - List view: `cluster_backend_index()` makes **2 cluster-wide calls** (all services and all EndpointSlices), and
    `summary_health()` uses them. Never make per-ingress API calls in the list endpoint.
  - Detail view: computes health from `service_info()` results, which do per-service reads, so both views use the same rules.
- Pods are resolved from the **service selector** (`list_namespaced_pod(label_selector=...)`), like `kubectl`.
  `pod_status()` approximates kubectl's STATUS column.
- `kubectl.kubernetes.io/last-applied-configuration` is hidden (`NOISY_ANNOTATIONS`), and `managedFields` is stripped from the YAML.

### Frontend (`app/static/index.html`)
- Hash routing: `#/?q=&ns=&status=` (list) and `#/ingress/<ns>/<name>` (detail). Filter changes use
  `history.replaceState` (`syncQuery()`), so they don't trigger `hashchange`.
- `#svc-<name>` links are in-page anchors and are intercepted so they don't route.
- Always escape interpolated values with `esc()` (or `highlight()`, which escapes).
- Namespace picker is a custom combobox (`setupNsCombo`), not a `<select>`, because it needs a filter input.
- Colors are CSS variables on `:root`, with a `prefers-color-scheme: dark` override. Reuse the badge classes `ok/warn/err/accent`.
- The list is fetched in full and filtered client-side, so filters don't make network calls.

## Local environment notes (this machine)

- **Only Docker is usable.** There's no Go, no `make`, no `kubectl`, no cluster, and `python3 -m venv` fails (ensurepip missing).
  Run tests via `scripts/dev.sh test`. It uses the `python:3.13-slim` and `node:22-alpine` images.
- There is no real cluster to test against. Verification means the mocked API tests plus jsdom UI tests.
  Say so honestly when reporting results.

## Workflow

1. Make the change.
2. Add or adjust tests: backend in `tests/test_api.py`, UI behaviour in `tests/ui/<feature>.test.js`.
   For a new UI test file, follow the existing pattern (JSDOM with `runScripts: "dangerously"`, a stubbed `fetch`,
   and `Element.prototype.scrollIntoView`). It's picked up automatically by the `*.test.js` glob.
3. Run `scripts/dev.sh test`. It must pass.
4. Update `README.md` (features/API) when behaviour changes, and update this file when architecture or decisions change.
5. Commit only when the user asks.

Pinned versions are in `requirements.txt` and `requirements-dev.txt`; the image is `python:3.13-slim`.
The image tag is `0.1.0` in `deploy/kustomization.yaml`, `deploy/deployment.yaml` and `scripts/dev.sh`, so bump all three together.

## Ideas / possible next steps (not requested yet)

- Optional real HTTP probe of hosts (careful: auth-protected hosts, internal DNS, timeouts).
- Auto-refresh of the list.
- Showing IngressClass / controller details.
- Helm chart if needed.

"""Ingress Query - a small read-only Kubernetes Ingress browser."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from kubernetes import client, config
from kubernetes.client.rest import ApiException

log = logging.getLogger("ingress-query")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

STATIC_DIR = Path(__file__).parent / "static"
NOISY_ANNOTATIONS = {"kubectl.kubernetes.io/last-applied-configuration"}

app = FastAPI(title="Ingress Query", docs_url=None, redoc_url=None)

_apis: dict[str, Any] = {}


def apis() -> dict[str, Any]:
    """Lazily load cluster config (in-cluster first, then local kubeconfig)."""
    if not _apis:
        try:
            config.load_incluster_config()
            log.info("Using in-cluster configuration")
        except config.ConfigException:
            config.load_kube_config()
            log.info("Using local kubeconfig")
        _apis["net"] = client.NetworkingV1Api()
        _apis["core"] = client.CoreV1Api()
        _apis["discovery"] = client.DiscoveryV1Api()
        _apis["client"] = client.ApiClient()
    return _apis


# --------------------------------------------------------------------------- helpers


def iso(ts) -> str | None:
    return ts.isoformat() if ts else None


def ingress_class(ing) -> str | None:
    annotations = ing.metadata.annotations or {}
    return ing.spec.ingress_class_name or annotations.get("kubernetes.io/ingress.class")


def backend_info(backend) -> dict[str, Any] | None:
    if backend is None:
        return None
    if backend.service:
        port = backend.service.port
        return {
            "kind": "Service",
            "name": backend.service.name,
            "port": (port.number if port.number is not None else port.name) if port else None,
        }
    if backend.resource:
        r = backend.resource
        return {"kind": r.kind, "name": r.name, "apiGroup": r.api_group, "port": None}
    return None


def all_backends(spec) -> list:
    backends = [spec.default_backend] if spec.default_backend else []
    for rule in spec.rules or []:
        if rule.http:
            backends.extend(p.backend for p in rule.http.paths or [])
    return backends


def service_names(spec) -> list[str]:
    return sorted({b.service.name for b in all_backends(spec) if b and b.service})


def tls_hosts(spec) -> set[str]:
    return {h for t in spec.tls or [] for h in t.hosts or []}


def lb_addresses(ing) -> list[str]:
    lb = ing.status.load_balancer if ing.status else None
    return [x.ip or x.hostname for x in (lb.ingress if lb and lb.ingress else []) if x.ip or x.hostname]


def ingress_summary(ing) -> dict[str, Any]:
    spec = ing.spec
    tls = tls_hosts(spec)
    hosts = sorted({r.host for r in spec.rules or [] if r.host})
    return {
        "namespace": ing.metadata.namespace,
        "name": ing.metadata.name,
        "ingressClass": ingress_class(ing),
        "hosts": [{"host": h, "tls": h in tls} for h in hosts],
        "addresses": lb_addresses(ing),
        "services": service_names(spec),
        "created": iso(ing.metadata.creation_timestamp),
    }


def count_ready(slice_endpoints) -> tuple[int, int]:
    ready = not_ready = 0
    for ep in slice_endpoints or []:
        n = len(ep.addresses or [])
        if ep.conditions is None or ep.conditions.ready is not False:
            ready += n
        else:
            not_ready += n
    return ready, not_ready


def service_state(found: bool, svc_type: str | None, ready: int | None) -> tuple[str, str]:
    """Classify one backend service as (up|down|unknown, reason)."""
    if not found:
        return "down", "service not found"
    if svc_type == "ExternalName":
        return "up", "ExternalName"  # traffic leaves the cluster; nothing to check here
    if ready is None:
        return "unknown", "endpoints unknown"
    return ("up", f"{ready} ready") if ready > 0 else ("down", "no ready endpoints")


def ingress_health(states: dict[str, tuple[str, str]]) -> dict[str, Any]:
    """Aggregate per-service states ({svc: (state, reason)}) into UP / DEGRADED / DOWN / UNKNOWN."""
    if not states:
        return {"status": "UNKNOWN", "reasons": ["No service backends to check"]}
    values = [s for s, _ in states.values()]
    reasons = [f"{name}: {reason}" for name, (s, reason) in sorted(states.items()) if s != "up"]
    if all(v == "up" for v in values):
        status = "UP"
    elif all(v == "down" for v in values):
        status = "DOWN"
    elif "up" in values:
        status = "DEGRADED"
    elif "down" in values:
        status = "DOWN"
    else:
        status = "UNKNOWN"
    return {"status": status, "reasons": reasons}


def cluster_backend_index() -> tuple[dict, dict | None]:
    """One cluster-wide snapshot: {(ns, svc): type} and {(ns, svc): ready count} (None if forbidden)."""
    services = {
        (s.metadata.namespace, s.metadata.name): s.spec.type
        for s in apis()["core"].list_service_for_all_namespaces().items
    }
    try:
        slices = apis()["discovery"].list_endpoint_slice_for_all_namespaces().items
    except ApiException as e:
        log.warning("Cannot list EndpointSlices cluster-wide: %s", e.reason)
        return services, None
    ready: dict[tuple[str, str], int] = {}
    for s in slices:
        svc = (s.metadata.labels or {}).get("kubernetes.io/service-name")
        if svc:
            key = (s.metadata.namespace, svc)
            ready[key] = ready.get(key, 0) + count_ready(s.endpoints)[0]
    return services, ready


def summary_health(summary: dict[str, Any], services: dict, ready: dict | None) -> dict[str, Any]:
    states = {}
    for name in summary["services"]:
        key = (summary["namespace"], name)
        found = key in services
        n = None if ready is None else ready.get(key, 0)
        states[name] = service_state(found, services.get(key), n)
    return ingress_health(states)


def pod_status(pod) -> str:
    """Approximate the STATUS column of `kubectl get pods`."""
    if pod.metadata.deletion_timestamp:
        return "Terminating"
    status = pod.status
    for cs in (status.init_container_statuses or []):
        state = cs.state
        if state and state.waiting and state.waiting.reason:
            return f"Init:{state.waiting.reason}"
        if state and state.terminated and state.terminated.exit_code != 0:
            return f"Init:{state.terminated.reason or 'Error'}"
    for cs in status.container_statuses or []:
        state = cs.state
        if state and state.waiting and state.waiting.reason:
            return state.waiting.reason
        if state and state.terminated and state.terminated.reason:
            return state.terminated.reason
    return status.reason or status.phase or "Unknown"


def pod_info(pod) -> dict[str, Any]:
    statuses = {cs.name: cs for cs in pod.status.container_statuses or []}
    containers = []
    for c in pod.spec.containers:
        cs = statuses.get(c.name)
        containers.append(
            {
                "name": c.name,
                "image": c.image,
                "ready": bool(cs and cs.ready),
                "restarts": cs.restart_count if cs else 0,
            }
        )
    owner = next(iter(pod.metadata.owner_references or []), None)
    return {
        "name": pod.metadata.name,
        "status": pod_status(pod),
        "ready": f"{sum(c['ready'] for c in containers)}/{len(containers)}",
        "restarts": sum(c["restarts"] for c in containers),
        "podIP": pod.status.pod_ip,
        "node": pod.spec.node_name,
        "created": iso(pod.metadata.creation_timestamp),
        "owner": f"{owner.kind}/{owner.name}" if owner else None,
        "containers": containers,
    }


def endpoint_counts(namespace: str, service: str) -> dict[str, int] | None:
    try:
        slices = apis()["discovery"].list_namespaced_endpoint_slice(
            namespace, label_selector=f"kubernetes.io/service-name={service}"
        )
    except ApiException as e:
        log.warning("Cannot list EndpointSlices for %s/%s: %s", namespace, service, e.reason)
        return None
    ready = not_ready = 0
    for s in slices.items:
        r, nr = count_ready(s.endpoints)
        ready += r
        not_ready += nr
    return {"ready": ready, "notReady": not_ready}


def service_info(namespace: str, name: str) -> dict[str, Any]:
    core = apis()["core"]
    try:
        svc = core.read_namespaced_service(name, namespace)
    except ApiException as e:
        if e.status == 404:
            return {"name": name, "found": False}
        raise
    selector = svc.spec.selector or {}
    pods: list[dict[str, Any]] = []
    if selector:
        label_selector = ",".join(f"{k}={v}" for k, v in selector.items())
        pod_list = core.list_namespaced_pod(namespace, label_selector=label_selector)
        pods = sorted((pod_info(p) for p in pod_list.items), key=lambda p: p["name"])
    return {
        "name": name,
        "found": True,
        "type": svc.spec.type,
        "clusterIP": svc.spec.cluster_ip,
        "externalName": svc.spec.external_name,
        "selector": selector,
        "ports": [
            {
                "name": p.name,
                "port": p.port,
                "targetPort": p.target_port,
                "protocol": p.protocol,
                "nodePort": p.node_port,
            }
            for p in svc.spec.ports or []
        ],
        "endpoints": endpoint_counts(namespace, name),
        "pods": pods,
    }


def ingress_events(namespace: str, name: str) -> list[dict[str, Any]] | None:
    try:
        events = apis()["core"].list_namespaced_event(
            namespace, field_selector=f"involvedObject.kind=Ingress,involvedObject.name={name}"
        )
    except ApiException as e:
        log.warning("Cannot list events for %s/%s: %s", namespace, name, e.reason)
        return None
    out = []
    for ev in events.items:
        last = ev.last_timestamp or ev.event_time or ev.metadata.creation_timestamp
        out.append(
            {
                "type": ev.type,
                "reason": ev.reason,
                "message": ev.message,
                "count": ev.count,
                "source": (ev.source.component if ev.source else None) or ev.reporting_component,
                "lastSeen": iso(last),
            }
        )
    return sorted(out, key=lambda e: e["lastSeen"] or "", reverse=True)


def read_ingress(namespace: str, name: str):
    try:
        return apis()["net"].read_namespaced_ingress(name, namespace)
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(404, f"Ingress {namespace}/{name} not found")
        raise


# --------------------------------------------------------------------------- routes


@app.exception_handler(ApiException)
async def api_exception_handler(request, exc: ApiException):
    log.error("Kubernetes API error: %s %s", exc.status, exc.reason)
    return PlainTextResponse(f"Kubernetes API error: {exc.status} {exc.reason}", status_code=502)


@app.exception_handler(config.ConfigException)
async def config_exception_handler(request, exc: config.ConfigException):
    log.error("No Kubernetes configuration: %s", exc)
    return PlainTextResponse(f"No Kubernetes configuration available: {exc}", status_code=503)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.get("/api/ingresses")
def list_ingresses(
    q: str | None = Query(None, description="Case-insensitive substring of metadata.name or any rule host"),
    namespace: str | None = None,
):
    net = apis()["net"]
    items = (
        net.list_namespaced_ingress(namespace).items
        if namespace
        else net.list_ingress_for_all_namespaces().items
    )
    if q:
        needle = q.lower()
        items = [
            i for i in items
            if needle in i.metadata.name.lower()
            or any(needle in (r.host or "").lower() for r in i.spec.rules or [])
        ]
    result = [ingress_summary(i) for i in items]
    services, ready = cluster_backend_index()
    for s in result:
        s["health"] = summary_health(s, services, ready)
    result.sort(key=lambda i: (i["namespace"], i["name"]))
    return {"items": result}


@app.get("/api/ingresses/{namespace}/{name}")
def get_ingress(namespace: str, name: str):
    ing = read_ingress(namespace, name)
    spec = ing.spec
    tls = tls_hosts(spec)
    rules = []
    for rule in spec.rules or []:
        paths = [
            {"path": p.path or "/", "pathType": p.path_type, "backend": backend_info(p.backend)}
            for p in (rule.http.paths if rule.http else [])
        ]
        rules.append({"host": rule.host, "tls": bool(rule.host) and rule.host in tls, "paths": paths})
    annotations = {
        k: v for k, v in (ing.metadata.annotations or {}).items() if k not in NOISY_ANNOTATIONS
    }
    backend_services = [service_info(namespace, s) for s in service_names(spec)]
    states = {}
    for s in backend_services:
        ready = s["endpoints"]["ready"] if s.get("endpoints") else None
        states[s["name"]] = service_state(s["found"], s.get("type"), ready)
    return {
        **ingress_summary(ing),
        "uid": ing.metadata.uid,
        "labels": ing.metadata.labels or {},
        "annotations": annotations,
        "defaultBackend": backend_info(spec.default_backend),
        "rules": rules,
        "tls": [{"hosts": t.hosts or [], "secretName": t.secret_name} for t in spec.tls or []],
        "health": ingress_health(states),
        "backendServices": backend_services,
        "events": ingress_events(namespace, name),
    }


@app.get("/api/ingresses/{namespace}/{name}/yaml", response_class=PlainTextResponse)
def get_ingress_yaml(namespace: str, name: str):
    ing = read_ingress(namespace, name)
    data = apis()["client"].sanitize_for_serialization(ing)
    meta = data.get("metadata", {})
    meta.pop("managedFields", None)
    for key in NOISY_ANNOTATIONS:
        (meta.get("annotations") or {}).pop(key, None)
    data = {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress", **data}
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")

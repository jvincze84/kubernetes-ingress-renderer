"""API tests against a mocked Kubernetes API (no cluster needed).

Run: make test-api   (or: pytest -q tests/ with requirements-dev.txt installed)
"""

from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from kubernetes import client as k
from kubernetes.client.rest import ApiException

import app.main as m

NOW = datetime.now(timezone.utc)


def make_ingress(ns, name, svc, host, tls=True, extra="missing"):
    def path(p, backend_svc, port):
        return k.V1HTTPIngressPath(
            path=p,
            path_type="Prefix",
            backend=k.V1IngressBackend(service=k.V1IngressServiceBackend(name=backend_svc, port=port)),
        )

    return k.V1Ingress(
        metadata=k.V1ObjectMeta(
            namespace=ns, name=name, creation_timestamp=NOW, uid="u1", labels={"x": "y"},
            annotations={"kubectl.kubernetes.io/last-applied-configuration": "{}", "a": "b"},
        ),
        spec=k.V1IngressSpec(
            ingress_class_name="nginx",
            tls=[k.V1IngressTLS(hosts=[host], secret_name="tls")] if tls else None,
            rules=[k.V1IngressRule(host=host, http=k.V1HTTPIngressRuleValue(paths=[
                path("/", svc, k.V1ServiceBackendPort(number=80)),
                path("/x", extra, k.V1ServiceBackendPort(name="http")),
            ]))],
        ),
        status=k.V1IngressStatus(load_balancer=k.V1IngressLoadBalancerStatus(
            ingress=[k.V1IngressLoadBalancerIngress(ip="10.0.0.1")])),
    )


# prod/web -> web-svc (0 ready) + missing  => DOWN
# dev/api  -> api-svc (1 ready) + missing  => DEGRADED
# dev/ok   -> api-svc only                 => UP
INGRESSES = [
    make_ingress("prod", "web", "web-svc", "web.example.com"),
    make_ingress("dev", "api", "api-svc", "api.dev", tls=False),
    make_ingress("dev", "ok", "api-svc", "ok.dev", tls=False, extra="api-svc"),
]

CRASHING_POD = k.V1Pod(
    metadata=k.V1ObjectMeta(
        name="web-1", creation_timestamp=NOW,
        owner_references=[k.V1OwnerReference(api_version="apps/v1", kind="ReplicaSet", name="web-abc", uid="1")],
    ),
    spec=k.V1PodSpec(node_name="n1", containers=[k.V1Container(name="c", image="nginx:1")]),
    status=k.V1PodStatus(phase="Running", pod_ip="10.1.1.1", container_statuses=[k.V1ContainerStatus(
        name="c", image="nginx:1", image_id="", ready=False, restart_count=3,
        state=k.V1ContainerState(waiting=k.V1ContainerStateWaiting(reason="CrashLoopBackOff")),
    )]),
)

SERVICES = [("prod", "web-svc"), ("dev", "api-svc")]


def endpoint_slice(ns, svc, ready):
    return k.V1EndpointSlice(
        address_type="IPv4",
        metadata=k.V1ObjectMeta(namespace=ns, labels={"kubernetes.io/service-name": svc}),
        endpoints=[k.V1Endpoint(addresses=["1.1.1.1"], conditions=k.V1EndpointConditions(ready=ready))],
    )


SLICES = [endpoint_slice("prod", "web-svc", False), endpoint_slice("dev", "api-svc", True)]


class FakeNetworking:
    def list_ingress_for_all_namespaces(self):
        return NS(items=INGRESSES)

    def list_namespaced_ingress(self, ns):
        return NS(items=[i for i in INGRESSES if i.metadata.namespace == ns])

    def read_namespaced_ingress(self, name, ns):
        for i in INGRESSES:
            if (i.metadata.namespace, i.metadata.name) == (ns, name):
                return i
        raise ApiException(status=404)


class FakeCore:
    def list_service_for_all_namespaces(self):
        return NS(items=[
            k.V1Service(metadata=k.V1ObjectMeta(namespace=n, name=s), spec=k.V1ServiceSpec(type="ClusterIP"))
            for n, s in SERVICES
        ])

    def read_namespaced_service(self, name, ns):
        if (ns, name) not in SERVICES:
            raise ApiException(status=404)
        return k.V1Service(spec=k.V1ServiceSpec(
            type="ClusterIP", cluster_ip="10.96.0.1", selector={"app": "web"},
            ports=[k.V1ServicePort(name="http", port=80, target_port=8080, protocol="TCP")],
        ))

    def list_namespaced_pod(self, ns, label_selector):
        assert label_selector == "app=web"
        return NS(items=[CRASHING_POD])

    def list_namespaced_event(self, ns, field_selector):
        raise ApiException(status=403, reason="Forbidden")  # app must degrade gracefully


class FakeDiscovery:
    def list_endpoint_slice_for_all_namespaces(self):
        return NS(items=SLICES)

    def list_namespaced_endpoint_slice(self, ns, label_selector):
        svc = label_selector.split("=", 1)[1]
        return NS(items=[s for s in SLICES
                         if s.metadata.namespace == ns and s.metadata.labels["kubernetes.io/service-name"] == svc])


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(m, "_apis", {
        "net": FakeNetworking(), "core": FakeCore(), "discovery": FakeDiscovery(), "client": k.ApiClient(),
    })
    return TestClient(m.app)


def names(resp):
    return sorted(i["name"] for i in resp.json()["items"])


def test_list_and_health(api):
    r = api.get("/api/ingresses")
    assert r.status_code == 200
    health = {i["name"]: i["health"] for i in r.json()["items"]}
    assert health["web"]["status"] == "DOWN"
    assert health["api"]["status"] == "DEGRADED"
    assert health["api"]["reasons"] == ["missing: service not found"]
    assert health["ok"]["status"] == "UP"


def test_search_by_name_and_host(api):
    assert names(api.get("/api/ingresses?q=WE")) == ["web"]
    assert names(api.get("/api/ingresses?q=EXAMPLE.COM")) == ["web"]
    assert names(api.get("/api/ingresses?q=.dev")) == ["api", "ok"]
    assert names(api.get("/api/ingresses?q=nomatch")) == []
    assert names(api.get("/api/ingresses?namespace=dev")) == ["api", "ok"]


def test_detail(api):
    d = api.get("/api/ingresses/prod/web").json()
    assert d["health"]["status"] == "DOWN"
    missing, web = d["backendServices"]
    assert missing == {"name": "missing", "found": False}
    assert web["endpoints"] == {"ready": 0, "notReady": 1}
    pod = web["pods"][0]
    assert pod["status"] == "CrashLoopBackOff" and pod["ready"] == "0/1" and pod["restarts"] == 3
    assert pod["owner"] == "ReplicaSet/web-abc"
    assert d["events"] is None  # forbidden -> null, not an error
    assert "kubectl.kubernetes.io/last-applied-configuration" not in d["annotations"]
    assert d["rules"][0]["paths"][1]["backend"]["port"] == "http"


def test_yaml_and_errors(api):
    y = api.get("/api/ingresses/prod/web/yaml")
    assert y.status_code == 200 and y.text.startswith("apiVersion: networking.k8s.io/v1")
    assert "last-applied-configuration" not in y.text
    assert api.get("/api/ingresses/prod/nope").status_code == 404
    assert api.get("/").status_code == 200
    assert api.get("/healthz").json() == {"status": "ok"}

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.config.config_exception import ConfigException

from .config import settings

# 集群内运行时用 in-cluster；本地/保底运行（.40 上 systemd/docker）回退到 kubeconfig
try:
    config.load_incluster_config()
except ConfigException:
    config.load_kube_config()

core = client.CoreV1Api()
apps = client.AppsV1Api()


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ApiException:
        return None


def list_pods(namespace: str) -> list[dict]:
    if namespace not in settings.namespaces:
        return []
    res = _safe(core.list_namespaced_pod, namespace)
    if res is None:
        return []
    items = res.items
    result = []
    for pod in items:
        statuses = pod.status.container_statuses or []
        result.append({
            "name": pod.metadata.name,
            "phase": pod.status.phase,
            "node": pod.spec.node_name,
            "restarts": sum(s.restart_count for s in statuses),
            "ready": all(s.ready for s in statuses) if statuses else False,
            "containers": [{
                "name": s.name,
                "ready": s.ready,
                "restarts": s.restart_count,
                "state": _state(s.state),
                "last_reason": (
                    s.last_state.terminated.reason
                    if s.last_state and s.last_state.terminated else None
                ),
            } for s in statuses],
        })
    return result


def _state(state) -> str:
    if state.running:
        return "running"
    if state.waiting:
        return f"waiting:{state.waiting.reason}"
    if state.terminated:
        return f"terminated:{state.terminated.reason}"
    return "unknown"


def pod_logs(namespace: str, pod: str, container: str | None = None,
             tail: int | None = None, previous: bool = False) -> str:
    if namespace not in settings.namespaces:
        return ""
    try:
        return core.read_namespaced_pod_log(
            name=pod, namespace=namespace, container=container,
            tail_lines=tail or settings.log_tail_lines, previous=previous,
        ) or ""
    except ApiException as exc:
        return f"[read log failed: {exc.status} {exc.reason}]"


def events(namespace: str, name: str, limit: int = 15) -> list[str]:
    if namespace not in settings.namespaces:
        return []
    res = _safe(core.list_namespaced_event, namespace)
    if res is None:
        return []
    rows = []
    ev = res.items
    for e in ev:
        if e.involved_object and e.involved_object.name == name:
            ts = e.last_timestamp or e.event_time or e.first_timestamp
            rows.append(f"{ts} [{e.type}] {e.reason}: {e.message}")
    return rows[-limit:]


def deployment_status(namespace: str) -> list[dict]:
    if namespace not in settings.namespaces:
        return []
    res = _safe(apps.list_namespaced_deployment, namespace)
    if res is None:
        return []
    return [{
        "name": d.metadata.name,
        "desired": d.spec.replicas,
        "ready": d.status.ready_replicas or 0,
        "available": d.status.available_replicas or 0,
        "updated": d.status.updated_replicas or 0,
    } for d in res.items]

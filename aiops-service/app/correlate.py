from . import k8s, prom
from .config import settings

# 告警名 → 需要补充的指标查询
ALERT_QUERIES = {
    "NodeCpuHigh": [
        ("cpu_percent", '100 - avg by(node)(rate(node_cpu_seconds_total{mode="idle",node="%s"}[5m])) * 100'),
        ("load5_per_core", 'node_load5{node="%s"} / count by(node)(node_cpu_seconds_total{mode="idle",node="%s"})'),
        ("top_cpu_pods", 'topk(5, sum by(namespace,pod)(rate(container_cpu_usage_seconds_total{node="%s",container!="",container!="POD"}[5m])))'),
    ],
    "NodeMemoryHigh": [
        ("memory_percent", '100 * (1 - node_memory_MemAvailable_bytes{node="%s"} / node_memory_MemTotal_bytes{node="%s"})'),
        ("oom_kill_10m", 'increase(node_vmstat_oom_kill{node="%s"}[10m])'),
    ],
    "NodeDiskHigh": [
        ("disk_percent", '100 * (1 - node_filesystem_avail_bytes{mountpoint="/",node="%s"} / node_filesystem_size_bytes{mountpoint="/",node="%s"})'),
    ],
    "NodeDown": [
        ("node_exporter_up", 'up{job="node_exporter",node="%s"}'),
        ("kubelet_up", 'up{job="kubelet",node="%s"}'),
        ("node_pods", 'count(kube_pod_info{node="%s"})'),
    ],
    "KubePodCrashLooping": [
        ("restarts_1h", 'increase(kube_pod_container_status_restarts_total{namespace="%s",pod="%s"}[1h])'),
        ("cpu_cores", 'sum by(pod)(rate(container_cpu_usage_seconds_total{namespace="%s",pod="%s",container!="",container!="POD"}[5m]))'),
        ("memory_bytes", 'sum by(pod)(container_memory_working_set_bytes{namespace="%s",pod="%s",container!="",container!="POD"})'),
    ],
    "KubeContainerOOMKilled": [
        ("memory_bytes", 'sum by(pod)(container_memory_working_set_bytes{namespace="%s",pod="%s",container!="",container!="POD"})'),
        ("restarts_1h", 'increase(kube_pod_container_status_restarts_total{namespace="%s",pod="%s"}[1h])'),
    ],
    "MySQLConnectionsHigh": [
        ("connections", 'mysql_global_status_threads_connected'),
        ("max_connections", 'mysql_global_variables_max_connections'),
        ("aborted_connects", 'increase(mysql_global_status_aborted_connects[10m])'),
        ("slow_queries_rate", 'rate(mysql_global_status_slow_queries[10m])'),
    ],
    "RedisDown": [
        ("redis_up", 'up{job="redis_exporter"}'),
        ("rejected_connections", 'increase(redis_rejected_connections_total[5m])'),
    ],
    "Ingress5xxHigh": [
        ("5xx_rate", 'sum by(host)(rate(nginx_ingress_controller_requests{status=~"5.."}[5m]))'),
        ("all_rate", 'sum by(host)(rate(nginx_ingress_controller_requests[5m]))'),
        ("p95_latency", 'histogram_quantile(0.95, sum by(le,host)(rate(nginx_ingress_controller_request_duration_seconds_bucket[5m])))'),
        ("ingress_5xx_by_service", 'sum by(service)(rate(nginx_ingress_controller_requests{status=~"5.."}[5m]))'),
    ],
    "MallBackendUnavailable": [
        ("available_replicas", 'kube_deployment_status_replicas_available{namespace="kube-mall"}'),
        ("pod_phase", 'sum by(deployment)(kube_pod_status_phase{namespace="kube-mall",phase!="Running"})'),
    ],
    "KubeDeploymentReplicasMismatch": [
        ("desired_replicas", 'kube_deployment_spec_replicas{namespace="kube-mall"}'),
        ("available_replicas", 'kube_deployment_status_replicas_available{namespace="kube-mall"}'),
    ],
    "KubePodNotReady": [
        ("ready_false", 'kube_pod_status_ready{condition="false",namespace="%s",pod="%s"}'),
        ("restarts_1h", 'increase(kube_pod_container_status_restarts_total{namespace="%s",pod="%s"}[1h])'),
    ],
}


async def build_context(labels: dict) -> dict:
    alertname = labels.get("alertname", "")
    node = labels.get("node") or (labels.get("instance", "").split(":")[0])
    namespace = labels.get("namespace", "kube-mall")
    pod = labels.get("pod", "")

    facts: dict[str, dict] = {}
    for key, expr_tpl in ALERT_QUERIES.get(alertname, []):
        expr = render(expr_tpl, node=node, namespace=namespace, pod=pod)
        if not expr:
            continue
        rng = await prom.query_range(expr, hours=1, step="60s")
        facts[key] = prom.summarize_range(rng)

    pods = k8s.list_pods(namespace)
    related = [p for p in pods if pod and pod in p["name"]][:3] if pod else []
    if not related:
        related = [p for p in pods if not p["ready"]][:3]

    log_chunks = []
    for p in related:
        text = k8s.pod_logs(namespace, p["name"], tail=settings.log_tail_lines)
        if p["restarts"] > 0:
            prev = k8s.pod_logs(namespace, p["name"], tail=60, previous=True)
            if prev and not prev.startswith("[read log failed"):
                text += "\n--- previous container ---\n" + prev
        text = filter_interesting(text)[-settings.max_log_chars:]
        log_chunks.append(f"--- pod {p['name']} ---\n{text}")

    return {
        "alertname": alertname,
        "severity": labels.get("severity"),
        "instance": labels.get("instance"),
        "node": node,
        "namespace": namespace,
        "pod": pod,
        "summary": labels.get("summary", ""),
        "description": labels.get("description", ""),
        "metrics": facts,
        "pods": related,
        "deployments": k8s.deployment_status(namespace),
        "events": k8s.events(namespace, pod) if pod else [],
        "logs": "\n".join(log_chunks)[-settings.max_log_chars:],
    }


def render(tpl: str, node: str, namespace: str, pod: str) -> str:
    count = tpl.count("%s")
    if count == 0:
        return tpl                      # MySQL/Redis/Ingress 等无占位符查询
    if "pod=" in tpl:
        values = (namespace, pod)
    elif "namespace=" in tpl:
        values = (namespace, namespace)
    elif "node=" in tpl:
        values = (node, node)
    else:
        return ""
    return tpl % values[:count]


def filter_interesting(text: str) -> str:
    keywords = ("ERROR", "WARN", "Exception", "error", "failed",
                "refused", "timeout", "OOM", "Killed", "panic",
                "Too many connections", "5xx", "502", "504")
    lines = [ln for ln in text.splitlines() if any(k in ln for k in keywords)]
    return "\n".join(lines[-200:]) if lines else text[-4000:]

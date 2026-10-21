import asyncio
import json
import re

import httpx

from .config import settings
from .prompts import SYSTEM_PROMPT, LOG_PROMPT, build_user_prompt


async def chat_json(system: str, user: str) -> tuple[dict, str]:
    if not settings.deepseek_api_key or not settings.llm_enabled:
        raise RuntimeError("deepseek disabled")

    payload = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": 1200,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    last_error = None
    async with httpx.AsyncClient(timeout=settings.deepseek_timeout) as client:
        for attempt in range(3):
            try:
                resp = await client.post(
                    f"{settings.deepseek_base_url}/chat/completions",
                    headers=headers, json=payload,
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                return json.loads(strip_fence(content)), content
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"deepseek failed: {last_error}")


def strip_fence(text: str) -> str:
    text = text.strip()
    fence = "`" * 3
    match = re.search(fence + r"(?:json)?\s*(\{.*\})\s*" + fence, text, re.S)
    return match.group(1) if match else text


async def analyze_alert(ctx: dict) -> tuple[dict, str, str]:
    """返回 (结果, 来源, 原始响应)。DeepSeek 失败时降级到规则引擎。"""
    try:
        data, raw = await chat_json(SYSTEM_PROMPT, build_user_prompt(ctx))
        return normalize(data, ctx), "llm", raw
    except Exception:
        return rule_fallback(ctx), "rule", ""


async def analyze_logs(log_text: str, namespace: str, pod: str) -> dict:
    user = f"namespace={namespace}, pod={pod}\n日志：\n{log_text}"
    try:
        data, _ = await chat_json(LOG_PROMPT, user)
        data.setdefault("category", "未知")
        data.setdefault("severity", "P2")
        return data
    except Exception:
        return log_fallback(log_text)


def normalize(data: dict, ctx: dict) -> dict:
    risk = str(data.get("risk_level", "P2")).upper()
    if risk not in ("P1", "P2", "P3"):
        risk = "P2"
    try:
        confidence = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    return {
        "risk_level": risk,
        "root_cause": str(data.get("root_cause", "证据不足，无法确定根因")),
        "confidence": max(0.0, min(1.0, confidence)),
        "evidence": [str(x) for x in data.get("evidence", [])][:8],
        "suggestions": [str(x) for x in data.get("suggestions", [])][:5],
        "fix_plan": normalize_fix_plan(data.get("fix_plan", [])),
        "need_human": bool(data.get("need_human", risk == "P1")),
    }


def normalize_fix_plan(raw) -> list[dict]:
    """把 fix_plan 规整为 [{step, action, command, risk}]，最多 5 步。"""
    plan = []
    if not isinstance(raw, list):
        return plan
    for i, item in enumerate(raw[:5], start=1):
        if isinstance(item, str):
            act, cmd, risk = item.strip(), "", "低"
        elif isinstance(item, dict):
            act = str(item.get("action", "")).strip()
            cmd = str(item.get("command", "")).strip()
            risk = str(item.get("risk", "低")).strip() or "低"
        else:
            continue
        if not act:
            continue
        if risk not in ("低", "中", "高"):
            risk = "低"
        plan.append({"step": i, "action": act, "command": cmd, "risk": risk})
    return plan


def rule_fallback(ctx: dict) -> dict:
    name = ctx.get("alertname", "")
    table = {
        "NodeCpuHigh": ("节点 CPU 持续高负载，可能由异常进程或流量突增导致", "P2",
                        ["检查 top 进程与容器 CPU", "确认 HPA 是否已扩容", "必要时迁移负载"]),
        "NodeMemoryHigh": ("节点内存接近耗尽，存在 OOM 风险", "P1",
                           ["检查内存 Top Pod", "确认是否泄漏", "必要时驱逐或扩容"]),
        "KubePodCrashLooping": ("容器反复崩溃，疑似启动配置、依赖或应用异常", "P2",
                                ["查看 kubectl logs --previous", "检查退出码与探针", "核对 ConfigMap/Secret"]),
        "KubeContainerOOMKilled": ("容器内存超过 limit 被 OOMKilled", "P1",
                                   ["检查内存曲线与泄漏", "调整 limit/request", "检查 JVM 堆参数"]),
        "MySQLConnectionsHigh": ("MySQL 连接数接近上限，可能连接池耗尽", "P1",
                                 ["检查应用连接池", "查看慢 SQL", "评估 max_connections"]),
        "RedisDown": ("Redis 不可用，缓存层故障影响业务", "P1",
                      ["检查 Redis Pod 状态", "确认密码/网络", "恢复后验证 mall 验证码接口"]),
        "Ingress5xxHigh": ("入口 5xx 升高，后端服务可能不可用", "P1",
                           ["检查后端 Deployment 副本", "查看 Pod 日志", "检查 Ingress 上游错误"]),
        "MallBackendUnavailable": ("mall 后端可用副本为 0，业务不可用", "P1",
                                   ["查看 Pod 事件", "检查镜像与资源", "回滚最近一次发布"]),
    }
    root, risk, suggestions = table.get(
        name, (f"检测到 {name} 异常，需人工进一步确认", "P2",
               ["查看告警详情", "检查相关指标与日志"])
    )
    facts = ctx.get("metrics", {})
    evidence = [f"{k}: {v}" for k, v in facts.items() if v]
    if ctx.get("logs"):
        evidence.append("已采集到相关 Pod 日志，请结合日志中的异常关键字确认")
    return {
        "risk_level": risk,
        "root_cause": root,
        "confidence": 0.55,
        "evidence": evidence[:6],
        "suggestions": suggestions,
        "fix_plan": rule_fix_plan(name, ctx),
        "need_human": risk == "P1",
    }


# 降级（规则引擎）时给出的可执行修复步骤：(action, command, risk)
RULE_FIXES = {
    "NodeCpuHigh": [
        ("确认节点 CPU 消耗主体（进程/容器）", "kubectl top pod -A --field-selector spec.nodeName={node} --sort-by=cpu", "低"),
        ("确认 HPA 是否已扩容，必要时临时迁移负载", "kubectl -n kube-mall get hpa", "中"),
    ],
    "NodeMemoryHigh": [
        ("查看节点内存 Top Pod，判断是否泄漏", "kubectl top pod -A --field-selector spec.nodeName={node} --sort-by=memory", "低"),
        ("必要时驱逐非关键 Pod 或扩容节点", "kubectl -n kube-mall get events --sort-by=.lastTimestamp", "高"),
    ],
    "NodeDown": [
        ("确认主机与网络是否可达", "ssh {node} 'uptime; systemctl status node_exporter'", "低"),
        ("恢复后确认 exporter 与 kubelet 均恢复", "systemctl restart node_exporter", "中"),
    ],
    "KubePodCrashLooping": [
        ("查看上一次容器的日志与退出码", "kubectl -n {namespace} logs {pod} --previous --tail=100", "低"),
        ("核对启动命令/ConfigMap/Secret 与探针", "kubectl -n {namespace} describe pod {pod}", "中"),
    ],
    "KubeContainerOOMKilled": [
        ("确认容器内存是否贴 limit 被 OOMKilled", "kubectl -n {namespace} describe pod {pod}", "低"),
        ("上调 limit/request 或修复泄漏后滚动重启", "kubectl -n {namespace} rollout restart deploy/<deployment>", "高"),
    ],
    "MySQLConnectionsHigh": [
        ("查看当前连接数与上限", "SHOW STATUS LIKE 'Threads_connected'; SHOW VARIABLES LIKE 'max_connections';", "低"),
        ("清理空闲连接或评估上调 max_connections", "SHOW PROCESSLIST", "中"),
    ],
    "RedisDown": [
        ("确认 Redis Pod 与密码/网络", "kubectl -n kube-mall get pod -l app=redis", "低"),
        ("恢复后验证 mall 验证码接口", "kubectl -n kube-mall scale deploy redis --replicas=1", "中"),
    ],
    "Ingress5xxHigh": [
        ("检查后端 Deployment 可用副本", "kubectl -n kube-mall get deploy", "低"),
        ("查看后端 Pod 日志与 Ingress 上游错误", "kubectl -n ingress-nginx logs deploy/ingress-nginx-controller --tail=50", "中"),
    ],
    "MallBackendUnavailable": [
        ("查看 Pod 事件，确认镜像/资源/调度问题", "kubectl -n kube-mall get events --sort-by=.lastTimestamp", "低"),
        ("必要时回滚最近一次发布", "helm -n kube-mall history mall", "高"),
    ],
}

_FALLBACK_PLAN = [
    {"action": "查看告警详情与相关指标，确认现象范围", "command": "", "risk": "低"},
    {"action": "结合最近一次变更排查，必要时先止损再定位", "command": "", "risk": "中"},
]


def rule_fix_plan(name: str, ctx: dict) -> list[dict]:
    Node, ns, pod = ctx.get("node") or "-", ctx.get("namespace") or "kube-mall", ctx.get("pod") or "<pod>"
    rows = RULE_FIXES.get(name)
    if not rows:
        return [{"step": i, "action": a["action"], "command": a["command"], "risk": a["risk"]}
                for i, a in enumerate(_FALLBACK_PLAN, start=1)]
    plan = []
    for i, (action, cmd, risk) in enumerate(rows[:5], start=1):
        plan.append({"step": i, "action": action,
                     "command": cmd.format(node=Node, namespace=ns, pod=pod), "risk": risk})
    return plan


def log_fallback(text: str) -> dict:
    low = text.lower()
    if "too many connections" in low:
        cat, comp = "数据库异常", "MySQL"
    elif "oom" in low or "outofmemory" in low:
        cat, comp = "容器异常", "Pod"
    elif "connection refused" in low or "timeout" in low:
        cat, comp = "网络异常", "下游服务"
    elif "no space left" in low:
        cat, comp = "磁盘异常", "节点磁盘"
    else:
        cat, comp = "应用异常", "未知"
    return {
        "category": cat,
        "severity": "P1" if cat in ("数据库异常", "容器异常") else "P2",
        "patterns": [ln for ln in text.splitlines() if "error" in ln.lower()][:3],
        "component": comp,
        "suggestions": ["结合最近变更排查", "检查相关依赖状态", "持续观察指标趋势"],
    }

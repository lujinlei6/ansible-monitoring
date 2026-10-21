import asyncio
import time
from contextlib import asynccontextmanager, closing

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app

from . import db, k8s, llm, mailer, predict
from .config import settings
from .correlate import build_context
from .metrics import (ALERTS_RECEIVED, DIAGNOSES, DIAGNOSIS_LATENCY,
                      LLM_ERRORS, RISK_GAUGE)
from .schemas import AMWebhook, LogRequest, LogTextRequest

SEM = asyncio.Semaphore(settings.max_concurrent_llm)
scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 每小时第 5 分钟刷新一次预测（滚动窗口，保证面板始终能看到未来 24h 曲线）
    scheduler.add_job(run_predictions, "cron", minute=5)
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="AIOps Intelligent Monitoring Service", lifespan=lifespan)
app.mount("/metrics", make_asgi_app())


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/api/v1/alerts", status_code=202)
async def receive_alerts(payload: AMWebhook, background: BackgroundTasks):
    accepted = 0
    for alert in payload.alerts:
        severity = alert.labels.get("severity", "warning")
        ALERTS_RECEIVED.labels(severity=severity).inc()
        if alert.status == "firing":
            background.add_task(handle_alert, alert.model_dump())
            accepted += 1
        elif alert.status == "resolved":
            # 只更新事件状态，不调用 LLM
            background.add_task(db.save_incident, alert.model_dump())
    return {"accepted": accepted, "receiver": payload.receiver}


async def handle_alert(alert: dict):
    async with SEM:
        fingerprint = alert.get("fingerprint", "")
        if db.recent_diagnosis(fingerprint, settings.dedup_window_minutes):
            return

        incident_id = db.save_incident(alert)
        started = time.time()
        try:
            ctx = await build_context(alert.get("labels", {}))
        except Exception as exc:
            # 取数失败也要给出结果，至少保留告警标签，避免诊断链路中断
            ctx = {
                "alertname": alert["labels"].get("alertname", "Unknown"),
                "labels": alert["labels"],
                "context_error": str(exc),
                "metrics": {}, "pods": [], "deployments": [],
                "events": [], "logs": "",
            }
        result, source, raw = await llm.analyze_alert(ctx)
        latency = int((time.time() - started) * 1000)

        db.save_diagnosis(incident_id, result, source,
                          settings.deepseek_model, latency, raw)
        # 诊断完成后补发一封"根因+修复方案"邮件（失败不影响主链路）
        try:
            mailer.send_diagnosis(alert, {**result, "source": source})
        except Exception:
            pass
        DIAGNOSES.labels(source=source).inc()
        DIAGNOSIS_LATENCY.observe(latency / 1000)
        if source == "rule":
            LLM_ERRORS.inc()
        risk_map = {"P1": 90, "P2": 60, "P3": 30}
        RISK_GAUGE.labels(
            instance=alert["labels"].get("instance", "unknown"),
            namespace=alert["labels"].get("namespace", "-"),
        ).set(risk_map.get(result["risk_level"], 50))


@app.post("/api/v1/analyze/log")
async def analyze_log(req: LogRequest):
    if req.namespace not in settings.namespaces:
        raise HTTPException(400, "namespace not allowed")
    text = k8s.pod_logs(req.namespace, req.pod, req.container, tail=req.lines)
    if not text or text.startswith("[read log failed"):
        # Pod 名不存在/容器未就绪时，列出该命名空间可用 Pod，避免误判为接口故障
        candidates = [p["name"] for p in k8s.list_pods(req.namespace)]
        raise HTTPException(
            404,
            f"读取 Pod {req.pod!r} 日志失败（名称不存在或容器未启动）；"
            f"当前 {req.namespace} 下可用 Pod: {candidates}",
        )
    result = await llm.analyze_logs(text, req.namespace, req.pod)
    return {"namespace": req.namespace, "pod": req.pod, "result": result}


@app.post("/api/v1/analyze/log/text")
async def analyze_log_text(req: LogTextRequest):
    """直接分析一段日志文本（评测/无 Pod 场景用），限制长度防滥用"""
    text = req.text[: settings.max_log_chars]
    if not text.strip():
        raise HTTPException(400, "empty text")
    result = await llm.analyze_logs(text, req.namespace, req.pod)
    return {"namespace": req.namespace, "pod": req.pod, "result": result}


@app.get("/api/v1/diagnoses")
async def list_diagnoses(limit: int = Query(20, le=200)):
    with closing(db.get_conn()) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT d.id, d.created_at, i.alertname, i.severity, i.instance,
                   d.risk_level, d.confidence, d.root_cause, d.source
            FROM diagnoses d JOIN incidents i ON i.id = d.incident_id
            ORDER BY d.id DESC LIMIT %s
        """, (limit,))
        return {"items": cur.fetchall()}


@app.get("/api/v1/diagnoses/{diagnosis_id}")
async def get_diagnosis(diagnosis_id: int):
    with closing(db.get_conn()) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT d.*, i.alertname, i.severity, i.instance, i.node,
                   i.namespace, i.pod, i.labels, i.annotations
            FROM diagnoses d JOIN incidents i ON i.id = d.incident_id
            WHERE d.id = %s
        """, (diagnosis_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, "not found")
    # JSON 字段解析成结构化数据返回（否则是数据库里的字符串）
    for key in ("labels", "annotations", "evidence", "suggestions", "fix_plan"):
        val = row.get(key)
        if isinstance(val, str) and val:
            try:
                import json as _json
                row[key] = _json.loads(val)
            except ValueError:
                pass
    return row


@app.get("/api/v1/incidents")
async def list_incidents(limit: int = Query(50, le=500)):
    with closing(db.get_conn()) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT id, created_at, alertname, severity, status, instance,
                   node, namespace, pod
            FROM incidents ORDER BY id DESC LIMIT %s
        """, (limit,))
        return {"items": cur.fetchall()}


@app.get("/api/v1/predict")
async def predict_api(
    metric: str = Query("cpu", pattern="^(cpu|memory|disk)$"),
    instance: str = Query(...),
    horizon_hours: int = Query(24, ge=1, le=168),
):
    return await predict.predict_metric(metric, instance, horizon_hours)


@app.post("/api/v1/feedback")
async def feedback(diagnosis_id: int, is_correct: bool, comment: str = ""):
    with closing(db.get_conn()) as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO feedback(diagnosis_id, is_correct, comment)
            VALUES (%s,%s,%s)
        """, (diagnosis_id, int(is_correct), comment[:500]))
    return {"ok": True}


async def run_predictions():
    for instance in settings.targets:
        for metric in ("cpu", "memory", "disk"):
            try:
                await predict.predict_metric(metric, instance, 24)
            except Exception:
                continue


@app.get("/")
async def root():
    return JSONResponse({
        "service": "aiops-service",
        "docs": "/docs",
        "health": "/healthz",
        "metrics": "/metrics",
    })

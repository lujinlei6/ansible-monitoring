import json
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pymysql
from pymysql.cursors import DictCursor

from .config import settings


def get_conn():
    return pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        database=settings.mysql_db,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=True,
        connect_timeout=5,
        # 让 CURRENT_TIMESTAMP 也产生 UTC 值（与 parse_time 的 UTC 存储保持一致）
        init_command="SET time_zone='+00:00'",
    )


def save_incident(alert: dict) -> int:
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    starts_at = parse_time(alert["startsAt"])
    sql = """
        INSERT INTO incidents
          (fingerprint, alertname, severity, status, instance, node,
           namespace, pod, labels, annotations, starts_at, ends_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
          status=VALUES(status), ends_at=VALUES(ends_at),
          annotations=VALUES(annotations), updated_at=NOW()
    """
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(sql, (
            alert.get("fingerprint", ""),
            labels.get("alertname", "Unknown"),
            labels.get("severity", "warning"),
            alert.get("status", "firing"),
            labels.get("instance"),
            labels.get("node"),
            labels.get("namespace"),
            labels.get("pod"),
            json.dumps(labels, ensure_ascii=False),
            json.dumps(annotations, ensure_ascii=False),
            starts_at,
            parse_optional_time(alert.get("endsAt")),
        ))
        cur.execute(
            "SELECT id FROM incidents WHERE fingerprint=%s AND starts_at=%s",
            (alert.get("fingerprint", ""), starts_at),
        )
        return cur.fetchone()["id"]


def recent_diagnosis(fingerprint: str, minutes: int) -> dict | None:
    sql = """
        SELECT d.* FROM diagnoses d
        JOIN incidents i ON i.id = d.incident_id
        WHERE i.fingerprint = %s
          AND d.created_at > NOW() - INTERVAL %s MINUTE
        ORDER BY d.id DESC LIMIT 1
    """
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(sql, (fingerprint, minutes))
        return cur.fetchone()


def save_diagnosis(incident_id: int, result: dict, source: str,
                   model: str, latency_ms: int, raw: str = "") -> int:
    sql = """
        INSERT INTO diagnoses
          (incident_id, root_cause, risk_level, confidence, evidence,
           suggestions, fix_plan, raw_response, source, model, latency_ms)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(sql, (
            incident_id,
            result["root_cause"],
            result["risk_level"],
            result["confidence"],
            json.dumps(result.get("evidence", []), ensure_ascii=False),
            json.dumps(result.get("suggestions", []), ensure_ascii=False),
            json.dumps(result.get("fix_plan", []), ensure_ascii=False),
            raw[:60000],
            source,
            model,
            latency_ms,
        ))
        return cur.lastrowid


def save_predictions(rows: list[dict]) -> None:
    if not rows:
        return
    sql = """
        INSERT INTO predictions
          (metric, instance, horizon_hours, target_time, predicted_value,
           lower_bound, upper_bound, model, mae, rmse, mape)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """
    with closing(get_conn()) as conn, conn.cursor() as cur:
        # 同一指标/实例/窗口只保留最近一次预测，避免 Grafana 曲线多次重叠
        for group in {(r["metric"], r["instance"], r["horizon_hours"]) for r in rows}:
            cur.execute(
                "DELETE FROM predictions WHERE metric=%s AND instance=%s AND horizon_hours=%s",
                group,
            )
        cur.executemany(sql, [(
            r["metric"], r["instance"], r["horizon_hours"], r["target_time"],
            r["predicted_value"], r.get("lower_bound"), r.get("upper_bound"),
            r["model"], r.get("mae"), r.get("rmse"), r.get("mape"),
        ) for r in rows])


def parse_time(value: str) -> datetime:
    # Alertmanager 格式：2026-10-19T08:31:20.123Z
    # 统一按 UTC 存储：Grafana 的 MySQL 数据源默认把 time 列当作 UTC 再转浏览器本地时区，
    # 若这里存成本地时间(+08:00)，面板会整体快 8 小时。
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    ).replace(tzinfo=None, microsecond=0)


def parse_optional_time(value: str | None) -> datetime | None:
    # firing 告警的 endsAt 是 0001-01-01T00:00:00Z，直接转时区会溢出
    if not value or value.startswith("0001-01-01"):
        return None
    return parse_time(value)

from prometheus_client import Counter, Histogram, Gauge

ALERTS_RECEIVED = Counter(
    "aiops_alerts_received_total", "Received Alertmanager alerts", ["severity"]
)
DIAGNOSES = Counter(
    "aiops_diagnoses_total", "Completed diagnoses", ["source"]
)
LLM_ERRORS = Counter(
    "aiops_llm_errors_total", "DeepSeek call failures"
)
DIAGNOSIS_LATENCY = Histogram(
    "aiops_diagnosis_duration_seconds", "Diagnosis latency",
    buckets=(1, 2, 5, 10, 20, 30, 60, 90, 120),
)
RISK_GAUGE = Gauge(
    "aiops_risk_score", "Latest risk score", ["instance", "namespace"]
)
PREDICTION_GAUGE = Gauge(
    "aiops_prediction_value", "Predicted value",
    ["metric", "instance", "horizon"],
)

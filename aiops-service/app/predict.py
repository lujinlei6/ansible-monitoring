from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from statsmodels.tsa.arima.model import ARIMA

from . import prom
from .db import save_predictions
from .metrics import PREDICTION_GAUGE

# 兼容垫片：statsmodels 0.14.4 仍向 scipy 的 fmin_l_bfgs_b 传 disp= 参数，
# 而新版 scipy(>=1.15) 已移除该参数，会导致 ARIMA 拟合直接 TypeError 而永远回退线性。
# 这里在进程内把 disp 参数丢掉，保证 ARIMA 能正常拟合。
try:
    import scipy.optimize as _sopt

    if "disp" not in _sopt.fmin_l_bfgs_b.__code__.co_varnames:
        _orig_lbfgsb = _sopt.fmin_l_bfgs_b

        def _lbfgsb_compat(*args, **kwargs):
            kwargs.pop("disp", None)
            return _orig_lbfgsb(*args, **kwargs)

        _sopt.fmin_l_bfgs_b = _lbfgsb_compat
except Exception:  # 兼容垫片失败不应影响服务启动
    pass

METRIC_QUERIES = {
    "cpu": '100 - avg by(instance)(rate(node_cpu_seconds_total{mode="idle",instance="%s"}[5m])) * 100',
    "memory": '100 * (1 - node_memory_MemAvailable_bytes{instance="%s"} / node_memory_MemTotal_bytes{instance="%s"})',
    "disk": '100 * (1 - node_filesystem_avail_bytes{mountpoint="/",instance="%s"} / node_filesystem_size_bytes{mountpoint="/",instance="%s"})',
}

# 优先 1h 粒度；若历史点不足 48，则自动降到 15m / 5m，保证监控刚上线也能出预测
STEP_CANDIDATES = (("1h", 60), ("15m", 15), ("5m", 5))
MIN_POINTS = 48


async def predict_metric(metric: str, instance: str, horizon_hours: int = 24,
                         days: int = 7) -> dict:
    tpl = METRIC_QUERIES[metric]
    expr = tpl % ((instance, instance) if tpl.count("%s") == 2 else (instance,))

    result, freq_minutes = None, 60
    for step, minutes in STEP_CANDIDATES:
        r = await prom.query_range(expr, hours=days * 24, step=step)
        if r and len(r[0].get("values", [])) >= MIN_POINTS:
            result, freq_minutes = r, minutes
            break
    if result is None:
        return {"error": f"历史数据不足（至少需要 {MIN_POINTS} 个点）", "points": []}

    values = result[0]["values"]
    series = pd.Series(
        [float(v[1]) for v in values],
        index=pd.to_datetime([float(v[0]) for v in values], unit="s", utc=True),
    ).replace([np.inf, -np.inf], np.nan).dropna()

    train, test = series[:-6], series[-6:]
    model_name = "arima"
    try:
        fitted = ARIMA(train, order=(2, 1, 2)).fit()
        test_pred = fitted.forecast(steps=6)
    except Exception:
        model_name = "linear"
        fitted = fit_linear(train)
        test_pred = linear_forecast(fitted, train.index, len(test))

    mae, rmse, mape = errors(test.values, np.asarray(test_pred))

    # 预测点数 = 预测小时数换算成当前粒度的步数
    steps = max(1, int(round(horizon_hours * 60 / freq_minutes)))
    try:
        forecast = fitted.forecast(steps=6 + steps)[-steps:]
    except Exception:
        forecast = linear_forecast(fitted, series.index, steps)

    start = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0) + timedelta(minutes=freq_minutes)
    points, rows = [], []
    for i, value in enumerate(forecast, start=1):
        target = start + timedelta(minutes=freq_minutes * (i - 1))
        value = round(float(value), 3)
        point = {
            "target_time": target.strftime("%Y-%m-%d %H:%M:%S"),
            "predicted_value": value,
            "lower_bound": round(value - 2 * rmse, 3),
            "upper_bound": round(value + 2 * rmse, 3),
        }
        points.append(point)
        rows.append({
            "metric": metric, "instance": instance,
            "horizon_hours": horizon_hours,
            "target_time": target, "predicted_value": value,
            "lower_bound": point["lower_bound"],
            "upper_bound": point["upper_bound"],
            "model": model_name, "mae": mae, "rmse": rmse, "mape": mape,
        })

    save_predictions(rows)
    PREDICTION_GAUGE.labels(metric=metric, instance=instance,
                            horizon=str(horizon_hours)).set(points[-1]["predicted_value"])
    return {
        "metric": metric, "instance": instance, "model": model_name,
        "granularity": f"{freq_minutes}m",
        "horizon_hours": horizon_hours,
        "metrics": {"mae": round(mae, 4), "rmse": round(rmse, 4),
                    "mape": round(mape, 2)},
        "warning": capacity_warning(points),
        "points": points,
    }


def fit_linear(series: pd.Series):
    x = np.arange(len(series))
    coef = np.polyfit(x, series.values, 1)
    return coef


def linear_forecast(coef, index, steps: int) -> np.ndarray:
    x0 = len(index)
    return np.polyval(coef, np.arange(x0, x0 + steps))


def errors(actual, predicted) -> tuple[float, float, float]:
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    mae = float(np.mean(np.abs(actual - predicted)))
    rmse = float(np.sqrt(np.mean((actual - predicted) ** 2)))
    mape = float(np.mean(np.abs((actual - predicted) / np.clip(actual, 1e-6, None))) * 100)
    return mae, rmse, mape


def capacity_warning(points: list[dict]) -> str | None:
    top = max(p["predicted_value"] for p in points)
    last = points[-1]["target_time"]
    if top >= 90:
        return f"预计到 {last} 峰值 {top:.1f}%，超过 90%，建议立即扩容"
    if top >= 80:
        return f"预计到 {last} 峰值 {top:.1f}%，超过 80%，建议提前评估容量"
    return None

import time

import httpx

from .config import settings


async def query(expr: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{settings.prometheus_url}/api/v1/query", params={"query": expr}
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "success":
            return []
        return body["data"]["result"]


async def query_range(expr: str, hours: int = 1, step: str = "60s") -> list[dict]:
    # 注意：Prometheus 的 query_range 只接受绝对时间戳（unix 秒或 RFC3339），
    # 不接受 "now" / "168h" 这类相对写法，否则返回 400 bad_data
    end = time.time()
    start = end - hours * 3600
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{settings.prometheus_url}/api/v1/query_range",
            params={"query": expr, "start": start, "end": end, "step": step},
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "success":
            return []
        return body["data"]["result"]


def scalar(result: list[dict]) -> float | None:
    if not result:
        return None
    try:
        return float(result[0]["value"][1])
    except (KeyError, IndexError, ValueError):
        return None


def summarize_range(result: list[dict], max_points: int = 12) -> dict:
    """把一条时间序列压缩成少量点 + 统计值，控制 Prompt 长度。"""
    if not result:
        return {}
    values = result[0].get("values", [])
    nums = [float(v[1]) for v in values if v[1] not in ("NaN", "+Inf", "-Inf")]
    if not nums:
        return {}
    step = max(1, len(nums) // max_points)
    sampled = nums[::step][-max_points:]
    return {
        "current": round(nums[-1], 3),
        "min": round(min(nums), 3),
        "max": round(max(nums), 3),
        "avg": round(sum(nums) / len(nums), 3),
        "samples": [round(x, 3) for x in sampled],
    }

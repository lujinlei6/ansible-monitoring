from pydantic import BaseModel, Field


class AMAlert(BaseModel):
    status: str
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str
    endsAt: str = ""
    generatorURL: str = ""
    fingerprint: str = ""


class AMWebhook(BaseModel):
    version: str = "4"
    status: str = "firing"
    receiver: str = ""
    groupKey: str = ""
    alerts: list[AMAlert] = Field(default_factory=list)


class LogRequest(BaseModel):
    namespace: str
    pod: str
    container: str | None = None
    lines: int = 200


class LogTextRequest(BaseModel):
    """直接分析一段日志文本（评测用，避免依赖真实 Pod）"""
    text: str
    namespace: str = "-"
    pod: str = "-"

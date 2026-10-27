from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Entity(Model):
    id: str
    kind: Literal["service", "pod"]
    name: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class Relation(Model):
    source: str
    target: str
    kind: Literal["contains", "calls"]
    evidence: str


class Query(Model):
    signal: Literal["metrics", "logs", "traces"]
    service: str = Field(min_length=1, max_length=253)
    start: datetime
    end: datetime
    limit: int = Field(default=100, ge=1, le=1000)
    metric: str | None = None
    step_seconds: int = Field(default=15, ge=1, le=3600)
    trace_id: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{16,32}$")

    @model_validator(mode="after")
    def window(self):
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("start/end must include a timezone")
        if not 0 < (self.end - self.start).total_seconds() <= 3600:
            raise ValueError("time window must be positive and at most 3600 seconds")
        if self.end.timestamp() > datetime.now(timezone.utc).timestamp() + 60:
            raise ValueError("end must not be in the future")
        if self.signal == "metrics" and not self.metric:
            raise ValueError("metrics queries require metric")
        if self.signal == "metrics" and (self.end - self.start).total_seconds() / self.step_seconds > 1000:
            raise ValueError("at most 1000 points per series")
        return self


class Observation(Model):
    signal: str
    source: str
    records: list[dict[str, Any]]
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)


class ActionRequest(Model):
    action: str
    service: str = Field(min_length=1, max_length=253)
    parameters: dict[str, Any] = Field(default_factory=dict)
    duration_seconds: int = Field(default=30, ge=1, le=300)
    dry_run: bool = True


class APIError(Exception):
    def __init__(self, code: str, message: str, status: int = 502):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)

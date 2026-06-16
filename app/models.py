from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class FailureCategory(str, Enum):
    TIMEOUT = "TIMEOUT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
    POISON_PILL = "POISON_PILL"
    UNKNOWN = "UNKNOWN"


class DLQMessage(BaseModel):
    message_id: str
    receipt_handle: str
    body: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    received_at: datetime = Field(default_factory=datetime.utcnow)
    retry_count: int = 0
    failure_category: FailureCategory = FailureCategory.UNKNOWN
    raw: dict[str, Any] = Field(default_factory=dict)

    def body_as_dict(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.body)
        except (json.JSONDecodeError, TypeError):
            return None


class RetryResult(BaseModel):
    message_id: str
    success: bool
    attempt: int
    error: str | None = None


class DLQStats(BaseModel):
    queue_url: str
    depth: int = 0
    messages_processed: int = 0
    messages_retried: int = 0
    messages_dead: int = 0
    alerts_sent: int = 0
    last_polled: datetime | None = None


class AlertPayload(BaseModel):
    topic: str
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    message: str
    queue_url: str
    depth: int
    timestamp: datetime = Field(default_factory=datetime.utcnow)

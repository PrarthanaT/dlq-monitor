import asyncio
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

NAMESPACE = "DLQMonitor"


class CloudWatchMetrics:
    def __init__(self, cloudwatch_client: Any) -> None:
        self._cw = cloudwatch_client

    async def put_metric(self, name: str, value: float, unit: str = "Count") -> None:
        def _put() -> None:
            self._cw.put_metric_data(
                Namespace=NAMESPACE,
                MetricData=[{"MetricName": name, "Value": value, "Unit": unit}],
            )

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, _put)
        except Exception as exc:
            logger.error("put_metric_failed", metric=name, error=str(exc))

    async def emit_poll_metrics(self, retried: int, dead: int, alerts: int) -> None:
        await asyncio.gather(
            self.put_metric("messages_retried", retried),
            self.put_metric("messages_dead", dead),
            self.put_metric("alerts_sent", alerts),
        )

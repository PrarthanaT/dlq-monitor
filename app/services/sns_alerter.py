import asyncio
import json
from typing import Any

import structlog

from app.config import Settings
from app.models import AlertPayload, DLQMessage

logger = structlog.get_logger(__name__)


class SNSAlerter:
    def __init__(self, settings: Settings, sns_client: Any) -> None:
        self._settings = settings
        self._sns = sns_client

    async def send_alert(
        self, payload: AlertPayload, correlation_id: str | None = None,
    ) -> bool:
        log = logger.bind(correlation_id=correlation_id) if correlation_id else logger
        subject = f"[DLQ-MONITOR] {payload.severity}: {payload.topic}"
        message_body = json.dumps(payload.model_dump(mode="json"), indent=2, default=str)

        def _publish() -> dict:
            return self._sns.publish(
                TopicArn=self._settings.SNS_TOPIC_ARN,
                Subject=subject[:100],
                Message=message_body,
            )

        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(None, _publish)
            log.info(
                "alert_sent",
                sns_message_id=response.get("MessageId"),
                severity=payload.severity,
                topic=payload.topic,
            )
            return True
        except Exception as exc:
            log.error("alert_failed", error=str(exc), topic=payload.topic)
            return False

    async def alert_high_depth(
        self, queue_url: str, depth: int, correlation_id: str | None = None,
    ) -> bool:
        if depth > 50:
            severity = "CRITICAL"
        elif depth > 20:
            severity = "HIGH"
        elif depth > 10:
            severity = "MEDIUM"
        else:
            severity = "LOW"

        payload = AlertPayload(
            topic="High DLQ Depth Detected",
            severity=severity,
            message=f"DLQ depth is {depth}, exceeding the threshold of {self._settings.ALERT_THRESHOLD}.",
            queue_url=queue_url,
            depth=depth,
        )
        return await self.send_alert(payload, correlation_id=correlation_id)

    async def alert_poison_pill(self, message: DLQMessage) -> bool:
        payload = AlertPayload(
            topic="Poison Pill Message Detected",
            severity="CRITICAL",
            message=(
                f"Message {message.message_id} has been received more than 5 times "
                f"and has been classified as a poison pill. Manual intervention required."
            ),
            queue_url=self._settings.DLQ_URL,
            depth=0,
        )
        return await self.send_alert(
            payload, correlation_id=message.correlation_id,
        )

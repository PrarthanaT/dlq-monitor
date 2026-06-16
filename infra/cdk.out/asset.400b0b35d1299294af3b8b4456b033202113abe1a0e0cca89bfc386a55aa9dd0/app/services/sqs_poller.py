import asyncio
from datetime import datetime
from typing import Any

import structlog

from app.config import Settings
from app.models import DLQMessage, DLQStats, FailureCategory
from app.services.classifier import FailureClassifier
from app.services.retry_engine import RetryEngine
from app.services.sns_alerter import SNSAlerter

logger = structlog.get_logger(__name__)


class SQSPoller:
    def __init__(
        self,
        settings: Settings,
        sqs_client: Any,
        classifier: FailureClassifier,
        retry_engine: RetryEngine,
        alerter: SNSAlerter,
        stats: DLQStats,
    ) -> None:
        self._settings = settings
        self._sqs = sqs_client
        self._classifier = classifier
        self._retry_engine = retry_engine
        self._alerter = alerter
        self._stats = stats
        self._running = False

    async def get_queue_depth(self) -> int:
        def _get() -> int:
            resp = self._sqs.get_queue_attributes(
                QueueUrl=self._settings.DLQ_URL,
                AttributeNames=["ApproximateNumberOfMessages"],
            )
            return int(resp["Attributes"].get("ApproximateNumberOfMessages", "0"))

        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, _get)
        except Exception as exc:
            logger.error("get_queue_depth_failed", error=str(exc))
            return 0

    async def poll_once(self) -> list[DLQMessage]:
        def _receive() -> list[dict]:
            resp = self._sqs.receive_message(
                QueueUrl=self._settings.DLQ_URL,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=5,
                AttributeNames=["All"],
                MessageAttributeNames=["All"],
            )
            return resp.get("Messages", [])

        loop = asyncio.get_event_loop()
        try:
            raw_messages = await loop.run_in_executor(None, _receive)
        except Exception as exc:
            logger.error("poll_failed", error=str(exc))
            return []

        depth = await self.get_queue_depth()
        self._stats.depth = depth
        self._stats.last_polled = datetime.utcnow()

        if depth > self._settings.ALERT_THRESHOLD:
            await self._alerter.alert_high_depth(self._settings.DLQ_URL, depth)
            self._stats.alerts_sent += 1

        messages: list[DLQMessage] = []
        for raw in raw_messages:
            msg = DLQMessage(
                message_id=raw["MessageId"],
                receipt_handle=raw["ReceiptHandle"],
                body=raw.get("Body", ""),
                attributes=raw.get("Attributes", {}),
                raw=raw,
            )
            msg.failure_category = self._classifier.classify(msg)
            self._stats.messages_processed += 1

            if msg.failure_category == FailureCategory.POISON_PILL:
                await self._alerter.alert_poison_pill(msg)
                self._stats.alerts_sent += 1

            messages.append(msg)
            logger.info(
                "message_classified",
                message_id=msg.message_id,
                category=msg.failure_category,
            )

        return messages

    async def delete_message(self, message: DLQMessage) -> None:
        def _delete() -> None:
            self._sqs.delete_message(
                QueueUrl=self._settings.DLQ_URL,
                ReceiptHandle=message.receipt_handle,
            )

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, _delete)
            logger.info("message_deleted", message_id=message.message_id)
        except Exception as exc:
            logger.error("delete_failed", message_id=message.message_id, error=str(exc))

    async def start_polling(self) -> None:
        self._running = True
        logger.info("polling_started", dlq_url=self._settings.DLQ_URL)
        while self._running:
            try:
                messages = await self.poll_once()
                if messages:
                    results = await self._retry_engine.retry_batch(messages)
                    for result, msg in zip(results, messages):
                        if result.success:
                            await self.delete_message(msg)
                            self._stats.messages_retried += 1
                        elif not self._classifier.is_retryable(msg.failure_category):
                            await self.delete_message(msg)
                            self._stats.messages_dead += 1
            except asyncio.CancelledError:
                logger.info("polling_cancelled")
                break
            except Exception as exc:
                logger.error("polling_error", error=str(exc))
            await asyncio.sleep(self._settings.POLL_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._running = False

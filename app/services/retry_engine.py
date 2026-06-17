import asyncio
from typing import Any

import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.config import Settings
from app.models import DLQMessage, RetryResult
from app.services.classifier import FailureClassifier

logger = structlog.get_logger(__name__)


class RetryEngine:
    def __init__(self, settings: Settings, sqs_client: Any, classifier: FailureClassifier) -> None:
        self._settings = settings
        self._sqs = sqs_client
        self._classifier = classifier

    def _handle_permanent_failure(self, message: DLQMessage) -> RetryResult:
        log = logger.bind(
            correlation_id=message.correlation_id,
            message_id=message.message_id,
        )
        log.error(
            "permanent_failure_dead_lettered",
            category=message.failure_category,
            body_preview=message.body[:200],
        )
        return RetryResult(
            message_id=message.message_id,
            correlation_id=message.correlation_id,
            success=False,
            attempt=0,
            error=f"Permanent failure: {message.failure_category.value}",
        )

    async def retry_message(self, message: DLQMessage) -> RetryResult:
        if not self._classifier.is_retryable(message.failure_category):
            return self._handle_permanent_failure(message)

        log = logger.bind(
            correlation_id=message.correlation_id,
            message_id=message.message_id,
        )
        attempt = 0

        @retry(
            stop=stop_after_attempt(self._settings.MAX_RETRY_ATTEMPTS),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        )
        def _send() -> None:
            nonlocal attempt
            attempt += 1
            self._sqs.send_message(
                QueueUrl=self._settings.SOURCE_QUEUE_URL,
                MessageBody=message.body,
                MessageAttributes={
                    "RetryCount": {
                        "DataType": "Number",
                        "StringValue": str(message.retry_count + 1),
                    },
                    "OriginalMessageId": {
                        "DataType": "String",
                        "StringValue": message.message_id,
                    },
                    "FailureCategory": {
                        "DataType": "String",
                        "StringValue": message.failure_category.value,
                    },
                },
            )

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, _send)
            log.info("retry_success", attempt=attempt)
            return RetryResult(
                message_id=message.message_id,
                correlation_id=message.correlation_id,
                success=True,
                attempt=attempt,
            )
        except Exception as exc:
            log.error("retry_failed", attempt=attempt, error=str(exc))
            return RetryResult(
                message_id=message.message_id,
                correlation_id=message.correlation_id,
                success=False,
                attempt=attempt,
                error=str(exc),
            )

    async def retry_batch(self, messages: list[DLQMessage]) -> list[RetryResult]:
        tasks = [self.retry_message(msg) for msg in messages]
        return await asyncio.gather(*tasks)

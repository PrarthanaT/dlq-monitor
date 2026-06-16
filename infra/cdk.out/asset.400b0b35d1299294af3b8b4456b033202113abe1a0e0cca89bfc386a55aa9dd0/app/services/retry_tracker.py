import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class RetryTracker:
    def __init__(self, dynamodb_client: Any, table_name: str) -> None:
        self._ddb = dynamodb_client
        self._table = table_name

    async def record_attempt(self, message_id: str, category: str, success: bool) -> int:
        now = datetime.now(timezone.utc).isoformat()

        def _update() -> int:
            resp = self._ddb.update_item(
                TableName=self._table,
                Key={"message_id": {"S": message_id}},
                UpdateExpression=(
                    "SET retry_count = if_not_exists(retry_count, :zero) + :inc, "
                    "last_retry_at = :now, failure_category = :cat, "
                    "#s = :status, created_at = if_not_exists(created_at, :now)"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":zero": {"N": "0"},
                    ":inc": {"N": "1"},
                    ":now": {"S": now},
                    ":cat": {"S": category},
                    ":status": {"S": "succeeded" if success else "retrying"},
                },
                ReturnValues="ALL_NEW",
            )
            return int(resp["Attributes"]["retry_count"]["N"])

        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, _update)
        except Exception as exc:
            logger.error("retry_tracker_failed", message_id=message_id, error=str(exc))
            return 0

    async def mark_dead(self, message_id: str, category: str) -> None:
        now = datetime.now(timezone.utc).isoformat()

        def _update() -> None:
            self._ddb.update_item(
                TableName=self._table,
                Key={"message_id": {"S": message_id}},
                UpdateExpression=(
                    "SET #s = :status, failure_category = :cat, "
                    "last_retry_at = :now, "
                    "created_at = if_not_exists(created_at, :now)"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":status": {"S": "dead"},
                    ":cat": {"S": category},
                    ":now": {"S": now},
                },
            )

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, _update)
        except Exception as exc:
            logger.error("mark_dead_failed", message_id=message_id, error=str(exc))

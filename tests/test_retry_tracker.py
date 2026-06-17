import pytest

from app.services.retry_tracker import RetryTracker


@pytest.fixture()
def tracker(dynamodb_client):
    return RetryTracker(dynamodb_client=dynamodb_client, table_name="dlq-retry-tracking")


def _get_item(dynamodb_client, message_id):
    resp = dynamodb_client.get_item(
        TableName="dlq-retry-tracking",
        Key={"message_id": {"S": message_id}},
    )
    return resp.get("Item")


# ── record_attempt ───────────────────────────────────────────────


class TestRecordAttempt:
    @pytest.mark.asyncio
    async def test_first_attempt_returns_1(self, tracker):
        count = await tracker.record_attempt("msg-new", "TIMEOUT", False)
        assert count == 1

    @pytest.mark.asyncio
    async def test_increments_on_each_call(self, tracker):
        await tracker.record_attempt("msg-inc", "TIMEOUT", False)
        await tracker.record_attempt("msg-inc", "TIMEOUT", False)
        count = await tracker.record_attempt("msg-inc", "TIMEOUT", False)
        assert count == 3

    @pytest.mark.asyncio
    async def test_success_sets_succeeded_status(self, tracker, dynamodb_client):
        await tracker.record_attempt("msg-ok", "TIMEOUT", True)
        item = _get_item(dynamodb_client, "msg-ok")
        assert item["status"]["S"] == "succeeded"

    @pytest.mark.asyncio
    async def test_failure_sets_retrying_status(self, tracker, dynamodb_client):
        await tracker.record_attempt("msg-fail", "TIMEOUT", False)
        item = _get_item(dynamodb_client, "msg-fail")
        assert item["status"]["S"] == "retrying"

    @pytest.mark.asyncio
    async def test_stores_category(self, tracker, dynamodb_client):
        await tracker.record_attempt("msg-cat", "DEPENDENCY_FAILURE", False)
        item = _get_item(dynamodb_client, "msg-cat")
        assert item["failure_category"]["S"] == "DEPENDENCY_FAILURE"

    @pytest.mark.asyncio
    async def test_sets_created_at_once(self, tracker, dynamodb_client):
        await tracker.record_attempt("msg-ts", "TIMEOUT", False)
        item1 = _get_item(dynamodb_client, "msg-ts")
        created1 = item1["created_at"]["S"]

        await tracker.record_attempt("msg-ts", "TIMEOUT", False)
        item2 = _get_item(dynamodb_client, "msg-ts")
        assert item2["created_at"]["S"] == created1

    @pytest.mark.asyncio
    async def test_updates_last_retry_at(self, tracker, dynamodb_client):
        await tracker.record_attempt("msg-lr", "TIMEOUT", False)
        item1 = _get_item(dynamodb_client, "msg-lr")

        await tracker.record_attempt("msg-lr", "TIMEOUT", False)
        item2 = _get_item(dynamodb_client, "msg-lr")
        assert item2["last_retry_at"]["S"] >= item1["last_retry_at"]["S"]


# ── Max retry threshold detection ────────────────────────────────


class TestThresholdDetection:
    @pytest.mark.asyncio
    async def test_count_tracks_toward_threshold(self, tracker):
        for _ in range(3):
            count = await tracker.record_attempt("msg-thr", "TIMEOUT", False)
        assert count == 3

    @pytest.mark.asyncio
    async def test_can_detect_max_retries_exceeded(self, tracker):
        max_retries = 3
        for _ in range(max_retries + 1):
            count = await tracker.record_attempt("msg-exc", "TIMEOUT", False)
        assert count > max_retries

    @pytest.mark.asyncio
    async def test_separate_messages_track_independently(self, tracker):
        await tracker.record_attempt("msg-a", "TIMEOUT", False)
        await tracker.record_attempt("msg-a", "TIMEOUT", False)
        count_b = await tracker.record_attempt("msg-b", "TIMEOUT", False)
        assert count_b == 1


# ── mark_dead ────────────────────────────────────────────────────


class TestMarkDead:
    @pytest.mark.asyncio
    async def test_mark_dead_sets_status(self, tracker, dynamodb_client):
        await tracker.record_attempt("msg-dead", "VALIDATION_ERROR", False)
        await tracker.mark_dead("msg-dead", "VALIDATION_ERROR")
        item = _get_item(dynamodb_client, "msg-dead")
        assert item["status"]["S"] == "dead"

    @pytest.mark.asyncio
    async def test_mark_dead_preserves_retry_count(self, tracker, dynamodb_client):
        await tracker.record_attempt("msg-d2", "TIMEOUT", False)
        await tracker.record_attempt("msg-d2", "TIMEOUT", False)
        await tracker.mark_dead("msg-d2", "TIMEOUT")
        item = _get_item(dynamodb_client, "msg-d2")
        assert int(item["retry_count"]["N"]) == 2

    @pytest.mark.asyncio
    async def test_mark_dead_new_message(self, tracker, dynamodb_client):
        await tracker.mark_dead("msg-fresh", "POISON_PILL")
        item = _get_item(dynamodb_client, "msg-fresh")
        assert item["status"]["S"] == "dead"
        assert item["failure_category"]["S"] == "POISON_PILL"


# ── Error handling ───────────────────────────────────────────────


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_record_attempt_returns_0_on_error(self):
        from unittest.mock import MagicMock
        bad_client = MagicMock()
        bad_client.update_item.side_effect = Exception("DynamoDB down")
        tracker = RetryTracker(dynamodb_client=bad_client, table_name="nonexistent")

        count = await tracker.record_attempt("msg-err", "TIMEOUT", False)
        assert count == 0

    @pytest.mark.asyncio
    async def test_mark_dead_does_not_raise_on_error(self):
        from unittest.mock import MagicMock
        bad_client = MagicMock()
        bad_client.update_item.side_effect = Exception("DynamoDB down")
        tracker = RetryTracker(dynamodb_client=bad_client, table_name="nonexistent")

        await tracker.mark_dead("msg-err", "TIMEOUT")

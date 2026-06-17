from unittest.mock import MagicMock

import pytest

from app.models import DLQMessage, FailureCategory
from app.services.classifier import FailureClassifier
from app.services.retry_engine import RetryEngine


def _msg(body="test", category=FailureCategory.TIMEOUT, retry_count=0):
    return DLQMessage(
        message_id="msg-001",
        receipt_handle="rh-001",
        body=body,
        failure_category=category,
        retry_count=retry_count,
    )


# ── Transient messages get re-queued ─────────────────────────────


class TestTransientRetry:
    @pytest.mark.asyncio
    async def test_timeout_message_is_retried(self, retry_engine, sqs_client, sqs_queues):
        _, source_url = sqs_queues
        msg = _msg(category=FailureCategory.TIMEOUT)

        result = await retry_engine.retry_message(msg)

        assert result.success is True
        assert result.attempt == 1
        resp = sqs_client.receive_message(QueueUrl=source_url, MaxNumberOfMessages=1)
        assert len(resp.get("Messages", [])) == 1
        assert resp["Messages"][0]["Body"] == "test"

    @pytest.mark.asyncio
    async def test_dependency_failure_is_retried(self, retry_engine, sqs_client, sqs_queues):
        _, source_url = sqs_queues
        msg = _msg(category=FailureCategory.DEPENDENCY_FAILURE)

        result = await retry_engine.retry_message(msg)

        assert result.success is True
        received = sqs_client.receive_message(QueueUrl=source_url, MaxNumberOfMessages=1)
        assert len(received["Messages"]) == 1

    @pytest.mark.asyncio
    async def test_unknown_is_retried(self, retry_engine, sqs_client, sqs_queues):
        _, source_url = sqs_queues
        msg = _msg(category=FailureCategory.UNKNOWN)

        result = await retry_engine.retry_message(msg)

        assert result.success is True

    @pytest.mark.asyncio
    async def test_retry_sets_message_attributes(self, retry_engine, sqs_client, sqs_queues):
        _, source_url = sqs_queues
        msg = _msg(category=FailureCategory.TIMEOUT, retry_count=2)

        await retry_engine.retry_message(msg)

        resp = sqs_client.receive_message(
            QueueUrl=source_url,
            MaxNumberOfMessages=1,
            MessageAttributeNames=["All"],
        )
        attrs = resp["Messages"][0]["MessageAttributes"]
        assert attrs["RetryCount"]["StringValue"] == "3"
        assert attrs["OriginalMessageId"]["StringValue"] == "msg-001"
        assert attrs["FailureCategory"]["StringValue"] == "TIMEOUT"


# ── Permanent messages never get re-queued ───────────────────────


class TestPermanentFailure:
    @pytest.mark.asyncio
    async def test_validation_error_not_retried(self, retry_engine, sqs_client, sqs_queues):
        _, source_url = sqs_queues
        msg = _msg(category=FailureCategory.VALIDATION_ERROR)

        result = await retry_engine.retry_message(msg)

        assert result.success is False
        assert result.attempt == 0
        assert "Permanent failure" in result.error
        resp = sqs_client.receive_message(QueueUrl=source_url, MaxNumberOfMessages=1)
        assert resp.get("Messages") is None

    @pytest.mark.asyncio
    async def test_poison_pill_not_retried(self, retry_engine, sqs_client, sqs_queues):
        _, source_url = sqs_queues
        msg = _msg(category=FailureCategory.POISON_PILL)

        result = await retry_engine.retry_message(msg)

        assert result.success is False
        assert result.attempt == 0
        assert "POISON_PILL" in result.error

    @pytest.mark.asyncio
    async def test_permanent_failure_error_includes_category(self, retry_engine):
        msg = _msg(category=FailureCategory.VALIDATION_ERROR)
        result = await retry_engine.retry_message(msg)
        assert "VALIDATION_ERROR" in result.error


# ── Send failures exhaust retries ────────────────────────────────


class TestRetryExhaustion:
    @pytest.mark.asyncio
    async def test_all_attempts_fail_returns_failure(self, settings):
        sqs_mock = MagicMock()
        sqs_mock.send_message.side_effect = Exception("network error")
        engine = RetryEngine(
            settings=settings,
            sqs_client=sqs_mock,
            classifier=FailureClassifier(),
        )
        msg = _msg(category=FailureCategory.TIMEOUT)

        result = await engine.retry_message(msg)

        assert result.success is False
        assert result.attempt == settings.MAX_RETRY_ATTEMPTS
        assert "network error" in result.error

    @pytest.mark.asyncio
    async def test_attempt_count_matches_max_retries(self, settings):
        sqs_mock = MagicMock()
        sqs_mock.send_message.side_effect = Exception("fail")
        settings.MAX_RETRY_ATTEMPTS = 2
        engine = RetryEngine(
            settings=settings,
            sqs_client=sqs_mock,
            classifier=FailureClassifier(),
        )
        msg = _msg(category=FailureCategory.TIMEOUT)

        result = await engine.retry_message(msg)

        assert result.attempt == 2
        assert sqs_mock.send_message.call_count == 2


# ── Retry succeeds on later attempt ─────────────────────────────


class TestPartialFailure:
    @pytest.mark.asyncio
    async def test_succeeds_on_second_attempt(self, settings):
        sqs_mock = MagicMock()
        sqs_mock.send_message.side_effect = [Exception("transient"), None]
        engine = RetryEngine(
            settings=settings,
            sqs_client=sqs_mock,
            classifier=FailureClassifier(),
        )
        msg = _msg(category=FailureCategory.TIMEOUT)

        result = await engine.retry_message(msg)

        assert result.success is True
        assert result.attempt == 2

    @pytest.mark.asyncio
    async def test_succeeds_on_third_attempt(self, settings):
        sqs_mock = MagicMock()
        sqs_mock.send_message.side_effect = [
            Exception("err1"),
            Exception("err2"),
            None,
        ]
        engine = RetryEngine(
            settings=settings,
            sqs_client=sqs_mock,
            classifier=FailureClassifier(),
        )
        msg = _msg(category=FailureCategory.TIMEOUT)

        result = await engine.retry_message(msg)

        assert result.success is True
        assert result.attempt == 3


# ── Batch processing ─────────────────────────────────────────────


class TestBatch:
    @pytest.mark.asyncio
    async def test_retry_batch_processes_all(self, retry_engine, sqs_client, sqs_queues):
        _, source_url = sqs_queues
        messages = [
            _msg(body="msg1", category=FailureCategory.TIMEOUT),
            _msg(body="msg2", category=FailureCategory.VALIDATION_ERROR),
            _msg(body="msg3", category=FailureCategory.DEPENDENCY_FAILURE),
        ]

        results = await retry_engine.retry_batch(messages)

        assert len(results) == 3
        assert results[0].success is True
        assert results[1].success is False
        assert results[2].success is True


# ── Exponential backoff timing ───────────────────────────────────


class TestBackoffTiming:
    @pytest.mark.asyncio
    async def test_backoff_delays_increase(self, settings):
        call_times = []
        call_count = 0

        def _failing_send(**kwargs):
            nonlocal call_count
            call_count += 1
            import time
            call_times.append(time.monotonic())
            raise Exception("fail")

        sqs_mock = MagicMock()
        sqs_mock.send_message.side_effect = _failing_send
        settings.MAX_RETRY_ATTEMPTS = 3
        engine = RetryEngine(
            settings=settings,
            sqs_client=sqs_mock,
            classifier=FailureClassifier(),
        )
        msg = _msg(category=FailureCategory.TIMEOUT)

        await engine.retry_message(msg)

        assert len(call_times) == 3
        gap1 = call_times[1] - call_times[0]
        gap2 = call_times[2] - call_times[1]
        assert gap2 >= gap1 * 0.8  # second gap at least ~as long as first (exponential)

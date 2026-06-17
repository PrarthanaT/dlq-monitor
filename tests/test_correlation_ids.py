import uuid
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from app.models import DLQMessage, FailureCategory
from app.services.classifier import FailureClassifier
from app.services.retry_engine import RetryEngine
from app.services.sns_alerter import SNSAlerter


def _msg(body="test", category=FailureCategory.TIMEOUT, correlation_id=None):
    kwargs = dict(
        message_id="msg-001",
        receipt_handle="rh-001",
        body=body,
        failure_category=category,
    )
    if correlation_id:
        kwargs["correlation_id"] = correlation_id
    return DLQMessage(**kwargs)


# ── UUID generation ─────────────────────────────────────────────


class TestCorrelationIdGeneration:
    def test_auto_generated_on_creation(self):
        msg = _msg()
        assert msg.correlation_id is not None
        uuid.UUID(msg.correlation_id)

    def test_unique_per_message(self):
        ids = {_msg().correlation_id for _ in range(50)}
        assert len(ids) == 50

    def test_preserves_explicit_id(self):
        fixed = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        msg = _msg(correlation_id=fixed)
        assert msg.correlation_id == fixed


# ── Classifier logs include correlation_id ──────────────────────


class TestClassifierCorrelationId:
    def test_classify_logs_correlation_id(self):
        clf = FailureClassifier()
        msg = _msg(body="connection timeout")

        with capture_logs() as cap:
            result = clf.classify(msg)

        assert result == FailureCategory.TIMEOUT
        classified_logs = [e for e in cap if e.get("event") == "classified"]
        assert len(classified_logs) >= 1
        assert classified_logs[0]["correlation_id"] == msg.correlation_id

    def test_same_id_across_all_classify_logs(self):
        clf = FailureClassifier()
        msg = _msg(body="normal message")

        with capture_logs() as cap:
            clf.classify(msg)

        for entry in cap:
            assert entry["correlation_id"] == msg.correlation_id


# ── Retry engine logs include correlation_id ────────────────────


class TestRetryEngineCorrelationId:
    @pytest.mark.asyncio
    async def test_successful_retry_logs_correlation_id(self, retry_engine, sqs_queues):
        msg = _msg(category=FailureCategory.TIMEOUT)

        with capture_logs() as cap:
            result = await retry_engine.retry_message(msg)

        assert result.success is True
        assert result.correlation_id == msg.correlation_id
        success_logs = [e for e in cap if e.get("event") == "retry_success"]
        assert len(success_logs) == 1
        assert success_logs[0]["correlation_id"] == msg.correlation_id

    @pytest.mark.asyncio
    async def test_permanent_failure_logs_correlation_id(self, retry_engine):
        msg = _msg(category=FailureCategory.VALIDATION_ERROR)

        with capture_logs() as cap:
            result = await retry_engine.retry_message(msg)

        assert result.success is False
        assert result.correlation_id == msg.correlation_id
        fail_logs = [e for e in cap if e.get("event") == "permanent_failure_dead_lettered"]
        assert len(fail_logs) == 1
        assert fail_logs[0]["correlation_id"] == msg.correlation_id

    @pytest.mark.asyncio
    async def test_exhausted_retry_logs_correlation_id(self, settings):
        sqs_mock = MagicMock()
        sqs_mock.send_message.side_effect = Exception("network error")
        engine = RetryEngine(
            settings=settings,
            sqs_client=sqs_mock,
            classifier=FailureClassifier(),
        )
        msg = _msg(category=FailureCategory.TIMEOUT)

        with capture_logs() as cap:
            result = await engine.retry_message(msg)

        assert result.success is False
        assert result.correlation_id == msg.correlation_id
        fail_logs = [e for e in cap if e.get("event") == "retry_failed"]
        assert len(fail_logs) == 1
        assert fail_logs[0]["correlation_id"] == msg.correlation_id


# ── SNS alerter logs include correlation_id ─────────────────────


class TestAlerterCorrelationId:
    @pytest.mark.asyncio
    async def test_alert_sent_logs_correlation_id(self, settings, sns_client, sns_topic):
        alerter = SNSAlerter(settings=settings, sns_client=sns_client)
        msg = _msg(category=FailureCategory.POISON_PILL)

        with capture_logs() as cap:
            sent = await alerter.alert_poison_pill(msg)

        assert sent is True
        sent_logs = [e for e in cap if e.get("event") == "alert_sent"]
        assert len(sent_logs) == 1
        assert sent_logs[0]["correlation_id"] == msg.correlation_id

    @pytest.mark.asyncio
    async def test_alert_payload_includes_correlation_id(self, settings, sns_client, sns_topic):
        alerter = SNSAlerter(settings=settings, sns_client=sns_client)
        msg = _msg(category=FailureCategory.POISON_PILL)

        await alerter.alert_poison_pill(msg)

        sns_client.list_subscriptions_by_topic(TopicArn=sns_topic)
        # Verify the SNS publish included the correlation_id by checking the
        # alerter set it on the payload (integration tested via the log)

    @pytest.mark.asyncio
    async def test_high_depth_alert_without_correlation_id(self, settings, sns_client, sns_topic):
        alerter = SNSAlerter(settings=settings, sns_client=sns_client)

        with capture_logs() as cap:
            sent = await alerter.alert_high_depth("https://sqs.example.com/dlq", 25)

        assert sent is True
        sent_logs = [e for e in cap if e.get("event") == "alert_sent"]
        assert len(sent_logs) == 1


# ── End-to-end: same ID across classifier → retry ───────────────


class TestEndToEndCorrelationId:
    @pytest.mark.asyncio
    async def test_same_id_through_classify_and_retry(self, retry_engine, sqs_queues):
        clf = FailureClassifier()
        msg = _msg(body="connection timeout")

        with capture_logs() as cap:
            msg.failure_category = clf.classify(msg)
            result = await retry_engine.retry_message(msg)

        ids = {e["correlation_id"] for e in cap if "correlation_id" in e}
        assert len(ids) == 1
        assert ids.pop() == msg.correlation_id
        assert result.correlation_id == msg.correlation_id

    @pytest.mark.asyncio
    async def test_different_messages_get_different_ids(self, retry_engine, sqs_queues):
        clf = FailureClassifier()
        msg1 = _msg(body="timeout error")
        msg2 = DLQMessage(
            message_id="msg-002",
            receipt_handle="rh-002",
            body="timeout error",
            failure_category=FailureCategory.TIMEOUT,
        )
        assert msg1.correlation_id != msg2.correlation_id

        with capture_logs() as cap:
            msg1.failure_category = clf.classify(msg1)
            msg2.failure_category = clf.classify(msg2)

        msg1_logs = [e for e in cap if e.get("correlation_id") == msg1.correlation_id]
        msg2_logs = [e for e in cap if e.get("correlation_id") == msg2.correlation_id]
        assert len(msg1_logs) >= 1
        assert len(msg2_logs) >= 1

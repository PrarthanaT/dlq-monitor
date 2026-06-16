import asyncio

from app.config import get_settings
from app.core.aws_client import (
    get_cloudwatch_client,
    get_dynamodb_client,
    get_sns_client,
    get_sqs_client,
)
from app.core.metrics import CloudWatchMetrics
from app.models import DLQStats
from app.services.classifier import FailureClassifier
from app.services.retry_engine import RetryEngine
from app.services.retry_tracker import RetryTracker
from app.services.sns_alerter import SNSAlerter
from app.services.sqs_poller import SQSPoller

settings = get_settings()
sqs_client = get_sqs_client(settings)
sns_client = get_sns_client(settings)
ddb_client = get_dynamodb_client(settings)
cw_client = get_cloudwatch_client(settings)

classifier = FailureClassifier()
alerter = SNSAlerter(settings=settings, sns_client=sns_client)
retry_engine = RetryEngine(settings=settings, sqs_client=sqs_client, classifier=classifier)
tracker = RetryTracker(dynamodb_client=ddb_client, table_name=settings.DYNAMODB_TABLE_NAME)
metrics = CloudWatchMetrics(cloudwatch_client=cw_client)


def handler(event, context):
    stats = DLQStats(queue_url=settings.DLQ_URL)
    poller = SQSPoller(
        settings=settings,
        sqs_client=sqs_client,
        classifier=classifier,
        retry_engine=retry_engine,
        alerter=alerter,
        stats=stats,
    )

    async def _run():
        messages = await poller.poll_once()
        if messages:
            results = await retry_engine.retry_batch(messages)
            for result, msg in zip(results, messages):
                if result.success:
                    await poller.delete_message(msg)
                    await tracker.record_attempt(
                        msg.message_id, msg.failure_category.value, True
                    )
                    stats.messages_retried += 1
                elif not classifier.is_retryable(msg.failure_category):
                    await poller.delete_message(msg)
                    await tracker.mark_dead(
                        msg.message_id, msg.failure_category.value
                    )
                    stats.messages_dead += 1
                else:
                    await tracker.record_attempt(
                        msg.message_id, msg.failure_category.value, False
                    )

        await metrics.emit_poll_metrics(
            retried=stats.messages_retried,
            dead=stats.messages_dead,
            alerts=stats.alerts_sent,
        )

        return {
            "processed": stats.messages_processed,
            "retried": stats.messages_retried,
            "dead": stats.messages_dead,
            "alerts": stats.alerts_sent,
        }

    return asyncio.get_event_loop().run_until_complete(_run())

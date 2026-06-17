import boto3
import pytest
from moto import mock_aws

from app.config import Settings
from app.services.classifier import FailureClassifier
from app.services.retry_engine import RetryEngine

AWS_REGION = "us-east-1"
TABLE_NAME = "dlq-retry-tracking"


@pytest.fixture()
def settings(sqs_queues, sns_topic):
    dlq_url, source_url = sqs_queues
    return Settings(
        AWS_REGION=AWS_REGION,
        DLQ_URL=dlq_url,
        SOURCE_QUEUE_URL=source_url,
        SNS_TOPIC_ARN=sns_topic,
        DYNAMODB_TABLE_NAME=TABLE_NAME,
        MAX_RETRY_ATTEMPTS=3,
        ALERT_THRESHOLD=5,
    )


@pytest.fixture()
def aws(request):
    with mock_aws():
        yield


@pytest.fixture()
def sqs_client(aws):
    return boto3.client("sqs", region_name=AWS_REGION)


@pytest.fixture()
def sqs_queues(sqs_client):
    dlq = sqs_client.create_queue(QueueName="test-dlq")["QueueUrl"]
    source = sqs_client.create_queue(QueueName="test-source")["QueueUrl"]
    return dlq, source


@pytest.fixture()
def dynamodb_client(aws):
    client = boto3.client("dynamodb", region_name=AWS_REGION)
    client.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "message_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "message_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return client


@pytest.fixture()
def sns_client(aws):
    return boto3.client("sns", region_name=AWS_REGION)


@pytest.fixture()
def sns_topic(sns_client):
    resp = sns_client.create_topic(Name="test-alerts")
    return resp["TopicArn"]


@pytest.fixture()
def classifier():
    return FailureClassifier()


@pytest.fixture()
def retry_engine(settings, sqs_client):
    return RetryEngine(
        settings=settings,
        sqs_client=sqs_client,
        classifier=FailureClassifier(),
    )

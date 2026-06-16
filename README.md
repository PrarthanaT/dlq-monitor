# DLQ Monitor

Automated dead letter queue triage: classifies SQS failures, retries transient errors with exponential backoff, and alerts on anomalies.

**[Live Demo](https://be7vs42u6g.execute-api.us-east-1.amazonaws.com/docs)**

## Architecture

```
┌──────────────┐         ┌──────────────────────────────────────────────────────────────┐
│ EventBridge  │         │                    DLQ Monitor                               │
│  (1 min)     │────────▶│  ┌─────────────┐    ┌──────────────┐    ┌────────────────┐   │
└──────────────┘         │  │   Poller     │───▶│  Classifier  │───▶│  Retry Engine  │   │
                         │  │   Lambda     │    │              │    │  (exp backoff) │   │
┌──────────────┐         │  └──────┬───────┘    └──────┬───────┘    └───┬────────┬───┘   │
│  API Gateway │────────▶│         │                   │                │        │       │
│  (HTTP API)  │         │  ┌──────▼───────┐           │         ┌──────▼──┐  ┌──▼────┐  │
└──────────────┘         │  │   FastAPI    │           │         │ Source  │  │ DynamoDB│ │
        ▲                │  │   Handlers   │           │         │ Queue   │  │Tracking│  │
        │                │  └──────────────┘           │         └─────────┘  └────────┘  │
   end users             │                      ┌──────▼───────┐                          │
                         │                      │  SNS Alerts  │                          │
                         │                      │  (depth /    │                          │
                         │                      │  poison pill)│                          │
                         │                      └──────────────┘                          │
                         └──────────────────────────────────────────────────────────────┘

         ┌──────────────────────────────────────────────────────────────┐
         │  CloudWatch Dashboard                                       │
         │  messages_retried  ·  messages_dead  ·  alerts_sent         │
         └──────────────────────────────────────────────────────────────┘

         ┌──────────┐     redrive policy      ┌──────────────┐
         │ SQS DLQ  │◀───── (maxReceiveCount=3) ──────│ Source Queue  │
         └──────────┘                          └──────────────┘
```

## Failure Classification

| Category | Retryable | Trigger |
|---|---|---|
| `TIMEOUT` | Yes | `timeout`, `timed out`, `timed_out` in body |
| `DEPENDENCY_FAILURE` | Yes | `connection refused`, `unavailable`, `503` in body |
| `UNKNOWN` | Yes | No pattern matched |
| `VALIDATION_ERROR` | No | `invalid`, `malformed`, `400`, `401`, `422`, etc. |
| `POISON_PILL` | No | `ApproximateReceiveCount` > 5 |

Transient failures retry with exponential backoff. Permanent failures get deleted and tracked in DynamoDB.

## Tech Stack

| Component | Technology |
|---|---|
| API | FastAPI + Mangum (Lambda adapter) |
| Compute | AWS Lambda (Python 3.12) |
| Gateway | API Gateway HTTP API |
| Queue | SQS (source + DLQ with redrive) |
| Storage | DynamoDB (retry tracking) |
| Alerts | SNS |
| Scheduler | EventBridge (1-minute poller) |
| Observability | CloudWatch dashboard + custom metrics |
| Retry logic | Tenacity (exponential backoff) |
| IaC | AWS CDK (Python) |
| Local dev | LocalStack + Docker Compose |

## Local Development

```bash
cp .env.example .env
docker compose up --build
```

LocalStack provisions the SQS queues, DLQ, SNS topic, and redrive policy automatically. API is at `http://localhost:8000`.

Seed test messages:

```bash
# Transient failure — will be retried
aws --endpoint-url=http://localhost:4566 sqs send-message \
  --queue-url http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/my-service-dlq \
  --message-body '{"error": "connection refused", "service": "payments-api"}'

# Permanent failure — will be dead-lettered
aws --endpoint-url=http://localhost:4566 sqs send-message \
  --queue-url http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/my-service-dlq \
  --message-body '{"error": "invalid schema: missing required field user_id"}'
```

## API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `GET` | `/stats` | Queue depth + processed/retried/dead counts |
| `GET` | `/messages` | Poll DLQ, classify, return messages |
| `GET` | `/messages?retry=true` | Poll, classify, auto-retry transient failures |
| `POST` | `/poll` | Trigger a one-off poll cycle |
| `POST` | `/retry/{message_id}` | Retry a specific message |
| `DELETE` | `/messages/{message_id}` | Delete a message from the DLQ |

### Examples

```bash
# Health check
curl https://be7vs42u6g.execute-api.us-east-1.amazonaws.com/health
# {"status":"ok"}

# Queue stats
curl https://be7vs42u6g.execute-api.us-east-1.amazonaws.com/stats
# {
#   "queue_url": "https://sqs.us-east-1.amazonaws.com/123456789/my-service-dlq",
#   "depth": 12,
#   "messages_processed": 47,
#   "messages_retried": 31,
#   "messages_dead": 8,
#   "alerts_sent": 3,
#   "last_polled": "2026-06-16T04:32:10.123456"
# }

# Poll and classify (no retry)
curl https://be7vs42u6g.execute-api.us-east-1.amazonaws.com/messages

# Poll, classify, and auto-retry transient failures
curl "https://be7vs42u6g.execute-api.us-east-1.amazonaws.com/messages?retry=true"

# Retry a specific message
curl -X POST https://be7vs42u6g.execute-api.us-east-1.amazonaws.com/retry/MSG_ID \
  -H "Content-Type: application/json" \
  -d '{"receipt_handle": "AQEBwJnKyrHigUMZj6rYigCgxlaS3...", "body": "{\"error\": \"connection refused\"}", "attributes": {}}'

# Delete a message
curl -X DELETE https://be7vs42u6g.execute-api.us-east-1.amazonaws.com/messages/MSG_ID \
  -H "Content-Type: application/json" \
  -d '{"receipt_handle": "AQEBwJnKyrHigUMZj6rYigCgxlaS3..."}'
```

## Deploy to AWS

Requires: AWS CDK CLI, Docker, Python 3.12+, configured AWS credentials.

```bash
cd infra
pip install -r requirements.txt
cdk bootstrap   # first time only
cdk deploy
```

CDK outputs the API Gateway URL on completion. The stack creates:

- SQS source queue + DLQ with redrive policy (maxReceiveCount=3)
- DynamoDB table for retry tracking (pay-per-request)
- SNS topic for alerts
- Two Lambda functions (API handler + poller) with least-privilege IAM
- API Gateway HTTP API with CORS
- EventBridge rule triggering the poller every 1 minute
- CloudWatch dashboard with `messages_retried`, `messages_dead`, `alerts_sent`

## Configuration

All configuration via environment variables (see `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `AWS_REGION` | `us-east-1` | AWS region |
| `AWS_ENDPOINT_URL` | — | LocalStack endpoint (omit for real AWS) |
| `DLQ_URL` | — | SQS DLQ URL to monitor |
| `SOURCE_QUEUE_URL` | — | Source queue URL for retries |
| `SNS_TOPIC_ARN` | — | SNS topic ARN for alerts |
| `DYNAMODB_TABLE_NAME` | `dlq-retry-tracking` | DynamoDB table for retry state |
| `POLL_INTERVAL_SECONDS` | `30` | Seconds between poll cycles (local dev) |
| `MAX_RETRY_ATTEMPTS` | `3` | Max retries per message |
| `ALERT_THRESHOLD` | `5` | Queue depth that triggers an alert |

## License

MIT

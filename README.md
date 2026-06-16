# DLQ Monitor

An automated Dead Letter Queue monitoring service that classifies failures, retries transient errors with exponential backoff, and alerts on queue depth anomalies. Built for teams running async workloads on AWS SQS who are tired of manually triaging DLQ messages at 2 AM.

## Why This Exists

Every production SQS consumer eventually drops messages into a DLQ. The default response is a CloudWatch alarm that says "you have 47 messages" — with zero context on whether they're retryable timeouts or permanently broken payloads. Engineers waste time manually inspecting messages, guessing at root causes, and copy-pasting `aws sqs` commands.

DLQ Monitor replaces that workflow. It continuously polls the DLQ, classifies each message into a failure category (timeout, validation error, dependency failure, poison pill), automatically retries transient failures back to the source queue, and fires SNS alerts when things go sideways. Permanent failures are surfaced and removed — not silently retried in a loop.

## Architecture

```
                          ┌──────────────────────────────────────────────────┐
                          │              DLQ Monitor Service                 │
                          │                                                  │
┌──────────┐  poll every  │  ┌───────────┐    ┌────────────────┐             │
│          │  N seconds   │  │           │    │    Failure      │             │
│ SQS DLQ  │─────────────►│  │  Poller   │───►│   Classifier   │             │
│          │              │  │           │    │                │             │
└──────────┘              │  └───────────┘    └───────┬────────┘             │
                          │                           │                      │
                          │              ┌────────────┼────────────┐         │
                          │              │            │            │         │
                          │         transient     permanent    poison        │
                          │              │            │         pill         │
                          │              ▼            ▼            │         │
                          │  ┌───────────────┐  ┌──────────┐      │         │
                          │  │ Retry Engine   │  │  Dead    │      │         │
                          │  │ (exp backoff)  │  │ Lettered │      │         │
                          │  └───────┬───────┘  └──────────┘      │         │
                          │          │                             │         │
                          └──────────┼─────────────────────────────┼─────────┘
                                     │                             │
                                     ▼                             ▼
                          ┌──────────────┐              ┌──────────────────┐
                          │ Source Queue  │              │   SNS Alerts     │
                          │  (re-enqueue)│              │ (depth/poison)   │
                          └──────────────┘              └──────────────────┘
```

## Tech Stack

| Layer          | Technology                          |
|----------------|-------------------------------------|
| Runtime        | Python 3.12                         |
| Framework      | FastAPI + Uvicorn                   |
| AWS SDK        | Boto3 (SQS, SNS)                   |
| Retry Logic    | Tenacity (exponential backoff)      |
| Data Models    | Pydantic v2                         |
| Configuration  | pydantic-settings + `.env`          |
| Logging        | structlog (structured JSON)         |
| Local AWS      | LocalStack 3.4                      |
| Containerization | Docker + Docker Compose           |

## Failure Classification

The classifier inspects message bodies, error fields, and SQS attributes to categorize failures:

| Category             | Retryable | Trigger Conditions                                          |
|----------------------|-----------|-------------------------------------------------------------|
| `TIMEOUT`            | Yes       | Body contains `timeout`, `timed out`, `timed_out`           |
| `DEPENDENCY_FAILURE` | Yes       | Body contains `connection refused`, `unavailable`, `503`    |
| `UNKNOWN`            | Yes       | No pattern matched — retried as a precaution                |
| `VALIDATION_ERROR`   | No        | Body contains `invalid`, `malformed`, `400`, `401`, `422`, etc. |
| `POISON_PILL`        | No        | `ApproximateReceiveCount` exceeds 5                         |

Transient failures are retried with exponential backoff. Permanent failures are logged, alerted on, and removed from the queue.

## Getting Started

### Prerequisites

- Docker and Docker Compose
- (Optional) `curl` or `httpx` for testing the API

### Run Locally

```bash
# Clone the repository
git clone https://github.com/<your-org>/dlq-monitor.git
cd dlq-monitor

# Copy environment config
cp .env.example .env

# Start everything (LocalStack + DLQ Monitor)
docker compose up --build
```

LocalStack automatically provisions the SQS queues, DLQ, SNS topic, and redrive policy on startup. The API is available at `http://localhost:8000` once both containers are healthy.

### Seed Test Messages

Push sample messages into the DLQ to see classification in action:

```bash
# Transient failure (will be retried)
aws --endpoint-url=http://localhost:4566 sqs send-message \
  --queue-url http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/my-service-dlq \
  --message-body '{"error": "connection refused", "service": "payments-api"}'

# Permanent failure (will be dead-lettered)
aws --endpoint-url=http://localhost:4566 sqs send-message \
  --queue-url http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/my-service-dlq \
  --message-body '{"error": "invalid schema: missing required field user_id"}'
```

## API Reference

| Method   | Endpoint                | Description                              |
|----------|-------------------------|------------------------------------------|
| `GET`    | `/health`               | Liveness check                           |
| `GET`    | `/stats`                | Queue depth, processed/retried/dead counts, alert count |
| `GET`    | `/messages`             | Poll DLQ and return classified messages  |
| `GET`    | `/messages?retry=true`  | Poll, classify, and auto-retry transient failures |
| `POST`   | `/poll`                 | Trigger a one-off poll cycle             |
| `POST`   | `/retry/{message_id}`   | Retry a specific message                 |
| `DELETE` | `/messages/{message_id}`| Delete a message from the DLQ            |

## Example Usage

### Check service health

```bash
curl http://localhost:8000/health
```
```json
{"status": "ok"}
```

### View queue stats

```bash
curl http://localhost:8000/stats
```
```json
{
  "queue_url": "http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/my-service-dlq",
  "depth": 12,
  "messages_processed": 47,
  "messages_retried": 31,
  "messages_dead": 8,
  "alerts_sent": 3,
  "last_polled": "2026-06-16T04:32:10.123456"
}
```

### Poll and classify messages (no retry)

```bash
curl http://localhost:8000/messages
```
```json
[
  {
    "message_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "body": "{\"error\": \"connection refused\", \"service\": \"payments-api\"}",
    "failure_category": "DEPENDENCY_FAILURE",
    "retry_count": 0
  }
]
```

### Poll, classify, and auto-retry transient failures

```bash
curl "http://localhost:8000/messages?retry=true"
```

Transient messages (`TIMEOUT`, `DEPENDENCY_FAILURE`, `UNKNOWN`) are re-enqueued to the source queue with exponential backoff. Permanent failures (`VALIDATION_ERROR`, `POISON_PILL`) are left untouched.

### Retry a specific message

```bash
curl -X POST http://localhost:8000/retry/a1b2c3d4-e5f6-7890-abcd-ef1234567890 \
  -H "Content-Type: application/json" \
  -d '{
    "receipt_handle": "AQEBwJnKyrHigUMZj6rYigCgxlaS3...",
    "body": "{\"error\": \"connection refused\"}",
    "attributes": {}
  }'
```
```json
{
  "message_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "success": true,
  "attempt": 1,
  "error": null
}
```

### Delete a message (manual triage)

```bash
curl -X DELETE http://localhost:8000/messages/a1b2c3d4-e5f6-7890-abcd-ef1234567890 \
  -H "Content-Type: application/json" \
  -d '{"receipt_handle": "AQEBwJnKyrHigUMZj6rYigCgxlaS3..."}'
```
```json
{"status": "deleted", "message_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"}
```

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable                | Default     | Description                                      |
|-------------------------|-------------|--------------------------------------------------|
| `AWS_REGION`            | `us-east-1` | AWS region                                      |
| `AWS_ENDPOINT_URL`      | —           | LocalStack endpoint (omit for real AWS)          |
| `DLQ_URL`               | —           | SQS DLQ URL to monitor                          |
| `SOURCE_QUEUE_URL`      | —           | Source queue URL for retries                     |
| `SNS_TOPIC_ARN`         | —           | SNS topic for alerts                             |
| `POLL_INTERVAL_SECONDS` | `30`        | Seconds between poll cycles                      |
| `MAX_RETRY_ATTEMPTS`    | `3`         | Max retries per message (exponential backoff)    |
| `ALERT_THRESHOLD`       | `5`         | Queue depth that triggers an SNS alert           |

## Live Demo

> [**Live Demo**](#) — *coming soon*

## License

MIT

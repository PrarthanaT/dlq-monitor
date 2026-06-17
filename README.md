# DLQ Monitor

![Tests](https://img.shields.io/badge/tests-75%20passing-brightgreen)
![Coverage](https://img.shields.io/badge/coverage-97%25-brightgreen)
![Python](https://img.shields.io/badge/python-3.12-blue)
[![AWS CDK](https://img.shields.io/badge/AWS-CDK%20deployed-orange)](https://be7vs42u6g.execute-api.us-east-1.amazonaws.com/docs)

Automated dead-letter queue triage: classifies SQS failures, retries transient errors with exponential backoff, dead-letters permanent failures, and alerts on anomalies.

**[Live Frontend](https://dlq-monitor-frontend.vercel.app)** · **[API Docs](https://be7vs42u6g.execute-api.us-east-1.amazonaws.com/docs)**

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                                AWS                                         │
│                                                                             │
│  ┌──────────────┐    ┌─────────────┐    ┌────────────┐    ┌─────────────┐  │
│  │ EventBridge  │───▶│   Poller    │───▶│ Classifier │───▶│Retry Engine │  │
│  │  (1 min)     │    │   Lambda    │    │            │    │(exp backoff)│  │
│  └──────────────┘    └──────┬──────┘    └─────┬──────┘    └──┬──────┬───┘  │
│                             │                 │              │      │      │
│                        ┌────▼────┐       ┌────▼─────┐  ┌────▼──┐ ┌─▼────┐ │
│                        │ SQS DLQ │       │   SNS    │  │Source │ │Dynamo│ │
│                        │         │       │  Alerts  │  │Queue  │ │  DB  │ │
│                        └─────────┘       └──────────┘  └───────┘ └──────┘ │
│                                                                           │
│  ┌──────────────┐    ┌─────────────┐    ┌────────────────────────────────┐ │
│  │ API Gateway  │───▶│  API Lambda │───▶│ FastAPI                        │ │
│  │  (HTTP API)  │    │             │    │ /inject /poll /stats /health   │ │
│  └──────┬───────┘    └─────────────┘    └────────────────────────────────┘ │
│         │                                                                  │
└─────────┼──────────────────────────────────────────────────────────────────┘
          │
          │  HTTPS
          │
┌─────────▼──────────┐
│   React Frontend   │
│     (Vercel)       │
└────────────────────┘
```

## Tech Stack

| Backend | Frontend |
|---|---|
| Python 3.12, FastAPI, Mangum | React 18, Vite |
| AWS Lambda (API + Poller) | Tailwind CSS |
| SQS (source queue + DLQ) | Recharts |
| DynamoDB (retry tracking) | Vercel (hosting) |
| SNS (alerts) | |
| EventBridge (scheduler) | |
| CloudWatch (metrics dashboard) | |
| AWS CDK (IaC) | |

## How It Works

- **Poll & Classify**: Every minute, EventBridge triggers the Poller Lambda to pull messages from the DLQ. Each message is classified as transient (timeout, dependency failure) or permanent (validation error, poison pill) based on its content.
- **Retry or Dead-Letter**: Transient failures are retried back to the source queue with exponential backoff (up to 3 attempts). Permanent failures are deleted from the DLQ and tracked in DynamoDB.
- **Alert & Observe**: SNS alerts fire when queue depth exceeds a threshold or a poison pill is detected. The React frontend shows live stats, metrics charts, and lets you inject test failures for triage.

## Failure Classification

| Error Type | Classification | Action |
|---|---|---|
| Connection Timeout | TRANSIENT | Auto-retried with exponential backoff |
| Dependency Failure | TRANSIENT | Auto-retried with exponential backoff |
| Unknown | TRANSIENT | Auto-retried with exponential backoff |
| Validation Error | PERMANENT | Dead-lettered + tracked in DynamoDB |
| Poison Pill (5+ receives) | PERMANENT | Dead-lettered immediately + SNS alert |

## API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `GET` | `/stats` | Queue depth + processed/retried/dead counts |
| `POST` | `/inject` | Inject a test failure into the DLQ |
| `POST` | `/poll` | Trigger a one-off poll cycle |
| `GET` | `/messages` | Poll DLQ, classify, return messages |
| `GET` | `/messages?retry=true` | Poll, classify, auto-retry transient failures |
| `POST` | `/retry/{message_id}` | Retry a specific message |
| `DELETE` | `/messages/{message_id}` | Delete a message from the DLQ |

## Local Development

```bash
cp .env.example .env
docker compose up --build
```

LocalStack provisions SQS queues, DLQ, SNS topic, and redrive policy automatically. API runs at `http://localhost:8000`.

```bash
# Frontend
cd frontend
npm install
npm run dev
```

## Deploy

```bash
# Backend (requires AWS CDK CLI, Docker, Python 3.12+, configured AWS credentials)
cd infra
pip install -r requirements.txt
cdk bootstrap   # first time only
cdk deploy

# Frontend
cd frontend
npm run build
# Deploy dist/ to Vercel, Netlify, S3, or any static host
```

## Configuration

All configuration via environment variables:

| Variable | Default | Description |
|---|---|---|
| `DLQ_URL` | — | SQS DLQ URL to monitor |
| `SOURCE_QUEUE_URL` | — | Source queue URL for retries |
| `SNS_TOPIC_ARN` | — | SNS topic ARN for alerts |
| `DYNAMODB_TABLE_NAME` | `dlq-retry-tracking` | DynamoDB table name |
| `MAX_RETRY_ATTEMPTS` | `3` | Max retries per message |
| `ALERT_THRESHOLD` | `5` | Queue depth that triggers an alert |
| `AWS_ENDPOINT_URL` | — | Set for LocalStack; omit for real AWS |

## Design Decisions

**Why CDK over SAM?**
CDK lets us define infrastructure in Python — the same language as the application — so the stack is a single codebase with shared constants, no YAML drift, and full IDE support. It also gives us L2 constructs (e.g. `DeadLetterQueue`, `HttpApi`) that handle IAM wiring and defaults that SAM requires you to spell out manually.

**Why DynamoDB for retry tracking vs SQS message attributes?**
SQS message attributes are lost when a message is deleted, so there's no durable record of what happened. DynamoDB gives us a queryable audit trail per message — retry count, timestamps, failure category, final status — that survives the message lifecycle and supports dashboarding, debugging, and compliance.

**Why FastAPI over plain Lambda handlers?**
FastAPI gives us automatic request validation, OpenAPI docs at `/docs`, dependency injection, and CORS middleware — all of which we'd have to hand-roll with raw Lambda handlers. Mangum adapts it to Lambda with zero overhead, so we get the full framework without paying for a long-running server.

**Why rules-based classifier vs ML?**
The failure taxonomy is small and well-defined (timeouts, validation errors, dependency failures, poison pills). A rules-based classifier is deterministic, testable with simple unit tests, and has zero cold-start cost. An ML model would add training infrastructure, inference latency, and a data pipeline — unjustified complexity for a classification problem with ~5 categories and clear keyword signals.

**Why EventBridge polling vs SQS triggers?**
SQS Lambda triggers would re-process DLQ messages immediately, which defeats the purpose of a dead-letter queue — the downstream service likely hasn't recovered yet. A 1-minute EventBridge schedule gives transient failures time to resolve, lets us batch messages for efficient processing, and gives us explicit control over retry timing and backoff without fighting the SQS trigger's own retry behavior.

## License

MIT

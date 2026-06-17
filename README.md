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

## Design Decisions

**Why CDK over SAM?**
SAM is template-driven YAML, which means your infrastructure definition and your application code live in different languages with different toolchains. CDK lets us write the stack in Python — the same language as the Lambda handlers — so constants like queue names and table names are shared references, not copy-pasted strings that drift. The L2 constructs (`DeadLetterQueue`, `HttpApi`) also handle the IAM policy wiring that SAM makes you spell out by hand, which matters when you have two Lambdas with different least-privilege policies talking to five services.

**Why DynamoDB for retry tracking vs SQS message attributes?**
SQS message attributes ride with the message and vanish when it's deleted — there's no way to query "show me everything that failed in the last hour" or build a dashboard over historical retry behavior. DynamoDB gives us a durable, queryable record per message (retry count, timestamps, failure category, terminal status) that outlives the SQS message lifecycle. The tradeoff is an extra AWS service to manage, but with on-demand billing and TTL-based cleanup, the operational cost is near zero for our volume.

**Why FastAPI over plain Lambda handlers?**
Raw Lambda handlers mean hand-rolling request validation, CORS, error serialization, and API documentation for every endpoint. FastAPI gives us all of that for free — including OpenAPI docs at `/docs` that double as a testing UI — and Mangum adapts the ASGI app to Lambda's event format with no measurable latency overhead. The tradeoff is a slightly larger deployment package (~2MB), but we gain the ability to run the same app locally with `uvicorn` without any Lambda emulation, which makes the local dev loop significantly faster.

**Why rules-based classifier vs ML?**
The failure taxonomy has five categories with unambiguous keyword signals: "timeout," "connection refused," HTTP 4xx codes, receive-count thresholds. A rules-based classifier is deterministic, fully unit-testable, and adds zero cold-start latency. ML would require a labeled training set we don't have, a model serving layer, and ongoing retraining — infrastructure that only pays off when the classification boundary is fuzzy. If the taxonomy grows beyond ~10 categories or we start seeing ambiguous failures, that's when ML earns its complexity.

**Why EventBridge polling vs SQS Lambda trigger?**
An SQS Lambda trigger would invoke on every message arrival, which is exactly the wrong behavior for a DLQ — the downstream service just failed, and immediate reprocessing will almost certainly fail again. The 1-minute EventBridge schedule introduces deliberate delay, giving transient issues time to resolve before we attempt retries with exponential backoff. It also means we control the batch size and retry cadence explicitly, instead of fighting the SQS trigger's own visibility-timeout-based retry loop that would compete with our backoff logic.

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

## License

MIT

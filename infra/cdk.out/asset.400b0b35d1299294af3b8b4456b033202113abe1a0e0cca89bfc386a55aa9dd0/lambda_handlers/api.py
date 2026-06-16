from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum

from app.api.routes import router
from app.config import get_settings
from app.core.aws_client import get_sns_client, get_sqs_client
from app.models import DLQStats
from app.services.classifier import FailureClassifier
from app.services.retry_engine import RetryEngine
from app.services.sns_alerter import SNSAlerter
from app.services.sqs_poller import SQSPoller

settings = get_settings()
sqs_client = get_sqs_client(settings)
sns_client = get_sns_client(settings)

stats = DLQStats(queue_url=settings.DLQ_URL)
classifier = FailureClassifier()
alerter = SNSAlerter(settings=settings, sns_client=sns_client)
retry_engine = RetryEngine(settings=settings, sqs_client=sqs_client, classifier=classifier)
poller = SQSPoller(
    settings=settings,
    sqs_client=sqs_client,
    classifier=classifier,
    retry_engine=retry_engine,
    alerter=alerter,
    stats=stats,
)

app = FastAPI(title="DLQ Monitor", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)

app.state.settings = settings
app.state.poller = poller
app.state.retry_engine = retry_engine
app.state.alerter = alerter
app.state.stats = stats

handler = Mangum(app, lifespan="off")

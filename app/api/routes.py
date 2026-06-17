import asyncio
import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.models import DLQMessage, DLQStats, FailureCategory, RetryResult

router = APIRouter()


class InjectRequest(BaseModel):
    body: str
    error_type: str


def get_poller(request: Request):
    return request.app.state.poller


def get_retry_engine(request: Request):
    return request.app.state.retry_engine


def get_stats(request: Request) -> DLQStats:
    return request.app.state.stats


def get_settings(request: Request):
    return request.app.state.settings


def get_sqs_client(request: Request):
    return request.app.state.sqs_client


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/stats", response_model=DLQStats)
async def get_stats_endpoint(stats: DLQStats = Depends(get_stats)) -> DLQStats:
    return stats


@router.get("/messages", response_model=list[DLQMessage])
async def list_messages(
    retry: bool = False,
    poller=Depends(get_poller),
    retry_engine=Depends(get_retry_engine),
    stats: DLQStats = Depends(get_stats),
) -> list[DLQMessage]:
    messages = await poller.poll_once()
    if retry and messages:
        results = await retry_engine.retry_batch(messages)
        for result, msg in zip(results, messages):
            if result.success:
                await poller.delete_message(msg)
                stats.messages_retried += 1
    return messages


@router.post("/retry/{message_id}", response_model=RetryResult)
async def retry_message(
    message_id: str,
    body: dict[str, Any],
    retry_engine=Depends(get_retry_engine),
) -> RetryResult:
    receipt_handle = body.get("receipt_handle")
    if not receipt_handle:
        raise HTTPException(status_code=422, detail="receipt_handle is required")

    msg = DLQMessage(
        message_id=message_id,
        receipt_handle=receipt_handle,
        body=body.get("body", ""),
        attributes=body.get("attributes", {}),
    )
    return await retry_engine.retry_message(msg)


@router.delete("/messages/{message_id}")
async def delete_message(
    message_id: str,
    body: dict[str, Any],
    poller=Depends(get_poller),
) -> dict[str, str]:
    receipt_handle = body.get("receipt_handle")
    if not receipt_handle:
        raise HTTPException(status_code=422, detail="receipt_handle is required")

    msg = DLQMessage(
        message_id=message_id,
        receipt_handle=receipt_handle,
        body="",
    )
    await poller.delete_message(msg)
    return {"status": "deleted", "message_id": message_id}


@router.post("/poll", response_model=list[DLQMessage])
async def trigger_poll(poller=Depends(get_poller)) -> list[DLQMessage]:
    return await poller.poll_once()


ERROR_TYPE_MAP = {
    "Connection Timeout": ("timeout", FailureCategory.TIMEOUT),
    "Validation Error": ("validation error: invalid payload", FailureCategory.VALIDATION_ERROR),
    "Dependency Failure": ("connection refused: service unavailable", FailureCategory.DEPENDENCY_FAILURE),
    "Unknown": ("unexpected error occurred", FailureCategory.UNKNOWN),
}


@router.post("/inject")
async def inject_failure(
    req: InjectRequest,
    settings=Depends(get_settings),
    sqs_client=Depends(get_sqs_client),
    poller=Depends(get_poller),
    retry_engine=Depends(get_retry_engine),
    stats: DLQStats = Depends(get_stats),
):
    error_snippet, category = ERROR_TYPE_MAP.get(
        req.error_type, ("unexpected error occurred", FailureCategory.UNKNOWN)
    )

    try:
        payload = json.loads(req.body)
    except (json.JSONDecodeError, TypeError):
        payload = {"raw": req.body}
    payload["error"] = error_snippet

    loop = asyncio.get_event_loop()
    send_resp = await loop.run_in_executor(
        None,
        lambda: sqs_client.send_message(
            QueueUrl=settings.DLQ_URL,
            MessageBody=json.dumps(payload),
        ),
    )
    injected_id = send_resp["MessageId"]

    await asyncio.sleep(1)

    messages = await poller.poll_once()

    action = "IGNORED"
    classification = category.value
    matched_msg = None
    for msg in messages:
        if msg.message_id == injected_id:
            matched_msg = msg
            classification = msg.failure_category.value
            break

    if not matched_msg and messages:
        matched_msg = messages[0]
        classification = matched_msg.failure_category.value

    if matched_msg:
        results = await retry_engine.retry_batch([matched_msg])
        result = results[0]
        if result.success:
            await poller.delete_message(matched_msg)
            stats.messages_retried += 1
            action = "RETRIED"
        elif not poller._classifier.is_retryable(matched_msg.failure_category):
            await poller.delete_message(matched_msg)
            stats.messages_dead += 1
            action = "DEAD"
        else:
            action = "RETRY_FAILED"

    return {
        "message_id": injected_id,
        "error_type": req.error_type,
        "classification": classification,
        "action": action,
        "body": payload,
    }

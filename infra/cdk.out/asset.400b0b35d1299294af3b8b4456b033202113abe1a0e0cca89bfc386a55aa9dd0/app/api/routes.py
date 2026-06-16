from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.models import DLQMessage, DLQStats, RetryResult

router = APIRouter()


def get_poller(request: Request):
    return request.app.state.poller


def get_retry_engine(request: Request):
    return request.app.state.retry_engine


def get_stats(request: Request) -> DLQStats:
    return request.app.state.stats


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

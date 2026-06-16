from typing import Any

import boto3

from app.config import Settings


def _client_kwargs(settings: Settings) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"region_name": settings.AWS_REGION}
    if settings.AWS_ENDPOINT_URL:
        kwargs["endpoint_url"] = settings.AWS_ENDPOINT_URL
    if settings.AWS_ACCESS_KEY_ID:
        kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
    if settings.AWS_SECRET_ACCESS_KEY:
        kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
    return kwargs


def get_sqs_client(settings: Settings) -> Any:
    return boto3.client("sqs", **_client_kwargs(settings))


def get_sns_client(settings: Settings) -> Any:
    return boto3.client("sns", **_client_kwargs(settings))


def get_dynamodb_client(settings: Settings) -> Any:
    return boto3.client("dynamodb", **_client_kwargs(settings))


def get_cloudwatch_client(settings: Settings) -> Any:
    return boto3.client("cloudwatch", **_client_kwargs(settings))

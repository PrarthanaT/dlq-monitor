from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    AWS_REGION: str = "us-east-1"
    AWS_ENDPOINT_URL: str = "http://localstack:4566"
    AWS_ACCESS_KEY_ID: str = "test"
    AWS_SECRET_ACCESS_KEY: str = "test"

    DLQ_URL: str = "http://localstack:4566/000000000000/my-service-dlq"
    SOURCE_QUEUE_URL: str = "http://localstack:4566/000000000000/my-service-queue"
    SNS_TOPIC_ARN: str = "arn:aws:sns:us-east-1:000000000000:dlq-alerts"

    POLL_INTERVAL_SECONDS: int = 30
    MAX_RETRY_ATTEMPTS: int = 3
    ALERT_THRESHOLD: int = 5

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


def get_settings() -> Settings:
    return Settings()

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    AWS_REGION: str = "us-east-1"
    AWS_ENDPOINT_URL: str = ""
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""

    DLQ_URL: str = ""
    SOURCE_QUEUE_URL: str = ""
    SNS_TOPIC_ARN: str = ""
    DYNAMODB_TABLE_NAME: str = "dlq-retry-tracking"

    POLL_INTERVAL_SECONDS: int = 30
    MAX_RETRY_ATTEMPTS: int = 3
    ALERT_THRESHOLD: int = 5

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


def get_settings() -> Settings:
    return Settings()

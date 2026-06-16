import structlog

from app.models import DLQMessage, FailureCategory

logger = structlog.get_logger(__name__)

_RETRYABLE = {FailureCategory.TIMEOUT, FailureCategory.DEPENDENCY_FAILURE, FailureCategory.UNKNOWN}
_NON_RETRYABLE = {FailureCategory.POISON_PILL, FailureCategory.VALIDATION_ERROR}


class FailureClassifier:
    def classify(self, message: DLQMessage) -> FailureCategory:
        body_lower = message.body.lower()

        receive_count = int(message.attributes.get("ApproximateReceiveCount", "0"))
        if receive_count > 5:
            logger.info("classified_poison_pill", message_id=message.message_id, receive_count=receive_count)
            return FailureCategory.POISON_PILL

        if any(kw in body_lower for kw in ("timeout", "timed out", "timed_out")):
            return FailureCategory.TIMEOUT

        if any(kw in body_lower for kw in ("validation", "schema", "invalid", "malformed",
                                            "bad request", "unauthorized", "forbidden",
                                            "not found", "unprocessable",
                                            " 400", " 401", " 403", " 404", " 409", " 422")):
            return FailureCategory.VALIDATION_ERROR

        if any(kw in body_lower for kw in ("connection refused", "unavailable", "503", "service unavailable")):
            return FailureCategory.DEPENDENCY_FAILURE

        parsed = message.body_as_dict()
        if parsed:
            error_str = str(parsed.get("error", "") or parsed.get("errorMessage", "") or "").lower()
            if any(kw in error_str for kw in ("timeout", "timed out")):
                return FailureCategory.TIMEOUT
            if any(kw in error_str for kw in ("validation", "schema", "invalid", "malformed",
                                               "bad request", "unauthorized", "forbidden",
                                               "not found", "unprocessable")):
                return FailureCategory.VALIDATION_ERROR
            if any(kw in error_str for kw in ("connection refused", "unavailable", "503")):
                return FailureCategory.DEPENDENCY_FAILURE

        return FailureCategory.UNKNOWN

    def is_retryable(self, category: FailureCategory) -> bool:
        return category in _RETRYABLE

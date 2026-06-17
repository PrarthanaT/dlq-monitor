import json

import pytest

from app.models import DLQMessage, FailureCategory
from app.services.classifier import FailureClassifier


def _msg(body="", attributes=None):
    return DLQMessage(
        message_id="test-id",
        receipt_handle="test-handle",
        body=body,
        attributes=attributes or {},
    )


@pytest.fixture()
def clf():
    return FailureClassifier()


# ── Poison pill (receive count > 5) ─────────────────────────────


class TestPoisonPill:
    def test_receive_count_6(self, clf):
        msg = _msg("normal message", {"ApproximateReceiveCount": "6"})
        assert clf.classify(msg) == FailureCategory.POISON_PILL

    def test_receive_count_100(self, clf):
        msg = _msg("timeout", {"ApproximateReceiveCount": "100"})
        assert clf.classify(msg) == FailureCategory.POISON_PILL

    def test_receive_count_5_is_not_poison(self, clf):
        msg = _msg("normal message", {"ApproximateReceiveCount": "5"})
        assert clf.classify(msg) != FailureCategory.POISON_PILL

    def test_poison_pill_overrides_other_keywords(self, clf):
        msg = _msg("connection timeout error", {"ApproximateReceiveCount": "10"})
        assert clf.classify(msg) == FailureCategory.POISON_PILL


# ── Timeout ──────────────────────────────────────────────────────


class TestTimeout:
    @pytest.mark.parametrize("body", [
        "connection timeout",
        "request timed out",
        "operation timed_out after 30s",
    ])
    def test_timeout_keywords(self, clf, body):
        assert clf.classify(_msg(body)) == FailureCategory.TIMEOUT

    def test_timeout_case_insensitive(self, clf):
        assert clf.classify(_msg("CONNECTION TIMEOUT")) == FailureCategory.TIMEOUT

    def test_timeout_mixed_case(self, clf):
        assert clf.classify(_msg("Connection Timed Out")) == FailureCategory.TIMEOUT

    def test_timeout_in_json_error_field(self, clf):
        body = json.dumps({"error": "upstream timeout", "code": 504})
        assert clf.classify(_msg(body)) == FailureCategory.TIMEOUT

    def test_timeout_in_json_error_message_field(self, clf):
        body = json.dumps({"errorMessage": "request timed out"})
        assert clf.classify(_msg(body)) == FailureCategory.TIMEOUT


# ── Validation error ─────────────────────────────────────────────


class TestValidationError:
    @pytest.mark.parametrize("body", [
        "validation failed: missing field",
        "schema mismatch",
        "invalid payload",
        "malformed json body",
        "bad request from client",
        "unauthorized access",
        "forbidden resource",
        "not found",
        "unprocessable entity",
    ])
    def test_validation_keywords(self, clf, body):
        assert clf.classify(_msg(body)) == FailureCategory.VALIDATION_ERROR

    @pytest.mark.parametrize("body", [
        "http error 400",
        "status 401",
        "error 403",
        "code 404",
        "conflict 409",
        "response 422",
    ])
    def test_validation_status_codes(self, clf, body):
        assert clf.classify(_msg(body)) == FailureCategory.VALIDATION_ERROR

    def test_validation_case_insensitive(self, clf):
        assert clf.classify(_msg("VALIDATION FAILED")) == FailureCategory.VALIDATION_ERROR

    def test_validation_in_json_error_field(self, clf):
        body = json.dumps({"error": "invalid schema: missing user_id"})
        assert clf.classify(_msg(body)) == FailureCategory.VALIDATION_ERROR


# ── Dependency failure ───────────────────────────────────────────


class TestDependencyFailure:
    @pytest.mark.parametrize("body", [
        "connection refused by host",
        "service unavailable",
        "http 503",
        "service unavailable: payments-api down",
    ])
    def test_dependency_keywords(self, clf, body):
        assert clf.classify(_msg(body)) == FailureCategory.DEPENDENCY_FAILURE

    def test_dependency_case_insensitive(self, clf):
        assert clf.classify(_msg("CONNECTION REFUSED")) == FailureCategory.DEPENDENCY_FAILURE

    def test_dependency_in_json_error_field(self, clf):
        body = json.dumps({"error": "connection refused", "service": "payments"})
        assert clf.classify(_msg(body)) == FailureCategory.DEPENDENCY_FAILURE

    def test_unavailable_in_json_error_field(self, clf):
        body = json.dumps({"error": "service unavailable"})
        assert clf.classify(_msg(body)) == FailureCategory.DEPENDENCY_FAILURE


# ── Unknown ──────────────────────────────────────────────────────


class TestUnknown:
    def test_empty_body(self, clf):
        assert clf.classify(_msg("")) == FailureCategory.UNKNOWN

    def test_no_matching_keywords(self, clf):
        assert clf.classify(_msg("something went wrong")) == FailureCategory.UNKNOWN

    def test_random_json(self, clf):
        body = json.dumps({"order_id": "123", "item": "book"})
        assert clf.classify(_msg(body)) == FailureCategory.UNKNOWN

    def test_json_with_empty_error(self, clf):
        body = json.dumps({"error": ""})
        assert clf.classify(_msg(body)) == FailureCategory.UNKNOWN

    def test_non_json_gibberish(self, clf):
        assert clf.classify(_msg("abc123xyz")) == FailureCategory.UNKNOWN


# ── is_retryable ─────────────────────────────────────────────────


class TestIsRetryable:
    @pytest.mark.parametrize("cat", [
        FailureCategory.TIMEOUT,
        FailureCategory.DEPENDENCY_FAILURE,
        FailureCategory.UNKNOWN,
    ])
    def test_transient_is_retryable(self, clf, cat):
        assert clf.is_retryable(cat) is True

    @pytest.mark.parametrize("cat", [
        FailureCategory.VALIDATION_ERROR,
        FailureCategory.POISON_PILL,
    ])
    def test_permanent_is_not_retryable(self, clf, cat):
        assert clf.is_retryable(cat) is False


# ── Priority: body-level keywords beat JSON-level ────────────────


class TestClassificationPriority:
    def test_body_keyword_takes_precedence_over_json_error(self, clf):
        body = json.dumps({"error": "connection refused"})
        body_with_timeout = "timeout " + body
        assert clf.classify(_msg(body_with_timeout)) == FailureCategory.TIMEOUT

    def test_poison_pill_takes_precedence_over_everything(self, clf):
        msg = _msg(
            json.dumps({"error": "validation failed"}),
            {"ApproximateReceiveCount": "6"},
        )
        assert clf.classify(msg) == FailureCategory.POISON_PILL

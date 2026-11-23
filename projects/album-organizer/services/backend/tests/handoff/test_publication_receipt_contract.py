from photo_organizer.handoff.delivery_ledger import PublicationReceiptState


def test_delivery_receipt_states_are_closed_and_retryable_is_scheduled() -> None:
    retryable = PublicationReceiptState.from_http_result(503, attempt=1)
    network_error = PublicationReceiptState.from_http_result(None, attempt=1)
    delivered = PublicationReceiptState.from_http_result(202, attempt=1)
    permanent = PublicationReceiptState.from_http_result(409, attempt=1)

    assert retryable.status == "retryable_failure"
    assert retryable.retry_after_seconds == 30
    assert retryable.error_code == "http_503"
    assert network_error.status == "retryable_failure"
    assert network_error.retry_after_seconds == 30
    assert network_error.error_code == "network_error"
    assert delivered.status == "delivered"
    assert delivered.retry_after_seconds is None
    assert delivered.error_code is None
    assert permanent.status == "permanent_failure"
    assert permanent.retry_after_seconds is None
    assert permanent.error_code == "http_409"

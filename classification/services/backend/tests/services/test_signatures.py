from importlib import import_module
from uuid import UUID

import pytest


def signature_modules():
    errors = import_module("product_engine.domain.errors")
    signatures = import_module("product_engine.security.signatures")
    return errors, signatures


def signature_arguments() -> dict[str, object]:
    return {
        "timestamp": 1_786_780_800,
        "source_system": "album",
        "tenant_id": UUID("22222222-2222-4222-8222-222222222222"),
        "method": "POST",
        "path": "/v1/test",
        "body": b'{"a":1}',
    }


def test_signature_binds_every_request_component() -> None:
    errors, signatures = signature_modules()
    secret = b"test-secret"
    signed = signature_arguments()
    signature = signatures.sign_request(secret=secret, **signed)

    signatures.verify_request_signature(
        secret=secret,
        now=1_786_780_850,
        tolerance_seconds=300,
        supplied_signature=signature,
        **signed,
    )

    mutations = [
        {**signed, "timestamp": 1_786_780_801},
        {**signed, "source_system": "sales"},
        {
            **signed,
            "tenant_id": UUID("99999999-9999-4999-8999-999999999999"),
        },
        {**signed, "method": "GET"},
        {**signed, "path": "/v1/other"},
        {**signed, "body": b'{"a":2}'},
    ]
    for changed in mutations:
        with pytest.raises(errors.InvalidSignature, match="signature_mismatch"):
            signatures.verify_request_signature(
                secret=secret,
                now=1_786_780_850,
                tolerance_seconds=300,
                supplied_signature=signature,
                **changed,
            )


def test_signature_rejects_timestamp_outside_tolerance() -> None:
    errors, signatures = signature_modules()
    secret = b"test-secret"
    signed = signature_arguments()
    signature = signatures.sign_request(secret=secret, **signed)

    with pytest.raises(errors.InvalidSignature, match="signature_timestamp_out_of_range"):
        signatures.verify_request_signature(
            secret=secret,
            now=1_786_781_101,
            tolerance_seconds=300,
            supplied_signature=signature,
            **signed,
        )

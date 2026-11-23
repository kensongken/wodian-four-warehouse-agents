from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import photo_organizer.handoff.wechat_current_attachment_evidence as evidence_module
from photo_organizer.handoff.wechat_current_attachment_evidence import (
    MAX_ATTACHMENT_BYTES,
    AuthorizedAsset,
    AuthorizedFact,
    AuthorizedProductPackage,
    CurrentAttachmentEvidenceError,
    PhotoEvidenceV1,
    analyze_current_attachment,
)

ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
EVIDENCE_REF = "catalog/synthetic-product-1/original"


def _package(*, sha256: str = ABC_SHA256) -> AuthorizedProductPackage:
    return AuthorizedProductPackage(
        schema_version="wechat-authorized-product-package/1.0",
        product_id="synthetic-product-1",
        category="synthetic-category",
        assets=(
            AuthorizedAsset(
                sha256=sha256,
                evidence_ref=EVIDENCE_REF,
                facts=(
                    AuthorizedFact(
                        fact="synthetic-authorized-fact",
                        evidence_ref=EVIDENCE_REF,
                    ),
                ),
            ),
        ),
    )


def test_exact_hash_match_returns_only_authorized_evidence(tmp_path: Path) -> None:
    attachment = tmp_path / "current.jpg"
    attachment.write_bytes(b"abc")

    result = analyze_current_attachment(
        request_id="request-1",
        attachment_path=attachment,
        package=_package(),
    )

    assert result == PhotoEvidenceV1(
        schema_version="1.0",
        request_id="request-1",
        status="matched",
        product_id="synthetic-product-1",
        category="synthetic-category",
        image_summary="authorized_exact_hash_match",
        confirmed_facts=(
            AuthorizedFact(
                fact="synthetic-authorized-fact",
                evidence_ref=EVIDENCE_REF,
            ),
        ),
        confidence=1.0,
        evidence_refs=(EVIDENCE_REF,),
    )
    serialized = result.model_dump_json()
    assert str(attachment) not in serialized
    assert ABC_SHA256 not in serialized


def test_non_matching_hash_returns_uncertain_without_product_claims(
    tmp_path: Path,
) -> None:
    attachment = tmp_path / "current.jpg"
    attachment.write_bytes(b"abc")

    result = analyze_current_attachment(
        request_id="request-2",
        attachment_path=attachment,
        package=_package(sha256="0" * 64),
    )

    assert result == PhotoEvidenceV1(
        schema_version="1.0",
        request_id="request-2",
        status="uncertain",
        product_id=None,
        category=None,
        image_summary="no_authorized_exact_hash_match",
        confirmed_facts=(),
        confidence=0.0,
        evidence_refs=(),
    )


def test_missing_authorized_package_returns_uncertain(tmp_path: Path) -> None:
    attachment = tmp_path / "current.jpg"
    attachment.write_bytes(b"abc")

    result = analyze_current_attachment(
        request_id="request-3",
        attachment_path=attachment,
        package=None,
    )

    assert result.status == "uncertain"
    assert result.image_summary == "authorized_product_package_missing"
    assert result.product_id is None
    assert result.confirmed_facts == ()


def test_missing_attachment_is_not_applicable() -> None:
    result = analyze_current_attachment(
        request_id="request-4",
        attachment_path=None,
        package=None,
    )

    assert result.status == "not_applicable"
    assert result.image_summary == "current_attachment_missing"
    assert result.product_id is None
    assert result.confirmed_facts == ()


def test_duplicate_asset_hashes_are_rejected() -> None:
    asset = _package().assets[0]

    with pytest.raises(ValidationError, match="duplicate_asset_sha256"):
        AuthorizedProductPackage(
            schema_version="wechat-authorized-product-package/1.0",
            product_id="synthetic-product-1",
            category="synthetic-category",
            assets=(asset, asset),
        )


def test_fact_must_cite_its_own_asset_evidence() -> None:
    with pytest.raises(ValidationError, match="fact_evidence_ref_mismatch"):
        AuthorizedAsset(
            sha256=ABC_SHA256,
            evidence_ref=EVIDENCE_REF,
            facts=(
                AuthorizedFact(
                    fact="synthetic-authorized-fact",
                    evidence_ref="catalog/another-product/original",
                ),
            ),
        )


@pytest.mark.parametrize(
    "unsafe_ref",
    (
        "catalog/../secret",
        "catalog//synthetic-product",
        "/catalog/synthetic-product",
        "https://example.invalid/product",
    ),
)
def test_evidence_reference_cannot_escape_its_opaque_namespace(
    unsafe_ref: str,
) -> None:
    with pytest.raises(ValidationError):
        AuthorizedFact(fact="synthetic-authorized-fact", evidence_ref=unsafe_ref)


def test_directory_attachment_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(CurrentAttachmentEvidenceError) as error:
        analyze_current_attachment(
            request_id="request-5",
            attachment_path=tmp_path,
            package=_package(),
        )

    assert error.value.code == "attachment_not_regular_file"


def test_symlink_attachment_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.jpg"
    target.write_bytes(b"abc")
    attachment = tmp_path / "current.jpg"
    attachment.symlink_to(target)

    with pytest.raises(CurrentAttachmentEvidenceError) as error:
        analyze_current_attachment(
            request_id="request-6",
            attachment_path=attachment,
            package=_package(),
        )

    assert error.value.code == "attachment_symlink_not_allowed"


def test_empty_attachment_is_rejected(tmp_path: Path) -> None:
    attachment = tmp_path / "current.jpg"
    attachment.write_bytes(b"")

    with pytest.raises(CurrentAttachmentEvidenceError) as error:
        analyze_current_attachment(
            request_id="request-7",
            attachment_path=attachment,
            package=_package(),
        )

    assert error.value.code == "attachment_empty"


def test_oversized_attachment_is_rejected_before_hashing(tmp_path: Path) -> None:
    attachment = tmp_path / "current.jpg"
    with attachment.open("wb") as output:
        output.truncate(MAX_ATTACHMENT_BYTES + 1)

    with pytest.raises(CurrentAttachmentEvidenceError) as error:
        analyze_current_attachment(
            request_id="request-8",
            attachment_path=attachment,
            package=_package(),
        )

    assert error.value.code == "attachment_too_large"


def test_output_rejects_matched_status_without_authorized_facts() -> None:
    with pytest.raises(ValidationError, match="matched_evidence_incomplete"):
        PhotoEvidenceV1(
            schema_version="1.0",
            request_id="request-9",
            status="matched",
            product_id="synthetic-product-1",
            category="synthetic-category",
            image_summary="authorized_exact_hash_match",
            confirmed_facts=(),
            confidence=1.0,
            evidence_refs=(EVIDENCE_REF,),
        )


def test_same_length_attachment_rewrite_during_read_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = tmp_path / "current.jpg"
    attachment.write_bytes(b"abc")
    real_read = evidence_module._read_chunk
    changed = False

    def read_then_rewrite(descriptor: int, size: int) -> bytes:
        nonlocal changed
        chunk = real_read(descriptor, size)
        if chunk and not changed:
            changed = True
            attachment.write_bytes(b"abd")
        return chunk

    monkeypatch.setattr(evidence_module, "_read_chunk", read_then_rewrite)

    with pytest.raises(CurrentAttachmentEvidenceError) as error:
        analyze_current_attachment(
            request_id="request-10",
            attachment_path=attachment,
            package=_package(),
        )

    assert error.value.code == "attachment_changed_during_read"


def test_attachment_path_replacement_during_read_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = tmp_path / "current.jpg"
    attachment.write_bytes(b"abc")
    replacement = tmp_path / "replacement.jpg"
    replacement.write_bytes(b"abc")
    real_read = evidence_module._read_chunk
    changed = False

    def read_then_replace(descriptor: int, size: int) -> bytes:
        nonlocal changed
        chunk = real_read(descriptor, size)
        if chunk and not changed:
            changed = True
            replacement.replace(attachment)
        return chunk

    monkeypatch.setattr(evidence_module, "_read_chunk", read_then_replace)

    with pytest.raises(CurrentAttachmentEvidenceError) as error:
        analyze_current_attachment(
            request_id="request-10-path",
            attachment_path=attachment,
            package=_package(),
        )

    assert error.value.code == "attachment_changed_during_read"


@pytest.mark.parametrize(
    ("status", "summary"),
    (
        ("uncertain", "authorized_exact_hash_match"),
        ("not_applicable", "authorized_product_package_missing"),
    ),
)
def test_output_status_rejects_a_contradictory_summary(
    status: str,
    summary: str,
) -> None:
    with pytest.raises(ValidationError, match="status_summary_mismatch"):
        PhotoEvidenceV1(
            schema_version="1.0",
            request_id="request-11",
            status=status,
            product_id=None,
            category=None,
            image_summary=summary,
            confirmed_facts=(),
            confidence=0.0,
            evidence_refs=(),
        )


def test_output_confidence_rejects_boolean_substitution() -> None:
    with pytest.raises(ValidationError):
        PhotoEvidenceV1(
            schema_version="1.0",
            request_id="request-12",
            status="uncertain",
            product_id=None,
            category=None,
            image_summary="no_authorized_exact_hash_match",
            confirmed_facts=(),
            confidence=False,
            evidence_refs=(),
        )

"""Fail-closed evidence for one explicitly supplied current attachment."""

from __future__ import annotations

import os
import stat
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    field_validator,
    model_validator,
)

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
_HASH_CHUNK_BYTES = 64 * 1024

RequestId = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
]
ProductId = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
]
Category = Annotated[str, Field(min_length=1, max_length=128)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
EvidenceRef = Annotated[
    str,
    Field(
        min_length=9,
        max_length=256,
        pattern=r"^catalog/[A-Za-z0-9][A-Za-z0-9._/-]*$",
    ),
]
FactText = Annotated[str, Field(min_length=1, max_length=500)]


class CurrentAttachmentEvidenceError(ValueError):
    """Stable, non-sensitive reason that evidence extraction is unsafe."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthorizedFact(_FrozenModel):
    fact: FactText
    evidence_ref: EvidenceRef

    @field_validator("fact")
    @classmethod
    def fact_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("fact_blank")
        return value

    @field_validator("evidence_ref")
    @classmethod
    def evidence_ref_must_be_opaque(cls, value: str) -> str:
        if ".." in value or "//" in value:
            raise ValueError("unsafe_evidence_ref")
        return value


class AuthorizedAsset(_FrozenModel):
    sha256: Sha256
    evidence_ref: EvidenceRef
    facts: Annotated[tuple[AuthorizedFact, ...], Field(min_length=1, max_length=50)]

    @field_validator("evidence_ref")
    @classmethod
    def evidence_ref_must_be_opaque(cls, value: str) -> str:
        if ".." in value or "//" in value:
            raise ValueError("unsafe_evidence_ref")
        return value

    @model_validator(mode="after")
    def every_fact_must_cite_this_asset(self) -> Self:
        if any(fact.evidence_ref != self.evidence_ref for fact in self.facts):
            raise ValueError("fact_evidence_ref_mismatch")
        return self


class AuthorizedProductPackage(_FrozenModel):
    schema_version: Literal["wechat-authorized-product-package/1.0"]
    product_id: ProductId
    category: Category
    assets: Annotated[tuple[AuthorizedAsset, ...], Field(min_length=1, max_length=3000)]

    @field_validator("category")
    @classmethod
    def category_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("category_blank")
        return value

    @model_validator(mode="after")
    def asset_hashes_must_be_unique(self) -> Self:
        hashes = [asset.sha256 for asset in self.assets]
        if len(hashes) != len(set(hashes)):
            raise ValueError("duplicate_asset_sha256")
        return self


class PhotoEvidenceV1(_FrozenModel):
    schema_version: Literal["1.0"]
    request_id: RequestId
    status: Literal["matched", "uncertain", "not_applicable"]
    product_id: ProductId | None
    category: Category | None
    image_summary: Literal[
        "authorized_exact_hash_match",
        "no_authorized_exact_hash_match",
        "authorized_product_package_missing",
        "current_attachment_missing",
    ]
    confirmed_facts: Annotated[tuple[AuthorizedFact, ...], Field(max_length=50)] = ()
    confidence: Annotated[StrictFloat, Field(ge=0, le=1)] = 0.0
    evidence_refs: Annotated[tuple[EvidenceRef, ...], Field(max_length=50)] = ()

    @model_validator(mode="after")
    def status_must_match_claims(self) -> Self:
        expected_summaries = {
            "matched": {"authorized_exact_hash_match"},
            "uncertain": {
                "no_authorized_exact_hash_match",
                "authorized_product_package_missing",
            },
            "not_applicable": {"current_attachment_missing"},
        }
        if self.image_summary not in expected_summaries[self.status]:
            raise ValueError("status_summary_mismatch")
        if self.status == "matched":
            if (
                self.product_id is None
                or self.category is None
                or not self.confirmed_facts
                or not self.evidence_refs
                or self.confidence != 1.0
                or self.image_summary != "authorized_exact_hash_match"
            ):
                raise ValueError("matched_evidence_incomplete")
            declared_refs = set(self.evidence_refs)
            if any(fact.evidence_ref not in declared_refs for fact in self.confirmed_facts):
                raise ValueError("matched_fact_ref_not_declared")
        elif (
            self.product_id is not None
            or self.category is not None
            or self.confirmed_facts
            or self.evidence_refs
            or self.confidence != 0.0
        ):
            raise ValueError("unverified_status_contains_claims")
        return self


def analyze_current_attachment(
    *,
    request_id: str,
    attachment_path: Path | None,
    package: AuthorizedProductPackage | None,
) -> PhotoEvidenceV1:
    """Compare one regular file with one explicitly authorized package."""
    if attachment_path is None:
        return _empty_evidence(
            request_id=request_id,
            status="not_applicable",
            image_summary="current_attachment_missing",
        )

    attachment_hash = _attachment_sha256(Path(attachment_path))
    if package is None:
        return _empty_evidence(
            request_id=request_id,
            status="uncertain",
            image_summary="authorized_product_package_missing",
        )

    try:
        package = AuthorizedProductPackage.model_validate(
            package.model_dump(mode="python")
        )
    except Exception as error:
        raise CurrentAttachmentEvidenceError(
            "invalid_authorized_product_package"
        ) from error

    matching_asset = next(
        (asset for asset in package.assets if asset.sha256 == attachment_hash),
        None,
    )
    if matching_asset is None:
        return _empty_evidence(
            request_id=request_id,
            status="uncertain",
            image_summary="no_authorized_exact_hash_match",
        )

    return PhotoEvidenceV1(
        schema_version="1.0",
        request_id=request_id,
        status="matched",
        product_id=package.product_id,
        category=package.category,
        image_summary="authorized_exact_hash_match",
        confirmed_facts=matching_asset.facts,
        confidence=1.0,
        evidence_refs=(matching_asset.evidence_ref,),
    )


def _empty_evidence(
    *,
    request_id: str,
    status: Literal["uncertain", "not_applicable"],
    image_summary: Literal[
        "no_authorized_exact_hash_match",
        "authorized_product_package_missing",
        "current_attachment_missing",
    ],
) -> PhotoEvidenceV1:
    return PhotoEvidenceV1(
        schema_version="1.0",
        request_id=request_id,
        status=status,
        product_id=None,
        category=None,
        image_summary=image_summary,
        confirmed_facts=(),
        confidence=0.0,
        evidence_refs=(),
    )


def _attachment_sha256(path: Path) -> str:
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise CurrentAttachmentEvidenceError("attachment_unavailable") from error
    if stat.S_ISLNK(path_stat.st_mode):
        raise CurrentAttachmentEvidenceError("attachment_symlink_not_allowed")
    if not stat.S_ISREG(path_stat.st_mode):
        raise CurrentAttachmentEvidenceError("attachment_not_regular_file")
    if path_stat.st_size == 0:
        raise CurrentAttachmentEvidenceError("attachment_empty")
    if path_stat.st_size > MAX_ATTACHMENT_BYTES:
        raise CurrentAttachmentEvidenceError("attachment_too_large")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CurrentAttachmentEvidenceError("attachment_unavailable") from error

    digest = sha256()
    total_bytes = 0
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise CurrentAttachmentEvidenceError("attachment_not_regular_file")
        if _stable_file_identity(opened_stat) != _stable_file_identity(path_stat):
            raise CurrentAttachmentEvidenceError("attachment_changed_during_read")
        if opened_stat.st_size == 0:
            raise CurrentAttachmentEvidenceError("attachment_empty")
        if opened_stat.st_size > MAX_ATTACHMENT_BYTES:
            raise CurrentAttachmentEvidenceError("attachment_too_large")
        while chunk := _read_chunk(descriptor, _HASH_CHUNK_BYTES):
            total_bytes += len(chunk)
            if total_bytes > MAX_ATTACHMENT_BYTES:
                raise CurrentAttachmentEvidenceError("attachment_too_large")
            digest.update(chunk)
        final_stat = os.fstat(descriptor)
        if (
            _stable_file_identity(final_stat) != _stable_file_identity(opened_stat)
            or total_bytes != opened_stat.st_size
        ):
            raise CurrentAttachmentEvidenceError("attachment_changed_during_read")
        try:
            final_path_stat = path.lstat()
        except OSError as error:
            raise CurrentAttachmentEvidenceError(
                "attachment_changed_during_read"
            ) from error
        if _stable_file_identity(final_path_stat) != _stable_file_identity(final_stat):
            raise CurrentAttachmentEvidenceError("attachment_changed_during_read")
    except OSError as error:
        raise CurrentAttachmentEvidenceError("attachment_unavailable") from error
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _stable_file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_chunk(descriptor: int, size: int) -> bytes:
    return os.read(descriptor, size)

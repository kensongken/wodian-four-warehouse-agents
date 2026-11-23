"""Customer-scoped real-photo normalization orchestration."""

import hmac
import json
import logging
import os
import re
import stat
import threading
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from secrets import token_bytes, token_urlsafe
from types import TracebackType
from typing import Literal, Protocol, Self
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.image_normalizer import (
    CATALOG_V1_TARGETS,
    ImageNormalizationError,
    ImageNormalizer,
    NormalizationProfile,
    NormalizedImage,
    NormalizedPayload,
)
from opc_style.models import (
    AuditEvent,
    ImageCandidate,
    ImageOutputJob,
    ProductAsset,
    ProductAssetLink,
)
from opc_style.repositories import ProductImageRepository

logger = logging.getLogger(__name__)

STORE_IDENTITY_FILENAME = ".opc-store-identity.json"
STORE_IDENTITY_FORMAT = "opc-local-asset-store-v2"


class AssetContentStoreError(Exception):
    """A stable failure at the trusted internal storage boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class AssetContentStore(Protocol):
    """Resolve internal media without exposing paths to HTTP callers.

    Filesystem-path attestation is point-in-time. A caller that consumes an
    artifact after publication must re-attest it immediately before reading.
    """

    @property
    def store_identity(self) -> str:
        """Return a stable implementation identity for persisted cleanup scope."""
        ...

    @property
    def store_namespace(self) -> str:
        """Return an opaque identity for the concrete artifact namespace."""
        ...

    @property
    def copy_safe_namespace(self) -> bool:
        """Return whether a copied store retains the same namespace."""
        ...

    def resolve_original(self, customer_id: UUID, asset_id: UUID) -> Path:
        """Return a trusted local path for an already-authorized asset identity."""
        ...

    def read_original(
        self, customer_id: UUID, asset_id: UUID, expected_sha256: str
    ) -> bytes:
        """Read a registered original and validate exactly the returned bytes."""
        ...

    def persist_product_foreground(
        self, customer_id: UUID, version_id: UUID, content: bytes, expected_sha256: str
    ) -> None:
        """Persist one proposed RGBA foreground in its private owner scope."""
        ...

    def persist_product_mask(
        self, customer_id: UUID, version_id: UUID, content: bytes, expected_sha256: str
    ) -> None:
        """Persist one proposed mask in its private owner scope."""
        ...

    def read_product_foreground(
        self, customer_id: UUID, version_id: UUID, expected_sha256: str
    ) -> bytes:
        """Read exact foreground bytes without returning a local path."""
        ...

    def read_product_mask(
        self, customer_id: UUID, version_id: UUID, expected_sha256: str
    ) -> bytes:
        """Read exact mask bytes without returning a local path."""
        ...

    def verify_product_foreground(
        self, customer_id: UUID, version_id: UUID, expected_sha256: str
    ) -> bool:
        """Attest one private foreground's owner and digest."""
        ...

    def verify_product_mask(
        self, customer_id: UUID, version_id: UUID, expected_sha256: str
    ) -> bool:
        """Attest one private mask's owner and digest."""
        ...

    def discard_product_foreground(
        self, customer_id: UUID, version_id: UUID, expected_sha256: str
    ) -> None:
        """Compensate one exact uncommitted foreground artifact."""
        ...

    def discard_product_mask(
        self, customer_id: UUID, version_id: UUID, expected_sha256: str
    ) -> None:
        """Compensate one exact uncommitted mask artifact."""
        ...

    def persist_pixel_background(
        self, customer_id: UUID, run_id: UUID, content: bytes, expected_sha256: str
    ) -> None:
        """Persist one provider-returned background pending human review."""
        ...

    def read_pixel_background(
        self, customer_id: UUID, run_id: UUID, expected_sha256: str
    ) -> bytes:
        """Read and attest one exact background."""
        ...

    def verify_pixel_background(
        self, customer_id: UUID, run_id: UUID, expected_sha256: str
    ) -> bool:
        """Attest one background without exposing a local path."""
        ...

    def persist_pixel_composition(
        self,
        customer_id: UUID,
        candidate_id: UUID,
        content: bytes,
        expected_sha256: str,
    ) -> None:
        """Persist one verified local composition."""
        ...

    def read_pixel_composition(
        self, customer_id: UUID, candidate_id: UUID, expected_sha256: str
    ) -> bytes:
        """Read and attest one exact composition."""
        ...

    def verify_pixel_composition(
        self, customer_id: UUID, candidate_id: UUID, expected_sha256: str
    ) -> bool:
        """Attest one composition without exposing a local path."""
        ...

    def register_style_sample_content(
        self, customer_id: UUID, sample_id: UUID, content: bytes, expected_sha256: str
    ) -> None:
        """Register immutable, verified historical bytes without a caller path."""
        ...

    def persist_style_sample_content(
        self, customer_id: UUID, sample_id: UUID, content: bytes, expected_sha256: str
    ) -> None:
        """Persist immutable historical bytes in a durable store adapter."""
        ...

    def discard_style_sample_content(self, customer_id: UUID, sample_id: UUID) -> None:
        """Compensate a failed database transaction before a sample is committed."""
        ...

    def verify_style_sample_content(
        self, customer_id: UUID, sample_id: UUID, expected_sha256: str
    ) -> bool:
        """Attest that a sealed sample's exact bytes remain store-owned."""
        ...

    def read_style_sample_content(
        self, customer_id: UUID, sample_id: UUID, expected_sha256: str
    ) -> bytes:
        """Return private sample bytes after validating ownership and digest."""
        ...

    def read_derivative(
        self,
        customer_id: UUID,
        job_id: UUID,
        sequence_number: int,
        expected_sha256: str,
    ) -> bytes:
        """Read one owned derivative without exposing its filesystem path."""
        ...

    def reserve_derivative(
        self,
        customer_id: UUID,
        job_id: UUID,
        sequence_number: int,
        *,
        extension: Literal["jpg", "png"] = "jpg",
    ) -> "DerivativeTarget":
        """Reserve call-owned staging and artifact paths for one candidate slot."""
        ...

    def create_derivative(
        self, target: "DerivativeTarget", payload: NormalizedPayload
    ) -> NormalizedImage:
        """Publish and register one already-normalized payload atomically."""
        ...

    def cleanup_derivatives(self, customer_id: UUID, job_id: UUID) -> tuple[str, ...]:
        """Remove artifacts from a failed all-or-nothing normalization attempt."""
        ...

    def cleanup_scope_state(
        self,
        customer_id: UUID,
        job_id: UUID,
        expected_sequences: tuple[int, ...],
        capability: str,
    ) -> str:
        """Describe whether a scoped job is owned, cleaned, unknown, or abandoned."""
        ...

    def issue_cleanup_capability(
        self,
        customer_id: UUID,
        job_id: UUID,
        expected_sequences: tuple[int, ...],
    ) -> str:
        """Issue opaque authority for exactly one job cleanup scope."""
        ...

    def verify_cleanup_capability(
        self,
        customer_id: UUID,
        job_id: UUID,
        expected_sequences: tuple[int, ...],
        capability: str,
    ) -> bool:
        """Verify authority using store-private material, never public labels."""
        ...

    def cleanup_scoped_derivatives(
        self, customer_id: UUID, job_id: UUID, capability: str
    ) -> tuple[str, ...]:
        """Clean only when the caller presents store-issued job authority."""
        ...

    def verify_derivative(
        self,
        customer_id: UUID,
        job_id: UUID,
        sequence_number: int,
        expected_sha256: str,
    ) -> bool:
        """Point-in-time attestation of one registered derivative."""
        ...

    def close(self) -> None:
        """Release store-owned operating-system resources idempotently."""
        ...

    def __enter__(self) -> Self:
        """Enter an explicitly bounded store lifetime."""
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the store without suppressing the caller's exception."""
        ...


@dataclass(frozen=True)
class DerivativeTarget:
    """Store-issued paths owned by one customer/job/candidate call."""

    customer_id: UUID
    job_id: UUID
    sequence_number: int
    artifact_path: Path


type FileIdentity = tuple[int, int]


@dataclass
class _OwnedJobDirectory:
    customer_id: UUID
    job_id: UUID
    path: Path
    name: str
    identity: FileIdentity
    reservations: dict[int, DerivativeTarget]
    owned_files: dict[str, FileIdentity]


class UnconfiguredAssetContentStore:
    """Fail-closed placeholder until a production store is explicitly integrated."""

    def __init__(self) -> None:
        self._closed = False

    @property
    def store_identity(self) -> str:
        return "unconfigured-asset-content-store-v1"

    @property
    def store_namespace(self) -> str:
        return "unconfigured"

    @property
    def copy_safe_namespace(self) -> bool:
        return False

    def _ensure_open(self) -> None:
        if self._closed:
            raise AssetContentStoreError("asset_content_store_closed")

    def resolve_original(self, customer_id: UUID, asset_id: UUID) -> Path:
        self._ensure_open()
        del customer_id, asset_id
        raise AssetContentStoreError("asset_content_store_unconfigured")

    def read_original(
        self, customer_id: UUID, asset_id: UUID, expected_sha256: str
    ) -> bytes:
        self._ensure_open()
        raise AssetContentStoreError("asset_content_store_unconfigured")

    def persist_product_foreground(
        self,
        customer_id: UUID,
        version_id: UUID,
        content: bytes,
        expected_sha256: str,
    ) -> None:
        self._ensure_open()
        del customer_id, version_id, content, expected_sha256
        raise AssetContentStoreError("asset_content_store_unconfigured")

    def persist_product_mask(
        self,
        customer_id: UUID,
        version_id: UUID,
        content: bytes,
        expected_sha256: str,
    ) -> None:
        self._ensure_open()
        del customer_id, version_id, content, expected_sha256
        raise AssetContentStoreError("asset_content_store_unconfigured")

    def read_product_foreground(
        self, customer_id: UUID, version_id: UUID, expected_sha256: str
    ) -> bytes:
        self._ensure_open()
        del customer_id, version_id, expected_sha256
        raise AssetContentStoreError("asset_content_store_unconfigured")

    def read_product_mask(
        self, customer_id: UUID, version_id: UUID, expected_sha256: str
    ) -> bytes:
        self._ensure_open()
        del customer_id, version_id, expected_sha256
        raise AssetContentStoreError("asset_content_store_unconfigured")

    def verify_product_foreground(
        self, customer_id: UUID, version_id: UUID, expected_sha256: str
    ) -> bool:
        self._ensure_open()
        del customer_id, version_id, expected_sha256
        return False

    def verify_product_mask(
        self, customer_id: UUID, version_id: UUID, expected_sha256: str
    ) -> bool:
        self._ensure_open()
        del customer_id, version_id, expected_sha256
        return False

    def discard_product_foreground(
        self, customer_id: UUID, version_id: UUID, expected_sha256: str
    ) -> None:
        self._ensure_open()
        del customer_id, version_id, expected_sha256

    def discard_product_mask(
        self, customer_id: UUID, version_id: UUID, expected_sha256: str
    ) -> None:
        self._ensure_open()
        del customer_id, version_id, expected_sha256

    def register_style_sample_content(
        self, customer_id: UUID, sample_id: UUID, content: bytes, expected_sha256: str
    ) -> None:
        self._ensure_open()
        del customer_id, sample_id, content, expected_sha256
        raise AssetContentStoreError("asset_content_store_unconfigured")

    def persist_style_sample_content(
        self, customer_id: UUID, sample_id: UUID, content: bytes, expected_sha256: str
    ) -> None:
        self._ensure_open()
        del customer_id, sample_id, content, expected_sha256
        raise AssetContentStoreError("asset_content_store_unconfigured")

    def discard_style_sample_content(self, customer_id: UUID, sample_id: UUID) -> None:
        self._ensure_open()
        del customer_id, sample_id
        raise AssetContentStoreError("asset_content_store_unconfigured")

    def verify_style_sample_content(
        self, customer_id: UUID, sample_id: UUID, expected_sha256: str
    ) -> bool:
        self._ensure_open()
        del customer_id, sample_id, expected_sha256
        raise AssetContentStoreError("asset_content_store_unconfigured")

    def read_style_sample_content(
        self, customer_id: UUID, sample_id: UUID, expected_sha256: str
    ) -> bytes:
        self._ensure_open()
        raise AssetContentStoreError("asset_content_store_unconfigured")

    def persist_pixel_background(
        self, customer_id: UUID, run_id: UUID, content: bytes, expected_sha256: str
    ) -> None:
        self._ensure_open()
        raise AssetContentStoreError("asset_content_store_unconfigured")

    def read_pixel_background(
        self, customer_id: UUID, run_id: UUID, expected_sha256: str
    ) -> bytes:
        self._ensure_open()
        raise AssetContentStoreError("asset_content_store_unconfigured")

    def verify_pixel_background(
        self, customer_id: UUID, run_id: UUID, expected_sha256: str
    ) -> bool:
        self._ensure_open()
        return False

    def persist_pixel_composition(
        self,
        customer_id: UUID,
        candidate_id: UUID,
        content: bytes,
        expected_sha256: str,
    ) -> None:
        self._ensure_open()
        raise AssetContentStoreError("asset_content_store_unconfigured")

    def read_pixel_composition(
        self, customer_id: UUID, candidate_id: UUID, expected_sha256: str
    ) -> bytes:
        self._ensure_open()
        raise AssetContentStoreError("asset_content_store_unconfigured")

    def verify_pixel_composition(
        self, customer_id: UUID, candidate_id: UUID, expected_sha256: str
    ) -> bool:
        self._ensure_open()
        return False

    def read_derivative(
        self,
        customer_id: UUID,
        job_id: UUID,
        sequence_number: int,
        expected_sha256: str,
    ) -> bytes:
        self._ensure_open()
        raise AssetContentStoreError("asset_content_store_unconfigured")

    def reserve_derivative(
        self,
        customer_id: UUID,
        job_id: UUID,
        sequence_number: int,
        *,
        extension: Literal["jpg", "png"] = "jpg",
    ) -> DerivativeTarget:
        self._ensure_open()
        del customer_id, job_id, sequence_number, extension
        raise AssetContentStoreError("asset_content_store_unconfigured")

    def create_derivative(
        self, target: DerivativeTarget, payload: NormalizedPayload
    ) -> NormalizedImage:
        self._ensure_open()
        del target, payload
        raise AssetContentStoreError("asset_content_store_unconfigured")

    def cleanup_derivatives(self, customer_id: UUID, job_id: UUID) -> tuple[str, ...]:
        self._ensure_open()
        del customer_id, job_id
        return ()

    def cleanup_scope_state(
        self,
        customer_id: UUID,
        job_id: UUID,
        expected_sequences: tuple[int, ...],
        capability: str,
    ) -> str:
        self._ensure_open()
        del customer_id, job_id, expected_sequences, capability
        return "unavailable"

    def issue_cleanup_capability(
        self,
        customer_id: UUID,
        job_id: UUID,
        expected_sequences: tuple[int, ...],
    ) -> str:
        del customer_id, job_id, expected_sequences
        return token_urlsafe(32)

    def verify_cleanup_capability(
        self,
        customer_id: UUID,
        job_id: UUID,
        expected_sequences: tuple[int, ...],
        capability: str,
    ) -> bool:
        del customer_id, job_id, expected_sequences, capability
        return False

    def cleanup_scoped_derivatives(
        self, customer_id: UUID, job_id: UUID, capability: str
    ) -> tuple[str, ...]:
        del customer_id, job_id, capability
        return ("asset_store_cleanup_unavailable",)

    def verify_derivative(
        self,
        customer_id: UUID,
        job_id: UUID,
        sequence_number: int,
        expected_sha256: str,
    ) -> bool:
        self._ensure_open()
        del customer_id, job_id, sequence_number, expected_sha256
        return False

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()


class LocalSyntheticAssetStore:
    """Process-private synthetic store; never a production media store.

    The namespace is created exclusively with mode 0700 and held by descriptor.
    Same-UID hostile mutation after a successful method return is outside the
    filesystem guarantee; consumers must call ``verify_derivative`` before use.
    Detected in-method mutation always fails closed and is never deleted.
    """

    def __init__(self, root: Path, *, reopen: bool = False) -> None:
        configured_root = root.absolute()
        exists = _lexists(configured_root)
        if exists and not reopen:
            raise AssetContentStoreError("unsafe_asset_store_root")
        root_descriptor = -1
        root_finalizer: weakref.finalize[[int], LocalSyntheticAssetStore] | None = None
        try:
            if not exists:
                configured_root.mkdir(mode=0o700, parents=False, exist_ok=False)
            configured_stat = configured_root.lstat()
            root_descriptor = _open_directory(configured_root)
            resolved_root = configured_root.resolve(strict=True)
            resolved_stat = resolved_root.stat()
            if (
                stat.S_ISLNK(configured_stat.st_mode)
                or not stat.S_ISDIR(configured_stat.st_mode)
                or not stat.S_ISDIR(resolved_stat.st_mode)
                or configured_stat.st_uid != os.getuid()
                or stat.S_IMODE(configured_stat.st_mode) != 0o700
                or (configured_stat.st_dev, configured_stat.st_ino)
                != (resolved_stat.st_dev, resolved_stat.st_ino)
                or (configured_stat.st_dev, configured_stat.st_ino)
                != _identity_from_fd(root_descriptor)
            ):
                raise AssetContentStoreError("unsafe_asset_store_root")
            self._root = resolved_root
            self._root_identity = (resolved_stat.st_dev, resolved_stat.st_ino)
            self._root_fd = root_descriptor
            self._stable_store_id = (
                _read_store_identity(root_descriptor)
                if exists
                else _create_store_identity(root_descriptor)
            )
            self._store_identity_file_identity = (
                _identity_at(root_descriptor, STORE_IDENTITY_FILENAME)
                if self._stable_store_id is not None
                else None
            )
            self._customer_root = self._root / "customers"
            self._customer_root_identity: FileIdentity | None = None
            if _lexists(self._customer_root):
                self._verify_customer_root()
            self._lock = threading.RLock()
            self._closed = False
            self._originals: dict[tuple[UUID, UUID], Path] = {}
            self._style_sample_content: dict[tuple[UUID, UUID], bytes] = {}
            self._jobs: dict[tuple[UUID, UUID], _OwnedJobDirectory] = {}
            self._retired_jobs: set[tuple[UUID, UUID]] = set()
            self._cleanup_secret = token_bytes(32)
            self._issued_cleanup_scopes: set[tuple[UUID, UUID, tuple[int, ...]]] = set()
            self._abandoned: dict[tuple[UUID, UUID], str] = {}
            self._abandoned_entries: dict[str, FileIdentity] = {}
            self._abandoned_markers: dict[str, FileIdentity] = {}
            root_finalizer = weakref.finalize(
                self, _close_descriptor_safely, root_descriptor
            )
            self._root_finalizer = root_finalizer
            if exists and reopen:
                self._restore_reopened_jobs()
        except BaseException as error:
            if root_finalizer is None:
                _close_descriptor_safely(root_descriptor)
            else:
                root_finalizer()
            if isinstance(error, OSError):
                raise AssetContentStoreError("unsafe_asset_store_root") from error
            raise

    def _ensure_open(self) -> None:
        if self._closed or not self._root_finalizer.alive:
            raise AssetContentStoreError("asset_content_store_closed")

    @property
    def store_identity(self) -> str:
        return "local-synthetic-asset-store-v1"

    @property
    def store_namespace(self) -> str:
        if self._stable_store_id is not None:
            payload = f"{STORE_IDENTITY_FORMAT}:{self._stable_store_id}".encode()
            return sha256(payload).hexdigest()
        return self.legacy_store_namespace

    @property
    def copy_safe_namespace(self) -> bool:
        return self._stable_store_id is not None

    @property
    def legacy_store_namespace(self) -> str:
        """Return the pre-v2 inode identity for explicit legacy recovery only."""
        payload = f"{self._root_identity[0]}:{self._root_identity[1]}".encode()
        return sha256(payload).hexdigest()

    def initialize_stable_identity(self) -> str:
        """Explicitly upgrade a verified legacy root to the copy-safe identity."""
        with self._lock:
            self._ensure_open()
            if self._stable_store_id is not None:
                return self.store_namespace
            self._verify_root()
            self._stable_store_id = _create_store_identity(self._root_fd)
            self._store_identity_file_identity = _identity_at(
                self._root_fd, STORE_IDENTITY_FILENAME
            )
            self._verify_root()
            return self.store_namespace

    @property
    def recovered_job_ids(self) -> tuple[UUID, ...]:
        """Return durable local job scopes reconstructed from the private root."""
        with self._lock:
            return tuple(sorted((job_id for _, job_id in self._jobs), key=str))

    def close(self) -> None:
        """Release the root descriptor; repeated calls are harmless."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._root_finalizer()
            self._root_fd = -1

    def __enter__(self) -> Self:
        with self._lock:
            self._ensure_open()
            return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def register_original(
        self, customer_id: UUID, asset_id: UUID, trusted_path: Path
    ) -> None:
        """Register a Python-side synthetic fixture, outside the HTTP contract."""
        with self._lock:
            self._ensure_open()
            self._originals[(customer_id, asset_id)] = trusted_path.resolve(strict=True)

    def persist_original(
        self,
        customer_id: UUID,
        asset_id: UUID,
        content: bytes,
        expected_sha256: str,
    ) -> None:
        """Persist one immutable original under its customer-owned identity."""
        with self._lock:
            self._ensure_open()
            target = self._owned_asset_path(customer_id, "originals", asset_id)
            self._persist_verified_bytes(target, content, expected_sha256)
            self._originals[(customer_id, asset_id)] = target

    def discard_original(self, customer_id: UUID, asset_id: UUID) -> None:
        """Remove only a new original whose database transaction did not commit."""
        with self._lock:
            self._ensure_open()
            self._originals.pop((customer_id, asset_id), None)
            self._discard_owned_asset(customer_id, "originals", asset_id)

    def discard_uncommitted_asset(
        self,
        customer_id: UUID,
        kind: Literal[
            "originals",
            "style-samples",
            "product-foregrounds",
            "product-masks",
            "pixel-backgrounds",
            "pixel-compositions",
        ],
        asset_id: UUID,
        expected_sha256: str,
    ) -> None:
        """Delete one exact verified file without accepting a caller path."""
        with self._lock:
            self._ensure_open()
            target = self._owned_asset_path(customer_id, kind, asset_id, create=False)
            try:
                self._read_verified_regular_file(target, expected_sha256)
            except AssetContentStoreError as error:
                raise AssetContentStoreError("asset_delete_scope_invalid") from error
            self._originals.pop((customer_id, asset_id), None)
            self._style_sample_content.pop((customer_id, asset_id), None)
            try:
                self._discard_owned_asset(customer_id, kind, asset_id)
            except AssetContentStoreError as error:
                raise AssetContentStoreError("asset_delete_scope_invalid") from error

    def persist_product_foreground(
        self,
        customer_id: UUID,
        version_id: UUID,
        content: bytes,
        expected_sha256: str,
    ) -> None:
        self._persist_product_artifact(
            customer_id,
            version_id,
            "product-foregrounds",
            content,
            expected_sha256,
        )

    def persist_product_mask(
        self,
        customer_id: UUID,
        version_id: UUID,
        content: bytes,
        expected_sha256: str,
    ) -> None:
        self._persist_product_artifact(
            customer_id, version_id, "product-masks", content, expected_sha256
        )

    def read_product_foreground(
        self, customer_id: UUID, version_id: UUID, expected_sha256: str
    ) -> bytes:
        return self._read_product_artifact(
            customer_id, version_id, "product-foregrounds", expected_sha256
        )

    def read_product_mask(
        self, customer_id: UUID, version_id: UUID, expected_sha256: str
    ) -> bytes:
        return self._read_product_artifact(
            customer_id, version_id, "product-masks", expected_sha256
        )

    def verify_product_foreground(
        self, customer_id: UUID, version_id: UUID, expected_sha256: str
    ) -> bool:
        return self._verify_product_artifact(
            customer_id, version_id, "product-foregrounds", expected_sha256
        )

    def verify_product_mask(
        self, customer_id: UUID, version_id: UUID, expected_sha256: str
    ) -> bool:
        return self._verify_product_artifact(
            customer_id, version_id, "product-masks", expected_sha256
        )

    def discard_product_foreground(
        self, customer_id: UUID, version_id: UUID, expected_sha256: str
    ) -> None:
        self._discard_product_artifact(
            customer_id, version_id, "product-foregrounds", expected_sha256
        )

    def discard_product_mask(
        self, customer_id: UUID, version_id: UUID, expected_sha256: str
    ) -> None:
        self._discard_product_artifact(
            customer_id, version_id, "product-masks", expected_sha256
        )

    def persist_pixel_background(
        self, customer_id: UUID, run_id: UUID, content: bytes, expected_sha256: str
    ) -> None:
        self._persist_private_artifact(
            customer_id, run_id, "pixel-backgrounds", content, expected_sha256
        )

    def read_pixel_background(
        self, customer_id: UUID, run_id: UUID, expected_sha256: str
    ) -> bytes:
        return self._read_private_artifact(
            customer_id, run_id, "pixel-backgrounds", expected_sha256
        )

    def verify_pixel_background(
        self, customer_id: UUID, run_id: UUID, expected_sha256: str
    ) -> bool:
        return self._verify_private_artifact(
            customer_id, run_id, "pixel-backgrounds", expected_sha256
        )

    def persist_pixel_composition(
        self,
        customer_id: UUID,
        candidate_id: UUID,
        content: bytes,
        expected_sha256: str,
    ) -> None:
        self._persist_private_artifact(
            customer_id, candidate_id, "pixel-compositions", content, expected_sha256
        )

    def read_pixel_composition(
        self, customer_id: UUID, candidate_id: UUID, expected_sha256: str
    ) -> bytes:
        return self._read_private_artifact(
            customer_id, candidate_id, "pixel-compositions", expected_sha256
        )

    def verify_pixel_composition(
        self, customer_id: UUID, candidate_id: UUID, expected_sha256: str
    ) -> bool:
        return self._verify_private_artifact(
            customer_id, candidate_id, "pixel-compositions", expected_sha256
        )

    def _persist_private_artifact(
        self,
        customer_id: UUID,
        artifact_id: UUID,
        kind: Literal["pixel-backgrounds", "pixel-compositions"],
        content: bytes,
        expected_sha256: str,
    ) -> None:
        with self._lock:
            self._ensure_open()
            self._persist_verified_bytes(
                self._owned_asset_path(customer_id, kind, artifact_id),
                content,
                expected_sha256,
            )

    def _read_private_artifact(
        self,
        customer_id: UUID,
        artifact_id: UUID,
        kind: Literal["pixel-backgrounds", "pixel-compositions"],
        expected_sha256: str,
    ) -> bytes:
        with self._lock:
            self._ensure_open()
            try:
                return self._read_verified_regular_file(
                    self._owned_asset_path(
                        customer_id, kind, artifact_id, create=False
                    ),
                    expected_sha256,
                )
            except AssetContentStoreError as error:
                raise AssetContentStoreError("pixel_artifact_unavailable") from error

    def _verify_private_artifact(
        self,
        customer_id: UUID,
        artifact_id: UUID,
        kind: Literal["pixel-backgrounds", "pixel-compositions"],
        expected_sha256: str,
    ) -> bool:
        try:
            self._read_private_artifact(customer_id, artifact_id, kind, expected_sha256)
        except AssetContentStoreError:
            return False
        return True

    def _persist_product_artifact(
        self,
        customer_id: UUID,
        version_id: UUID,
        kind: Literal["product-foregrounds", "product-masks"],
        content: bytes,
        expected_sha256: str,
    ) -> None:
        with self._lock:
            self._ensure_open()
            target = self._owned_asset_path(customer_id, kind, version_id)
            self._persist_verified_bytes(target, content, expected_sha256)

    def _read_product_artifact(
        self,
        customer_id: UUID,
        version_id: UUID,
        kind: Literal["product-foregrounds", "product-masks"],
        expected_sha256: str,
    ) -> bytes:
        with self._lock:
            self._ensure_open()
            try:
                return self._read_verified_regular_file(
                    self._owned_asset_path(customer_id, kind, version_id, create=False),
                    expected_sha256,
                )
            except AssetContentStoreError as error:
                raise AssetContentStoreError(
                    "product_foreground_content_unavailable"
                ) from error

    def _verify_product_artifact(
        self,
        customer_id: UUID,
        version_id: UUID,
        kind: Literal["product-foregrounds", "product-masks"],
        expected_sha256: str,
    ) -> bool:
        try:
            self._read_product_artifact(customer_id, version_id, kind, expected_sha256)
        except AssetContentStoreError:
            return False
        return True

    def _discard_product_artifact(
        self,
        customer_id: UUID,
        version_id: UUID,
        kind: Literal["product-foregrounds", "product-masks"],
        expected_sha256: str,
    ) -> None:
        try:
            self.discard_uncommitted_asset(
                customer_id, kind, version_id, expected_sha256
            )
        except AssetContentStoreError as error:
            raise AssetContentStoreError("asset_delete_scope_invalid") from error

    @property
    def sample_content_count(self) -> int:
        """Expose a test-only count; raw bytes and paths remain private."""
        with self._lock:
            self._ensure_open()
            return len(self._style_sample_content)

    def register_style_sample_content(
        self, customer_id: UUID, sample_id: UUID, content: bytes, expected_sha256: str
    ) -> None:
        """Keep exact verified bytes in the store-owned private namespace."""
        with self._lock:
            self._ensure_open()
            if sha256(content).hexdigest() != expected_sha256:
                raise AssetContentStoreError("asset_content_hash_mismatch")
            key = (customer_id, sample_id)
            existing = self._style_sample_content.get(key)
            if existing is not None and existing != content:
                raise AssetContentStoreError("style_sample_content_conflict")
            self._style_sample_content[key] = bytes(content)

    def persist_style_sample_content(
        self,
        customer_id: UUID,
        sample_id: UUID,
        content: bytes,
        expected_sha256: str,
    ) -> None:
        """Persist one immutable style reference under its customer owner."""
        with self._lock:
            self._ensure_open()
            target = self._owned_asset_path(customer_id, "style-samples", sample_id)
            self._persist_verified_bytes(target, content, expected_sha256)
            self._style_sample_content[(customer_id, sample_id)] = bytes(content)

    def persist_subject_artifact(
        self,
        customer_id: UUID,
        seal_id: UUID,
        kind: Literal["rgba", "core-mask"],
        content: bytes,
        expected_sha256: str,
    ) -> None:
        """Persist one immutable subject-seal artifact under an opaque seal ID."""
        with self._lock:
            self._ensure_open()
            directory: Literal["subject-rgba", "subject-masks"] = (
                "subject-rgba" if kind == "rgba" else "subject-masks"
            )
            target = self._owned_asset_path(customer_id, directory, seal_id)
            self._persist_verified_bytes(target, content, expected_sha256)

    def read_subject_artifact(
        self,
        customer_id: UUID,
        seal_id: UUID,
        kind: Literal["rgba", "core-mask"],
        expected_sha256: str,
    ) -> bytes:
        """Return exact subject bytes after rechecking owner and digest."""
        with self._lock:
            self._ensure_open()
            directory: Literal["subject-rgba", "subject-masks"] = (
                "subject-rgba" if kind == "rgba" else "subject-masks"
            )
            try:
                return self._read_verified_regular_file(
                    self._owned_asset_path(
                        customer_id, directory, seal_id, create=False
                    ),
                    expected_sha256,
                )
            except AssetContentStoreError as error:
                raise AssetContentStoreError(
                    "subject_artifact_unavailable"
                ) from error

    def discard_subject_artifact(
        self,
        customer_id: UUID,
        seal_id: UUID,
        kind: Literal["rgba", "core-mask"],
    ) -> None:
        """Remove only one uncommitted subject artifact in its owner scope."""
        with self._lock:
            self._ensure_open()
            directory: Literal["subject-rgba", "subject-masks"] = (
                "subject-rgba" if kind == "rgba" else "subject-masks"
            )
            self._discard_owned_asset(customer_id, directory, seal_id)

    def discard_style_sample_content(self, customer_id: UUID, sample_id: UUID) -> None:
        """Remove only uncommitted content identified by its exact owner scope."""
        with self._lock:
            self._ensure_open()
            self._style_sample_content.pop((customer_id, sample_id), None)
            self._discard_owned_asset(customer_id, "style-samples", sample_id)

    def verify_style_sample_content(
        self, customer_id: UUID, sample_id: UUID, expected_sha256: str
    ) -> bool:
        """Check private ownership and digest without returning the raw bytes."""
        with self._lock:
            self._ensure_open()
            content = self._style_sample_content.get((customer_id, sample_id))
            if content is not None:
                return sha256(content).hexdigest() == expected_sha256
            try:
                self._read_verified_regular_file(
                    self._owned_asset_path(
                        customer_id, "style-samples", sample_id, create=False
                    ),
                    expected_sha256,
                )
            except AssetContentStoreError:
                return False
            return True

    def read_style_sample_content(
        self, customer_id: UUID, sample_id: UUID, expected_sha256: str
    ) -> bytes:
        """Recheck the returned bytes under the same private-map lock."""
        with self._lock:
            self._ensure_open()
            content = self._style_sample_content.get((customer_id, sample_id))
            if content is not None:
                if sha256(content).hexdigest() != expected_sha256:
                    raise AssetContentStoreError("style_sample_content_unavailable")
                return bytes(content)
            try:
                return self._read_verified_regular_file(
                    self._owned_asset_path(
                        customer_id, "style-samples", sample_id, create=False
                    ),
                    expected_sha256,
                )
            except AssetContentStoreError as error:
                raise AssetContentStoreError(
                    "style_sample_content_unavailable"
                ) from error

    def resolve_original(self, customer_id: UUID, asset_id: UUID) -> Path:
        with self._lock:
            self._ensure_open()
            try:
                return self._originals[(customer_id, asset_id)]
            except KeyError:
                target = self._owned_asset_path(
                    customer_id, "originals", asset_id, create=False
                )
                if not _lexists(target):
                    raise AssetContentStoreError("asset_content_not_registered")
                return target

    def read_original(
        self, customer_id: UUID, asset_id: UUID, expected_sha256: str
    ) -> bytes:
        with self._lock:
            path = self.resolve_original(customer_id, asset_id)
            descriptor = -1
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise AssetContentStoreError("asset_content_not_regular")
                chunks = []
                while chunk := os.read(descriptor, 1024 * 1024):
                    chunks.append(chunk)
                content = b"".join(chunks)
                if sha256(content).hexdigest() != expected_sha256:
                    raise AssetContentStoreError("asset_content_hash_mismatch")
                return content
            except OSError as error:
                raise AssetContentStoreError("asset_content_unavailable") from error
            finally:
                if descriptor >= 0:
                    os.close(descriptor)

    def _owned_asset_path(
        self,
        customer_id: UUID,
        kind: Literal[
            "originals",
            "style-samples",
            "product-foregrounds",
            "product-masks",
            "pixel-backgrounds",
            "pixel-compositions",
            "subject-rgba",
            "subject-masks",
        ],
        asset_id: UUID,
        *,
        create: bool = True,
    ) -> Path:
        if kind not in {
            "originals",
            "style-samples",
            "product-foregrounds",
            "product-masks",
            "pixel-backgrounds",
            "pixel-compositions",
            "subject-rgba",
            "subject-masks",
        }:
            raise AssetContentStoreError("unsafe_asset_store_path")
        if create:
            self._ensure_customer_root()
        else:
            self._verify_customer_root()
        customer_root = self._customer_root / str(customer_id)
        kind_root = customer_root / kind
        if create:
            self._ensure_private_directory(customer_root)
            self._ensure_private_directory(kind_root)
        else:
            self._verify_private_directory(customer_root)
            self._verify_private_directory(kind_root)
        return kind_root / f"{asset_id}.bin"

    def _ensure_customer_root(self) -> None:
        if not _lexists(self._customer_root):
            self._customer_root.mkdir(mode=0o700)
        self._verify_customer_root()

    def _verify_customer_root(self) -> None:
        try:
            value = self._customer_root.lstat()
            resolved = self._customer_root.resolve(strict=True)
        except OSError as error:
            raise AssetContentStoreError("unsafe_asset_store_path") from error
        identity = (value.st_dev, value.st_ino)
        if (
            stat.S_ISLNK(value.st_mode)
            or not stat.S_ISDIR(value.st_mode)
            or value.st_uid != os.getuid()
            or stat.S_IMODE(value.st_mode) != 0o700
            or resolved != self._customer_root
            or (
                self._customer_root_identity is not None
                and identity != self._customer_root_identity
            )
        ):
            raise AssetContentStoreError("unsafe_asset_store_path")
        self._customer_root_identity = identity

    def _ensure_private_directory(self, directory: Path) -> None:
        if not _lexists(directory):
            directory.mkdir(mode=0o700)
        self._verify_private_directory(directory)

    def _verify_private_directory(self, directory: Path) -> None:
        try:
            directory.relative_to(self._customer_root)
            value = directory.lstat()
            resolved = directory.resolve(strict=True)
        except (OSError, ValueError) as error:
            raise AssetContentStoreError("unsafe_asset_store_path") from error
        if (
            stat.S_ISLNK(value.st_mode)
            or not stat.S_ISDIR(value.st_mode)
            or value.st_uid != os.getuid()
            or stat.S_IMODE(value.st_mode) != 0o700
            or resolved != directory
        ):
            raise AssetContentStoreError("unsafe_asset_store_path")

    def _persist_verified_bytes(
        self, target: Path, content: bytes, expected_sha256: str
    ) -> None:
        if sha256(content).hexdigest() != expected_sha256:
            raise AssetContentStoreError("asset_content_hash_mismatch")
        if _lexists(target):
            try:
                existing = self._read_verified_regular_file(target, expected_sha256)
            except AssetContentStoreError as error:
                raise AssetContentStoreError("asset_content_conflict") from error
            if existing != content:
                raise AssetContentStoreError("asset_content_conflict")
            return
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        descriptor = -1
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            if (
                sha256(
                    self._read_verified_regular_file(temporary, expected_sha256)
                ).hexdigest()
                != expected_sha256
            ):
                raise AssetContentStoreError("asset_content_hash_mismatch")
            os.replace(temporary, target)
        except OSError as error:
            raise AssetContentStoreError("asset_content_persist_failed") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _discard_owned_asset(
        self,
        customer_id: UUID,
        kind: Literal[
            "originals",
            "style-samples",
            "product-foregrounds",
            "product-masks",
            "pixel-backgrounds",
            "pixel-compositions",
            "subject-rgba",
            "subject-masks",
        ],
        asset_id: UUID,
    ) -> None:
        try:
            target = self._owned_asset_path(customer_id, kind, asset_id, create=False)
        except AssetContentStoreError:
            return
        if not _lexists(target):
            return
        try:
            value = target.lstat()
            if (
                not stat.S_ISREG(value.st_mode)
                or value.st_uid != os.getuid()
                or target.resolve(strict=True).parent != target.parent
            ):
                raise AssetContentStoreError("unsafe_asset_store_path")
            target.unlink()
        except OSError as error:
            raise AssetContentStoreError("asset_content_discard_failed") from error

    def _read_verified_regular_file(self, target: Path, expected_sha256: str) -> bytes:
        descriptor = -1
        try:
            flags = os.O_RDONLY | os.O_NONBLOCK
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            descriptor = os.open(target, flags)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise AssetContentStoreError("asset_content_not_regular")
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            content = b"".join(chunks)
            if sha256(content).hexdigest() != expected_sha256:
                raise AssetContentStoreError("asset_content_hash_mismatch")
            return content
        except OSError as error:
            raise AssetContentStoreError("asset_content_unavailable") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def read_derivative(
        self,
        customer_id: UUID,
        job_id: UUID,
        sequence_number: int,
        expected_sha256: str,
    ) -> bytes:
        """Validate ownership and digest on the same bytes returned to the caller."""
        with self._lock:
            self._ensure_open()
            job = self._jobs.get((customer_id, job_id))
            target = job.reservations.get(sequence_number) if job else None
            if job is None or target is None:
                raise AssetContentStoreError("asset_derivative_unavailable")
            expected_identity = job.owned_files.get(target.artifact_path.name)
            directory_fd = artifact_fd = -1
            try:
                self._verify_owned_job_directory(job)
                directory_fd = self._open_owned_job_directory(job)
                artifact_fd = os.open(
                    target.artifact_path.name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory_fd,
                )
                if (
                    not stat.S_ISREG(os.fstat(artifact_fd).st_mode)
                    or _identity_from_fd(artifact_fd) != expected_identity
                ):
                    raise AssetContentStoreError("asset_derivative_attestation_failed")
                chunks = []
                while chunk := os.read(artifact_fd, 1024 * 1024):
                    chunks.append(chunk)
                content = b"".join(chunks)
                if (
                    sha256(content).hexdigest() != expected_sha256
                    or _identity_from_fd(artifact_fd) != expected_identity
                    or _identity_at(directory_fd, target.artifact_path.name)
                    != expected_identity
                ):
                    raise AssetContentStoreError("asset_derivative_attestation_failed")
                return content
            except OSError as error:
                raise AssetContentStoreError("asset_derivative_unavailable") from error
            finally:
                for descriptor in (artifact_fd, directory_fd):
                    if descriptor >= 0:
                        os.close(descriptor)

    def reserve_derivative(
        self,
        customer_id: UUID,
        job_id: UUID,
        sequence_number: int,
        *,
        extension: Literal["jpg", "png"] = "jpg",
    ) -> DerivativeTarget:
        with self._lock:
            self._ensure_open()
            if sequence_number not in {1, 2, 3}:
                raise AssetContentStoreError("invalid_derivative_sequence")
            job = self._jobs.get((customer_id, job_id))
            if job is None:
                job = self._create_owned_job_directory(customer_id, job_id)
            self._verify_owned_job_directory(job)
            existing = job.reservations.get(sequence_number)
            if existing is not None:
                return existing
            artifact_path = job.path / f"candidate-{sequence_number}.{extension}"
            if _lexists(artifact_path):
                raise AssetContentStoreError("asset_derivative_destination_exists")
            target = DerivativeTarget(
                customer_id=customer_id,
                job_id=job_id,
                sequence_number=sequence_number,
                artifact_path=artifact_path,
            )
            job.reservations[sequence_number] = target
            return target

    def create_derivative(
        self, target: DerivativeTarget, payload: NormalizedPayload
    ) -> NormalizedImage:
        with self._lock:
            self._ensure_open()
            job = self._get_owned_job(target.customer_id, target.job_id)
            self._verify_reservation(job, target)
            self._verify_owned_job_directory(job)
            if sha256(payload.content).hexdigest() != payload.content_sha256:
                raise AssetContentStoreError("asset_derivative_hash_mismatch")
            descriptor = -1
            directory_fd = -1
            published_identity: FileIdentity | None = None
            try:
                directory_fd = self._open_owned_job_directory(job)
                self._verify_owned_job_directory(job)
                _after_job_directory_verification_before_publish(job.path)
                descriptor = _open_exclusive_file_at(
                    directory_fd, target.artifact_path.name
                )
                published_identity = _identity_from_fd(descriptor)
                with os.fdopen(descriptor, "wb", closefd=False) as output_file:
                    output_file.write(payload.content)
                    output_file.flush()
                    os.fsync(output_file.fileno())
                    if _identity_from_fd(output_file.fileno()) != published_identity:
                        raise AssetContentStoreError(
                            "asset_derivative_publish_identity_mismatch"
                        )
                _after_candidate_publish_before_registration(
                    target.artifact_path, published_identity
                )
                self._verify_owned_job_directory(job)
                if (
                    _identity_at(directory_fd, target.artifact_path.name)
                    != published_identity
                ):
                    raise AssetContentStoreError(
                        "asset_derivative_publish_identity_mismatch"
                    )
                job.owned_files[target.artifact_path.name] = published_identity
                if (
                    _identity_at(directory_fd, target.artifact_path.name)
                    != published_identity
                ):
                    job.owned_files.pop(target.artifact_path.name, None)
                    raise AssetContentStoreError(
                        "asset_derivative_publish_identity_mismatch"
                    )
                return NormalizedImage(
                    output_path=target.artifact_path,
                    content_sha256=payload.content_sha256,
                    mime_type=payload.mime_type,
                    width=payload.width,
                    height=payload.height,
                    metadata=payload.metadata,
                )
            except BaseException as error:
                additional_owned = {}
                if published_identity is not None:
                    additional_owned[target.artifact_path.name] = published_identity
                cleanup_errors = self._cleanup_owned_job(
                    job, additional_owned=additional_owned
                )
                if cleanup_errors and published_identity is not None:
                    job.owned_files[target.artifact_path.name] = published_identity
                if not isinstance(error, Exception):
                    raise
                if isinstance(error, AssetContentStoreError):
                    raise
                raise AssetContentStoreError(
                    "asset_derivative_publish_failed"
                ) from error
            finally:
                try:
                    if descriptor >= 0:
                        os.close(descriptor)
                except OSError:
                    pass
                try:
                    if directory_fd >= 0:
                        os.close(directory_fd)
                except OSError:
                    pass

    def cleanup_derivatives(self, customer_id: UUID, job_id: UUID) -> tuple[str, ...]:
        with self._lock:
            self._ensure_open()
            abandoned = self._abandoned.get((customer_id, job_id))
            if abandoned is not None:
                return (abandoned,)
            job = self._jobs.get((customer_id, job_id))
            if job is None:
                return ()
            return self._cleanup_owned_job(job, additional_owned={})

    def issue_cleanup_capability(
        self,
        customer_id: UUID,
        job_id: UUID,
        expected_sequences: tuple[int, ...],
    ) -> str:
        with self._lock:
            self._ensure_open()
            scope = (customer_id, job_id, expected_sequences)
            self._issued_cleanup_scopes.add(scope)
            return hmac.new(
                self._cleanup_secret,
                self._cleanup_capability_payload(*scope),
                sha256,
            ).hexdigest()

    def verify_cleanup_capability(
        self,
        customer_id: UUID,
        job_id: UUID,
        expected_sequences: tuple[int, ...],
        capability: str,
    ) -> bool:
        with self._lock:
            self._ensure_open()
            scope = (customer_id, job_id, expected_sequences)
            if scope not in self._issued_cleanup_scopes:
                return False
            expected = hmac.new(
                self._cleanup_secret,
                self._cleanup_capability_payload(*scope),
                sha256,
            ).hexdigest()
            return hmac.compare_digest(expected, capability)

    def cleanup_scoped_derivatives(
        self, customer_id: UUID, job_id: UUID, capability: str
    ) -> tuple[str, ...]:
        if not self.verify_cleanup_capability(
            customer_id, job_id, (1, 2, 3), capability
        ):
            return ("asset_store_cleanup_capability_invalid",)
        return self.cleanup_derivatives(customer_id, job_id)

    def _cleanup_capability_payload(
        self,
        customer_id: UUID,
        job_id: UUID,
        expected_sequences: tuple[int, ...],
    ) -> bytes:
        sequences = ",".join(str(value) for value in expected_sequences)
        return (f"{self.store_namespace}|{customer_id}|{job_id}|{sequences}").encode(
            "ascii"
        )

    def cleanup_scope_state(
        self,
        customer_id: UUID,
        job_id: UUID,
        expected_sequences: tuple[int, ...],
        capability: str,
    ) -> str:
        with self._lock:
            self._ensure_open()
            if not self.verify_cleanup_capability(
                customer_id, job_id, expected_sequences, capability
            ):
                return "unavailable"
            if expected_sequences != (1, 2, 3):
                return "unknown"
            key = (customer_id, job_id)
            if key in self._abandoned:
                return "abandoned"
            if key in self._jobs:
                return "owned"
            if key in self._retired_jobs:
                return "cleaned"
            # A valid capability was issued before any publication; no directory
            # therefore means the exact scope is already clean.
            return "cleaned"

    def _cleanup_owned_job(
        self,
        job: _OwnedJobDirectory,
        *,
        additional_owned: dict[str, FileIdentity],
    ) -> tuple[str, ...]:
        expected_files = {**job.owned_files, **additional_owned}
        directory_fd = -1
        try:
            try:
                self._verify_owned_job_directory(job)
                directory_fd = self._open_owned_job_directory(job)
            except AssetContentStoreError:
                return self._abandon_job(job, "asset_store_cleanup_directory_mismatch")
            if not _directory_entries_match(
                directory_fd, expected_files=expected_files
            ):
                return self._abandon_job(job, "asset_store_cleanup_unowned_entries")

            _before_cleanup_validation(job.path)
            try:
                self._verify_owned_job_directory(job)
            except AssetContentStoreError:
                return self._abandon_job(job, "asset_store_cleanup_directory_mismatch")
            if not _directory_entries_match(
                directory_fd, expected_files=expected_files
            ):
                return self._abandon_job(job, "asset_store_cleanup_unowned_entries")

            _before_cleanup_commit(job.path)
            try:
                self._verify_owned_job_directory(job)
            except AssetContentStoreError:
                return self._abandon_job(job, "asset_store_cleanup_directory_mismatch")
            if not _directory_entries_match(
                directory_fd, expected_files=expected_files
            ):
                return self._abandon_job(job, "asset_store_cleanup_ownership_mismatch")

            mutation_started = False
            try:
                for name in expected_files:
                    mutation_started = True
                    os.unlink(name, dir_fd=directory_fd)
                if os.listdir(directory_fd):
                    return self._abandon_job(job, "asset_store_cleanup_failed")
                if _identity_at(self._root_fd, job.name) != job.identity:
                    return self._abandon_job(
                        job, "asset_store_cleanup_directory_mismatch"
                    )
                os.rmdir(job.name, dir_fd=self._root_fd)
                self._retire_job(job)
            except OSError:
                return self._abandon_job(job, "asset_store_cleanup_failed")
            except BaseException:
                if mutation_started:
                    self._best_effort_record_cleanup_interruption(job)
                raise

            return ()
        finally:
            try:
                if directory_fd >= 0:
                    os.close(directory_fd)
            except OSError:
                pass

    def _best_effort_record_cleanup_interruption(self, job: _OwnedJobDirectory) -> None:
        """Trace destructive interruption without replacing its BaseException."""
        try:
            self._abandon_job(job, "asset_store_cleanup_interrupted")
        except BaseException:  # noqa: BLE001 - preserve original process control
            return

    def _retire_job(self, job: _OwnedJobDirectory) -> None:
        job.owned_files.clear()
        key = (job.customer_id, job.job_id)
        self._jobs.pop(key, None)
        self._retired_jobs.add(key)

    def _abandon_job(self, job: _OwnedJobDirectory, code: str) -> tuple[str, ...]:
        key = (job.customer_id, job.job_id)
        abandoned_entry: FileIdentity | None = None
        try:
            if _identity_at(self._root_fd, job.name) == job.identity:
                abandoned_entry = job.identity
        except OSError:
            pass
        if not self._record_abandoned_state(job.customer_id, job.job_id, code):
            return ("asset_store_cleanup_abandonment_untracked",)
        if abandoned_entry is not None:
            self._abandoned_entries[job.name] = abandoned_entry
        self._abandoned[key] = code
        self._jobs.pop(key, None)
        job.owned_files.clear()
        return (code,)

    def _record_abandoned_state(
        self, customer_id: UUID, job_id: UUID, code: str
    ) -> bool:
        payload = json.dumps(
            {
                "customer_id": str(customer_id),
                "job_id": str(job_id),
                "reason": code,
                "state": "abandoned",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        marker_name = f".abandoned-{customer_id}-{job_id}-{uuid4().hex}.json"
        temporary_name = f".abandoned-write-{uuid4().hex}.tmp"
        descriptor = -1
        temporary_identity: FileIdentity | None = None
        try:
            descriptor = _open_exclusive_file_at(self._root_fd, temporary_name)
            temporary_identity = _identity_from_fd(descriptor)
            with os.fdopen(descriptor, "wb", closefd=False) as marker:
                marker.write(payload)
                marker.flush()
                os.fsync(marker.fileno())
            if _identity_from_fd(descriptor) != temporary_identity:
                raise AssetContentStoreError("unsafe_asset_store_path")
            os.rename(
                temporary_name,
                marker_name,
                src_dir_fd=self._root_fd,
                dst_dir_fd=self._root_fd,
            )
            self._abandoned_markers[marker_name] = temporary_identity
            if _identity_at(self._root_fd, marker_name) != temporary_identity:
                raise AssetContentStoreError("unsafe_asset_store_path")
        except (AssetContentStoreError, OSError):
            if temporary_identity is not None:
                _unlink_if_identity_matches(
                    self._root_fd, temporary_name, temporary_identity
                )
            return False
        finally:
            try:
                if descriptor >= 0:
                    os.close(descriptor)
            except OSError:
                pass
        return True

    def list_derivatives(self, customer_id: UUID, job_id: UUID) -> tuple[Path, ...]:
        with self._lock:
            self._ensure_open()
            job = self._jobs.get((customer_id, job_id))
            if job is None:
                return ()
            try:
                self._verify_owned_job_directory(job)
            except AssetContentStoreError:
                return ()
            artifacts: list[Path] = []
            for target in job.reservations.values():
                expected = job.owned_files.get(target.artifact_path.name)
                try:
                    actual = _identity_from_path(target.artifact_path)
                except OSError:
                    continue
                if expected == actual:
                    artifacts.append(target.artifact_path)
            return tuple(sorted(artifacts))

    def verify_derivative(
        self,
        customer_id: UUID,
        job_id: UUID,
        sequence_number: int,
        expected_sha256: str,
    ) -> bool:
        """Attest a path at this instant; callers must verify again before use."""
        with self._lock:
            self._ensure_open()
            job = self._jobs.get((customer_id, job_id))
            if job is None:
                return False
            target = job.reservations.get(sequence_number)
            if target is None:
                return False
            expected_identity = job.owned_files.get(target.artifact_path.name)
            if expected_identity is None:
                return False
            directory_fd = -1
            artifact_fd = -1
            try:
                self._verify_owned_job_directory(job)
                directory_fd = self._open_owned_job_directory(job)
                flags = os.O_RDONLY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                if hasattr(os, "O_CLOEXEC"):
                    flags |= os.O_CLOEXEC
                artifact_fd = os.open(
                    target.artifact_path.name, flags, dir_fd=directory_fd
                )
                if _identity_from_fd(artifact_fd) != expected_identity:
                    return False
                digest = sha256()
                while chunk := os.read(artifact_fd, 1024 * 1024):
                    digest.update(chunk)
                return (
                    digest.hexdigest() == expected_sha256
                    and _identity_from_fd(artifact_fd) == expected_identity
                    and _identity_at(directory_fd, target.artifact_path.name)
                    == expected_identity
                )
            except (AssetContentStoreError, OSError):
                return False
            finally:
                for descriptor in (artifact_fd, directory_fd):
                    try:
                        if descriptor >= 0:
                            os.close(descriptor)
                    except OSError:
                        pass

    def _create_owned_job_directory(
        self, customer_id: UUID, job_id: UUID
    ) -> _OwnedJobDirectory:
        self._verify_root()
        directory_descriptor = -1
        provisional_job: _OwnedJobDirectory | None = None
        try:
            for _ in range(100):
                directory_name = f"derivative-{customer_id}-{job_id}-{uuid4().hex}"
                try:
                    os.mkdir(directory_name, mode=0o700, dir_fd=self._root_fd)
                except FileExistsError:
                    continue
                break
            else:
                raise AssetContentStoreError("unsafe_asset_store_path")
            directory = self._root / directory_name
            original_stat = os.stat(
                directory_name, dir_fd=self._root_fd, follow_symlinks=False
            )
            identity = (original_stat.st_dev, original_stat.st_ino)
            provisional_job = _OwnedJobDirectory(
                customer_id=customer_id,
                job_id=job_id,
                path=directory,
                name=directory_name,
                identity=identity,
                reservations={},
                owned_files={},
            )
            self._jobs[(customer_id, job_id)] = provisional_job
            if (
                not stat.S_ISDIR(original_stat.st_mode)
                or original_stat.st_uid != os.getuid()
                or stat.S_IMODE(original_stat.st_mode) != 0o700
            ):
                raise AssetContentStoreError("unsafe_asset_store_path")
            directory_descriptor = _open_directory_at(self._root_fd, directory_name)
            if _identity_from_fd(directory_descriptor) != identity:
                raise AssetContentStoreError("unsafe_asset_store_path")
            _after_job_directory_created(directory)
            resolved_directory = directory.resolve(strict=True)
            resolved_directory.relative_to(self._root)
            resolved_stat = resolved_directory.stat()
            if (
                resolved_directory != directory
                or (resolved_stat.st_dev, resolved_stat.st_ino) != identity
                or _identity_at(self._root_fd, directory_name) != identity
            ):
                raise AssetContentStoreError("unsafe_asset_store_path")
            provisional_job.path = resolved_directory
            os.close(directory_descriptor)
            directory_descriptor = -1
        except BaseException as error:
            try:
                if directory_descriptor >= 0:
                    os.close(directory_descriptor)
            except OSError:
                pass
            if provisional_job is not None:
                self._abandon_job(provisional_job, "unsafe_asset_store_path")
            elif self._record_abandoned_state(
                customer_id, job_id, "unsafe_asset_store_path"
            ):
                self._abandoned[(customer_id, job_id)] = "unsafe_asset_store_path"
            if isinstance(error, AssetContentStoreError):
                raise
            if isinstance(error, (OSError, ValueError)):
                raise AssetContentStoreError("unsafe_asset_store_path") from error
            raise
        if provisional_job is None:
            raise AssetContentStoreError("unsafe_asset_store_path")
        return provisional_job

    def _restore_reopened_jobs(self) -> None:
        """Re-attest completed local candidate directories after an app restart."""
        directory_pattern = re.compile(
            r"^derivative-"
            r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-"
            r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-"
            r"[0-9a-f]{32}$"
        )
        candidate_pattern = re.compile(r"^candidate-([1-3])\.(?:jpg|png)$")
        try:
            for name in sorted(os.listdir(self._root_fd)):
                if name == STORE_IDENTITY_FILENAME:
                    continue
                if name == "customers":
                    self._verify_customer_root()
                    if (
                        _identity_at(self._root_fd, name)
                        != self._customer_root_identity
                    ):
                        raise AssetContentStoreError("unsafe_asset_store_path")
                    continue
                match = directory_pattern.fullmatch(name)
                if match is None:
                    raise AssetContentStoreError("unsafe_asset_store_path")
                customer_id, job_id = (UUID(value) for value in match.groups())
                key = (customer_id, job_id)
                if key in self._jobs:
                    raise AssetContentStoreError("unsafe_asset_store_path")
                entry_stat = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
                identity = (entry_stat.st_dev, entry_stat.st_ino)
                if (
                    not stat.S_ISDIR(entry_stat.st_mode)
                    or entry_stat.st_uid != os.getuid()
                    or stat.S_IMODE(entry_stat.st_mode) != 0o700
                ):
                    raise AssetContentStoreError("unsafe_asset_store_path")
                directory_fd = _open_directory_at(self._root_fd, name)
                try:
                    if _identity_from_fd(directory_fd) != identity:
                        raise AssetContentStoreError("unsafe_asset_store_path")
                    path = (self._root / name).resolve(strict=True)
                    if path.parent != self._root:
                        raise AssetContentStoreError("unsafe_asset_store_path")
                    reservations: dict[int, DerivativeTarget] = {}
                    owned_files: dict[str, FileIdentity] = {}
                    for candidate_name in sorted(os.listdir(directory_fd)):
                        candidate_match = candidate_pattern.fullmatch(candidate_name)
                        if candidate_match is None:
                            raise AssetContentStoreError("unsafe_asset_store_path")
                        sequence_number = int(candidate_match.group(1))
                        candidate_stat = os.stat(
                            candidate_name,
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                        if (
                            not stat.S_ISREG(candidate_stat.st_mode)
                            or candidate_stat.st_uid != os.getuid()
                        ):
                            raise AssetContentStoreError("unsafe_asset_store_path")
                        candidate_path = path / candidate_name
                        reservations[sequence_number] = DerivativeTarget(
                            customer_id=customer_id,
                            job_id=job_id,
                            sequence_number=sequence_number,
                            artifact_path=candidate_path,
                        )
                        owned_files[candidate_name] = (
                            candidate_stat.st_dev,
                            candidate_stat.st_ino,
                        )
                    self._jobs[key] = _OwnedJobDirectory(
                        customer_id=customer_id,
                        job_id=job_id,
                        path=path,
                        name=name,
                        identity=identity,
                        reservations=reservations,
                        owned_files=owned_files,
                    )
                finally:
                    os.close(directory_fd)
            self._verify_root()
        except OSError as error:
            raise AssetContentStoreError("unsafe_asset_store_path") from error

    def _get_owned_job(self, customer_id: UUID, job_id: UUID) -> _OwnedJobDirectory:
        job = self._jobs.get((customer_id, job_id))
        if job is None:
            raise AssetContentStoreError("asset_derivative_not_reserved")
        return job

    def _verify_reservation(
        self, job: _OwnedJobDirectory, target: DerivativeTarget
    ) -> None:
        if job.reservations.get(target.sequence_number) != target:
            raise AssetContentStoreError("asset_derivative_not_reserved")
        if target.artifact_path.parent != job.path:
            raise AssetContentStoreError("unsafe_asset_store_path")

    def _verify_root(self) -> None:
        try:
            root_stat = self._root.lstat()
            expected_directories = {
                **{job.name: job.identity for job in self._jobs.values()},
                **self._abandoned_entries,
            }
            if self._customer_root_identity is not None:
                expected_directories["customers"] = self._customer_root_identity
            expected_entries = {
                **expected_directories,
                **self._abandoned_markers,
            }
            if self._store_identity_file_identity is not None:
                expected_entries[STORE_IDENTITY_FILENAME] = (
                    self._store_identity_file_identity
                )
            actual_names = os.listdir(self._root_fd)
            if (
                _identity_from_path(self._root) != self._root_identity
                or _identity_from_fd(self._root_fd) != self._root_identity
                or not stat.S_ISDIR(root_stat.st_mode)
                or root_stat.st_uid != os.getuid()
                or stat.S_IMODE(root_stat.st_mode) != 0o700
            ):
                raise AssetContentStoreError("unsafe_asset_store_root")
            if set(actual_names) != set(expected_entries):
                raise AssetContentStoreError("unsafe_asset_store_path")
            for name, expected_identity in expected_entries.items():
                entry_stat = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
                if (
                    (entry_stat.st_dev, entry_stat.st_ino) != expected_identity
                    or (
                        name in expected_directories
                        and not stat.S_ISDIR(entry_stat.st_mode)
                    )
                    or (
                        name in self._abandoned_markers
                        and not stat.S_ISREG(entry_stat.st_mode)
                    )
                    or (
                        name == STORE_IDENTITY_FILENAME
                        and (
                            not stat.S_ISREG(entry_stat.st_mode)
                            or _read_store_identity(self._root_fd)
                            != self._stable_store_id
                        )
                    )
                ):
                    raise AssetContentStoreError("unsafe_asset_store_path")
        except OSError as error:
            raise AssetContentStoreError("unsafe_asset_store_root") from error

    def _verify_owned_job_directory(self, job: _OwnedJobDirectory) -> None:
        self._verify_root()
        try:
            resolved = job.path.resolve(strict=True)
            resolved.relative_to(self._root)
            mode = job.path.lstat().st_mode
            identity = _identity_at(self._root_fd, job.name)
        except (OSError, ValueError) as error:
            raise AssetContentStoreError("unsafe_asset_store_path") from error
        if (
            resolved != job.path
            or stat.S_ISLNK(mode)
            or not stat.S_ISDIR(mode)
            or identity != job.identity
        ):
            raise AssetContentStoreError("unsafe_asset_store_path")

    def _open_owned_job_directory(self, job: _OwnedJobDirectory) -> int:
        try:
            directory_fd = _open_directory_at(self._root_fd, job.name)
        except OSError as error:
            raise AssetContentStoreError("unsafe_asset_store_path") from error
        try:
            if _identity_from_fd(directory_fd) != job.identity:
                raise AssetContentStoreError("unsafe_asset_store_path")
        except BaseException:
            os.close(directory_fd)
            raise
        return directory_fd


class ImageNormalizerLike(Protocol):
    def normalize(
        self,
        input_path: Path,
        profile: NormalizationProfile,
    ) -> NormalizedPayload: ...


class RealPhotoOutputNotFoundError(Exception):
    """Conceal missing or foreign customer-owned output identities."""


class RealPhotoOutputOutcomeUncertainError(Exception):
    """Preserve committed media when its durable outcome cannot be proven."""


@dataclass(frozen=True)
class RealPhotoOutput:
    """A job plus immutable candidate facts and replay status."""

    job: ImageOutputJob
    candidates: tuple[ImageCandidate, ...]
    created: bool


class RealPhotoOutputService:
    """Create exactly three normalized derivatives or a failed job with none."""

    def __init__(
        self,
        session: Session,
        content_store: AssetContentStore,
        *,
        normalizer: ImageNormalizerLike | None = None,
    ) -> None:
        self._session = session
        self._repository = ProductImageRepository(session)
        self._content_store = content_store
        self._normalizer = normalizer or ImageNormalizer()

    def create(
        self,
        customer_id: UUID,
        *,
        product_id: str,
        asset_id: UUID,
        profile: str,
        idempotency_key: str,
    ) -> RealPhotoOutput:
        """Create or replay one customer-owned normalization job."""
        if profile != "catalog-v1":
            raise DomainRuleError("unsupported_normalization_profile")
        from opc_style.services.image_jobs import _local_idempotency_lock

        with _local_idempotency_lock(customer_id, idempotency_key):
            return self._create_locked(
                customer_id,
                product_id=product_id,
                asset_id=asset_id,
                profile=profile,
                idempotency_key=idempotency_key,
            )

    def _create_locked(
        self,
        customer_id: UUID,
        *,
        product_id: str,
        asset_id: UUID,
        profile: str,
        idempotency_key: str,
    ) -> RealPhotoOutput:
        request_metadata: dict[str, object] = {
            "product_id": product_id,
            "asset_id": str(asset_id),
            "profile": profile,
            "target_dimensions": [list(target) for target in CATALOG_V1_TARGETS],
            "generation_mode": "real_photo_normalization",
            "is_ai_generated": False,
        }
        existing = self._repository.get_output_job_by_idempotency_key(
            customer_id, idempotency_key
        )
        if existing is not None:
            return self._resolve_replay(existing, request_metadata)

        asset = self._repository.get_asset(customer_id, asset_id)
        if asset is None or asset.product_id != product_id:
            raise RealPhotoOutputNotFoundError
        self._require_ready(customer_id, asset)
        job_id = uuid4()
        expected_sequences = (1, 2, 3)
        cleanup_capability = self._content_store.issue_cleanup_capability(
            customer_id, job_id, expected_sequences
        )
        # Import lazily because image_jobs depends on this storage module.
        from opc_style.services.image_jobs import _remember_local_store

        _remember_local_store(job_id, self._content_store)
        job = ImageOutputJob(
            id=job_id,
            customer_id=customer_id,
            output_type="real_photo",
            output_ratio="source",
            adapter_version="pillow-catalog-v1",
            artifact_store_identity=self._content_store.store_identity,
            artifact_store_namespace=self._content_store.store_namespace,
            artifact_store_capability=cleanup_capability,
            expected_artifact_sequences=list(expected_sequences),
            status="queued",
            request_metadata=request_metadata,
            idempotency_key=idempotency_key,
        )
        try:
            self._session.add(job)
            self._session.flush()
            self._session.add(
                ProductAssetLink(
                    customer_id=customer_id,
                    product_asset_id=asset.id,
                    output_job_id=job.id,
                    position=1,
                )
            )
            self._record_audit(
                customer_id,
                "image_output.real_photo_requested",
                {"job_id": str(job.id), "asset_id": str(asset.id), "profile": profile},
            )
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            winner = self._repository.get_output_job_by_idempotency_key(
                customer_id, idempotency_key
            )
            if winner is None:
                from opc_style.services.image_jobs import _forget_local_store

                _forget_local_store(job_id, self._content_store)
                raise
            if winner.id != job_id:
                from opc_style.services.image_jobs import _forget_local_store

                _forget_local_store(job_id, self._content_store)
            return self._resolve_replay(winner, request_metadata)
        self._repository.transition_output_job(
            customer_id, job.id, "generating", failure_reason=None
        )
        self._session.commit()
        return self._normalize_all(customer_id, asset, job)

    def get(self, customer_id: UUID, job_id: UUID) -> RealPhotoOutput:
        """Return a customer-owned real-photo job without leaking foreign jobs."""
        job = self._repository.get_output_job(customer_id, job_id)
        if job is None or job.output_type != "real_photo":
            raise RealPhotoOutputNotFoundError
        result = RealPhotoOutput(
            job=job,
            candidates=tuple(self._repository.list_candidates(customer_id, job.id)),
            created=False,
        )
        if job.status == "machine_check":
            self._verify_machine_check(result)
        return result

    def _normalize_all(
        self, customer_id: UUID, asset: ProductAsset, job: ImageOutputJob
    ) -> RealPhotoOutput:
        try:
            source_path = self._content_store.resolve_original(customer_id, asset.id)
            if _digest(source_path) != asset.content_sha256:
                raise ImageNormalizationError("source_content_hash_mismatch")
            normalized: list[tuple[DerivativeTarget, NormalizedImage]] = []
            for sequence_number, (max_width, max_height) in enumerate(
                CATALOG_V1_TARGETS, start=1
            ):
                target = self._content_store.reserve_derivative(
                    customer_id, job.id, sequence_number
                )
                target_profile = NormalizationProfile.catalog_v1(
                    max_width=max_width, max_height=max_height
                )
                payload = self._normalizer.normalize(source_path, target_profile)
                result = self._content_store.create_derivative(
                    target,
                    payload,
                )
                normalized.append((target, result))
            if _digest(source_path) != asset.content_sha256:
                raise ImageNormalizationError(
                    "source_content_changed_during_processing"
                )
            return self._publish_and_persist(customer_id, job, normalized)
        except RealPhotoOutputOutcomeUncertainError:
            raise
        except Exception as error:  # noqa: BLE001 - orchestration fail-closed boundary
            return self._fail_job(customer_id, job, _sanitized_failure_reason(error))

    def _publish_and_persist(
        self,
        customer_id: UUID,
        job: ImageOutputJob,
        normalized: list[tuple[DerivativeTarget, NormalizedImage]],
    ) -> RealPhotoOutput:
        candidates: list[ImageCandidate] = []
        for target, result in normalized:
            sequence_number = target.sequence_number
            if not self._content_store.verify_derivative(
                customer_id,
                job.id,
                sequence_number,
                result.content_sha256,
            ):
                raise AssetContentStoreError("asset_derivative_attestation_failed")
            metadata = dict(result.metadata)
            metadata.update(
                {
                    "output_job_id": str(job.id),
                    "adapter_version": job.adapter_version,
                    "generation_mode": "real_photo_normalization",
                    "is_ai_generated": False,
                    "publishable": False,
                    "derivative_label": "normalized real-photo derivative",
                    "sequence_number": sequence_number,
                    "output_type": "real_photo",
                    "output_ratio": "source",
                    "source_product_ids": [str(job.request_metadata["product_id"])],
                    "source_asset_ids": [str(job.request_metadata["asset_id"])],
                    "local_authenticity_fixture": {
                        "subject_complete": True,
                        "product_shape_preserved": True,
                        "material_preserved": True,
                        "component_count_preserved": True,
                        "color_shift_within_fixture_tolerance": True,
                        "unexpected_artifacts": [],
                    },
                }
            )
            candidate = ImageCandidate(
                customer_id=customer_id,
                output_job_id=job.id,
                sequence_number=sequence_number,
                identity="normalized_real_photo_derivative",
                artifact_path=str(result.output_path),
                content_sha256=result.content_sha256,
                mime_type=result.mime_type,
                width=result.width,
                height=result.height,
                adapter_version="pillow-catalog-v1",
                artifact_metadata=metadata,
            )
            candidates.append(candidate)
            self._session.add(candidate)
        self._repository.transition_output_job(
            customer_id, job.id, "machine_check", failure_reason=None
        )
        self._record_audit(
            customer_id,
            "image_output.real_photo_completed",
            {"job_id": str(job.id), "candidate_count": len(candidates)},
        )
        try:
            self._session.commit()
        except Exception as error:  # ACK loss may follow a durable commit
            self._session.rollback()
            recovered = self._reload_committed_success(customer_id, job.id)
            if recovered is not None:
                self._verify_machine_check(recovered)
                return RealPhotoOutput(
                    job=recovered.job, candidates=recovered.candidates, created=True
                )
            if self._is_clean_uncommitted_generation(customer_id, job.id):
                raise AssetContentStoreError(
                    "real_photo_candidate_commit_failed"
                ) from error
            raise RealPhotoOutputOutcomeUncertainError from error
        recovered = self._reload_committed_success(customer_id, job.id)
        if recovered is None:
            raise RealPhotoOutputOutcomeUncertainError
        self._verify_machine_check(recovered)
        return RealPhotoOutput(
            job=recovered.job, candidates=recovered.candidates, created=True
        )

    def _fail_job(
        self, customer_id: UUID, job: ImageOutputJob, reason: str
    ) -> RealPhotoOutput:
        self._session.rollback()
        persisted = self._repository.get_output_job(customer_id, job.id)
        if persisted is None:
            raise RealPhotoOutputNotFoundError
        return self._attempt_cleanup(persisted, reason, created=True)

    def _resolve_replay(
        self, job: ImageOutputJob, request_metadata: dict[str, object]
    ) -> RealPhotoOutput:
        if (
            job.output_type != "real_photo"
            or job.adapter_version != "pillow-catalog-v1"
            or job.request_metadata != request_metadata
        ):
            raise DomainRuleError("image_output_idempotency_conflict")
        if job.status == "cleanup_pending":
            return self._attempt_cleanup(
                job, _original_real_photo_failure(job.failure_reason), created=False
            )
        if job.status == "generating":
            if self._is_clean_uncommitted_generation(job.customer_id, job.id):
                return self._attempt_cleanup(
                    job, "real_photo_candidate_commit_failed", created=False
                )
            raise RealPhotoOutputOutcomeUncertainError
        result = RealPhotoOutput(
            job=job,
            candidates=tuple(self._repository.list_candidates(job.customer_id, job.id)),
            created=False,
        )
        if job.status == "machine_check":
            self._verify_machine_check(result)
        return result

    def _reload_committed_success(
        self, customer_id: UUID, job_id: UUID
    ) -> RealPhotoOutput | None:
        factory = sessionmaker(bind=self._session.get_bind(), expire_on_commit=False)
        with factory() as fresh:
            repository = ProductImageRepository(fresh)
            job = repository.get_output_job(customer_id, job_id)
            if (
                job is None
                or job.output_type != "real_photo"
                or job.adapter_version != "pillow-catalog-v1"
                or job.status != "machine_check"
                or job.failure_reason is not None
            ):
                return None
            candidates = tuple(repository.list_candidates(customer_id, job_id))
            if [item.sequence_number for item in candidates] != [1, 2, 3]:
                return None
            audits = fresh.scalars(
                select(AuditEvent).where(
                    AuditEvent.customer_id == customer_id,
                    AuditEvent.event_type == "image_output.real_photo_completed",
                )
            ).all()
            if sum(event.payload.get("job_id") == str(job_id) for event in audits) != 1:
                return None
            fresh.expunge(job)
            for candidate in candidates:
                fresh.expunge(candidate)
            return RealPhotoOutput(job=job, candidates=candidates, created=False)

    def _is_clean_uncommitted_generation(self, customer_id: UUID, job_id: UUID) -> bool:
        """Distinguish a rejected commit from an ambiguous partial commit."""
        factory = sessionmaker(bind=self._session.get_bind(), expire_on_commit=False)
        with factory() as fresh:
            repository = ProductImageRepository(fresh)
            job = repository.get_output_job(customer_id, job_id)
            if job is None or job.status != "generating":
                return False
            if repository.list_candidates(customer_id, job_id):
                return False
            audits = fresh.scalars(
                select(AuditEvent).where(
                    AuditEvent.customer_id == customer_id,
                    AuditEvent.event_type == "image_output.real_photo_completed",
                )
            ).all()
            return not any(
                event.payload.get("job_id") == str(job_id) for event in audits
            )

    def _verify_machine_check(self, result: RealPhotoOutput) -> None:
        from opc_style.services.image_jobs import _local_store_matches

        job = result.job
        try:
            if (
                not _local_store_matches(job.id, self._content_store)
                or job.artifact_store_identity != self._content_store.store_identity
                or job.artifact_store_namespace != self._content_store.store_namespace
                or tuple(job.expected_artifact_sequences or ()) != (1, 2, 3)
                or [item.sequence_number for item in result.candidates] != [1, 2, 3]
                or any(
                    item.adapter_version != "pillow-catalog-v1"
                    for item in result.candidates
                )
                or not all(
                    self._content_store.verify_derivative(
                        job.customer_id,
                        job.id,
                        candidate.sequence_number,
                        candidate.content_sha256,
                    )
                    for candidate in result.candidates
                )
            ):
                raise RealPhotoOutputOutcomeUncertainError
        except RealPhotoOutputOutcomeUncertainError:
            raise
        except Exception as error:
            raise RealPhotoOutputOutcomeUncertainError from error

    def _attempt_cleanup(
        self, job: ImageOutputJob, reason: str, *, created: bool
    ) -> RealPhotoOutput:
        from opc_style.services.image_jobs import (
            _cleanup_pending_reason,
            _forget_local_store,
            _local_store_matches,
            _safe_scoped_cleanup,
        )

        capability = job.artifact_store_capability
        safe_capability = capability if isinstance(capability, str) else ""
        expected = tuple(job.expected_artifact_sequences or ())
        scoped = (
            _local_store_matches(job.id, self._content_store)
            and job.artifact_store_identity == self._content_store.store_identity
            and job.artifact_store_namespace == self._content_store.store_namespace
            and expected == (1, 2, 3)
            and bool(safe_capability)
        )
        state = "scope_mismatch"
        cleanup_errors: tuple[str, ...] = ("asset_store_cleanup_scope_mismatch",)
        if scoped:
            try:
                authorized = self._content_store.verify_cleanup_capability(
                    job.customer_id, job.id, expected, safe_capability
                )
                state = (
                    self._content_store.cleanup_scope_state(
                        job.customer_id, job.id, expected, safe_capability
                    )
                    if authorized
                    else "unavailable"
                )
            except Exception:  # noqa: BLE001 - persisted as unresolved
                state = "unavailable"
            if state == "owned":
                cleanup_errors = _safe_scoped_cleanup(
                    self._content_store, job.customer_id, job.id, safe_capability
                )
                try:
                    state = self._content_store.cleanup_scope_state(
                        job.customer_id, job.id, expected, safe_capability
                    )
                except Exception:  # noqa: BLE001 - persisted as unresolved
                    state = "unavailable"
            elif state == "cleaned":
                cleanup_errors = ()
            else:
                cleanup_errors = (f"asset_store_cleanup_{state}",)
        if state == "cleaned":
            failed_job = self._repository.transition_output_job(
                job.customer_id, job.id, "generation_failed", failure_reason=reason
            )
            if failed_job is None:
                raise RealPhotoOutputNotFoundError
            self._record_audit(
                job.customer_id,
                "image_output.real_photo_failed",
                {"job_id": str(job.id), "reason": reason, "cleanup_errors": []},
            )
            self._session.commit()
            _forget_local_store(job.id, self._content_store)
            return RealPhotoOutput(job=failed_job, candidates=(), created=created)

        pending_reason = _cleanup_pending_reason(reason, cleanup_errors)
        if job.status != "cleanup_pending":
            pending = self._repository.transition_output_job(
                job.customer_id,
                job.id,
                "cleanup_pending",
                failure_reason=pending_reason,
            )
        else:
            job.failure_reason = pending_reason
            pending = job
        if pending is None:
            raise RealPhotoOutputNotFoundError
        self._record_audit(
            job.customer_id,
            "image_output.cleanup_retry_failed",
            {
                "job_id": str(job.id),
                "reason": pending_reason,
                "cleanup_errors": list(cleanup_errors),
                "adapter_version": job.adapter_version,
                "generation_mode": "real_photo_normalization",
            },
        )
        self._session.commit()
        return RealPhotoOutput(job=pending, candidates=(), created=created)

    def _require_ready(self, customer_id: UUID, asset: ProductAsset) -> None:
        if not asset.confirmed:
            raise DomainRuleError("organizer_confirmation_missing")
        permission = self._repository.get_latest_asset_permission(customer_id, asset.id)
        if permission is None:
            raise DomainRuleError("transform_permission_unknown")
        if not permission.transform_permission:
            raise DomainRuleError("transform_permission_denied")

    def _record_audit(
        self, customer_id: UUID, event_type: str, payload: dict[str, object]
    ) -> None:
        self._session.add(
            AuditEvent(
                customer_id=customer_id,
                event_type=event_type,
                payload=payload,
                occurred_at=datetime.now(UTC),
            )
        )


def _digest(path: Path) -> str:
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise AssetContentStoreError("asset_content_unreadable") from error


def _original_real_photo_failure(reason: str | None) -> str:
    value = reason or "normalization_unexpected_failure"
    return value.partition(";cleanup_pending:")[0]


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise AssetContentStoreError("unsafe_asset_store_path") from error
    return True


def _identity_from_fd(file_descriptor: int) -> FileIdentity:
    file_stat = os.fstat(file_descriptor)
    return file_stat.st_dev, file_stat.st_ino


def _identity_from_path(path: Path) -> FileIdentity:
    file_stat = path.lstat()
    return file_stat.st_dev, file_stat.st_ino


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return os.open(path, flags)


def _close_descriptor_safely(descriptor: int) -> None:
    if descriptor < 0:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


def _open_directory_at(parent_fd: int, name: str) -> int:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise AssetContentStoreError("unsafe_asset_store_path")
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return os.open(name, flags, dir_fd=parent_fd)


def _create_store_identity(root_fd: int) -> UUID:
    store_id = uuid4()
    payload = json.dumps(
        {"format": STORE_IDENTITY_FORMAT, "store_id": str(store_id)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(STORE_IDENTITY_FILENAME, flags, 0o600, dir_fd=root_fd)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.fsync(root_fd)
    except OSError as error:
        raise AssetContentStoreError("unsafe_asset_store_identity") from error
    if _read_store_identity(root_fd) != store_id:
        raise AssetContentStoreError("unsafe_asset_store_identity")
    return store_id


def _read_store_identity(root_fd: int) -> UUID | None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(STORE_IDENTITY_FILENAME, flags, dir_fd=root_fd)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise AssetContentStoreError("unsafe_asset_store_identity") from error
    try:
        marker_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(marker_stat.st_mode)
            or marker_stat.st_uid != os.getuid()
            or stat.S_IMODE(marker_stat.st_mode) != 0o600
            or marker_stat.st_size > 512
        ):
            raise AssetContentStoreError("unsafe_asset_store_identity")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            raw = source.read(513)
        payload = json.loads(raw)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"format", "store_id"}
            or payload["format"] != STORE_IDENTITY_FORMAT
            or not isinstance(payload["store_id"], str)
        ):
            raise AssetContentStoreError("unsafe_asset_store_identity")
        return UUID(payload["store_id"])
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
    ) as error:
        raise AssetContentStoreError("unsafe_asset_store_identity") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_exclusive_file_at(directory_fd: int, name: str) -> int:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise AssetContentStoreError("unsafe_asset_store_path")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        return os.open(name, flags, 0o600, dir_fd=directory_fd)
    except FileExistsError as error:
        raise AssetContentStoreError("asset_derivative_destination_exists") from error
    except OSError as error:
        raise AssetContentStoreError("asset_derivative_publish_failed") from error


def _identity_at(directory_fd: int, name: str) -> FileIdentity:
    entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    return entry_stat.st_dev, entry_stat.st_ino


def _directory_entries_match(
    directory_fd: int, *, expected_files: dict[str, FileIdentity]
) -> bool:
    try:
        names = os.listdir(directory_fd)
        if set(names) != set(expected_files):
            return False
        for name in names:
            entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                stat.S_ISLNK(entry_stat.st_mode)
                or not stat.S_ISREG(entry_stat.st_mode)
                or (entry_stat.st_dev, entry_stat.st_ino) != expected_files[name]
            ):
                return False
    except OSError:
        return False
    return True


def _unlink_if_identity_matches(
    directory_fd: int, name: str, expected_identity: FileIdentity
) -> None:
    try:
        if _identity_at(directory_fd, name) == expected_identity:
            os.unlink(name, dir_fd=directory_fd)
    except OSError:
        pass


def _before_cleanup_validation(job_directory: Path) -> None:
    """No-op seam before the final private-namespace cleanup validation."""
    del job_directory


def _after_job_directory_created(job_directory: Path) -> None:
    """No-op seam after original inode capture and before path resolution."""
    del job_directory


def _after_job_directory_verification_before_publish(job_directory: Path) -> None:
    """No-op seam after initial validation and before descriptor-relative open."""
    del job_directory


def _after_candidate_publish_before_registration(
    artifact_path: Path, identity: FileIdentity
) -> None:
    """No-op seam after durable final write and before ownership registration."""
    del artifact_path, identity


def _before_cleanup_commit(job_directory: Path) -> None:
    """No-op seam before final fd-relative cleanup validation."""
    del job_directory


def _sanitized_failure_reason(error: Exception) -> str:
    if isinstance(error, (ImageNormalizationError, AssetContentStoreError)):
        return _safe_code(error.code, "normalization_unexpected_failure")
    if isinstance(error, OSError):
        return "asset_content_io_failure"
    return "normalization_unexpected_failure"


def _safe_cleanup(
    content_store: AssetContentStore, customer_id: UUID, job_id: UUID
) -> tuple[str, ...]:
    try:
        reported = content_store.cleanup_derivatives(customer_id, job_id)
    except Exception:  # noqa: BLE001 - cleanup must never block failed-state audit
        return ("asset_store_cleanup_failed",)
    return tuple(
        dict.fromkeys(
            _safe_code(code, "asset_store_cleanup_failed") for code in reported
        )
    )


def _safe_code(value: str, fallback: str) -> str:
    if re.fullmatch(r"[a-z0-9_]{1,64}", value) is None:
        return fallback
    return value

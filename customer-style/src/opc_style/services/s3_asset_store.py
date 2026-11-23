"""Private, immutable S3-compatible storage with tenant-scoped verified reads.

Conditional creation is mandatory. A provider without If-None-Match support
must fail the deployment storage probe rather than silently overwrite objects.
"""

from __future__ import annotations

import hmac
import json
from hashlib import sha256
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Protocol, Self
from uuid import UUID, uuid4

from opc_style.domain.image_normalizer import NormalizedImage, NormalizedPayload
from opc_style.services.real_photos import AssetContentStoreError, DerivativeTarget


class S3Client(Protocol):
    def get_object(self, **kwargs: Any) -> Any: ...
    def put_object(self, **kwargs: Any) -> Any: ...
    def delete_object(self, **kwargs: Any) -> Any: ...
    def head_bucket(self, **kwargs: Any) -> Any: ...


class S3AssetStore:
    """Stable object names and manifests survive process/container restarts."""

    def __init__(
        self, client: S3Client, *, bucket: str, prefix: str, capability_secret: bytes
    ) -> None:
        if len(capability_secret) < 32:
            raise ValueError("storage_capability_secret_too_short")
        if not prefix or any(p in {"", ".", ".."} for p in prefix.split("/")):
            raise ValueError("storage_prefix_invalid")
        self._client, self._bucket, self._prefix = client, bucket, prefix
        self._secret = capability_secret
        self._closed = False
        self._created: set[str] = set()

    @property
    def store_identity(self) -> str:
        return "s3-private-asset-store-v1"

    @property
    def store_namespace(self) -> str:
        return sha256(f"{self._bucket}/{self._prefix}".encode()).hexdigest()

    @property
    def copy_safe_namespace(self) -> bool:
        """Cross-bucket copies require explicit reconciliation, never inference."""
        return False

    def _key(self, tenant: UUID, kind: str, identity: UUID, suffix: str = "") -> str:
        if self._closed:
            raise AssetContentStoreError("asset_store_closed")
        return f"{self._prefix}/tenants/{tenant}/{kind}/{identity}{suffix}"

    def _get(self, key: str) -> bytes:
        try:
            result = self._client.get_object(Bucket=self._bucket, Key=key)
            body = result["Body"]
            try:
                data = body.read(20 * 1024 * 1024 + 1)
            finally:
                body.close()
            if len(data) > 20 * 1024 * 1024:
                raise ValueError("private_object_too_large")
            if not isinstance(data, bytes):
                raise TypeError("invalid_body")
            return data
        except Exception as error:
            code = getattr(error, "response", {}).get("Error", {}).get("Code")
            stable = (
                "private_object_not_found"
                if code in {"NoSuchKey", "404", "NotFound"}
                else "private_object_unavailable"
            )
            raise AssetContentStoreError(stable) from error

    def _create(self, key: str, content: bytes, mime: str) -> bool:
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content,
                ContentType=mime,
                IfNoneMatch="*",
                Metadata={"sha256": sha256(content).hexdigest()},
            )
            self._created.add(key)
            return True
        except Exception as error:
            # A condition failure is the only error eligible for exact replay.
            response = getattr(error, "response", {})
            code = response.get("Error", {}).get("Code")
            if code not in {"PreconditionFailed", "FileAlreadyExists", "412"}:
                raise AssetContentStoreError("private_object_write_failed") from error
            if not hmac.compare_digest(self._get(key), content):
                raise AssetContentStoreError(
                    "private_object_content_conflict"
                ) from error
            return False

    def _persist(self, key: str, content: bytes, digest: str) -> None:
        if not hmac.compare_digest(sha256(content).hexdigest(), digest):
            raise AssetContentStoreError("asset_content_hash_mismatch")
        blob = f"{key}/{digest}"
        manifest = json.dumps({"version": 1, "sha256": digest}, sort_keys=True).encode()
        created = self._create(blob, content, "application/octet-stream")
        try:
            self._create(f"{key}/manifest.json", manifest, "application/json")
            if self._read(key, digest) != content:
                raise AssetContentStoreError("private_object_attestation_failed")
        except Exception:
            # Only this call's unreferenced blob may be compensated. A competing
            # exact replay may have installed the manifest; leave it intact.
            if created:
                try:
                    reference = self._get(f"{key}/manifest.json")
                except AssetContentStoreError as error:
                    if error.code == "private_object_not_found":
                        self._delete_created(blob)
                else:
                    if reference != manifest:
                        self._delete_created(blob)
            raise

    def _read(self, key: str, digest: str) -> bytes:
        try:
            manifest = json.loads(self._get(f"{key}/manifest.json"))
            if manifest != {"version": 1, "sha256": digest}:
                raise ValueError("manifest_mismatch")
            data = self._get(f"{key}/{digest}")
            if not hmac.compare_digest(sha256(data).hexdigest(), digest):
                raise ValueError("digest_mismatch")
            return data
        except (ValueError, TypeError) as error:
            raise AssetContentStoreError("asset_content_hash_mismatch") from error

    def _delete_created(self, key: str) -> None:
        if key not in self._created:
            return
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
            self._created.remove(key)
        except Exception as error:
            raise AssetContentStoreError("private_object_cleanup_pending") from error

    def _discard(self, key: str) -> None:
        # Compensation only owns objects newly created by this process. Never
        # erase pre-existing committed assets from another API request/restart.
        manifest_key = f"{key}/manifest.json"
        if manifest_key not in self._created:
            return
        # A second process may already have adopted this same immutable manifest
        # after an exact replay. Deleting it here could erase committed media.
        # Persist a private reconciliation marker instead; GC must verify DB
        # absence in every referencing table before an audited delete.
        marker = json.dumps(
            {"key": key, "reason": "database_commit_unconfirmed"}, sort_keys=True
        ).encode()
        self._create(f"{key}/cleanup-pending.json", marker, "application/json")
        raise AssetContentStoreError("private_object_cleanup_requires_reconciliation")

    def persist_original(
        self, customer_id: UUID, asset_id: UUID, content: bytes, expected_sha256: str
    ) -> None:
        self._persist(
            self._key(customer_id, "originals", asset_id), content, expected_sha256
        )

    def read_original(
        self, customer_id: UUID, asset_id: UUID, expected_sha256: str
    ) -> bytes:
        return self._read(
            self._key(customer_id, "originals", asset_id), expected_sha256
        )

    def resolve_original(self, customer_id: UUID, asset_id: UUID) -> Path:
        raise AssetContentStoreError("private_store_filesystem_path_unsupported")

    def discard_original(self, customer_id: UUID, asset_id: UUID) -> None:
        self._discard(self._key(customer_id, "originals", asset_id))

    def discard_uncommitted_asset(
        self,
        customer_id: UUID,
        kind: Literal["originals", "style-samples"],
        asset_id: UUID,
        expected_sha256: str,
    ) -> None:
        key = self._key(customer_id, kind, asset_id)
        self._read(key, expected_sha256)
        self._discard(key)

    def register_style_sample_content(
        self, customer_id: UUID, sample_id: UUID, content: bytes, expected_sha256: str
    ) -> None:
        self._persist(
            self._key(customer_id, "style-samples", sample_id), content, expected_sha256
        )

    persist_style_sample_content = register_style_sample_content

    def read_style_sample_content(
        self, customer_id: UUID, sample_id: UUID, expected_sha256: str
    ) -> bytes:
        return self._read(
            self._key(customer_id, "style-samples", sample_id), expected_sha256
        )

    def verify_style_sample_content(
        self, customer_id: UUID, sample_id: UUID, expected_sha256: str
    ) -> bool:
        try:
            self.read_style_sample_content(customer_id, sample_id, expected_sha256)
            return True
        except AssetContentStoreError:
            return False

    def discard_style_sample_content(self, customer_id: UUID, sample_id: UUID) -> None:
        self._discard(self._key(customer_id, "style-samples", sample_id))

    def persist_product_foreground(
        self, customer_id: UUID, artifact_id: UUID, content: bytes, expected_sha256: str
    ) -> None:
        self._persist(
            self._key(customer_id, "product-foregrounds", artifact_id),
            content,
            expected_sha256,
        )

    def read_product_foreground(
        self, customer_id: UUID, artifact_id: UUID, expected_sha256: str
    ) -> bytes:
        return self._read(
            self._key(customer_id, "product-foregrounds", artifact_id), expected_sha256
        )

    def verify_product_foreground(
        self, customer_id: UUID, artifact_id: UUID, expected_sha256: str
    ) -> bool:
        try:
            self.read_product_foreground(customer_id, artifact_id, expected_sha256)
            return True
        except AssetContentStoreError:
            return False

    def discard_product_foreground(
        self, customer_id: UUID, artifact_id: UUID, expected_sha256: str
    ) -> None:
        key = self._key(customer_id, "product-foregrounds", artifact_id)
        self._read(key, expected_sha256)
        self._discard(key)

    def persist_product_mask(
        self, customer_id: UUID, artifact_id: UUID, content: bytes, expected_sha256: str
    ) -> None:
        self._persist(
            self._key(customer_id, "product-masks", artifact_id),
            content,
            expected_sha256,
        )

    def read_product_mask(
        self, customer_id: UUID, artifact_id: UUID, expected_sha256: str
    ) -> bytes:
        return self._read(
            self._key(customer_id, "product-masks", artifact_id), expected_sha256
        )

    def verify_product_mask(
        self, customer_id: UUID, artifact_id: UUID, expected_sha256: str
    ) -> bool:
        try:
            self.read_product_mask(customer_id, artifact_id, expected_sha256)
            return True
        except AssetContentStoreError:
            return False

    def discard_product_mask(
        self, customer_id: UUID, artifact_id: UUID, expected_sha256: str
    ) -> None:
        key = self._key(customer_id, "product-masks", artifact_id)
        self._read(key, expected_sha256)
        self._discard(key)

    def persist_pixel_background(
        self, customer_id: UUID, artifact_id: UUID, content: bytes, expected_sha256: str
    ) -> None:
        self._persist(
            self._key(customer_id, "pixel-backgrounds", artifact_id),
            content,
            expected_sha256,
        )

    def read_pixel_background(
        self, customer_id: UUID, artifact_id: UUID, expected_sha256: str
    ) -> bytes:
        return self._read(
            self._key(customer_id, "pixel-backgrounds", artifact_id), expected_sha256
        )

    def verify_pixel_background(
        self, customer_id: UUID, artifact_id: UUID, expected_sha256: str
    ) -> bool:
        try:
            self.read_pixel_background(customer_id, artifact_id, expected_sha256)
            return True
        except AssetContentStoreError:
            return False

    def persist_pixel_composition(
        self, customer_id: UUID, artifact_id: UUID, content: bytes, expected_sha256: str
    ) -> None:
        self._persist(
            self._key(customer_id, "pixel-compositions", artifact_id),
            content,
            expected_sha256,
        )

    def read_pixel_composition(
        self, customer_id: UUID, artifact_id: UUID, expected_sha256: str
    ) -> bytes:
        return self._read(
            self._key(customer_id, "pixel-compositions", artifact_id), expected_sha256
        )

    def verify_pixel_composition(
        self, customer_id: UUID, artifact_id: UUID, expected_sha256: str
    ) -> bool:
        try:
            self.read_pixel_composition(customer_id, artifact_id, expected_sha256)
            return True
        except AssetContentStoreError:
            return False

    def persist_subject_artifact(
        self,
        customer_id: UUID,
        seal_id: UUID,
        kind: Literal["rgba", "core-mask"],
        content: bytes,
        expected_sha256: str,
    ) -> None:
        self._persist(
            self._key(customer_id, f"subject-{kind}", seal_id), content, expected_sha256
        )

    def read_subject_artifact(
        self,
        customer_id: UUID,
        seal_id: UUID,
        kind: Literal["rgba", "core-mask"],
        expected_sha256: str,
    ) -> bytes:
        return self._read(
            self._key(customer_id, f"subject-{kind}", seal_id), expected_sha256
        )

    def discard_subject_artifact(
        self, customer_id: UUID, seal_id: UUID, kind: Literal["rgba", "core-mask"]
    ) -> None:
        self._discard(self._key(customer_id, f"subject-{kind}", seal_id))

    def reserve_derivative(
        self,
        customer_id: UUID,
        job_id: UUID,
        sequence_number: int,
        *,
        extension: Literal["jpg", "png"] = "jpg",
    ) -> DerivativeTarget:
        if sequence_number not in {1, 2, 3}:
            raise AssetContentStoreError("invalid_derivative_sequence")
        # Opaque identity only; no public URL or readable local path is returned.
        key = self._key(
            customer_id,
            "candidates",
            job_id,
            f"/candidate-{sequence_number}.{extension}",
        )
        return DerivativeTarget(customer_id, job_id, sequence_number, Path(key))

    def create_derivative(
        self, target: DerivativeTarget, payload: NormalizedPayload
    ) -> NormalizedImage:
        extension: Literal["jpg", "png"] = (
            "png" if payload.mime_type == "image/png" else "jpg"
        )
        owned = self.reserve_derivative(
            target.customer_id,
            target.job_id,
            target.sequence_number,
            extension=extension,
        )
        if target != owned:
            raise AssetContentStoreError("asset_derivative_scope_invalid")
        self._persist(
            str(target.artifact_path), payload.content, payload.content_sha256
        )
        return NormalizedImage(
            target.artifact_path,
            payload.content_sha256,
            payload.mime_type,
            payload.width,
            payload.height,
            payload.metadata,
        )

    def read_derivative(
        self,
        customer_id: UUID,
        job_id: UUID,
        sequence_number: int,
        expected_sha256: str,
    ) -> bytes:
        for extension in ("png", "jpg"):
            key = self._key(
                customer_id,
                "candidates",
                job_id,
                f"/candidate-{sequence_number}.{extension}",
            )
            try:
                return self._read(key, expected_sha256)
            except AssetContentStoreError:
                pass
        raise AssetContentStoreError("asset_derivative_unavailable")

    def verify_derivative(
        self,
        customer_id: UUID,
        job_id: UUID,
        sequence_number: int,
        expected_sha256: str,
    ) -> bool:
        try:
            self.read_derivative(customer_id, job_id, sequence_number, expected_sha256)
            return True
        except AssetContentStoreError:
            return False

    def _capability(
        self, customer_id: UUID, job_id: UUID, sequences: tuple[int, ...]
    ) -> str:
        value = f"{self.store_namespace}:{customer_id}:{job_id}:{sequences}".encode()
        return hmac.new(self._secret, value, "sha256").hexdigest()

    def issue_cleanup_capability(
        self, customer_id: UUID, job_id: UUID, expected_sequences: tuple[int, ...]
    ) -> str:
        if expected_sequences != (1, 2, 3):
            raise AssetContentStoreError("asset_cleanup_scope_invalid")
        return self._capability(customer_id, job_id, expected_sequences)

    def verify_cleanup_capability(
        self,
        customer_id: UUID,
        job_id: UUID,
        expected_sequences: tuple[int, ...],
        capability: str,
    ) -> bool:
        return expected_sequences == (1, 2, 3) and hmac.compare_digest(
            self._capability(customer_id, job_id, expected_sequences), capability
        )

    def cleanup_derivatives(self, customer_id: UUID, job_id: UUID) -> tuple[str, ...]:
        failures: list[str] = []
        for sequence in (1, 2, 3):
            for extension in ("png", "jpg"):
                key = self._key(
                    customer_id,
                    "candidates",
                    job_id,
                    f"/candidate-{sequence}.{extension}",
                )
                try:
                    self._discard(key)
                except AssetContentStoreError:
                    failures.append("private_object_cleanup_pending")
        return tuple(failures)

    def cleanup_scope_state(
        self,
        customer_id: UUID,
        job_id: UUID,
        expected_sequences: tuple[int, ...],
        capability: str,
    ) -> str:
        if not self.verify_cleanup_capability(
            customer_id, job_id, expected_sequences, capability
        ):
            return "unknown"
        # Restart cannot prove absence without checking remote manifests.
        for sequence in expected_sequences:
            for extension in ("png", "jpg"):
                key = self._key(
                    customer_id,
                    "candidates",
                    job_id,
                    f"/candidate-{sequence}.{extension}/manifest.json",
                )
                try:
                    self._get(key)
                    return "owned"
                except AssetContentStoreError:
                    return "unknown"
        return "unknown"

    def cleanup_scoped_derivatives(
        self, customer_id: UUID, job_id: UUID, capability: str
    ) -> tuple[str, ...]:
        if not self.verify_cleanup_capability(
            customer_id, job_id, (1, 2, 3), capability
        ):
            return ("asset_cleanup_scope_invalid",)
        # Restart recovery intentionally fails closed rather than deleting objects
        # not created by this process. An audited operator can reconcile them.
        prefix = self._key(customer_id, "candidates", job_id)
        if not any(key.startswith(prefix + "/") for key in self._created):
            return ("private_object_cleanup_requires_reconciliation",)
        return self.cleanup_derivatives(customer_id, job_id)

    def validate_backend(self) -> None:
        """Operator deployment probe: read/write, conditional creation, deletion.

        Probes only own random keys outside all tenant/product namespaces.
        Successful validation installs the durable readiness canary.
        """
        key = f"{self._prefix}/probes/{uuid4()}"
        marker = json.dumps(
            {
                "schema": "private-storage-validation-v1",
                "namespace": self.store_namespace,
                "conditional_create": "verified",
            },
            sort_keys=True,
        ).encode()
        try:
            self._create(key, b"private-storage-probe", "application/octet-stream")
            try:
                self._create(key, b"must-not-overwrite", "application/octet-stream")
            except AssetContentStoreError as error:
                if error.code != "private_object_content_conflict":
                    raise
            else:
                raise AssetContentStoreError("conditional_object_creation_unsupported")
            if self._get(key) != b"private-storage-probe":
                raise AssetContentStoreError("conditional_object_creation_unsupported")
            self._delete_created(key)
            self._create(
                f"{self._prefix}/probes/backend-validated.json",
                marker,
                "application/json",
            )
        finally:
            self._delete_created(key)

    def probe(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
            marker = json.loads(
                self._get(f"{self._prefix}/probes/backend-validated.json")
            )
            if marker != {
                "schema": "private-storage-validation-v1",
                "namespace": self.store_namespace,
                "conditional_create": "verified",
            }:
                raise ValueError("storage_validation_missing")
        except Exception as error:
            raise AssetContentStoreError("private_storage_probe_failed") from error

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

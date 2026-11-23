"""Read-only validation for approved, digest-pinned local model artifacts."""

from __future__ import annotations

import hashlib
from runtime_profiles import runtime_profile_for, runtime_profile_inventory_matches
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


APPROVED_LICENSES = frozenset({
    "Apache-2.0",
    "BSD-3-Clause",
    "ISC",
    "MIT",
    "MPL-2.0",
    "Apache-2.0 AND MIT",
})
REVISION_LENGTH = 40
DIGEST_LENGTH = 64
WORKER_RUNTIME_DEPENDENCY_LOCK_SHA256 = (
    "774b30f7f04e3d78faf72f75fca0d0b5823f8244d751c6c10f9c97f45fd85521"
)
WORKER_RUNTIME_WORKER_DEPENDENCY_LOCK_SHA256 = (
    "7b395d3286c1ce2012c5fb2507ad2a3ce4accb010dab69d3de2a973ed84b5041"
)
WORKER_RUNTIME_FIELDS = frozenset({
    "schemaVersion", "id", "version", "pythonVersion", "platform", "architecture",
    "minimumOsVersion", "dependencyLockSha256", "workerDependencyLockSha256",
    "materialInventorySha256",
    "approvalPacketId", "archive", "manifest", "packageInventory",
    "derivationReceipt", "treeSha256", "licenseReview", "enabled", "approved",
    "approvedBy", "approvedAt", "probe",
})
WORKER_RUNTIME_SIDECAR_FIELDS = frozenset({"relativePath", "sha256", "byteLength"})
WORKER_RUNTIME_PROBE_FIELDS = frozenset({
    "receiptPath", "receiptSha256", "runtimeTreeSha256", "modelSha256",
    "promptVersion", "inputSha256",
})
RUNTIME_MANIFEST_FIELDS = frozenset({"schemaVersion", "files", "treeSha256"})
RUNTIME_MANIFEST_FILE_FIELDS = frozenset({
    "relativePath", "byteLength", "sha256", "type", "executable",
})
RUNTIME_PACKAGE_FIELDS = frozenset({
    "name", "version", "sourceMaterialId", "sourceSha256", "licenseExpression",
    "licenseEvidence", "moduleRoots",
})
RUNTIME_PYTHON_FIELDS = frozenset({
    "name", "version", "build", "sourceMaterialId", "sourceSha256",
    "licenseExpression", "licenseEvidence", "moduleRoots",
})
RUNTIME_PACKAGE_INVENTORY_FIELDS = frozenset({
    "schemaVersion", "python", "packages", "recordOwnership", "licenseReview",
})
RUNTIME_DERIVATION_RECEIPT_FIELDS = frozenset({
    "schemaVersion", "status", "materialPacketId", "materialPacketSha256",
    "approvalReceiptSha256", "supplementalMaterialPacketId",
    "supplementalApprovalReceiptSha256", "builderToolPacketId",
    "builderToolApprovalReceiptSha256", "builderToolVerifiedReceiptSha256",
    "builderToolExecutionSecurityBoundary", "builderToolExecutable",
    "dependencyLockSha256", "workerDependencyLockSha256", "target", "builder",
    "promotionVerification", "commands",
    "startedAt", "completedAt", "archiveSha256", "treeSha256",
})
SAFE_RUNTIME_ARTIFACT_ID = re.compile(r"[A-Za-z0-9._-]{1,200}\Z")


class ArtifactRegistryError(ValueError):
    """Stable registry failure that never contains a local path or lock content."""


@dataclass(frozen=True)
class RuntimeIdentity:
    artifact_id: str
    version: str
    root: str
    tree_sha256: str
    python_version: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise ArtifactRegistryError("studio-model-artifact-missing") from error
    return digest.hexdigest()


def _is_hex(value: Any, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _url_bound_to_revision(source_url: Any, revision: str, file_url: Any) -> bool:
    if not isinstance(source_url, str) or not isinstance(file_url, str):
        return False
    try:
        source = urlparse(source_url)
        file = urlparse(file_url)
    except ValueError:
        return False
    source_path = source.path.rstrip("/")
    if source.scheme != "https" or file.scheme != "https" or source.netloc != file.netloc:
        return False
    if not file.path.startswith(f"{source_path}/"):
        return False
    return (
        file.path.startswith(f"{source_path}/resolve/{revision}/")
        or file.path.startswith(f"{source_path}/raw/{revision}/")
        or (
            source.netloc == "codeload.github.com"
            and file.path == f"{source_path}/tar.gz/{revision}"
        )
        or file.path == f"{source_path}/archive/{revision}"
        or file.path == f"{source_path}/archive/{revision}.tar.gz"
        or file.path == f"{source_path}/archive/{revision}.zip"
    )


def _license_url_bound_to_revision(source_url: Any, revision: str, evidence_url: Any) -> bool:
    if _url_bound_to_revision(source_url, revision, evidence_url):
        return True
    if not isinstance(source_url, str) or not isinstance(evidence_url, str):
        return False
    try:
        source = urlparse(source_url)
        evidence = urlparse(evidence_url)
    except ValueError:
        return False
    source_path = source.path.rstrip("/")
    return (
        source.scheme == "https"
        and source.netloc == "codeload.github.com"
        and evidence.scheme == "https"
        and evidence.netloc == "raw.githubusercontent.com"
        and evidence.path.startswith(f"{source_path}/{revision}/")
    )


def _has_evidence_placeholder(source_url: str, revision: str, evidence_url: str) -> bool:
    source = urlparse(source_url)
    source_path = source.path.rstrip("/")
    parsed = urlparse(evidence_url)
    prefixes = (
        f"{source_path}/resolve/{revision}",
        f"{source_path}/raw/{revision}",
        f"{source_path}/archive/{revision}",
    )
    prefix = next((candidate for candidate in prefixes if parsed.path.startswith(candidate)), None)
    if (
        prefix is None
        and source.netloc == "codeload.github.com"
        and parsed.netloc == "raw.githubusercontent.com"
        and parsed.path.startswith(f"{source_path}/{revision}/")
    ):
        prefix = f"{source_path}/{revision}"
    if prefix is None:
        return True
    try:
        suffix = unquote(f"{parsed.path[len(prefix):]}?{parsed.query}#{parsed.fragment}").lower()
    except (UnicodeDecodeError, ValueError):
        return True
    tokens = "".join(character if character.isalnum() else " " for character in suffix).split()
    return "todo" in tokens or "placeholder" in tokens


def _valid_approval_time(value: Any) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _valid_relative_file_path(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and all(0x20 <= ord(character) <= 0x7E for character in value)
        and not Path(value).is_absolute()
        and "\\" not in value
        and all(part not in ("", ".", "..") for part in value.split("/"))
    )


def _runtime_path_collision_key(value: str) -> str:
    return "".join(
        chr(ord(character) + 0x20) if "A" <= character <= "Z" else character
        for character in value
    )


def _valid_runtime_path_list(value: Any, *, single_segment: bool = False) -> bool:
    if not isinstance(value, list) or not value:
        return False
    collision_keys: set[str] = set()
    for item in value:
        if not _valid_relative_file_path(item) or (single_segment and "/" in item):
            return False
        collision_key = _runtime_path_collision_key(item)
        if collision_key in collision_keys:
            return False
        collision_keys.add(collision_key)
    return True


def _valid_runtime_metadata_string(value: Any) -> bool:
    return (
        type(value) is str
        and bool(value)
        and all(0x20 <= ord(character) <= 0x7E for character in value)
        and value[0] not in "\t\n\v\f\r "
        and value[-1] not in "\t\n\v\f\r "
    )


def _valid_runtime_package_schema_version(value: Any) -> bool:
    if type(value) is int:
        return value == 1
    if type(value) is float:
        return math.isfinite(value) and value.is_integer() and value == 1
    return False


def _valid_runtime_package_entry(
    value: Any,
    fields: frozenset[str],
    *,
    python_version: str | None = None,
) -> bool:
    return (
        _exact_fields(value, fields)
        and _valid_runtime_metadata_string(value["name"])
        and _valid_runtime_metadata_string(value["version"])
        and (python_version is None or value["version"] == python_version)
        and (
            python_version is None
            or value["build"] is None
            or _valid_runtime_metadata_string(value["build"])
        )
        and _valid_runtime_metadata_string(value["sourceMaterialId"])
        and _is_hex(value["sourceSha256"], DIGEST_LENGTH)
        and _valid_runtime_metadata_string(value["licenseExpression"])
        and _valid_runtime_path_list(value["licenseEvidence"])
        and _valid_runtime_path_list(value["moduleRoots"], single_segment=True)
    )


def _validate_runtime_package_inventory(
    inventory: Any,
    python_version: str,
) -> None:
    python = inventory.get("python") if isinstance(inventory, dict) else None
    packages = inventory.get("packages") if isinstance(inventory, dict) else None
    if not (
        _exact_fields(inventory, RUNTIME_PACKAGE_INVENTORY_FIELDS)
        and _valid_runtime_package_schema_version(inventory.get("schemaVersion"))
        and _valid_runtime_package_entry(
            python, RUNTIME_PYTHON_FIELDS, python_version=python_version
        )
        and isinstance(packages, list) and bool(packages)
        and inventory.get("recordOwnership") == {"unownedFiles": []}
        and inventory.get("licenseReview") == "pending"
    ):
        raise ArtifactRegistryError("studio-runtime-package-inventory-invalid")
    names: set[str] = set()
    for package in packages:
        if not _valid_runtime_package_entry(package, RUNTIME_PACKAGE_FIELDS):
            raise ArtifactRegistryError("studio-runtime-package-inventory-invalid")
        normalized_name = re.sub(
            r"[-_.]+", "-", _runtime_path_collision_key(package["name"])
        )
        if normalized_name in names:
            raise ArtifactRegistryError("studio-runtime-package-inventory-invalid")
        names.add(normalized_name)


def _exact_fields(value: Any, fields: frozenset[str]) -> bool:
    return isinstance(value, dict) and set(value) == fields


def _runtime_tree_sha256(files: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for file in sorted(files, key=lambda item: item["relativePath"].encode("utf-8")):
        digest.update(file["relativePath"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(file["sha256"].encode("ascii"))
        digest.update(b"\0")
        digest.update(str(file["byteLength"]).encode("ascii"))
        digest.update(b"\0file\0")
        digest.update(b"1\n" if file["executable"] else b"0\n")
    return digest.hexdigest()


def _runtime_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise ArtifactRegistryError("studio-runtime-sidecar-mismatch") from error
    return digest.hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _runtime_bundle_packet_ids(
    bundle: dict[str, Any],
    receipt: dict[str, Any],
    manifest: dict[str, Any],
    installed_sidecars: dict[str, dict[str, Any]],
    package_inventory: dict[str, Any],
) -> frozenset[str]:
    packet_core = {
        "schemaVersion": 1,
        "status": "awaiting-exact-bundle-and-license-approval",
        "approved": False,
        "enabled": False,
        "licenseReview": "pending",
        "materialPacketId": receipt["materialPacketId"],
        "supplementalMaterialPacketId": receipt["supplementalMaterialPacketId"],
        "builderToolPacketId": receipt["builderToolPacketId"],
        "builderToolExecutableSha256": receipt["builderToolExecutable"]["sha256"],
        "builderToolExecutableBytes": receipt["builderToolExecutable"]["byteLength"],
        "builderToolVerifiedReceiptSha256": receipt["builderToolVerifiedReceiptSha256"],
        "builderToolExecutionSecurityBoundary": receipt["builderToolExecutionSecurityBoundary"],
        "dependencyLockSha256": bundle["dependencyLockSha256"],
        "workerDependencyLockSha256": bundle["workerDependencyLockSha256"],
        "pythonVersion": bundle["pythonVersion"],
        "platform": bundle["platform"],
        "architecture": bundle["architecture"],
        "minimumOsVersion": bundle["minimumOsVersion"],
        "treeSha256": bundle["treeSha256"],
        "totalInstalledBytes": sum(file["byteLength"] for file in manifest["files"]),
        "archive": bundle["archive"],
        "manifest": installed_sidecars["manifest"],
        "packageInventory": installed_sidecars["packageInventory"],
        "derivationReceipt": installed_sidecars["derivationReceipt"],
    }
    if "runtimeProfileId" in bundle:
        packet_core["runtimeProfileId"] = bundle["runtimeProfileId"]
    extended_packet_core = {
        **packet_core,
        "licenseInventorySummary": {
            "sha256": bundle["packageInventory"]["sha256"],
            "installedDistributionCount": len(package_inventory["packages"]),
            "unknownLicenseNames": [
                package["name"] for package in package_inventory["packages"]
                if package["licenseExpression"] == "UNKNOWN"
            ],
            "scenePackages": [
                {
                    "name": package["name"],
                    "version": package["version"],
                    "licenseExpression": package["licenseExpression"],
                }
                for package in package_inventory["packages"]
                if package["name"] in {"platformdirs", "scenedetect"}
            ],
        },
        "noIllusion": {
            "retrieved": True,
            "built": True,
            "deterministic": True,
            "installed": False,
            "qwenProbeRun": False,
            "customerContentUsed": False,
            "productionReady": False,
        },
    }
    return frozenset(
        "worker-runtime-bundle-v1-sha256-"
        + hashlib.sha256(_canonical_json(core).encode("utf-8")).hexdigest()
        for core in (packet_core, extended_packet_core)
    )


class ArtifactRegistry:
    def __init__(self, lock_path: str, root: str):
        lock = Path(lock_path)
        model_root = Path(root)
        if not lock.is_absolute() or not model_root.is_absolute():
            raise ArtifactRegistryError("studio-model-artifact-registry-invalid")
        try:
            payload = json.loads(lock.read_text(encoding="utf-8"))
            if model_root.is_symlink():
                raise ArtifactRegistryError("studio-model-artifact-path-outside-manifest")
            self.root = model_root.resolve(strict=True)
            root_details = self.root.lstat()
            if (
                stat.S_ISLNK(root_details.st_mode)
                or not stat.S_ISDIR(root_details.st_mode)
            ):
                raise ArtifactRegistryError("studio-model-artifact-path-outside-manifest")
            self._root_identity = (root_details.st_dev, root_details.st_ino)
        except ArtifactRegistryError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ArtifactRegistryError("studio-model-artifact-registry-invalid") from error
        artifacts = payload.get("artifacts") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("schemaVersion") != 1
            or not isinstance(artifacts, list)
        ):
            raise ArtifactRegistryError("studio-model-artifact-registry-invalid")
        ids = [artifact.get("id") for artifact in artifacts if isinstance(artifact, dict)]
        if len(ids) != len(artifacts) or len(set(ids)) != len(ids):
            raise ArtifactRegistryError("studio-model-artifact-registry-invalid")
        self.artifacts = {artifact["id"]: artifact for artifact in artifacts}
        runtimes = payload.get("workerRuntimes", [])
        if not isinstance(runtimes, list):
            raise ArtifactRegistryError("studio-model-artifact-registry-invalid")
        runtime_ids = [runtime.get("id") for runtime in runtimes if isinstance(runtime, dict)]
        if len(runtime_ids) != len(runtimes) or len(set(runtime_ids)) != len(runtime_ids):
            raise ArtifactRegistryError("studio-model-artifact-registry-invalid")
        self.worker_runtimes = {runtime["id"]: runtime for runtime in runtimes}

    def resolve_ready_worker_runtime(
        self,
        artifact_id: str,
        configured_root: str,
        expected_tree_sha256: str,
    ) -> RuntimeIdentity:
        """Validate approval, locked sidecars and the exact installed runtime tree."""
        bundle = self.worker_runtimes.get(artifact_id)
        if bundle is None or not self._valid_worker_runtime(bundle):
            raise ArtifactRegistryError("studio-runtime-artifact-unapproved")
        if expected_tree_sha256 != bundle["treeSha256"]:
            raise ArtifactRegistryError("studio-runtime-tree-mismatch")
        configured = self._runtime_root(artifact_id, configured_root)
        sidecar_snapshots = {
            "manifest": self._read_runtime_sidecar(configured, bundle["manifest"]),
            "packageInventory": self._read_runtime_sidecar(
                configured, bundle["packageInventory"]
            ),
            "derivationReceipt": self._read_runtime_sidecar(
                configured, bundle["derivationReceipt"]
            ),
        }
        sidecars = {name: snapshot[0] for name, snapshot in sidecar_snapshots.items()}
        observed_sidecars = {name: snapshot[1] for name, snapshot in sidecar_snapshots.items()}
        manifest = self._runtime_manifest(sidecars["manifest"], bundle)
        package_inventory = self._runtime_package_inventory(
            sidecars["packageInventory"], bundle
        )
        receipt = self._runtime_derivation_receipt(sidecars["derivationReceipt"], bundle)
        if bundle["approvalPacketId"] not in _runtime_bundle_packet_ids(
            bundle,
            receipt,
            manifest,
            observed_sidecars,
            package_inventory,
        ):
            raise ArtifactRegistryError("studio-runtime-artifact-unapproved")
        self._verify_runtime_tree(configured, manifest, bundle)
        return RuntimeIdentity(
            artifact_id=bundle["id"],
            version=bundle["version"],
            root=str(configured),
            tree_sha256=bundle["treeSha256"],
            python_version=bundle["pythonVersion"],
        )

    def require_runtime_profile(self, artifact_id: str, profile_id: str | None) -> None:
        bundle=self.worker_runtimes.get(artifact_id)
        if bundle is None or not self._valid_worker_runtime(bundle):
            raise ArtifactRegistryError("studio-runtime-artifact-unapproved")
        expected=bundle.get("runtimeProfileId", "legacy")
        if (expected!="legacy" and profile_id!=expected) or (expected=="legacy" and profile_id not in (None,"legacy")):
            raise ArtifactRegistryError("studio-runtime-profile-mismatch")

    def validate_worker_runtime_model_binding(
        self,
        artifact_id: str,
        model_sha256: str | None,
    ) -> None:
        bundle = self.worker_runtimes.get(artifact_id)
        if bundle is None or not self._valid_worker_runtime(bundle):
            raise ArtifactRegistryError("studio-runtime-artifact-unapproved")
        probe = bundle["probe"]
        if (
            probe is not None
            and (model_sha256 is None or probe["modelSha256"] != model_sha256)
        ):
            raise ArtifactRegistryError("studio-runtime-model-binding-mismatch")

    @staticmethod
    def _valid_worker_runtime(bundle: dict[str, Any]) -> bool:
        fields = WORKER_RUNTIME_FIELDS | {"runtimeProfileId"} if "runtimeProfileId" in bundle else WORKER_RUNTIME_FIELDS
        if not _exact_fields(bundle, fields):
            return False
        profile = runtime_profile_for(bundle, WORKER_RUNTIME_DEPENDENCY_LOCK_SHA256, WORKER_RUNTIME_WORKER_DEPENDENCY_LOCK_SHA256)
        if profile is None:
            return False
        if not (
            bundle.get("schemaVersion") == 1
            and isinstance(bundle.get("id"), str) and bool(bundle["id"].strip())
            and SAFE_RUNTIME_ARTIFACT_ID.fullmatch(bundle["id"]) is not None
            and bundle["id"] not in {".", ".."}
            and isinstance(bundle.get("version"), str) and bool(bundle["version"].strip())
            and isinstance(bundle.get("pythonVersion"), str)
            and bundle["pythonVersion"].startswith("3.12.")
            and bundle.get("platform") == "darwin"
            and bundle.get("architecture") == "arm64"
            and isinstance(bundle.get("minimumOsVersion"), str)
            and bool(bundle["minimumOsVersion"].strip())
            and bundle.get("dependencyLockSha256") == profile["dependencyLockSha256"]
            and bundle.get("workerDependencyLockSha256")
            == profile["workerDependencyLockSha256"]
            and _is_hex(bundle.get("materialInventorySha256"), DIGEST_LENGTH)
            and isinstance(bundle.get("approvalPacketId"), str)
            and bundle["approvalPacketId"].startswith("worker-runtime-bundle-v1-sha256-")
            and _is_hex(bundle["approvalPacketId"].removeprefix(
                "worker-runtime-bundle-v1-sha256-"
            ), DIGEST_LENGTH)
            and _is_hex(bundle.get("treeSha256"), DIGEST_LENGTH)
            and bundle.get("licenseReview") == "approved"
            and bundle.get("enabled") is True
            and bundle.get("approved") is True
            and isinstance(bundle.get("approvedBy"), str)
            and bool(bundle["approvedBy"].strip())
            and _valid_approval_time(bundle.get("approvedAt"))
        ):
            return False
        locked = (bundle["archive"], bundle["manifest"], bundle["packageInventory"], bundle["derivationReceipt"])
        if any(not ArtifactRegistry._valid_runtime_sidecar(sidecar) for sidecar in locked):
            return False
        if len({sidecar["relativePath"] for sidecar in locked}) != 4:
            return False
        if [sidecar["relativePath"] for sidecar in locked] != [
            "runtime.tar", "runtime-manifest.json", "runtime-packages.json",
            "derivation-receipt.json",
        ]:
            return False
        probe = bundle["probe"]
        return probe is None or (
            _exact_fields(probe, WORKER_RUNTIME_PROBE_FIELDS)
            and _valid_relative_file_path(probe["receiptPath"])
            and _is_hex(probe["receiptSha256"], DIGEST_LENGTH)
            and probe["runtimeTreeSha256"] == bundle["treeSha256"]
            and _is_hex(probe["modelSha256"], DIGEST_LENGTH)
            and isinstance(probe["promptVersion"], str) and bool(probe["promptVersion"].strip())
            and _is_hex(probe["inputSha256"], DIGEST_LENGTH)
        )

    @staticmethod
    def _valid_runtime_sidecar(sidecar: Any) -> bool:
        return (
            _exact_fields(sidecar, WORKER_RUNTIME_SIDECAR_FIELDS)
            and _valid_relative_file_path(sidecar["relativePath"])
            and _is_hex(sidecar["sha256"], DIGEST_LENGTH)
            and type(sidecar["byteLength"]) is int
            and sidecar["byteLength"] > 0
        )

    def _runtime_root(self, artifact_id: str, configured_root: str) -> Path:
        descriptors: list[int] = []
        try:
            if (
                SAFE_RUNTIME_ARTIFACT_ID.fullmatch(artifact_id) is None
                or artifact_id in {".", ".."}
            ):
                raise ValueError
            configured = Path(configured_root)
            runtime_parent = self.root / "runtime"
            expected = runtime_parent / artifact_id
            if not configured.is_absolute() or configured != expected:
                raise ValueError
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            root_before = self.root.lstat()
            runtime_before = runtime_parent.lstat()
            artifact_before = expected.lstat()
            if any(
                stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode)
                for details in (root_before, runtime_before, artifact_before)
            ):
                raise ValueError
            descriptors.append(os.open(self.root, directory_flags))
            descriptors.append(os.open("runtime", directory_flags, dir_fd=descriptors[-1]))
            descriptors.append(os.open(artifact_id, directory_flags, dir_fd=descriptors[-1]))
            opened = tuple(os.fstat(descriptor) for descriptor in descriptors)
            if (
                (opened[0].st_dev, opened[0].st_ino) != self._root_identity
                or any(
                    (path_details.st_dev, path_details.st_ino)
                    != (opened_details.st_dev, opened_details.st_ino)
                    for path_details, opened_details in zip(
                        (root_before, runtime_before, artifact_before),
                        opened,
                    )
                )
                or configured.resolve(strict=True) != expected
            ):
                raise ValueError
            root_after = self.root.lstat()
            runtime_after = runtime_parent.lstat()
            artifact_after = expected.lstat()
            if any(
                (path_details.st_dev, path_details.st_ino)
                != (opened_details.st_dev, opened_details.st_ino)
                for path_details, opened_details in zip(
                    (root_after, runtime_after, artifact_after),
                    opened,
                )
            ):
                raise ValueError
            return expected
        except (OSError, ValueError) as error:
            raise ArtifactRegistryError("studio-runtime-tree-mismatch") from error
        finally:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    @staticmethod
    def _contained_runtime_path(root: Path, relative_path: str) -> Path:
        try:
            if not _valid_relative_file_path(relative_path):
                raise ValueError
            current = root
            for part in Path(relative_path).parts:
                current /= part
                details = current.lstat()
                if stat.S_ISLNK(details.st_mode):
                    raise ValueError
            resolved = current.resolve(strict=True)
            resolved.relative_to(root)
            return resolved
        except (OSError, ValueError) as error:
            raise ArtifactRegistryError("studio-runtime-tree-mismatch") from error

    def _read_runtime_sidecar(
        self,
        root: Path,
        sidecar: dict[str, Any],
    ) -> tuple[bytes, dict[str, Any]]:
        self._contained_runtime_path(root, sidecar["relativePath"])
        descriptors: list[int] = []
        try:
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            descriptors.append(os.open(root, directory_flags))
            parts = Path(sidecar["relativePath"]).parts
            for part in parts[:-1]:
                descriptors.append(os.open(
                    part,
                    directory_flags,
                    dir_fd=descriptors[-1],
                ))
            file_descriptor = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=descriptors[-1],
            )
            descriptors.append(file_descriptor)
            before = os.fstat(file_descriptor)
            chunks = []
            while chunk := os.read(file_descriptor, 1024 * 1024):
                chunks.append(chunk)
            payload = b"".join(chunks)
            after = os.fstat(file_descriptor)
            digest = hashlib.sha256(payload).hexdigest()
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size != sidecar["byteLength"]
                or digest != sidecar["sha256"]
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                raise ValueError
            return payload, {
                "relativePath": sidecar["relativePath"],
                "sha256": digest,
                "byteLength": len(payload),
            }
        except (OSError, ValueError) as error:
            raise ArtifactRegistryError("studio-runtime-sidecar-mismatch") from error
        finally:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    @staticmethod
    def _decode_runtime_json(payload: bytes, code: str) -> dict[str, Any]:
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ArtifactRegistryError(code) from error
        if not isinstance(value, dict):
            raise ArtifactRegistryError(code)
        return value

    def _runtime_manifest(
        self,
        payload: bytes,
        bundle: dict[str, Any],
    ) -> dict[str, Any]:
        manifest = self._decode_runtime_json(payload, "studio-runtime-manifest-invalid")
        if (
            not _exact_fields(manifest, RUNTIME_MANIFEST_FIELDS)
            or manifest.get("schemaVersion") != 1
            or not isinstance(manifest.get("files"), list)
            or not manifest["files"]
            or manifest.get("treeSha256") != bundle["treeSha256"]
        ):
            raise ArtifactRegistryError("studio-runtime-manifest-invalid")
        paths: set[str] = set()
        normalized_paths: set[str] = set()
        normalized_directories: dict[str, str] = {}
        for file in manifest["files"]:
            if not (
                _exact_fields(file, RUNTIME_MANIFEST_FILE_FIELDS)
                and _valid_relative_file_path(file["relativePath"])
                and type(file["byteLength"]) is int
                and file["byteLength"] >= 0
                and _is_hex(file["sha256"], DIGEST_LENGTH)
                and file["type"] == "file"
                and type(file["executable"]) is bool
            ):
                raise ArtifactRegistryError("studio-runtime-manifest-invalid")
            normalized = _runtime_path_collision_key(file["relativePath"])
            if file["relativePath"] in paths or normalized in normalized_paths:
                raise ArtifactRegistryError("studio-runtime-manifest-invalid")
            paths.add(file["relativePath"])
            normalized_paths.add(normalized)
            parts = file["relativePath"].split("/")
            for index in range(1, len(parts)):
                directory = "/".join(parts[:index])
                normalized_directory = _runtime_path_collision_key(directory)
                if normalized_directories.get(normalized_directory, directory) != directory:
                    raise ArtifactRegistryError("studio-runtime-manifest-invalid")
                normalized_directories[normalized_directory] = directory
        sidecar_paths = {
            bundle["manifest"]["relativePath"],
            bundle["packageInventory"]["relativePath"],
            bundle["derivationReceipt"]["relativePath"],
        }
        if (
            normalized_paths & normalized_directories.keys()
            or paths & sidecar_paths
            or _runtime_tree_sha256(manifest["files"]) != bundle["treeSha256"]
        ):
            raise ArtifactRegistryError("studio-runtime-tree-mismatch")
        return manifest

    def _runtime_package_inventory(
        self,
        payload: bytes,
        bundle: dict[str, Any],
    ) -> dict[str, Any]:
        inventory = self._decode_runtime_json(payload, "studio-runtime-package-inventory-invalid")
        _validate_runtime_package_inventory(inventory, bundle["pythonVersion"])
        if not runtime_profile_inventory_matches(bundle,inventory):
            raise ArtifactRegistryError("studio-runtime-profile-inventory-mismatch")
        return inventory

    def _runtime_derivation_receipt(
        self,
        payload: bytes,
        bundle: dict[str, Any],
    ) -> dict[str, Any]:
        receipt = self._decode_runtime_json(payload, "studio-runtime-derivation-receipt-invalid")
        target = receipt.get("target")
        executable = receipt.get("builderToolExecutable")
        if not (
            _exact_fields(receipt, RUNTIME_DERIVATION_RECEIPT_FIELDS)
            and receipt.get("schemaVersion") == 1
            and receipt.get("status") == "succeeded"
            and receipt.get("dependencyLockSha256") == bundle["dependencyLockSha256"]
            and receipt.get("workerDependencyLockSha256")
            == bundle["workerDependencyLockSha256"]
            and receipt.get("archiveSha256") == bundle["archive"]["sha256"]
            and receipt.get("treeSha256") == bundle["treeSha256"]
            and receipt.get("materialPacketId")
            == "worker-runtime-materials-v1-sha256-" + bundle["materialInventorySha256"]
            and _is_hex(receipt.get("materialPacketSha256"), DIGEST_LENGTH)
            and _is_hex(receipt.get("approvalReceiptSha256"), DIGEST_LENGTH)
            and isinstance(receipt.get("supplementalMaterialPacketId"), str)
            and receipt["supplementalMaterialPacketId"].startswith(
                "worker-runtime-build-materials-v1-sha256-"
            )
            and _is_hex(
                receipt["supplementalMaterialPacketId"].removeprefix(
                    "worker-runtime-build-materials-v1-sha256-"
                ),
                DIGEST_LENGTH,
            )
            and _is_hex(receipt.get("supplementalApprovalReceiptSha256"), DIGEST_LENGTH)
            and isinstance(receipt.get("builderToolPacketId"), str)
            and receipt["builderToolPacketId"].startswith(
                "worker-runtime-builder-tool-v1-sha256-"
            )
            and _is_hex(
                receipt["builderToolPacketId"].removeprefix(
                    "worker-runtime-builder-tool-v1-sha256-"
                ),
                DIGEST_LENGTH,
            )
            and _is_hex(receipt.get("builderToolApprovalReceiptSha256"), DIGEST_LENGTH)
            and _is_hex(receipt.get("builderToolVerifiedReceiptSha256"), DIGEST_LENGTH)
            and isinstance(receipt.get("builderToolExecutionSecurityBoundary"), dict)
            and bool(receipt["builderToolExecutionSecurityBoundary"])
            and isinstance(executable, dict)
            and type(executable.get("byteLength")) is int
            and executable["byteLength"] > 0
            and _is_hex(executable.get("sha256"), DIGEST_LENGTH)
            and isinstance(target, dict)
            and target.get("pythonVersionRange") == ">=3.12.0 <3.13"
            and target.get("platform") == bundle["platform"]
            and target.get("architecture") == bundle["architecture"]
            and isinstance(receipt.get("promotionVerification"), dict)
            and receipt["promotionVerification"].get("relocationImportSelfCheck")
            == "passed-before-promotion"
        ):
            raise ArtifactRegistryError("studio-runtime-derivation-receipt-invalid")
        return receipt

    def _verify_runtime_tree(
        self,
        root: Path,
        manifest: dict[str, Any],
        bundle: dict[str, Any],
    ) -> None:
        expected_files = {file["relativePath"]: file for file in manifest["files"]}
        expected_files.update({
            bundle["manifest"]["relativePath"]: None,
            bundle["packageInventory"]["relativePath"]: None,
            bundle["derivationReceipt"]["relativePath"]: None,
        })
        expected_directories = {""}
        for relative_path in expected_files:
            parts = Path(relative_path).parts
            expected_directories.update(
                Path(*parts[:index]).as_posix() for index in range(1, len(parts))
            )
        actual_files: set[str] = set()
        actual_directories: set[str] = set()

        def collect(directory: Path, relative: str) -> None:
            try:
                details = directory.lstat()
                expected_mode = 0o700 if not relative else 0o755
                if (
                    stat.S_ISLNK(details.st_mode)
                    or not stat.S_ISDIR(details.st_mode)
                    or stat.S_IMODE(details.st_mode) != expected_mode
                ):
                    raise ValueError
                actual_directories.add(relative)
                for child in directory.iterdir():
                    child_relative = child.name if not relative else f"{relative}/{child.name}"
                    child_details = child.lstat()
                    if stat.S_ISLNK(child_details.st_mode):
                        raise ValueError
                    if stat.S_ISDIR(child_details.st_mode):
                        collect(child, child_relative)
                    elif stat.S_ISREG(child_details.st_mode):
                        if child_details.st_nlink != 1:
                            raise ValueError
                        actual_files.add(child_relative)
                    else:
                        raise ValueError
            except (OSError, ValueError) as error:
                raise ArtifactRegistryError("studio-runtime-tree-mismatch") from error

        collect(root, "")
        if actual_files != set(expected_files) or actual_directories != expected_directories:
            raise ArtifactRegistryError("studio-runtime-tree-mismatch")
        observed: list[dict[str, Any]] = []
        for relative_path, file in expected_files.items():
            if file is None:
                continue
            path = self._contained_runtime_path(root, relative_path)
            try:
                details = path.lstat()
                expected_mode = 0o755 if file["executable"] else 0o644
                if (
                    details.st_size != file["byteLength"]
                    or stat.S_IMODE(details.st_mode) != expected_mode
                    or _runtime_sha256(path) != file["sha256"]
                ):
                    raise ValueError
            except (OSError, ValueError) as error:
                raise ArtifactRegistryError("studio-runtime-tree-mismatch") from error
            observed.append(file)
        if _runtime_tree_sha256(observed) != bundle["treeSha256"]:
            raise ArtifactRegistryError("studio-runtime-tree-mismatch")

    def resolve_ready_artifact(
        self,
        artifact_id: str,
        configured_path: str,
        *,
        executable: bool,
        required_source_kind: str,
    ) -> dict[str, str]:
        artifact = self.artifacts.get(artifact_id)
        if artifact is None:
            raise ArtifactRegistryError("studio-model-artifact-unapproved")
        if not self._is_approved(artifact):
            raise ArtifactRegistryError("studio-model-artifact-unapproved")
        manifest = self._manifest(artifact)
        try:
            configured = Path(configured_path)
            if not configured.is_absolute():
                raise ValueError
            configured = configured.resolve(strict=True)
            configured.relative_to(self.root)
        except (OSError, ValueError) as error:
            raise ArtifactRegistryError("studio-model-artifact-path-outside-manifest") from error

        selected = None
        for file in manifest:
            path = self._manifest_path(file["relativePath"])
            if path == configured:
                selected = file
            try:
                details = path.stat()
            except OSError as error:
                raise ArtifactRegistryError("studio-model-artifact-missing") from error
            if details.st_size != file["byteLength"] or _sha256(path) != file["sha256"]:
                raise ArtifactRegistryError("studio-model-artifact-digest-mismatch")
            has_execute_bit = bool(details.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
            permission_matches = (
                has_execute_bit and os.access(path, os.X_OK)
                if file["executable"]
                else not has_execute_bit
            )
            if not permission_matches:
                raise ArtifactRegistryError("studio-model-artifact-permission-mismatch")
        if (
            selected is None
            or selected["sourceKind"] != required_source_kind
            or selected["executable"] is not executable
        ):
            raise ArtifactRegistryError("studio-model-artifact-path-outside-manifest")
        if executable and not configured.is_file():
            raise ArtifactRegistryError("studio-model-artifact-missing")
        return {
            "id": artifact["id"],
            "version": artifact["version"],
            "path": str(configured),
            "sha256": selected["sha256"],
        }

    def resolve_ready_runtime_tree(
        self,
        artifact_id: str,
        configured_root: str,
        *,
        package_name: str,
    ) -> dict[str, str]:
        artifact = self.artifacts.get(artifact_id)
        if artifact is None or not self._is_approved(artifact):
            raise ArtifactRegistryError("studio-model-artifact-unapproved")
        manifest = self._manifest(artifact)
        try:
            configured = Path(configured_root)
            if not configured.is_absolute() or configured.is_symlink():
                raise ValueError
            configured = configured.resolve(strict=True)
            configured.relative_to(self.root)
            if not configured.is_dir():
                raise ValueError
        except (OSError, ValueError) as error:
            raise ArtifactRegistryError("studio-model-artifact-path-outside-manifest") from error

        manifest_by_path: dict[Path, dict[str, Any]] = {}
        for file in manifest:
            path = self._manifest_path(file["relativePath"])
            try:
                details = path.stat()
            except OSError as error:
                raise ArtifactRegistryError("studio-model-artifact-missing") from error
            if (
                not path.is_file()
                or details.st_size != file["byteLength"]
                or _sha256(path) != file["sha256"]
            ):
                raise ArtifactRegistryError("studio-model-artifact-digest-mismatch")
            has_execute_bit = bool(details.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
            if file["executable"] or has_execute_bit:
                raise ArtifactRegistryError("studio-model-artifact-permission-mismatch")
            manifest_by_path[path] = file

        package_root = configured / package_name
        distribution_root = configured / f"{package_name}-{artifact['version']}.dist-info"
        actual_files: set[Path] = set()

        def collect(directory: Path) -> None:
            try:
                if directory.is_symlink() or not directory.is_dir():
                    raise ArtifactRegistryError("studio-model-runtime-manifest-incomplete")
                for child in directory.iterdir():
                    if child.is_symlink():
                        raise ArtifactRegistryError("studio-model-runtime-manifest-incomplete")
                    if child.is_dir():
                        if child.name == "__pycache__":
                            raise ArtifactRegistryError("studio-model-runtime-manifest-incomplete")
                        collect(child)
                    elif child.is_file():
                        actual_files.add(child.resolve(strict=True))
                    else:
                        raise ArtifactRegistryError("studio-model-runtime-manifest-incomplete")
            except ArtifactRegistryError:
                raise
            except OSError as error:
                raise ArtifactRegistryError("studio-model-runtime-manifest-incomplete") from error

        collect(package_root)
        collect(distribution_root)
        try:
            init_path = (package_root / "__init__.py").resolve(strict=True)
            metadata_path = (distribution_root / "METADATA").resolve(strict=True)
        except OSError as error:
            raise ArtifactRegistryError("studio-model-runtime-manifest-incomplete") from error
        code_manifest_paths = {
            path
            for path, file in manifest_by_path.items()
            if file["sourceKind"] == "code" and path.is_relative_to(configured)
        }
        if (
            not actual_files
            or init_path not in actual_files
            or metadata_path not in actual_files
            or actual_files != code_manifest_paths
        ):
            raise ArtifactRegistryError("studio-model-runtime-manifest-incomplete")
        try:
            metadata = metadata_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as error:
            raise ArtifactRegistryError("studio-model-runtime-manifest-incomplete") from error
        names = [line[5:].strip() for line in metadata if line.startswith("Name:")]
        versions = [line[8:].strip() for line in metadata if line.startswith("Version:")]
        if names != ["mlx-vlm"] or versions != [artifact["version"]]:
            raise ArtifactRegistryError("studio-vision-runtime-version-mismatch")

        digest = hashlib.sha256()
        for path in sorted(actual_files):
            file = manifest_by_path[path]
            digest.update(file["relativePath"].encode("utf-8"))
            digest.update(b"\0")
            digest.update(file["sha256"].encode("ascii"))
            digest.update(b"\0")
            digest.update(str(file["byteLength"]).encode("ascii"))
            digest.update(b"\n")
        return {
            "id": artifact["id"],
            "version": artifact["version"],
            "path": str(configured),
            "sha256": digest.hexdigest(),
        }

    def validate_ready_runtime_modules(
        self,
        artifact_id: str,
        configured_root: str,
        *,
        package_name: str,
        module_files: tuple[str, ...],
    ) -> dict[str, str]:
        current = self.resolve_ready_runtime_tree(
            artifact_id,
            configured_root,
            package_name=package_name,
        )
        configured = Path(current["path"])
        artifact = self.artifacts[artifact_id]
        manifest_paths = {
            self._manifest_path(file["relativePath"])
            for file in self._manifest(artifact)
            if file["sourceKind"] == "code"
        }
        for module_file in module_files:
            try:
                path = Path(module_file)
                if not path.is_absolute():
                    raise ValueError
                path = path.resolve(strict=True)
                path.relative_to(configured)
            except (OSError, ValueError) as error:
                raise ArtifactRegistryError("studio-vision-runtime-origin-invalid") from error
            if path not in manifest_paths:
                raise ArtifactRegistryError("studio-vision-runtime-origin-invalid")
        return current

    def _manifest_path(self, relative_path: str) -> Path:
        candidate = self.root
        try:
            for part in Path(relative_path).parts:
                candidate /= part
                if stat.S_ISLNK(candidate.lstat().st_mode):
                    raise ArtifactRegistryError(
                        "studio-model-artifact-path-outside-manifest"
                    )
            path = candidate.resolve(strict=True)
            path.relative_to(self.root)
            return path
        except ArtifactRegistryError:
            raise
        except FileNotFoundError as error:
            raise ArtifactRegistryError("studio-model-artifact-missing") from error
        except (OSError, ValueError) as error:
            raise ArtifactRegistryError("studio-model-artifact-path-outside-manifest") from error

    @staticmethod
    def _is_approved(artifact: dict[str, Any]) -> bool:
        if not (
            artifact.get("approved") is True
            and artifact.get("enabled") is True
            and artifact.get("licenseReview") == "approved"
            and isinstance(artifact.get("version"), str)
            and artifact["version"].strip()
            and artifact.get("artifactKind") in (
                "source-package", "binary-model", "checkpoint"
            )
            and isinstance(artifact.get("runtimeBackend"), str)
            and artifact["runtimeBackend"].strip()
            and isinstance(artifact.get("approvedBy"), str)
            and artifact["approvedBy"].strip()
            and _valid_approval_time(artifact.get("approvedAt"))
        ):
            return False
        sources = artifact.get("sources")
        evidence_set = artifact.get("licenseEvidence")
        if not isinstance(sources, list) or not sources or not isinstance(evidence_set, dict):
            return False
        source_ids = [source.get("id") for source in sources if isinstance(source, dict)]
        source_kinds = [source.get("kind") for source in sources if isinstance(source, dict)]
        if (
            len(source_ids) != len(sources)
            or len(set(source_ids)) != len(source_ids)
            or len(set(source_kinds)) != len(source_kinds)
            or any(kind not in ("code", "weights") for kind in source_kinds)
        ):
            return False
        for kind in ("code", "weights"):
            source = next((item for item in sources if item["kind"] == kind), None)
            evidence = evidence_set.get(kind)
            if source is None:
                if evidence != {
                    "applies": False,
                    "sourceId": None,
                    "sourceRevision": None,
                    "spdxExpression": None,
                    "evidenceUrl": None,
                }:
                    return False
                continue
            revision = source.get("sourceRevision")
            if not (
                _is_hex(revision, REVISION_LENGTH)
                and source.get("inventoryVerified") is True
                and isinstance(evidence, dict)
                and evidence.get("applies") is True
                and evidence.get("sourceId") == source.get("id")
                and evidence.get("sourceRevision") == revision
                and evidence.get("spdxExpression") in APPROVED_LICENSES
                and _license_url_bound_to_revision(
                    source.get("sourceUrl"), revision, evidence.get("evidenceUrl")
                )
                and not _has_evidence_placeholder(
                    source["sourceUrl"], revision, evidence["evidenceUrl"]
                )
            ):
                return False
        return True

    @staticmethod
    def _manifest(artifact: dict[str, Any]) -> list[dict[str, Any]]:
        source_manifest = []
        for source in artifact["sources"]:
            files = source.get("files")
            revision = source["sourceRevision"]
            if not isinstance(files, list) or not files:
                raise ArtifactRegistryError("studio-model-artifact-unapproved")
            for file in files:
                if not (
                    isinstance(file, dict)
                    and file.get("required") is True
                    and _valid_relative_file_path(file.get("relativePath"))
                    and type(file.get("executable")) is bool
                    and _is_hex(file.get("sha256"), DIGEST_LENGTH)
                    and type(file.get("byteLength")) is int
                    and file["byteLength"] > 0
                    and _url_bound_to_revision(
                        source.get("sourceUrl"), revision, file.get("sourceUrl")
                    )
                ):
                    raise ArtifactRegistryError("studio-model-artifact-unapproved")
                source_manifest.append({**file, "sourceKind": source["kind"]})

        source_paths = [file["relativePath"] for file in source_manifest]
        if not source_manifest or len(set(source_paths)) != len(source_paths):
            raise ArtifactRegistryError("studio-model-artifact-unapproved")

        has_derived_fields = "runtimeFiles" in artifact or "derivationReceipt" in artifact
        if not has_derived_fields:
            return source_manifest

        receipt = artifact.get("derivationReceipt")
        runtime_files = artifact.get("runtimeFiles")
        source_ids = {source["id"] for source in artifact["sources"]}
        if not (
            isinstance(runtime_files, list)
            and bool(runtime_files)
            and isinstance(receipt, dict)
            and receipt.get("schemaVersion") == 1
            and isinstance(receipt.get("recipeId"), str)
            and bool(receipt["recipeId"].strip())
            and isinstance(receipt.get("inputSourceIds"), list)
            and bool(receipt["inputSourceIds"])
            and len(set(receipt["inputSourceIds"])) == len(receipt["inputSourceIds"])
            and all(source_id in source_ids for source_id in receipt["inputSourceIds"])
            and _is_hex(receipt.get("outputTreeSha256"), DIGEST_LENGTH)
            and _is_hex(receipt.get("probeReportSha256"), DIGEST_LENGTH)
            and receipt.get("status") == "succeeded"
            and receipt.get("syntheticInput") is True
            and receipt.get("realModel") is True
        ):
            raise ArtifactRegistryError("studio-model-artifact-unapproved")

        source_kinds = {source["kind"] for source in artifact["sources"]}
        manifest = []
        for file in runtime_files:
            if not (
                isinstance(file, dict)
                and file.get("required") is True
                and _valid_relative_file_path(file.get("relativePath"))
                and _valid_relative_file_path(file.get("candidateRelativePath"))
                and type(file.get("executable")) is bool
                and file.get("sourceKind") in source_kinds
                and _is_hex(file.get("sha256"), DIGEST_LENGTH)
                and type(file.get("byteLength")) is int
                and file["byteLength"] > 0
                and "sourceUrl" not in file
            ):
                raise ArtifactRegistryError("studio-model-artifact-unapproved")
            manifest.append({**file, "derived": True})

        paths = [file["relativePath"] for file in manifest]
        candidate_paths = [file["candidateRelativePath"] for file in manifest]
        if (
            not manifest
            or len(set(paths)) != len(paths)
            or len(set(candidate_paths)) != len(candidate_paths)
        ):
            raise ArtifactRegistryError("studio-model-artifact-unapproved")
        return manifest

import hashlib
import json
import os
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest.mock import patch


WORKER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER_ROOT))

from artifact_registry import ArtifactRegistry, ArtifactRegistryError, RuntimeIdentity  # noqa: E402
import runtime_integrity  # noqa: E402
from runtime_integrity import (  # noqa: E402
    RuntimeExpectation,
    RuntimeIntegrityError,
    _critical_loaded_module_files,
    _declared_package_bindings,
    validate_runtime_process,
)


APPROVED_DEPENDENCY_LOCK_SHA256 = (
    "774b30f7f04e3d78faf72f75fca0d0b5823f8244d751c6c10f9c97f45fd85521"
)
APPROVED_WORKER_DEPENDENCY_LOCK_SHA256 = (
    "7b395d3286c1ce2012c5fb2507ad2a3ce4accb010dab69d3de2a973ed84b5041"
)
CRITICAL_PACKAGES = {
    "mlx-vlm": ("0.6.6", "mlx_vlm"),
    "mlx": ("0.32.0", "mlx"),
    "transformers": ("4.57.6", "transformers"),
    "Pillow": ("11.3.0", "PIL"),
    "numpy": ("2.4.1", "numpy"),
    "opencv-python": ("4.13.0.90", "cv2"),
    "scenedetect": ("0.7.1", "scenedetect"),
    "platformdirs": ("4.11.8", "platformdirs"),
}


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def tree_sha256(files: list[dict]) -> str:
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


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class FakeDistribution:
    name: str
    version: str
    origin: Path

    @property
    def metadata(self):
        return {"Name": self.name, "Version": self.version}

    @property
    def _path(self):
        return self.origin


def approval_packet_id(
    bundle: dict,
    receipt: dict,
    manifest: dict,
    package_inventory: dict | None = None,
) -> str:
    core = {
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
        "manifest": bundle["manifest"],
        "packageInventory": bundle["packageInventory"],
        "derivationReceipt": bundle["derivationReceipt"],
    }
    if package_inventory is not None:
        core.update({
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
        })
    return "worker-runtime-bundle-v1-sha256-" + sha256_bytes(
        canonical_json(core).encode("utf-8")
    )


class RuntimeFixture:
    def __init__(
        self,
        root: Path,
        *,
        probe_model_sha256: str | None = None,
        runtime_id: str = "synthetic-worker-runtime",
    ):
        self.model_root = root / "models"
        self.runtime_id = runtime_id
        self.runtime_version = "0.6.6-py312"
        self.python_version = "3.12.13"
        self.runtime_root = self.model_root / "runtime" / self.runtime_id
        self.runtime_root.mkdir(parents=True)
        self.runtime_root.chmod(0o700)
        file_payloads = {
            "bin/run-offline-worker": b"#!/bin/sh\nexit 0\n",
            "policy/deny-network.sb": b"(version 1)\n(deny network*)\n",
            "python/bin/python3.12": b"synthetic-python",
        }
        for name, (version, module_root) in CRITICAL_PACKAGES.items():
            file_payloads[f"site-packages/{module_root}/__init__.py"] = (
                f"__version__ = {version!r}\n".encode("utf-8")
            )
            normalized = name.replace("-", "_")
            metadata_path = f"site-packages/{normalized}-{version}.dist-info/METADATA"
            record_path = f"site-packages/{normalized}-{version}.dist-info/RECORD"
            file_payloads[metadata_path] = (
                f"Name: {name}\nVersion: {version}\n".encode("utf-8")
            )
            file_payloads[record_path] = (
                f"{module_root}/__init__.py,,\n"
                f"{normalized}-{version}.dist-info/METADATA,,\n"
                f"{normalized}-{version}.dist-info/RECORD,,\n"
            ).encode("utf-8")
        file_payloads.update({
            "site-packages/mlx_metal-0.32.0.dist-info/METADATA": (
                b"Name: mlx-metal\nVersion: 0.32.0\n"
            ),
            "site-packages/mlx_metal-0.32.0.dist-info/RECORD": (
                b"mlx/__init__.py,,\n"
                b"mlx_metal-0.32.0.dist-info/METADATA,,\n"
                b"mlx_metal-0.32.0.dist-info/RECORD,,\n"
            ),
        })
        executable_paths = {"bin/run-offline-worker", "python/bin/python3.12"}
        manifest_files = []
        for relative_path, payload in file_payloads.items():
            path = self.runtime_root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.parent.chmod(0o755)
            path.write_bytes(payload)
            executable = relative_path in executable_paths
            path.chmod(0o755 if executable else 0o644)
            manifest_files.append({
                "relativePath": relative_path,
                "byteLength": len(payload),
                "sha256": sha256_bytes(payload),
                "type": "file",
                "executable": executable,
            })
        self.tree_sha256 = tree_sha256(manifest_files)
        self.manifest = {
            "schemaVersion": 1,
            "files": manifest_files,
            "treeSha256": self.tree_sha256,
        }
        self.packages = {
            "schemaVersion": 1,
            "python": {
                "name": "cpython",
                "version": self.python_version,
                "build": None,
                "sourceMaterialId": "python-runtime:cpython:3.12.13:" + "1" * 64,
                "sourceSha256": "1" * 64,
                "licenseExpression": "PSF-2.0",
                "licenseEvidence": ["python/LICENSE"],
                "moduleRoots": ["python"],
            },
            "packages": [{
                "name": name,
                "version": version,
                "sourceMaterialId": f"python-distribution:{name}:{version}:" + "2" * 64,
                "sourceSha256": "2" * 64,
                "licenseExpression": "MIT",
                "licenseEvidence": [
                    f"site-packages/{name.replace('-', '_')}-{version}.dist-info/METADATA"
                ],
                "moduleRoots": [module_root],
            } for name, (version, module_root) in CRITICAL_PACKAGES.items()],
            "recordOwnership": {"unownedFiles": []},
            "licenseReview": "pending",
        }
        self.packages["packages"].append({
            "name": "mlx-metal",
            "version": "0.32.0",
            "sourceMaterialId": "python-distribution:mlx-metal:0.32.0:" + "2" * 64,
            "sourceSha256": "2" * 64,
            "licenseExpression": "MIT",
            "licenseEvidence": ["site-packages/mlx_metal-0.32.0.dist-info/METADATA"],
            "moduleRoots": ["mlx"],
        })
        archive_sha256 = "3" * 64
        self.receipt = {
            "schemaVersion": 1,
            "status": "succeeded",
            "materialPacketId": "worker-runtime-materials-v1-sha256-" + "4" * 64,
            "materialPacketSha256": "5" * 64,
            "approvalReceiptSha256": "6" * 64,
            "supplementalMaterialPacketId": (
                "worker-runtime-build-materials-v1-sha256-" + "c" * 64
            ),
            "supplementalApprovalReceiptSha256": "d" * 64,
            "builderToolPacketId": "worker-runtime-builder-tool-v1-sha256-" + "7" * 64,
            "builderToolApprovalReceiptSha256": "8" * 64,
            "builderToolVerifiedReceiptSha256": "9" * 64,
            "builderToolExecutionSecurityBoundary": {"network": "denied"},
            "builderToolExecutable": {
                "byteLength": 123,
                "sha256": "a" * 64,
                "sourceArchiveSha256": "b" * 64,
                "sourceMemberPath": "uv",
            },
            "dependencyLockSha256": APPROVED_DEPENDENCY_LOCK_SHA256,
            "workerDependencyLockSha256": APPROVED_WORKER_DEPENDENCY_LOCK_SHA256,
            "target": {
                "pythonVersionRange": ">=3.12.0 <3.13",
                "platform": "darwin",
                "architecture": "arm64",
            },
            "builder": {
                "uvVersion": "0.8.22",
                "archiveFormat": "ustar",
                "networkSandbox": "macos-sandbox-exec-deny-network",
            },
            "promotionVerification": {
                "relocationImportSelfCheck": "passed-before-promotion",
                "selfCheckScratchBoundary": "owned-private-outside-runtime-and-candidate-tree",
                "finalTreeVerification": "descriptor-safe-manifest-match-after-self-check-and-document-staging",
                "finalFileVerification": "runtime-tar-and-all-sidecars-rehashed-after-document-staging-immediately-before-promotion",
                "promotionFailureRecovery": "candidate-directory-dev-ino-plus-bundle-four-files-tree-exact-or-recovery-required",
            },
            "commands": ["uv --offline"],
            "startedAt": "2026-09-27T00:00:00.000Z",
            "completedAt": "2026-09-27T00:00:00.000Z",
            "archiveSha256": archive_sha256,
            "treeSha256": self.tree_sha256,
        }
        manifest_bytes = (canonical_json(self.manifest) + "\n").encode("utf-8")
        packages_bytes = (canonical_json(self.packages) + "\n").encode("utf-8")
        receipt_bytes = (canonical_json(self.receipt) + "\n").encode("utf-8")
        sidecars = {
            "archive": {
                "relativePath": "runtime.tar",
                "sha256": archive_sha256,
                "byteLength": 999,
            },
            "manifest": self._write_sidecar("runtime-manifest.json", manifest_bytes),
            "packageInventory": self._write_sidecar("runtime-packages.json", packages_bytes),
            "derivationReceipt": self._write_sidecar("derivation-receipt.json", receipt_bytes),
        }
        model_path = self.model_root / "qwen/model.safetensors"
        model_path.parent.mkdir(parents=True)
        model_path.write_bytes(b"synthetic-qwen-weights")
        model_path.chmod(0o600)
        self.model_sha256 = sha256_bytes(model_path.read_bytes())
        revision = "a" * 40
        model_artifact = {
            "id": "synthetic-qwen3-vl",
            "version": "fixture-4bit",
            "artifactKind": "checkpoint",
            "sources": [{
                "id": "weights",
                "kind": "weights",
                "sourceUrl": "https://models.invalid/org/qwen",
                "sourceRevision": revision,
                "inventoryVerified": True,
                "files": [{
                    "relativePath": "qwen/model.safetensors",
                    "sourceUrl": (
                        f"https://models.invalid/org/qwen/resolve/{revision}/model.safetensors"
                    ),
                    "required": True,
                    "executable": False,
                    "sha256": self.model_sha256,
                    "byteLength": model_path.stat().st_size,
                }],
            }],
            "licenseEvidence": {
                "code": {
                    "applies": False,
                    "sourceId": None,
                    "sourceRevision": None,
                    "spdxExpression": None,
                    "evidenceUrl": None,
                },
                "weights": {
                    "applies": True,
                    "sourceId": "weights",
                    "sourceRevision": revision,
                    "spdxExpression": "Apache-2.0",
                    "evidenceUrl": (
                        f"https://models.invalid/org/qwen/raw/{revision}/README.md"
                    ),
                },
            },
            "licenseReview": "approved",
            "runtimeBackend": "mlx-vlm-metal",
            "quantization": "4bit",
            "enabled": True,
            "approved": True,
            "approvedBy": "fixture-reviewer",
            "approvedAt": "2026-09-27T00:00:00.000Z",
            "testedDevices": ["synthetic"],
            "testSetVersion": "fixture-v1",
        }
        bundle = {
            "schemaVersion": 1,
            "id": self.runtime_id,
            "version": self.runtime_version,
            "pythonVersion": self.python_version,
            "platform": "darwin",
            "architecture": "arm64",
            "minimumOsVersion": "14.0",
            "dependencyLockSha256": APPROVED_DEPENDENCY_LOCK_SHA256,
            "workerDependencyLockSha256": APPROVED_WORKER_DEPENDENCY_LOCK_SHA256,
            "materialInventorySha256": "4" * 64,
            "approvalPacketId": "",
            **sidecars,
            "treeSha256": self.tree_sha256,
            "licenseReview": "approved",
            "enabled": True,
            "approved": True,
            "approvedBy": "fixture-reviewer",
            "approvedAt": "2026-09-27T00:00:00.000Z",
            "probe": None if probe_model_sha256 is None else {
                "receiptPath": "runtime/probes/synthetic.json",
                "receiptSha256": "e" * 64,
                "runtimeTreeSha256": self.tree_sha256,
                "modelSha256": probe_model_sha256,
                "promptVersion": "studio-qwen3-vl-zh-json-v2",
                "inputSha256": "f" * 64,
            },
        }
        bundle["approvalPacketId"] = approval_packet_id(bundle, self.receipt, self.manifest)
        self.lock_path = root / "models.lock.json"
        self.lock_path.write_text(canonical_json({
            "schemaVersion": 1,
            "artifacts": [model_artifact],
            "workerRuntimes": [bundle],
        }), encoding="utf-8")
        self.registry = ArtifactRegistry(
            str(self.lock_path.resolve()),
            str(self.model_root.resolve()),
        )
        self.model_path = model_path

    def _reload_registry(self) -> None:
        self.registry = ArtifactRegistry(
            str(self.lock_path.resolve()),
            str(self.model_root.resolve()),
        )

    def refresh_bundle(self) -> None:
        lock = json.loads(self.lock_path.read_text(encoding="utf-8"))
        bundle = lock["workerRuntimes"][0]
        for file in self.manifest["files"]:
            path = self.runtime_root / file["relativePath"]
            file["byteLength"] = path.stat().st_size
            file["sha256"] = sha256_bytes(path.read_bytes())
        self.tree_sha256 = tree_sha256(self.manifest["files"])
        self.manifest["treeSha256"] = self.tree_sha256
        self.receipt["treeSha256"] = self.tree_sha256
        bundle["treeSha256"] = self.tree_sha256
        if bundle["probe"] is not None:
            bundle["probe"]["runtimeTreeSha256"] = self.tree_sha256
        for field, relative_path, value in (
            ("manifest", "runtime-manifest.json", self.manifest),
            ("packageInventory", "runtime-packages.json", self.packages),
            ("derivationReceipt", "derivation-receipt.json", self.receipt),
        ):
            payload = (canonical_json(value) + "\n").encode("utf-8")
            path = self.runtime_root / relative_path
            path.write_bytes(payload)
            path.chmod(0o600)
            bundle[field].update({
                "sha256": sha256_bytes(payload),
                "byteLength": len(payload),
            })
        bundle["approvalPacketId"] = approval_packet_id(bundle, self.receipt, self.manifest)
        self.lock_path.write_text(canonical_json(lock), encoding="utf-8")
        self._reload_registry()

    def add_manifest_file(self, relative_path: str, payload: bytes) -> Path:
        path = self.runtime_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o755)
        path.write_bytes(payload)
        path.chmod(0o644)
        self.manifest["files"].append({
            "relativePath": relative_path,
            "byteLength": len(payload),
            "sha256": sha256_bytes(payload),
            "type": "file",
            "executable": False,
        })
        self.refresh_bundle()
        return path

    def _write_sidecar(self, relative_path: str, payload: bytes) -> dict:
        path = self.runtime_root / relative_path
        path.write_bytes(payload)
        path.chmod(0o600)
        return {
            "relativePath": relative_path,
            "sha256": sha256_bytes(payload),
            "byteLength": len(payload),
        }

    def expectation(self, **overrides) -> RuntimeExpectation:
        values = {
            "registry": self.registry,
            "artifact_id": self.runtime_id,
            "runtime_root": str(self.runtime_root.resolve()),
            "tree_sha256": self.tree_sha256,
            "executable": self.runtime_root / "python/bin/python3.12",
            "prefix": self.runtime_root / "python",
            "environment": {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "PYTHONNOUSERSITE": "1",
                "PYTHONSAFEPATH": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            "distributions": tuple(
                FakeDistribution(
                    name,
                    version,
                    self.runtime_root
                    / f"site-packages/{name.replace('-', '_')}-{version}.dist-info",
                )
                for name, (version, _module_root) in CRITICAL_PACKAGES.items()
            ) + (FakeDistribution(
                "mlx-metal",
                "0.32.0",
                self.runtime_root / "site-packages/mlx_metal-0.32.0.dist-info",
            ),),
            "module_files": {
                module_root: self.runtime_root / f"site-packages/{module_root}/__init__.py"
                for _name, (_version, module_root) in CRITICAL_PACKAGES.items()
            },
            "model_sha256": self.model_sha256,
        }
        values.update(overrides)
        return RuntimeExpectation(**values)


def runtime_fixture(root: Path, **arguments) -> RuntimeFixture:
    return RuntimeFixture(root, **arguments)


class RuntimeBundleResolutionTest(unittest.TestCase):
    def test_resolves_exact_installed_worker_runtime_identity(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory))
            locked = json.loads(fixture.lock_path.read_text(encoding="utf-8"))

            identity = fixture.registry.resolve_ready_worker_runtime(
                fixture.runtime_id,
                str(fixture.runtime_root.resolve()),
                fixture.tree_sha256,
            )

        self.assertEqual(identity, RuntimeIdentity(
            artifact_id="synthetic-worker-runtime",
            version="0.6.6-py312",
            root=str(fixture.runtime_root.resolve()),
            tree_sha256=fixture.tree_sha256,
            python_version="3.12.13",
        ))
        self.assertEqual(
            locked["workerRuntimes"][0]["approvalPacketId"],
            "worker-runtime-bundle-v1-sha256-"
            "5b16ed143840eda6c1c6d6868d899d3a3895e63e912352677b66a7affabc09a4",
        )

    def test_resolves_exact_extended_scene_bundle_identity(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory))
            locked = json.loads(fixture.lock_path.read_text(encoding="utf-8"))
            bundle = locked["workerRuntimes"][0]
            bundle["approvalPacketId"] = approval_packet_id(
                bundle,
                fixture.receipt,
                fixture.manifest,
                fixture.packages,
            )
            fixture.lock_path.write_text(canonical_json(locked), encoding="utf-8")
            fixture._reload_registry()

            identity = fixture.registry.resolve_ready_worker_runtime(
                fixture.runtime_id,
                str(fixture.runtime_root.resolve()),
                fixture.tree_sha256,
            )

        self.assertEqual(identity.tree_sha256, fixture.tree_sha256)

    def test_rejects_forged_bundle_packet_id_and_unbound_archive_field(self):
        for mutation in ("forged-packet", "archive-field"):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                fixture = runtime_fixture(Path(directory))
                lock = json.loads(fixture.lock_path.read_text(encoding="utf-8"))
                bundle = lock["workerRuntimes"][0]
                if mutation == "forged-packet":
                    bundle["approvalPacketId"] = (
                        "worker-runtime-bundle-v1-sha256-" + "0" * 64
                    )
                else:
                    bundle["archive"]["byteLength"] += 1
                fixture.lock_path.write_text(canonical_json(lock), encoding="utf-8")
                fixture._reload_registry()

                with self.assertRaisesRegex(
                    ArtifactRegistryError,
                    "studio-runtime-artifact-unapproved",
                ):
                    fixture.registry.resolve_ready_worker_runtime(
                        fixture.runtime_id,
                        str(fixture.runtime_root.resolve()),
                        fixture.tree_sha256,
                    )

    def test_rejects_arbitrary_material_packet_prefix_even_with_reapproved_bundle(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory))
            fixture.receipt["materialPacketId"] = "arbitrary-prefix-" + "4" * 64
            fixture.refresh_bundle()

            with self.assertRaisesRegex(
                ArtifactRegistryError,
                "studio-runtime-derivation-receipt-invalid",
            ):
                fixture.registry.resolve_ready_worker_runtime(
                    fixture.runtime_id,
                    str(fixture.runtime_root.resolve()),
                    fixture.tree_sha256,
                )

    def test_rejects_malformed_canonical_packet_inputs_with_stable_error(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory))
            fixture.receipt["builderToolExecutable"]["byteLength"] = 0
            fixture.refresh_bundle()

            try:
                fixture.registry.resolve_ready_worker_runtime(
                    fixture.runtime_id,
                    str(fixture.runtime_root.resolve()),
                    fixture.tree_sha256,
                )
            except ArtifactRegistryError as error:
                outcome = str(error)
            except Exception as error:
                outcome = f"unstable:{type(error).__name__}"
            else:
                outcome = "accepted"

        self.assertEqual(outcome, "studio-runtime-derivation-receipt-invalid")

    def test_rejects_unsafe_runtime_artifact_id_before_path_resolution(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory), runtime_id="../escaped")

            with self.assertRaisesRegex(
                ArtifactRegistryError,
                "studio-runtime-artifact-unapproved",
            ):
                fixture.registry.resolve_ready_worker_runtime(
                    fixture.runtime_id,
                    str(fixture.runtime_root.resolve()),
                    fixture.tree_sha256,
                )

    def test_rejects_symlinked_lexical_runtime_parent_and_artifact_directory(self):
        for mutation in ("runtime-parent", "artifact-directory"):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                fixture = runtime_fixture(Path(directory))
                runtime_parent = fixture.model_root / "runtime"
                if mutation == "runtime-parent":
                    relocated = fixture.model_root / "relocated-runtime"
                    runtime_parent.rename(relocated)
                    runtime_parent.symlink_to(relocated, target_is_directory=True)
                else:
                    relocated = runtime_parent / "relocated-artifact"
                    fixture.runtime_root.rename(relocated)
                    fixture.runtime_root.symlink_to(
                        relocated.name,
                        target_is_directory=True,
                    )
                lexical_runtime_root = (
                    fixture.model_root.resolve() / "runtime" / fixture.runtime_id
                )

                with self.assertRaisesRegex(
                    ArtifactRegistryError,
                    "studio-runtime-tree-mismatch",
                ):
                    fixture.registry.resolve_ready_worker_runtime(
                        fixture.runtime_id,
                        str(lexical_runtime_root),
                        fixture.tree_sha256,
                    )

    def test_pep503_duplicate_distribution_names_are_rejected(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory))
            template = dict(fixture.packages["packages"][0])
            fixture.packages["packages"].extend((
                {**template, "name": "Foo..Bar", "version": "1.0"},
                {**template, "name": "Foo__Bar", "version": "1.0"},
            ))
            fixture.refresh_bundle()

            with self.assertRaisesRegex(
                ArtifactRegistryError,
                "studio-runtime-package-inventory-invalid",
            ):
                fixture.registry.resolve_ready_worker_runtime(
                    fixture.runtime_id,
                    str(fixture.runtime_root.resolve()),
                    fixture.tree_sha256,
                )

    def test_rejects_sidecar_tamper_and_unknown_manifest_fields(self):
        for mutation in ("sidecar", "manifest-schema"):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                fixture = runtime_fixture(Path(directory))
                manifest_path = fixture.runtime_root / "runtime-manifest.json"
                if mutation == "sidecar":
                    manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
                else:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest["unexpected"] = True
                    payload = (canonical_json(manifest) + "\n").encode("utf-8")
                    manifest_path.write_bytes(payload)
                    lock = json.loads(fixture.lock_path.read_text(encoding="utf-8"))
                    lock["workerRuntimes"][0]["manifest"].update({
                        "sha256": sha256_bytes(payload),
                        "byteLength": len(payload),
                    })
                    fixture.lock_path.write_text(canonical_json(lock), encoding="utf-8")
                    fixture.registry = ArtifactRegistry(
                        str(fixture.lock_path.resolve()), str(fixture.model_root.resolve())
                    )
                with self.assertRaisesRegex(ArtifactRegistryError, "studio-runtime-"):
                    fixture.registry.resolve_ready_worker_runtime(
                        fixture.runtime_id,
                        str(fixture.runtime_root.resolve()),
                        fixture.tree_sha256,
                    )

    def test_rejects_extra_tree_file_and_executable_bit_mismatch(self):
        for mutation in ("extra", "mode"):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                fixture = runtime_fixture(Path(directory))
                if mutation == "extra":
                    extra = fixture.runtime_root / "site-packages/ambient.py"
                    extra.write_text("ambient = True\n", encoding="utf-8")
                    extra.chmod(0o644)
                else:
                    (fixture.runtime_root / "site-packages/mlx/__init__.py").chmod(0o755)
                with self.assertRaisesRegex(
                    ArtifactRegistryError,
                    "studio-runtime-tree-mismatch",
                ):
                    fixture.registry.resolve_ready_worker_runtime(
                        fixture.runtime_id,
                        str(fixture.runtime_root.resolve()),
                        fixture.tree_sha256,
                    )

    def test_rejects_symlinks_and_hardlinks_in_installed_tree(self):
        for mutation in ("symlink", "hardlink"):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                fixture = runtime_fixture(Path(directory))
                target = fixture.runtime_root / "site-packages/mlx/__init__.py"
                if mutation == "symlink":
                    target.unlink()
                    target.symlink_to(fixture.runtime_root / "site-packages/numpy/__init__.py")
                else:
                    link = fixture.runtime_root / "site-packages/hardlink.py"
                    os.link(target, link)
                with self.assertRaisesRegex(
                    ArtifactRegistryError,
                    "studio-runtime-tree-mismatch",
                ):
                    fixture.registry.resolve_ready_worker_runtime(
                        fixture.runtime_id,
                        str(fixture.runtime_root.resolve()),
                        fixture.tree_sha256,
                    )


class RuntimeProcessIntegrityTest(unittest.TestCase):
    def test_accepts_inventory_metadata_paths_relative_to_site_packages(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory))
            for package in fixture.packages["packages"]:
                package["licenseEvidence"] = [
                    path.removeprefix("site-packages/")
                    for path in package["licenseEvidence"]
                ]
            fixture.refresh_bundle()

            identity = validate_runtime_process(fixture.expectation())

        self.assertEqual(identity.artifact_id, fixture.runtime_id)

    def test_discovers_distributions_only_from_the_bound_dependency_layer(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory))
            declared = fixture.expectation().distributions
            bundled_python_pip = FakeDistribution(
                "pip",
                "26.2.1",
                fixture.runtime_root
                / "python/lib/python3.12/site-packages/pip-26.2.1.dist-info",
            )

            def distributions(*, path=None):
                if path == [str(fixture.runtime_root.resolve() / "site-packages")]:
                    return declared
                return (*declared, bundled_python_pip)

            with patch(
                "runtime_integrity.importlib.metadata.distributions",
                side_effect=distributions,
            ):
                identity = validate_runtime_process(fixture.expectation(
                    distributions=None,
                ))

        self.assertEqual(identity.artifact_id, fixture.runtime_id)

    def test_accepts_bound_namespace_root_and_binds_virtual_lazy_modules(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory))
            modules = {}
            for name, (_version, module_root) in CRITICAL_PACKAGES.items():
                module = ModuleType(module_root)
                module.__file__ = str(
                    fixture.runtime_root / f"site-packages/{module_root}/__init__.py"
                )
                modules[module_root] = module
            del modules["mlx"].__file__
            modules["mlx"].__path__ = [str(
                fixture.runtime_root / "site-packages/mlx"
            )]
            mlx_core = ModuleType("mlx.core")
            mlx_core.__file__ = str(
                fixture.runtime_root / "site-packages/mlx/__init__.py"
            )
            modules["mlx.core"] = mlx_core
            modules["transformers.virtual"] = ModuleType("transformers.virtual")

            with patch.object(
                runtime_integrity.importlib,
                "import_module",
                side_effect=lambda name: modules[name],
            ), patch.object(runtime_integrity.sys, "modules", modules):
                module_files = _critical_loaded_module_files()

        self.assertEqual(module_files["mlx"], Path(mlx_core.__file__))
        self.assertEqual(
            module_files["transformers.virtual"],
            Path(modules["transformers"].__file__),
        )

    def test_package_binding_revalidates_python_inventory_paths(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory))
            fixture.packages["python"]["licenseEvidence"] = ["python/é-LICENSE"]
            (fixture.runtime_root / "runtime-packages.json").write_text(
                canonical_json(fixture.packages) + "\n", encoding="utf-8"
            )
            identity = RuntimeIdentity(
                artifact_id=fixture.runtime_id,
                version=fixture.runtime_version,
                root=str(fixture.runtime_root.resolve()),
                tree_sha256=fixture.tree_sha256,
                python_version=fixture.python_version,
            )
            with self.assertRaisesRegex(
                RuntimeIntegrityError, "studio-runtime-package-origin-invalid"
            ):
                _declared_package_bindings(identity)

    def test_rejects_mlx_loaded_from_ambient_pythonpath(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory))
            ambient = Path(directory) / "outside/mlx/__init__.py"
            ambient.parent.mkdir(parents=True)
            ambient.write_text("__version__ = '0.32.0'\n", encoding="utf-8")
            module_files = dict(fixture.expectation().module_files)
            module_files["mlx"] = ambient

            with self.assertRaisesRegex(
                RuntimeIntegrityError,
                "studio-runtime-module-origin-invalid",
            ):
                validate_runtime_process(fixture.expectation(module_files=module_files))

    def test_rejects_missing_or_originless_scene_module_before_model_binding(self):
        for mutation in ("missing", "originless"):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                fixture = runtime_fixture(Path(directory))
                module_files = dict(fixture.expectation().module_files)
                if mutation == "missing":
                    module_files.pop("scenedetect")
                else:
                    module_files["platformdirs"] = None

                with self.assertRaisesRegex(
                    RuntimeIntegrityError,
                    "studio-runtime-module-origin-invalid",
                ):
                    validate_runtime_process(fixture.expectation(module_files=module_files))

    def test_rejects_scene_distribution_version_drift(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory))
            distributions = list(fixture.expectation().distributions)
            scene_index = next(
                index for index, distribution in enumerate(distributions)
                if distribution.name == "scenedetect"
            )
            current = distributions[scene_index]
            distributions[scene_index] = FakeDistribution(
                "scenedetect",
                "0.7.0",
                current.origin,
            )

            with self.assertRaisesRegex(
                RuntimeIntegrityError,
                "studio-runtime-package-version-mismatch",
            ):
                validate_runtime_process(fixture.expectation(
                    distributions=tuple(distributions),
                ))

    def test_rejects_scene_modules_from_repository_venv_or_user_site(self):
        for outside in (
            Path("repository/.venv/lib/python3.12/site-packages/scenedetect/__init__.py"),
            Path("Library/Python/3.12/lib/python/site-packages/scenedetect/__init__.py"),
        ):
            with self.subTest(outside=outside), TemporaryDirectory() as directory:
                fixture = runtime_fixture(Path(directory))
                ambient = Path(directory) / outside
                ambient.parent.mkdir(parents=True)
                ambient.write_text("__version__ = '0.7.1'\n", encoding="utf-8")
                module_files = dict(fixture.expectation().module_files)
                module_files["scenedetect"] = ambient

                with self.assertRaisesRegex(
                    RuntimeIntegrityError,
                    "studio-runtime-module-origin-invalid",
                ):
                    validate_runtime_process(fixture.expectation(
                        module_files=module_files,
                    ))

    def test_rejects_python_executable_outside_runtime(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory))
            with self.assertRaisesRegex(
                RuntimeIntegrityError,
                "studio-runtime-python-mismatch",
            ):
                validate_runtime_process(fixture.expectation(executable=Path("/usr/bin/python3")))

    def test_rejects_wrong_prefix_and_missing_offline_variable(self):
        for mutation in ("prefix", "offline"):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                fixture = runtime_fixture(Path(directory))
                overrides = {}
                expected = "studio-runtime-python-mismatch"
                if mutation == "prefix":
                    overrides["prefix"] = Path(directory) / "outside-python"
                else:
                    environment = dict(fixture.expectation().environment)
                    environment.pop("TRANSFORMERS_OFFLINE")
                    overrides["environment"] = environment
                    expected = "studio-runtime-offline-environment-required"
                with self.assertRaisesRegex(RuntimeIntegrityError, expected):
                    validate_runtime_process(fixture.expectation(**overrides))

    def test_explicit_empty_environment_cannot_fall_back_to_ambient_values(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory))
            with patch.dict(os.environ, fixture.expectation().environment):
                with self.assertRaisesRegex(
                    RuntimeIntegrityError,
                    "studio-runtime-offline-environment-required",
                ):
                    validate_runtime_process(fixture.expectation(environment={}))

    def test_rejects_package_version_mismatch(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory))
            distributions = list(fixture.expectation().distributions)
            distributions[1] = FakeDistribution(
                "mlx",
                "0.31.0",
                fixture.runtime_root / "site-packages/mlx-0.32.0.dist-info",
            )
            with self.assertRaisesRegex(
                RuntimeIntegrityError,
                "studio-runtime-package-version-mismatch",
            ):
                validate_runtime_process(fixture.expectation(distributions=tuple(distributions)))

    def test_rejects_distribution_metadata_loaded_outside_runtime(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory))
            distributions = list(fixture.expectation().distributions)
            ambient = Path(directory) / "ambient/mlx_vlm-0.6.6.dist-info"
            ambient.mkdir(parents=True)
            distributions[0] = FakeDistribution("mlx-vlm", "0.6.6", ambient)

            with self.assertRaisesRegex(
                RuntimeIntegrityError,
                "studio-runtime-package-origin-invalid",
            ):
                validate_runtime_process(fixture.expectation(
                    distributions=tuple(distributions),
                ))

    def test_revalidates_runtime_tree_after_distribution_and_record_checks(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory))
            distributions = fixture.expectation().distributions

            def mutate_after_enumeration():
                yield from distributions
                (fixture.runtime_root / "site-packages/mlx/__init__.py").write_text(
                    "mutated-after-package-checks\n",
                    encoding="utf-8",
                )

            with self.assertRaisesRegex(
                RuntimeIntegrityError,
                "studio-runtime-tree-mismatch",
            ):
                validate_runtime_process(fixture.expectation(
                    distributions=mutate_after_enumeration(),
                ))

    def test_rejects_critical_module_mapped_to_another_declared_package_file(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory))
            module_files = dict(fixture.expectation().module_files)
            module_files["mlx"] = (
                fixture.runtime_root / "site-packages/numpy/__init__.py"
            )

            try:
                validate_runtime_process(fixture.expectation(module_files=module_files))
            except RuntimeIntegrityError as error:
                outcome = str(error)
            except Exception as error:  # A raw TypeError is not a fail-closed protocol result.
                outcome = f"unstable:{type(error).__name__}"
            else:
                outcome = "accepted"

            self.assertEqual(outcome, "studio-runtime-module-origin-invalid")

    def test_rejects_namespace_module_without_a_file_origin(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory))
            module_files = dict(fixture.expectation().module_files)
            module_files["mlx"] = None

            try:
                validate_runtime_process(fixture.expectation(module_files=module_files))
            except RuntimeIntegrityError as error:
                outcome = str(error)
            except Exception as error:  # A raw TypeError is not a fail-closed protocol result.
                outcome = f"unstable:{type(error).__name__}"
            else:
                outcome = "accepted"

            self.assertEqual(outcome, "studio-runtime-module-origin-invalid")

    def test_rejects_bytecode_origin_even_when_manifested_and_record_owned(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory))
            bytecode_relative = "site-packages/mlx/__pycache__/__init__.cpython-312.pyc"
            record = fixture.runtime_root / "site-packages/mlx-0.32.0.dist-info/RECORD"
            record.write_text(
                record.read_text(encoding="utf-8") + "mlx/__pycache__/__init__.cpython-312.pyc,,\n",
                encoding="utf-8",
            )
            bytecode = fixture.add_manifest_file(bytecode_relative, b"synthetic-bytecode")
            module_files = dict(fixture.expectation().module_files)
            module_files["mlx"] = bytecode

            with self.assertRaisesRegex(
                RuntimeIntegrityError,
                "studio-runtime-module-origin-invalid",
            ):
                validate_runtime_process(fixture.expectation(module_files=module_files))

    def test_rejects_qwen_bound_to_a_different_runtime_probe(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory), probe_model_sha256="d" * 64)
            with self.assertRaisesRegex(
                RuntimeIntegrityError,
                "studio-runtime-model-binding-mismatch",
            ):
                validate_runtime_process(fixture.expectation())

    def test_accepts_exact_python_packages_and_all_critical_module_origins(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory))

            identity = validate_runtime_process(fixture.expectation())

        self.assertEqual(identity.tree_sha256, fixture.tree_sha256)
        self.assertEqual(identity.root, str(fixture.runtime_root.resolve()))

    def test_pep503_equivalent_inventory_and_distribution_names_match(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory))
            fixture.packages["packages"][0]["name"] = "MLX..VLM"
            fixture.refresh_bundle()

            identity = validate_runtime_process(fixture.expectation())

        self.assertEqual(identity.artifact_id, fixture.runtime_id)


if __name__ == "__main__":
    unittest.main()

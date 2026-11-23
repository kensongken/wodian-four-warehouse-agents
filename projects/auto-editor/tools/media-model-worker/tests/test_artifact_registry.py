import hashlib
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


WORKER_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(WORKER_ROOT))

import artifact_registry as artifact_registry_module  # noqa: E402
from artifact_registry import (  # noqa: E402
    ArtifactRegistry,
    ArtifactRegistryError,
    RuntimeIdentity,
    _runtime_tree_sha256,
    _runtime_path_collision_key,
    _valid_relative_file_path,
)


UTF8_PATH_ORDER = [
    "runtime/A-upper.txt",
    "runtime/a-lower.txt",
    "runtime/e\u0301-combining.txt",
    "runtime/ñ-non-ascii.txt",
    "runtime/\ue000-private.txt",
    "runtime/\U00010000-supplementary.txt",
]
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
UTF8_PATH_RECORD_TREE_SHA256 = (
    "ce65004fb2ff951ea50a7ca4922e6e65f6aaa677ecb0a2a58517a55023459428"
)
PACKAGE_INVENTORY_SCALAR_VECTORS = json.loads(
    (
        REPOSITORY_ROOT
        / "scripts/tests/worker-runtime-package-inventory-scalar-vectors.json"
    ).read_text(encoding="utf-8")
)
APPROVED_DEPENDENCY_LOCK_SHA256 = (
    "774b30f7f04e3d78faf72f75fca0d0b5823f8244d751c6c10f9c97f45fd85521"
)
APPROVED_WORKER_DEPENDENCY_LOCK_SHA256 = (
    "7b395d3286c1ce2012c5fb2507ad2a3ce4accb010dab69d3de2a973ed84b5041"
)


def valid_runtime_package_inventory() -> dict:
    return {
        "schemaVersion": 1,
        "python": {
            "name": "cpython", "version": "3.12.13", "build": None,
            "sourceMaterialId": "python-runtime:cpython:3.12.13:" + "1" * 64,
            "sourceSha256": "1" * 64, "licenseExpression": "PSF-2.0",
            "licenseEvidence": ["python/LICENSE"], "moduleRoots": ["python"],
        },
        "packages": [{
            "name": "fixture-package", "version": "1.0.0",
            "sourceMaterialId": "python-distribution:fixture-package:1.0.0:" + "2" * 64,
            "sourceSha256": "2" * 64, "licenseExpression": "MIT",
            "licenseEvidence": ["fixture_package-1.0.0.dist-info/METADATA"],
            "moduleRoots": ["fixture_package"],
        }],
        "recordOwnership": {"unownedFiles": []},
        "licenseReview": "pending",
    }


class RuntimePackageInventoryContractTest(unittest.TestCase):
    @staticmethod
    def _set_nested_value(value: dict, target: str, replacement) -> None:
        parts = target.split(".")
        parent = value
        for part in parts[:-1]:
            parent = parent[int(part)] if part.isdecimal() else parent[part]
        parent[parts[-1]] = replacement

    def test_python_and_package_paths_share_one_portable_ascii_contract(self):
        validator = getattr(artifact_registry_module, "_validate_runtime_package_inventory", None)
        self.assertTrue(callable(validator))
        self.assertEqual(validator(valid_runtime_package_inventory(), "3.12.13"), None)
        mutations = (
            lambda value: value.update(schemaVersion=True),
            lambda value: value["python"].update(sourceSha256=None),
            lambda value: value["python"].update(licenseEvidence="python/LICENSE"),
            lambda value: value["packages"][0].update(moduleRoots="fixture_package"),
            lambda value: value["python"].update(licenseEvidence=["python/é"]),
            lambda value: value["python"].update(moduleRoots=["é-python"]),
            lambda value: value["packages"][0].update(licenseEvidence=["pkg/é"]),
            lambda value: value["packages"][0].update(moduleRoots=["é-package"]),
            lambda value: value["packages"][0].update(moduleRoots=["nested/root"]),
            lambda value: value["packages"][0].update(moduleRoots=[""]),
            lambda value: value["packages"][0].update(moduleRoots=["."]),
            lambda value: value["packages"][0].update(moduleRoots=[".."]),
            lambda value: value["packages"][0].update(licenseEvidence=["LICENSE", "license"]),
            lambda value: value["packages"][0].update(moduleRoots=["Pkg", "pkg"]),
            lambda value: value["packages"].append({
                **value["packages"][0], "name": "fixture_package",
            }),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                value = valid_runtime_package_inventory()
                mutate(value)
                with self.assertRaisesRegex(
                    ArtifactRegistryError, "studio-runtime-package-inventory-invalid"
                ):
                    validator(value, "3.12.13")

    def test_raw_malformed_package_inventory_json_is_rejected(self):
        registry = ArtifactRegistry.__new__(ArtifactRegistry)
        with self.assertRaisesRegex(
            ArtifactRegistryError, "studio-runtime-package-inventory-invalid"
        ):
            registry._runtime_package_inventory(b"\xff", {"pythonVersion": "3.12.13"})

    def test_scalar_metadata_matches_the_shared_cross_language_vectors(self):
        validator = artifact_registry_module._validate_runtime_package_inventory
        for vector in PACKAGE_INVENTORY_SCALAR_VECTORS["schemaVersions"]:
            with self.subTest(vector=vector["label"]):
                value = valid_runtime_package_inventory()
                value["schemaVersion"] = json.loads(vector["valueJson"])
                if vector["valid"]:
                    self.assertEqual(validator(value, "3.12.13"), None)
                else:
                    with self.assertRaises(ArtifactRegistryError):
                        validator(value, "3.12.13")
        for vector in PACKAGE_INVENTORY_SCALAR_VECTORS["metadataScalars"]:
            with self.subTest(vector=vector["label"]):
                value = valid_runtime_package_inventory()
                replacement = json.loads(vector["valueJson"])
                self._set_nested_value(value, vector["target"], replacement)
                python_version = (
                    replacement if vector["target"] == "python.version" else "3.12.13"
                )
                if vector["valid"]:
                    self.assertEqual(validator(value, python_version), None)
                else:
                    with self.assertRaises(ArtifactRegistryError):
                        validator(value, python_version)


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def valid_worker_runtime_bundle() -> dict:
    sidecar = lambda path, digit: {
        "relativePath": path, "sha256": digit * 64, "byteLength": 128,
    }
    return {
        "schemaVersion": 1,
        "id": "synthetic-worker-runtime",
        "version": "0.6.6-py312",
        "pythonVersion": "3.12.13",
        "platform": "darwin",
        "architecture": "arm64",
        "minimumOsVersion": "14.0",
        "dependencyLockSha256": APPROVED_DEPENDENCY_LOCK_SHA256,
        "workerDependencyLockSha256": APPROVED_WORKER_DEPENDENCY_LOCK_SHA256,
        "materialInventorySha256": "1" * 64,
        "approvalPacketId": "worker-runtime-bundle-v1-sha256-" + "a" * 64,
        "archive": sidecar("runtime.tar", "1"),
        "manifest": sidecar("runtime-manifest.json", "2"),
        "packageInventory": sidecar("runtime-packages.json", "3"),
        "derivationReceipt": sidecar("derivation-receipt.json", "4"),
        "treeSha256": "5" * 64,
        "licenseReview": "approved",
        "enabled": True,
        "approved": True,
        "approvedBy": "fixture-reviewer",
        "approvedAt": "2026-09-28T01:00:00Z",
        "probe": None,
    }


def valid_runtime_derivation_receipt(bundle: dict) -> dict:
    return {
        "schemaVersion": 1,
        "status": "succeeded",
        "materialPacketId": (
            "worker-runtime-materials-v1-sha256-"
            + bundle["materialInventorySha256"]
        ),
        "materialPacketSha256": "6" * 64,
        "approvalReceiptSha256": "7" * 64,
        "supplementalMaterialPacketId": (
            "worker-runtime-build-materials-v1-sha256-" + "8" * 64
        ),
        "supplementalApprovalReceiptSha256": "9" * 64,
        "builderToolPacketId": (
            "worker-runtime-builder-tool-v1-sha256-" + "a" * 64
        ),
        "builderToolApprovalReceiptSha256": "b" * 64,
        "builderToolVerifiedReceiptSha256": "c" * 64,
        "builderToolExecutionSecurityBoundary": {"sandbox": "deny-network"},
        "builderToolExecutable": {"byteLength": 1, "sha256": "d" * 64},
        "dependencyLockSha256": bundle["dependencyLockSha256"],
        "workerDependencyLockSha256": bundle["workerDependencyLockSha256"],
        "target": {
            "pythonVersionRange": ">=3.12.0 <3.13",
            "platform": bundle["platform"],
            "architecture": bundle["architecture"],
        },
        "builder": {"id": "fixture-builder"},
        "promotionVerification": {
            "relocationImportSelfCheck": "passed-before-promotion",
        },
        "commands": [],
        "startedAt": "2026-09-28T01:00:00Z",
        "completedAt": "2026-09-28T01:01:00Z",
        "archiveSha256": bundle["archive"]["sha256"],
        "treeSha256": bundle["treeSha256"],
    }


class WorkerRuntimeDualLockContractTest(unittest.TestCase):
    def test_runtime_bundle_requires_both_exact_dependency_locks(self):
        bundle = valid_worker_runtime_bundle()
        self.assertTrue(ArtifactRegistry._valid_worker_runtime(bundle))

        missing = dict(bundle)
        del missing["workerDependencyLockSha256"]
        self.assertFalse(ArtifactRegistry._valid_worker_runtime(missing))

        mismatched = dict(bundle, workerDependencyLockSha256="e" * 64)
        self.assertFalse(ArtifactRegistry._valid_worker_runtime(mismatched))

    def test_derivation_receipt_binds_the_worker_lock(self):
        bundle = valid_worker_runtime_bundle()
        receipt = valid_runtime_derivation_receipt(bundle)
        registry = ArtifactRegistry.__new__(ArtifactRegistry)

        parsed = registry._runtime_derivation_receipt(
            json.dumps(receipt).encode("utf-8"), bundle
        )
        self.assertEqual(
            parsed["workerDependencyLockSha256"],
            APPROVED_WORKER_DEPENDENCY_LOCK_SHA256,
        )

        receipt["workerDependencyLockSha256"] = "e" * 64
        with self.assertRaisesRegex(
            ArtifactRegistryError, "studio-runtime-derivation-receipt-invalid"
        ):
            registry._runtime_derivation_receipt(
                json.dumps(receipt).encode("utf-8"), bundle
            )


class RuntimePathOrderingTests(unittest.TestCase):
    def test_tree_digest_uses_fixed_unsigned_utf8_path_order(self):
        files = [
            {
                "relativePath": relative_path,
                "byteLength": 0,
                "sha256": EMPTY_SHA256,
                "type": "file",
                "executable": False,
            }
            for relative_path in reversed(UTF8_PATH_ORDER)
        ]

        self.assertEqual(
            _runtime_tree_sha256(files),
            UTF8_PATH_RECORD_TREE_SHA256,
        )

    def test_runtime_paths_are_printable_portable_ascii_only(self):
        self.assertTrue(_valid_relative_file_path(
            "site-packages/scipy/io/tests/data/Transparent Busy.ani"
        ))
        for path in (
            "runtime/e\u0301-combining.txt",
            "runtime/\u00f1-bmp.txt",
            "runtime/\ue000-private.txt",
            "runtime/\U00010000-supplementary.txt",
            "runtime/\u00df.txt",
            "runtime/\ud800.txt",
            "runtime/\udc00.txt",
            "runtime/bad\\path",
            "runtime/../escape",
            "runtime/control\n.txt",
        ):
            with self.subTest(path=repr(path)):
                self.assertFalse(_valid_relative_file_path(path))

    def test_runtime_path_collision_key_folds_ascii_only(self):
        self.assertEqual(_runtime_path_collision_key("Pkg/Case.PY"), "pkg/case.py")
        self.assertEqual(_runtime_path_collision_key("runtime/SS.txt"), "runtime/ss.txt")
        self.assertFalse(_valid_relative_file_path("runtime/ß.txt"))
        self.assertTrue(_valid_relative_file_path("runtime/ss.txt"))


def derived_lock(root: Path, *, receipt_status: str = "succeeded") -> tuple[Path, Path]:
    revision = "a" * 40
    runtime_bytes = b"synthetic-derived-checkpoint"
    runtime_path = root / "qwen/model.safetensors"
    runtime_path.parent.mkdir(parents=True)
    runtime_path.write_bytes(runtime_bytes)
    runtime_path.chmod(0o600)
    source_root = "https://models.invalid/org/qwen"
    lock_path = root / "models.lock.json"
    lock_path.write_text(json.dumps({
        "schemaVersion": 1,
        "artifacts": [{
            "id": "synthetic-derived-qwen",
            "version": "fixture-4bit",
            "artifactKind": "checkpoint",
            "sources": [{
                "id": "code",
                "kind": "code",
                "sourceUrl": "https://codeload.github.com/example/converter",
                "sourceRevision": revision,
                "inventoryVerified": True,
                "files": [{
                    "relativePath": "inputs/converter.tar.gz",
                    "sourceUrl": f"https://codeload.github.com/example/converter/tar.gz/{revision}",
                    "required": True,
                    "executable": False,
                    "sha256": "4" * 64,
                    "byteLength": 200,
                }],
            }, {
                "id": "weights",
                "kind": "weights",
                "sourceUrl": source_root,
                "sourceRevision": revision,
                "inventoryVerified": True,
                "files": [{
                    "relativePath": "inputs/model.safetensors",
                    "sourceUrl": f"{source_root}/resolve/{revision}/model.safetensors",
                    "required": True,
                    "executable": False,
                    "sha256": "3" * 64,
                    "byteLength": 100,
                }],
            }],
            "runtimeFiles": [{
                "relativePath": "qwen/model.safetensors",
                "candidateRelativePath": "qwen/model.safetensors",
                "required": True,
                "executable": False,
                "sourceKind": "weights",
                "sha256": sha256(runtime_bytes),
                "byteLength": len(runtime_bytes),
            }],
            "derivationReceipt": {
                "schemaVersion": 1,
                "recipeId": "synthetic-qwen-conversion-v1",
                "inputSourceIds": ["code", "weights"],
                "outputTreeSha256": "1" * 64,
                "probeReportSha256": "2" * 64,
                "status": receipt_status,
                "syntheticInput": True,
                "realModel": True,
            },
            "licenseEvidence": {
                "code": {
                    "applies": True,
                    "sourceId": "code",
                    "sourceRevision": revision,
                    "spdxExpression": "MIT",
                    "evidenceUrl": f"https://raw.githubusercontent.com/example/converter/{revision}/LICENSE",
                },
                "weights": {
                    "applies": True,
                    "sourceId": "weights",
                    "sourceRevision": revision,
                    "spdxExpression": "Apache-2.0",
                    "evidenceUrl": f"{source_root}/raw/{revision}/README.md",
                },
            },
            "licenseReview": "approved",
            "runtimeBackend": "synthetic-mlx",
            "quantization": "4bit",
            "enabled": True,
            "approved": True,
            "approvedBy": "fixture-reviewer",
            "approvedAt": "2026-09-22T18:12:31Z",
            "testedDevices": ["synthetic"],
            "testSetVersion": "fixture-v1",
        }],
    }), encoding="utf-8")
    return lock_path, runtime_path


class ArtifactRegistryDerivedRuntimeTest(unittest.TestCase):
    def test_runtime_identity_is_immutable(self):
        identity = RuntimeIdentity(
            artifact_id="runtime-a",
            version="1.0.0",
            root="/private/runtime-a",
            tree_sha256="a" * 64,
            python_version="3.12.13",
        )

        with self.assertRaises(AttributeError):
            identity.version = "2.0.0"

    def test_resolves_a_probed_derived_runtime_instead_of_the_input_source_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path, runtime_path = derived_lock(root)
            registry = ArtifactRegistry(str(lock_path.resolve()), str(root.resolve()))

            result = registry.resolve_ready_artifact(
                "synthetic-derived-qwen",
                str(runtime_path.resolve()),
                executable=False,
                required_source_kind="weights",
            )

            self.assertEqual(result["path"], str(runtime_path.resolve()))
            self.assertEqual(result["sha256"], sha256(runtime_path.read_bytes()))

    def test_rejects_a_derived_runtime_without_a_successful_real_model_probe(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path, runtime_path = derived_lock(root, receipt_status="pending")
            registry = ArtifactRegistry(str(lock_path.resolve()), str(root.resolve()))

            with self.assertRaisesRegex(
                ArtifactRegistryError,
                "studio-model-artifact-unapproved",
            ):
                registry.resolve_ready_artifact(
                    "synthetic-derived-qwen",
                    str(runtime_path.resolve()),
                    executable=False,
                    required_source_kind="weights",
                )


if __name__ == "__main__":
    unittest.main()

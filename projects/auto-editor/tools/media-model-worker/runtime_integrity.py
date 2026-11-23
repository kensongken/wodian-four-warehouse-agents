"""Fail-closed identity checks for the installed formal worker runtime."""

from __future__ import annotations

import csv
import importlib
import importlib.metadata
import io
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from artifact_registry import (
    ArtifactRegistry,
    ArtifactRegistryError,
    RuntimeIdentity,
    _validate_runtime_package_inventory,
)


CRITICAL_MODULES = (
    "mlx_vlm",
    "mlx",
    "transformers",
    "PIL",
    "numpy",
    "cv2",
    "scenedetect",
    "platformdirs",
)
OFFLINE_ENVIRONMENT = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "PYTHONNOUSERSITE": "1",
    "PYTHONSAFEPATH": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}


class RuntimeIntegrityError(ValueError):
    """Stable runtime-process failure that never exposes a local path."""


@dataclass(frozen=True)
class RuntimeExpectation:
    registry: ArtifactRegistry
    artifact_id: str
    runtime_root: str
    tree_sha256: str
    executable: Path | None = None
    prefix: Path | None = None
    environment: Mapping[str, str] | None = None
    distributions: Iterable[Any] | None = None
    module_files: Mapping[str, Path] | None = None
    model_sha256: str | None = None


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value.lower())


def _require_path_inside(root: str, path: Path, *, expected: Path) -> None:
    try:
        runtime = Path(root).resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(runtime)
        if resolved != expected.resolve(strict=True):
            raise ValueError
    except (OSError, ValueError) as error:
        raise RuntimeIntegrityError("studio-runtime-python-mismatch") from error


def _require_offline_environment(environment: Mapping[str, str]) -> None:
    if any(environment.get(name) != value for name, value in OFFLINE_ENVIRONMENT.items()):
        raise RuntimeIntegrityError("studio-runtime-offline-environment-required")


def _load_runtime_sidecar(identity: RuntimeIdentity, name: str) -> dict[str, Any]:
    try:
        path = Path(identity.root) / name
        resolved = path.resolve(strict=True)
        resolved.relative_to(Path(identity.root).resolve(strict=True))
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeIntegrityError("studio-runtime-sidecar-mismatch") from error
    if not isinstance(value, dict):
        raise RuntimeIntegrityError("studio-runtime-sidecar-mismatch")
    return value


def _distribution_identity(distribution: Any) -> tuple[str, str]:
    try:
        metadata = distribution.metadata
        name = metadata["Name"]
        version = metadata["Version"]
    except (AttributeError, KeyError, TypeError) as error:
        raise RuntimeIntegrityError("studio-runtime-package-version-mismatch") from error
    if not isinstance(name, str) or not name.strip() or not isinstance(version, str) or not version:
        raise RuntimeIntegrityError("studio-runtime-package-version-mismatch")
    return _normalized_distribution_name(name), version


def _manifest_paths(identity: RuntimeIdentity) -> set[str]:
    manifest = _load_runtime_sidecar(identity, "runtime-manifest.json")
    return {
        file["relativePath"]
        for file in manifest.get("files", [])
        if isinstance(file, dict) and isinstance(file.get("relativePath"), str)
    }


def _path_relative_to_runtime(root: Path, value: Any, code: str) -> str:
    try:
        path = Path(value)
        if not path.is_absolute() or path.is_symlink():
            raise ValueError
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode):
            raise ValueError
        return path.resolve(strict=True).relative_to(root).as_posix()
    except (OSError, TypeError, ValueError) as error:
        raise RuntimeIntegrityError(code) from error


def _record_paths(
    identity: RuntimeIdentity,
    record_relative: str,
    manifest_paths: set[str],
) -> set[str]:
    runtime = Path(identity.root).resolve(strict=True)
    if record_relative not in manifest_paths:
        raise RuntimeIntegrityError("studio-runtime-package-origin-invalid")
    try:
        payload = (runtime / record_relative).read_text(encoding="utf-8")
        rows = csv.reader(io.StringIO(payload), strict=True)
        owned: set[str] = set()
        for row in rows:
            if len(row) != 3 or not row[0]:
                raise ValueError
            candidate = Path(row[0])
            if (
                candidate.is_absolute()
                or "\\" in row[0]
                or any(part in {"", ".", ".."} for part in candidate.parts)
            ):
                raise ValueError
            relative = f"site-packages/{candidate.as_posix()}"
            if relative in owned or relative not in manifest_paths:
                raise ValueError
            owned.add(relative)
        return owned
    except (OSError, UnicodeDecodeError, csv.Error, ValueError) as error:
        raise RuntimeIntegrityError("studio-runtime-package-origin-invalid") from error


def _declared_package_bindings(identity: RuntimeIdentity) -> tuple[
    dict[str, dict[str, Any]],
    set[str],
]:
    inventory = _load_runtime_sidecar(identity, "runtime-packages.json")
    try:
        _validate_runtime_package_inventory(inventory, identity.python_version)
    except ArtifactRegistryError as error:
        raise RuntimeIntegrityError("studio-runtime-package-origin-invalid") from error
    manifest_paths = _manifest_paths(identity)
    bindings: dict[str, dict[str, Any]] = {}
    for package in inventory.get("packages", []):
        normalized = _normalized_distribution_name(package["name"])
        metadata_inventory_paths = [
            path for path in package["licenseEvidence"]
            if path.endswith(".dist-info/METADATA")
        ]
        if len(metadata_inventory_paths) != 1:
            raise RuntimeIntegrityError("studio-runtime-package-origin-invalid")
        metadata_inventory_path = metadata_inventory_paths[0]
        metadata_relative = (
            metadata_inventory_path
            if metadata_inventory_path.startswith("site-packages/")
            else f"site-packages/{metadata_inventory_path}"
        )
        if metadata_relative not in manifest_paths:
            raise RuntimeIntegrityError("studio-runtime-package-origin-invalid")
        dist_info_relative = str(Path(metadata_relative).parent.as_posix())
        record_relative = f"{dist_info_relative}/RECORD"
        owned = _record_paths(identity, record_relative, manifest_paths)
        if metadata_relative not in owned or record_relative not in owned:
            raise RuntimeIntegrityError("studio-runtime-package-origin-invalid")
        roots = set(package["moduleRoots"])
        bindings[normalized] = {
            "version": package["version"],
            "distInfo": dist_info_relative,
            "moduleRoots": roots,
            "owned": owned,
        }
    return bindings, manifest_paths


def _require_declared_packages(
    identity: RuntimeIdentity,
    distributions: Iterable[Any],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    declared, manifest_paths = _declared_package_bindings(identity)
    observed: dict[str, str] = {}
    for distribution in distributions:
        name, version = _distribution_identity(distribution)
        if name in observed:
            raise RuntimeIntegrityError("studio-runtime-package-version-mismatch")
        binding = declared.get(name)
        if binding is None:
            raise RuntimeIntegrityError("studio-runtime-package-version-mismatch")
        origin = getattr(distribution, "_path", None)
        relative = _path_relative_to_runtime(
            Path(identity.root).resolve(strict=True),
            origin,
            "studio-runtime-package-origin-invalid",
        )
        if relative != binding["distInfo"]:
            raise RuntimeIntegrityError("studio-runtime-package-origin-invalid")
        observed[name] = version
    expected_versions = {name: value["version"] for name, value in declared.items()}
    if observed != expected_versions:
        raise RuntimeIntegrityError("studio-runtime-package-version-mismatch")
    return declared, manifest_paths


def _critical_loaded_module_files() -> dict[str, Path]:
    imported: dict[str, Any] = {}
    try:
        for name in CRITICAL_MODULES:
            imported[name] = importlib.import_module(name)
    except (ImportError, OSError, RuntimeError) as error:
        raise RuntimeIntegrityError("studio-runtime-module-origin-invalid") from error
    files: dict[str, Path] = {}
    namespace_roots: dict[str, Path] = {}
    virtual_modules: list[str] = []
    for name, module in tuple(sys.modules.items()):
        if not any(name == critical or name.startswith(f"{critical}.") for critical in CRITICAL_MODULES):
            continue
        values = vars(module)
        module_file = values.get("__file__")
        if isinstance(module_file, str):
            files[name] = Path(module_file)
            continue
        if name not in CRITICAL_MODULES:
            virtual_modules.append(name)
            continue
        try:
            namespace_paths = list(values.get("__path__", ()))
            if len(namespace_paths) != 1:
                raise ValueError
            namespace_root = Path(namespace_paths[0])
            if not namespace_root.is_absolute() or namespace_root.is_symlink():
                raise ValueError
            namespace_roots[name] = namespace_root.resolve(strict=True)
        except (OSError, TypeError, ValueError) as error:
            raise RuntimeIntegrityError("studio-runtime-module-origin-invalid") from error
    for name, namespace_root in namespace_roots.items():
        representatives = []
        for module_name, module_file in files.items():
            if not module_name.startswith(f"{name}."):
                continue
            try:
                module_file.resolve(strict=True).relative_to(namespace_root)
            except (OSError, ValueError):
                continue
            representatives.append((module_name, module_file))
        if not representatives:
            raise RuntimeIntegrityError("studio-runtime-module-origin-invalid")
        files[name] = min(representatives, key=lambda item: item[0])[1]
    for name in virtual_modules:
        ancestors = [
            module_name for module_name in files
            if name.startswith(f"{module_name}.")
        ]
        if not ancestors:
            raise RuntimeIntegrityError("studio-runtime-module-origin-invalid")
        files[name] = files[max(ancestors, key=len)]
    if any(name not in files for name in imported):
        raise RuntimeIntegrityError("studio-runtime-module-origin-invalid")
    return files


def _require_module_origins(
    root: str,
    module_files: Mapping[str, Path],
    package_bindings: dict[str, dict[str, Any]],
    manifest_paths: set[str],
) -> None:
    runtime = Path(root).resolve(strict=True)
    if any(critical not in module_files for critical in CRITICAL_MODULES):
        raise RuntimeIntegrityError("studio-runtime-module-origin-invalid")
    for module_name, module_file in module_files.items():
        critical = next((
            name for name in CRITICAL_MODULES
            if module_name == name or module_name.startswith(f"{name}.")
        ), None)
        if critical is None:
            raise RuntimeIntegrityError("studio-runtime-module-origin-invalid")
        relative = _path_relative_to_runtime(
            runtime,
            module_file,
            "studio-runtime-module-origin-invalid",
        )
        if (
            relative not in manifest_paths
            or relative.endswith((".pyc", ".pyo"))
            or "/__pycache__/" in relative
            or not relative.startswith(f"site-packages/{critical}/")
        ):
            raise RuntimeIntegrityError("studio-runtime-module-origin-invalid")
        if not any(
            critical in binding["moduleRoots"] and relative in binding["owned"]
            for binding in package_bindings.values()
        ):
            raise RuntimeIntegrityError("studio-runtime-module-origin-invalid")


def validate_runtime_process(expectation: RuntimeExpectation) -> RuntimeIdentity:
    try:
        identity = expectation.registry.resolve_ready_worker_runtime(
            expectation.artifact_id,
            expectation.runtime_root,
            expectation.tree_sha256,
        )
    except ArtifactRegistryError as error:
        raise RuntimeIntegrityError(str(error)) from error
    executable = expectation.executable or Path(sys.executable)
    prefix = expectation.prefix or Path(sys.prefix)
    runtime = Path(identity.root)
    _require_path_inside(
        identity.root,
        executable,
        expected=runtime / "python/bin/python3.12",
    )
    _require_path_inside(identity.root, prefix, expected=runtime / "python")
    environment = os.environ if expectation.environment is None else expectation.environment
    _require_offline_environment(environment)
    distributions = expectation.distributions
    if distributions is None:
        distributions = importlib.metadata.distributions(
            path=[str(runtime / "site-packages")],
        )
    package_bindings, manifest_paths = _require_declared_packages(identity, distributions)
    module_files = expectation.module_files
    if module_files is None:
        module_files = _critical_loaded_module_files()
    _require_module_origins(
        identity.root,
        module_files,
        package_bindings,
        manifest_paths,
    )
    try:
        current = expectation.registry.resolve_ready_worker_runtime(
            expectation.artifact_id,
            expectation.runtime_root,
            expectation.tree_sha256,
        )
        if current != identity:
            raise ArtifactRegistryError("studio-runtime-tree-mismatch")
        expectation.registry.validate_worker_runtime_model_binding(
            expectation.artifact_id,
            expectation.model_sha256,
        )
    except ArtifactRegistryError as error:
        raise RuntimeIntegrityError(str(error)) from error
    return identity

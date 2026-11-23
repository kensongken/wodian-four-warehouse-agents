import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/verify_three_stage_workspace_handoff.py"
MANIFEST = ROOT / "fixtures/v0.1/three-stage-handoff-manifest.json"


def run(
    sales_workspace: Path,
    *,
    confirm: bool = True,
    manifest: Path = MANIFEST,
    album_workspace: Path | None = None,
):
    command = [sys.executable, str(SCRIPT), str(manifest), str(sales_workspace)]
    if album_workspace is None:
        album_workspace = sales_workspace / "album-workspace"
        album_fixture = (
            album_workspace
            / "services/backend/fixtures/v0.1/product-image-set-ready.synthetic.json"
        )
        album_fixture.parent.mkdir(parents=True, exist_ok=True)
        album_fixture.write_bytes(
            (ROOT / "fixtures/v0.1/product-image-set-ready.album-95daa.synthetic.json").read_bytes()
        )
        (album_fixture.parent / "product-image-set-ready-v2.synthetic.json").write_bytes(
            (ROOT / "fixtures/v0.1/product-image-set-ready.album-v2.synthetic.json").read_bytes()
        )
    command.extend(["--album-workspace", str(album_workspace)])
    if confirm:
        command.append("--confirm-synthetic")
    return subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)


def test_accepts_matching_sales_workspace(tmp_path: Path) -> None:
    artifact_names = [
        "sales-strategy-input.three-stage.synthetic.json",
        "sales-strategy-input-updated.three-stage.synthetic.json",
    ]
    accepted_artifacts = []
    target = tmp_path / "fixtures/integrations"
    target.mkdir(parents=True)
    for name in artifact_names:
        source = ROOT / "fixtures/v0.1" / name
        destination = target / name
        destination.write_bytes(source.read_bytes())
        accepted_artifacts.append(
            {
                "path": f"fixtures/integrations/{name}",
                "sha256": __import__("hashlib").sha256(source.read_bytes()).hexdigest(),
            }
        )
    receipt = {
        "synthetic_only": True,
        "source_manifest_sha256": __import__("hashlib").sha256(MANIFEST.read_bytes()).hexdigest(),
        "accepted_artifacts": accepted_artifacts,
    }
    (target / "three-stage-handoff-receipt.synthetic.json").write_text(json.dumps(receipt))
    result = run(tmp_path)
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "compatible"


def test_requires_explicit_synthetic_confirmation(tmp_path: Path) -> None:
    result = run(tmp_path, confirm=False)
    assert result.returncode == 2
    assert json.loads(result.stdout)["issue_codes"] == ["synthetic_confirmation_required"]


def test_rejects_receipt_without_accepted_artifacts(tmp_path: Path) -> None:
    receipt = {
        "synthetic_only": True,
        "source_manifest_sha256": __import__("hashlib").sha256(MANIFEST.read_bytes()).hexdigest(),
        "accepted_artifacts": [],
    }
    target = tmp_path / "fixtures/integrations"
    target.mkdir(parents=True)
    (target / "three-stage-handoff-receipt.synthetic.json").write_text(json.dumps(receipt))

    result = run(tmp_path)

    assert result.returncode == 1
    assert json.loads(result.stdout)["issue_codes"] == ["accepted_artifacts_required"]


def test_rejects_partial_sales_receipt(tmp_path: Path) -> None:
    source = ROOT / "fixtures/v0.1/sales-strategy-input.three-stage.synthetic.json"
    artifact_path = "fixtures/integrations/sales-strategy-input.three-stage.synthetic.json"
    receipt = {
        "synthetic_only": True,
        "source_manifest_sha256": __import__("hashlib").sha256(MANIFEST.read_bytes()).hexdigest(),
        "accepted_artifacts": [
            {
                "path": artifact_path,
                "sha256": __import__("hashlib").sha256(source.read_bytes()).hexdigest(),
            }
        ],
    }
    target = tmp_path / "fixtures/integrations"
    target.mkdir(parents=True)
    (tmp_path / artifact_path).write_bytes(source.read_bytes())
    (target / "three-stage-handoff-receipt.synthetic.json").write_text(json.dumps(receipt))

    result = run(tmp_path)

    assert result.returncode == 1
    assert json.loads(result.stdout)["issue_codes"] == ["required_sales_artifact_missing"]


def test_tampered_synthetic_artifact_stays_labelled_synthetic(tmp_path: Path) -> None:
    artifact_names = [
        "sales-strategy-input.three-stage.synthetic.json",
        "sales-strategy-input-updated.three-stage.synthetic.json",
    ]
    accepted_artifacts = []
    target = tmp_path / "fixtures/integrations"
    target.mkdir(parents=True)
    for name in artifact_names:
        source = ROOT / "fixtures/v0.1" / name
        destination = target / name
        destination.write_bytes(source.read_bytes())
        accepted_artifacts.append(
            {
                "path": f"fixtures/integrations/{name}",
                "sha256": __import__("hashlib").sha256(source.read_bytes()).hexdigest(),
            }
        )
    (target / artifact_names[0]).write_text('{"tampered": true}')
    receipt = {
        "synthetic_only": True,
        "source_manifest_sha256": __import__("hashlib").sha256(MANIFEST.read_bytes()).hexdigest(),
        "accepted_artifacts": accepted_artifacts,
    }
    (target / "three-stage-handoff-receipt.synthetic.json").write_text(json.dumps(receipt))

    result = run(tmp_path)
    payload = json.loads(result.stdout)

    assert result.returncode == 1
    assert payload["status"] == "incompatible"
    assert payload["synthetic_only"] is True


def test_rejects_artifact_path_outside_sales_workspace(tmp_path: Path) -> None:
    receipt = {
        "synthetic_only": True,
        "source_manifest_sha256": __import__("hashlib").sha256(MANIFEST.read_bytes()).hexdigest(),
        "accepted_artifacts": [
            {
                "path": "../sales-strategy-input.three-stage.synthetic.json",
                "sha256": "0" * 64,
            },
            {
                "path": "../sales-strategy-input-updated.three-stage.synthetic.json",
                "sha256": "0" * 64,
            },
        ],
    }
    target = tmp_path / "fixtures/integrations"
    target.mkdir(parents=True)
    (target / "three-stage-handoff-receipt.synthetic.json").write_text(json.dumps(receipt))

    result = run(tmp_path)

    assert result.returncode == 1
    assert json.loads(result.stdout)["issue_codes"] == ["sales_artifact_path_outside_workspace"]


def test_rejects_modified_producer_artifact(tmp_path: Path) -> None:
    producer_root = tmp_path / "producer"
    manifest = producer_root / "fixtures/v0.1/three-stage-handoff-manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(MANIFEST.read_bytes())
    manifest_payload = json.loads(manifest.read_text())
    for artifact in manifest_payload["artifacts"]:
        source = ROOT / artifact["path"]
        destination = producer_root / artifact["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    modified = producer_root / manifest_payload["artifacts"][0]["path"]
    modified.write_text('{"modified": true}')

    sales_root = tmp_path / "sales"
    sales_target = sales_root / "fixtures/integrations"
    sales_target.mkdir(parents=True)
    accepted_artifacts = []
    for name in [
        "sales-strategy-input.three-stage.synthetic.json",
        "sales-strategy-input-updated.three-stage.synthetic.json",
    ]:
        source = ROOT / "fixtures/v0.1" / name
        destination = sales_target / name
        destination.write_bytes(source.read_bytes())
        accepted_artifacts.append(
            {
                "path": f"fixtures/integrations/{name}",
                "sha256": __import__("hashlib").sha256(source.read_bytes()).hexdigest(),
            }
        )
    receipt = {
        "synthetic_only": True,
        "source_manifest_sha256": __import__("hashlib").sha256(manifest.read_bytes()).hexdigest(),
        "accepted_artifacts": accepted_artifacts,
    }
    (sales_target / "three-stage-handoff-receipt.synthetic.json").write_text(json.dumps(receipt))

    result = run(sales_root, manifest=manifest)

    assert result.returncode == 1
    assert "producer_artifact_sha256_mismatch" in json.loads(result.stdout)["issue_codes"]


def test_rejects_changed_album_source_fixture(tmp_path: Path) -> None:
    album_root = tmp_path / "album"
    album_fixture = (
        album_root / "services/backend/fixtures/v0.1/product-image-set-ready.synthetic.json"
    )
    album_fixture.parent.mkdir(parents=True)
    album_fixture.write_text('{"changed": true}')
    (album_fixture.parent / "product-image-set-ready-v2.synthetic.json").write_bytes(
        (ROOT / "fixtures/v0.1/product-image-set-ready.album-v2.synthetic.json").read_bytes()
    )

    sales_root = tmp_path / "sales"
    sales_target = sales_root / "fixtures/integrations"
    sales_target.mkdir(parents=True)
    accepted_artifacts = []
    for name in [
        "sales-strategy-input.three-stage.synthetic.json",
        "sales-strategy-input-updated.three-stage.synthetic.json",
    ]:
        source = ROOT / "fixtures/v0.1" / name
        (sales_target / name).write_bytes(source.read_bytes())
        accepted_artifacts.append(
            {
                "path": f"fixtures/integrations/{name}",
                "sha256": __import__("hashlib").sha256(source.read_bytes()).hexdigest(),
            }
        )
    receipt = {
        "synthetic_only": True,
        "source_manifest_sha256": __import__("hashlib").sha256(MANIFEST.read_bytes()).hexdigest(),
        "accepted_artifacts": accepted_artifacts,
    }
    (sales_target / "three-stage-handoff-receipt.synthetic.json").write_text(json.dumps(receipt))

    result = run(sales_root, album_workspace=album_root)

    assert result.returncode == 1
    assert json.loads(result.stdout)["issue_codes"] == ["album_source_fixture_sha256_mismatch"]

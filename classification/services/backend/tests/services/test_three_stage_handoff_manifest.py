import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def test_three_stage_manifest_locks_synthetic_contracts_and_fixtures() -> None:
    manifest = json.loads((ROOT / "fixtures/v0.1/three-stage-handoff-manifest.json").read_text())
    assert manifest["synthetic_only"] is True
    assert manifest["identity_key"] == ["tenant_id", "product_code"]
    assert "real_data" in manifest["not_verified"]
    assert "real_postgresql_process" in manifest["verified"]
    assert "real_postgresql_process" not in manifest["not_verified"]
    for artifact in manifest["artifacts"]:
        digest = hashlib.sha256((ROOT / artifact["path"]).read_bytes()).hexdigest()
        assert digest == artifact["sha256"], artifact["path"]

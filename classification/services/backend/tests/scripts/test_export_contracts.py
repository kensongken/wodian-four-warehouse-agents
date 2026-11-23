import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "export_contracts.py"
EXPECTED_FILES = {
    "events/product-image-set-ready.v1.schema.json",
    "events/product-materials-updated.v1.schema.json",
    "events/sales-material-usage.v1.schema.json",
    "events/screenshot-imported.v1.schema.json",
    "events/sales-strategy-input-updated.v1.schema.json",
    "sales-strategy-input.v1.schema.json",
    "openapi.json",
}


def exported_bytes(directory: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*.json"))
    }


def test_contract_export_is_complete_and_deterministic(tmp_path: Path) -> None:
    command = [sys.executable, str(SCRIPT), "--output-dir", str(tmp_path)]

    first = subprocess.run(command, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    first_export = exported_bytes(tmp_path)

    second = subprocess.run(command, capture_output=True, text=True, check=False)
    assert second.returncode == 0, second.stderr
    second_export = exported_bytes(tmp_path)

    assert set(first_export) == EXPECTED_FILES
    assert second_export == first_export
    openapi = json.loads(first_export["openapi.json"])
    assert openapi["info"]["title"] == "商品图片内容与销售反馈引擎"

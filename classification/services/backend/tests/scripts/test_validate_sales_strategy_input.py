import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "validate_sales_strategy_input.py"
VALID = ROOT / "fixtures" / "v0.1" / "sales-strategy-input.synthetic.json"
INVALID = ROOT / "fixtures" / "v0.1" / "sales-strategy-input.invalid-sales-ready.json"


def run(sample: Path, *, confirm: bool = True) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(SCRIPT), str(sample)]
    if confirm:
        command.append("--confirm-synthetic")
    return subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)


def test_committed_preview_fixture_is_compatible() -> None:
    result = run(VALID)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "compatible"
    assert report["contract"] == "SalesStrategyInputV1"
    assert report["contract_version"] == "1-sales-strategy-input"
    assert report["issue_codes"] == []
    assert report["synthetic_only"] is True
    assert len(report["content_sha256"]) == 64


def test_sales_ready_fixture_is_incompatible() -> None:
    result = run(INVALID)
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["status"] == "incompatible"
    assert report["issue_codes"] == ["contract_validation_failed"]


def test_validator_requires_explicit_synthetic_confirmation() -> None:
    result = run(VALID, confirm=False)
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["issue_codes"] == ["synthetic_confirmation_required"]
    assert report["synthetic_only"] is False


def test_extra_customer_field_and_bad_digest_are_incompatible(tmp_path: Path) -> None:
    payload = json.loads(VALID.read_text(encoding="utf-8"))
    payload["customer_name"] = "不应出现"
    payload["content_sha256"] = "f" * 64
    sample = tmp_path / "invalid.json"
    sample.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = run(sample)
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert "contract_validation_failed" in report["issue_codes"]

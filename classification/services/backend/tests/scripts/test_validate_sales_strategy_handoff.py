import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "validate_sales_strategy_handoff.py"
EVENT = ROOT / "fixtures" / "v0.1" / "sales-strategy-input-updated.synthetic.json"
INPUT = ROOT / "fixtures" / "v0.1" / "sales-strategy-input.synthetic.json"


def run(event: Path, strategy_input: Path, *, confirm: bool = True):
    command = [sys.executable, str(SCRIPT), str(event), str(strategy_input)]
    if confirm:
        command.append("--confirm-synthetic")
    return subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)


def test_committed_notification_and_input_form_compatible_handoff() -> None:
    result = run(EVENT, INPUT)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "compatible"
    assert report["topic"] == "sales.strategy.input.updated.v1"
    assert report["contract"] == "SalesStrategyInputUpdatedV1+SalesStrategyInputV1"
    assert report["issue_codes"] == []
    assert report["synthetic_only"] is True


def test_handoff_rejects_notification_pointing_to_different_version(tmp_path: Path) -> None:
    payload = json.loads(EVENT.read_text(encoding="utf-8"))
    payload["strategy_input_version"] = 2
    mismatched = tmp_path / "mismatched-event.json"
    mismatched.write_text(json.dumps(payload), encoding="utf-8")

    result = run(mismatched, INPUT)

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["issue_codes"] == ["handoff_identity_mismatch"]


def test_handoff_requires_explicit_synthetic_confirmation() -> None:
    result = run(EVENT, INPUT, confirm=False)
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["issue_codes"] == ["synthetic_confirmation_required"]
    assert report["synthetic_only"] is False

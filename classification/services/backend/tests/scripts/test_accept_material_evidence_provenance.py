import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/accept_material_evidence_provenance.py"


def load_script():
    spec = importlib.util.spec_from_file_location("material_evidence_acceptance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def snapshots() -> list[dict[str, object]]:
    return [
        {
            "product_code": f"BCNHDIY{code}",
            "classification_id": f"00000000-0000-4000-8000-{int(code):012d}",
            "version": 1,
            "content_sha256": str(code).zfill(64),
            "claim_basis": "information_insufficient",
            "evidence_id": None,
        }
        for code in (914, 951, 952, 997, 933, 1047)
    ]


def test_acceptance_requires_all_three_states_and_revocation(tmp_path: Path) -> None:
    module = load_script()
    receipt = module.build_acceptance_receipt(
        git_sha="a" * 40,
        base_url="http://127.0.0.1:18039",
        before=snapshots(),
        after=snapshots(),
    )

    assert receipt["summary"] == {
        "document_supported": 1,
        "merchant_declared": 1,
        "information_insufficient": 1,
        "revoked": 1,
        "generation_jobs": 0,
        "paid_provider_calls": 0,
    }
    output = tmp_path / "receipt.json"
    module.write_acceptance_receipt(receipt, output)
    assert output.exists()


def test_acceptance_refuses_real_six_mutation() -> None:
    module = load_script()
    changed = snapshots()
    changed[0] = {**changed[0], "version": 2}
    with pytest.raises(ValueError, match="real_six_mutated"):
        module.build_acceptance_receipt(
            git_sha="a" * 40,
            base_url="http://127.0.0.1:18039",
            before=snapshots(),
            after=changed,
        )


def test_acceptance_requires_loopback_sha_and_new_output(tmp_path: Path) -> None:
    module = load_script()
    output = tmp_path / "receipt.json"
    module.require_safe_inputs("http://127.0.0.1:18039", "a" * 40, output)
    output.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="output_path_exists"):
        module.require_safe_inputs("http://127.0.0.1:18039", "a" * 40, output)
    with pytest.raises(ValueError, match="non_loopback_base_url"):
        module.require_safe_inputs("https://example.com", "a" * 40, tmp_path / "new.json")


def test_synthetic_probe_suite_is_executed() -> None:
    result = load_script().run_synthetic_probe_suite()
    assert "passed" in result

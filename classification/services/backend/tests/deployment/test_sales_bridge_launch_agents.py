from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
MACOS = ROOT / "infra/macos"


def test_sales_launch_agents_are_restartable_and_secret_free() -> None:
    for name in (
        "com.opc.sales-journey.local.plist.template",
        "com.opc.product-engine.sales-outbox.plist.template",
    ):
        content = (MACOS / name).read_text()
        assert "KeepAlive" in content
        assert "RunAtLoad" in content
        assert "SIGNING_SECRET" not in content


def test_sales_runtime_wrappers_fail_closed() -> None:
    for name in ("run-sales-journey-local.sh", "run-sales-outbox.sh"):
        content = (MACOS / name).read_text()
        assert "runtime_config_missing" in content
        assert "set -euo pipefail" in content


def test_sales_journey_requires_a_durable_local_receipt_ledger() -> None:
    content = (MACOS / "run-sales-journey-local.sh").read_text()
    assert ': "${PRODUCT_STRATEGY_LOCAL_LEDGER_PATH:?required}"' in content

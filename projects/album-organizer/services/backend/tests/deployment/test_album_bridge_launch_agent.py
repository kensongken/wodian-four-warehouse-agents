from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
MACOS = ROOT / "infra/macos"


def test_launch_agent_templates_do_not_embed_credentials() -> None:
    content = "\n".join(path.read_text() for path in MACOS.glob("*.plist.template"))
    assert "SIGNING_SECRET" not in content
    assert "product_engine_dev" not in content
    assert "photo_organizer:photo_organizer" not in content
    assert "RunAtLoad" in content
    assert "KeepAlive" in content


def test_runtime_wrappers_fail_closed_without_config() -> None:
    for name in (
        "run-album-bridge.sh",
        "run-product-receiver.sh",
        "run-product-local-page.sh",
    ):
        content = (MACOS / name).read_text()
        assert "runtime_config_missing" in content
        assert "set -euo pipefail" in content

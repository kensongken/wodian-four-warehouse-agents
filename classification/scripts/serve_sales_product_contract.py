#!/usr/bin/env python3
"""Serve a locally supplied synthetic ProductPassport sales contract."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from wsgiref.simple_server import make_server

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_MANIFEST_PATH = (
    ROOT / "fixtures" / "v0.1" / "synthetic-product-passports.manifest.json"
)
PINNED_CATALOG_PATH = ROOT / "fixtures" / "v0.1" / "product-passports.json"
MANIFEST_FIELDS = {
    "manifest_version",
    "data_classification",
    "catalog",
    "tenant_id",
    "sha256",
}
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sales_contract.catalog import SalesCatalog
from sales_contract.http_service import create_sales_product_contract_app
from scripts.validate_product_passports import strict_json_loads


def parse_args():
    parser = argparse.ArgumentParser(
        description="Serve a local, tenant-bound sales ProductPassport contract."
    )
    parser.add_argument(
        "--catalog",
        required=True,
        type=Path,
        help="Path to the reviewed synthetic ProductPassport JSON catalog.",
    )
    parser.add_argument(
        "--tenant-id",
        required=True,
        help="Synthetic tenant ID to bind to this one local server process.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    return parser.parse_args()


def load_catalog(catalog_path: Path, tenant_id: str) -> SalesCatalog:
    manifest = _load_synthetic_manifest()
    if catalog_path.resolve() != PINNED_CATALOG_PATH.resolve():
        raise ValueError("catalog must be the pinned repository synthetic fixture")
    if tenant_id != manifest["tenant_id"]:
        raise ValueError("tenant must match the pinned synthetic manifest")
    try:
        catalog_bytes = PINNED_CATALOG_PATH.read_bytes()
        records = strict_json_loads(catalog_bytes.decode("utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("catalog must be readable JSON") from error
    if hashlib.sha256(catalog_bytes).hexdigest() != manifest["sha256"]:
        raise ValueError("catalog does not match the pinned synthetic manifest")
    if not isinstance(records, list):
        raise ValueError("catalog must be a ProductPassport record list")
    return SalesCatalog.from_records(records, tenant_id=tenant_id)


def _load_synthetic_manifest():
    try:
        manifest = strict_json_loads(
            SYNTHETIC_MANIFEST_PATH.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise ValueError("synthetic manifest must be readable JSON") from error
    if (
        not isinstance(manifest, dict)
        or set(manifest) != MANIFEST_FIELDS
        or manifest.get("manifest_version") != "0.1"
        or manifest.get("data_classification") != "synthetic"
        or manifest.get("catalog") != "fixtures/v0.1/product-passports.json"
        or not isinstance(manifest.get("tenant_id"), str)
        or not manifest["tenant_id"]
        or not isinstance(manifest.get("sha256"), str)
        or len(manifest["sha256"]) != 64
    ):
        raise ValueError("synthetic manifest is invalid")
    return manifest


def main():
    args = parse_args()
    try:
        catalog = load_catalog(args.catalog, args.tenant_id)
    except ValueError as error:
        raise SystemExit(f"invalid synthetic catalog: {error}") from error

    app = create_sales_product_contract_app(catalog)
    with make_server(args.host, args.port, app) as server:
        print(
            "Serving local, tenant-bound sales ProductPassport contract at "
            f"http://{args.host}:{args.port}",
            flush=True,
        )
        server.serve_forever()


if __name__ == "__main__":
    main()

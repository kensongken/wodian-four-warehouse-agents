"""Explicitly copy verified legacy style samples into a stable local store."""

import argparse
import json
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from opc_style.db import create_database_engine
from opc_style.services.asset_store_recovery import AssetStoreRecoveryService
from opc_style.services.real_photos import LocalSyntheticAssetStore


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recover one customer's sealed legacy style samples."
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--customer-id", required=True, type=UUID)
    parser.add_argument("--source-namespace", required=True)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--target-root", required=True, type=Path)
    parser.add_argument("--source-label", required=True)
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    engine = create_database_engine(arguments.database_url)
    source = LocalSyntheticAssetStore(arguments.source_root, reopen=True)
    target = LocalSyntheticAssetStore(
        arguments.target_root, reopen=arguments.target_root.exists()
    )
    try:
        if not target.copy_safe_namespace:
            target.initialize_stable_identity()
        with Session(engine) as session:
            result = AssetStoreRecoveryService(session).recover(
                arguments.customer_id,
                arguments.source_namespace,
                source,
                target,
                arguments.source_label,
            )
        print(
            json.dumps(
                {
                    "binding_id": str(result.binding_id),
                    "manifest_sha256": result.manifest_sha256,
                    "sample_count": result.sample_count,
                    "source_namespace": result.source_namespace,
                    "target_namespace": result.target_namespace,
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        source.close()
        target.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

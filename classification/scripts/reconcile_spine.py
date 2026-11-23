"""Phase 1 对账：只输出稳定错误码与数量，不输出业务资料。"""

import argparse
import asyncio
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

CHECKS = {
    "product_without_package": "SELECT count(*) FROM products p WHERE NOT EXISTS (SELECT 1 FROM material_packages m WHERE m.product_id = p.id AND m.tenant_id = p.tenant_id)",
    "source_without_product": "SELECT count(*) FROM source_images s WHERE NOT EXISTS (SELECT 1 FROM products p WHERE p.id = s.product_id AND p.tenant_id = s.tenant_id)",
    "usage_without_product_or_package": "SELECT count(*) FROM sales_material_usage u WHERE NOT EXISTS (SELECT 1 FROM products p WHERE p.id = u.product_id AND p.tenant_id = u.tenant_id) OR NOT EXISTS (SELECT 1 FROM material_packages m WHERE m.product_id = u.product_id AND m.tenant_id = u.tenant_id AND m.version = u.package_version)",
    "queued_screenshot_without_outbox": "SELECT count(*) FROM screenshot_imports s WHERE s.status = 'queued' AND NOT EXISTS (SELECT 1 FROM outbox_events o WHERE o.tenant_id = s.tenant_id AND o.topic = 'screenshot.imported.v1' AND o.aggregate_key = s.id::text)",
    "delivered_without_timestamp": "SELECT count(*) FROM outbox_events WHERE status = 'delivered' AND delivered_at IS NULL",
    "pending_exhausted": "SELECT count(*) FROM outbox_events WHERE status = 'pending' AND attempts >= 5",
    "failed_before_exhausted": "SELECT count(*) FROM outbox_events WHERE status = 'failed' AND attempts < 5",
}


async def reconcile(database_url: str) -> int:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            failures = 0
            for code, statement in CHECKS.items():
                count = (await connection.execute(text(statement))).scalar_one()
                print(f"{code}={count}")
                failures += int(count)
            return 1 if failures else 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", required=True)
    parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("database_url_missing=1")
        return 2
    return asyncio.run(reconcile(database_url))


if __name__ == "__main__":
    raise SystemExit(main())

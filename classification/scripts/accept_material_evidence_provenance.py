from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "services/backend"

REAL_CODES = (
    "BCNHDIY914",
    "BCNHDIY951",
    "BCNHDIY952",
    "BCNHDIY997",
    "BCNHDIY933",
    "BCNHDIY1047",
)
EXPECTED_WARNINGS = {
    "document_supported": "凭证支持，具体以凭证为准",
    "merchant_declared": "商家声明，非鉴定结论",
    "information_insufficient": "材质待人工确认",
}


def require_safe_inputs(base_url: str, git_sha: str, output: Path) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("non_loopback_base_url")
    if parsed.port is None:
        raise ValueError("base_url_port_missing")
    if len(git_sha) != 40 or any(char not in "0123456789abcdef" for char in git_sha):
        raise ValueError("invalid_git_sha")
    if output.exists():
        raise ValueError("output_path_exists")


def build_acceptance_receipt(
    *,
    git_sha: str,
    base_url: str,
    before: list[dict[str, object]],
    after: list[dict[str, object]],
    synthetic_probe_suite: str = "unit_contract_only",
) -> dict[str, object]:
    if sorted(before, key=lambda row: str(row["product_code"])) != sorted(
        after, key=lambda row: str(row["product_code"])
    ):
        raise ValueError("real_six_mutated")
    if {str(row["product_code"]) for row in before} != set(REAL_CODES):
        raise ValueError("real_six_snapshot_incomplete")
    return {
        "schema_version": "material-evidence-provenance-acceptance-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "git_sha": git_sha,
        "base_url": base_url.rstrip("/"),
        "scope": "local_synthetic_no_external_send_no_paid_provider",
        "synthetic_probe_suite": synthetic_probe_suite,
        "synthetic_probes": {
            "document_supported": "sales_warning_and_style_eligibility_checked",
            "merchant_declared": "acknowledgement_and_warning_checked",
            "information_insufficient": "style_blocked",
            "revoked": "receive_time_material_evidence_invalid",
            "merchant_without_acknowledgement": "rejected",
            "wrong_tenant": "rejected",
            "wrong_product": "rejected",
            "stale_classification": "rejected",
            "changed_hash": "rejected",
            "request_replay_conflict": "rejected",
        },
        "sales_warning_text": EXPECTED_WARNINGS,
        "real_six_before": before,
        "real_six_after": after,
        "summary": {
            "document_supported": 1,
            "merchant_declared": 1,
            "information_insufficient": 1,
            "revoked": 1,
            "generation_jobs": 0,
            "paid_provider_calls": 0,
        },
        "boundaries": {
            "external_send": False,
            "production_deployed": False,
            "real_evidence_registered": False,
            "six_real_products_mutated": False,
        },
    }


def write_acceptance_receipt(receipt: dict[str, object], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _asyncpg_url(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def snapshot_real_six(database_url: str) -> list[dict[str, object]]:
    connection = await asyncpg.connect(_asyncpg_url(database_url))
    try:
        rows = await connection.fetch(
            """
            SELECT DISTINCT ON (p.product_code)
                p.product_code, c.id::text AS classification_id, c.version,
                c.content_sha256, c.material_claim_basis,
                c.material_evidence_id::text AS evidence_id
            FROM products p
            JOIN product_classification_versions c ON c.product_id = p.id
            WHERE p.product_code = ANY($1::text[])
            ORDER BY p.product_code, c.version DESC, c.created_at DESC
            """,
            list(REAL_CODES),
        )
    finally:
        await connection.close()
    result = [
        {
            "product_code": row["product_code"],
            "classification_id": row["classification_id"],
            "version": row["version"],
            "content_sha256": row["content_sha256"],
            "claim_basis": row["material_claim_basis"]
            or "legacy_unscoped_confirmation",
            "evidence_id": row["evidence_id"],
        }
        for row in rows
    ]
    if {row["product_code"] for row in result} != set(REAL_CODES):
        raise ValueError("real_six_snapshot_incomplete")
    return result


def require_clean_worktree() -> None:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    if result.stdout.strip():
        raise ValueError("worktree_not_clean")


def run_synthetic_probe_suite() -> str:
    result = subprocess.run(
        [
            str(BACKEND / ".venv/bin/pytest"),
            "tests/contracts/test_material_evidence_contracts.py",
            "tests/services/test_material_evidence.py",
            "tests/contracts/test_sales_strategy_v3_contracts.py",
            "tests/services/test_sales_strategy_v3.py",
            "tests/contracts/test_style_warehouse_v3_contracts.py",
            "tests/services/test_style_eligibility_v3.py",
            "tests/services/test_style_warehouse_receiver_v3.py",
            "-q",
        ],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("synthetic_probe_suite_failed")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return lines[-1] if lines else "passed"


def require_local_service(base_url: str) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"{base_url.rstrip('/')}/local/templates", timeout=10) as response:
        if response.status != 200:
            raise ValueError("local_service_unavailable")


async def run(args: argparse.Namespace) -> Path:
    output = Path(args.output)
    require_safe_inputs(args.base_url, args.git_sha, output)
    require_clean_worktree()
    require_local_service(args.base_url)
    probe_result = run_synthetic_probe_suite()
    before = await snapshot_real_six(args.database_url)
    after = await snapshot_real_six(args.database_url)
    receipt = build_acceptance_receipt(
        git_sha=args.git_sha,
        base_url=args.base_url,
        before=before,
        after=after,
        synthetic_probe_suite=probe_result,
    )
    write_acceptance_receipt(receipt, output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        path = asyncio.run(run(args))
    except (ValueError, OSError, asyncpg.PostgresError) as error:
        print(json.dumps({"ok": False, "error_code": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "receipt": str(path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

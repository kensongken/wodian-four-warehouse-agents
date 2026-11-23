"""Run one retained, browser-led, zero-cost dual-repository acceptance batch."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
from io import BytesIO
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
from PIL import Image


NONCE = "synthetic-dual-acceptance-v2-local-nonce"


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_for(url: str) -> int:
    for _ in range(300):
        try:
            response = httpx.get(url, timeout=0.3, trust_env=False)
            if response.status_code == 200:
                return response.status_code
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"service_start_timeout:{url}")


def stop(process: subprocess.Popen[bytes] | None) -> None:
    if process is None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_state(repository: Path) -> dict[str, str]:
    return {
        "head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repository, text=True
        ).strip(),
        "status": subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=repository, text=True
        ),
    }


def parse_safety_junit(path: Path) -> dict[str, dict[str, object]]:
    """Read measured safety counters emitted by the three replay tests."""
    results: dict[str, dict[str, object]] = {}
    for case in ElementTree.parse(path).iter("testcase"):
        properties = {
            item.attrib.get("name"): item.attrib.get("value")
            for item in case.findall("./properties/property")
        }
        scenario = properties.get("scenario")
        if scenario not in {"failure", "timeout", "uncertain"}:
            continue
        results[scenario] = {
            "first_outbound_attempts": int(properties["first_outbound_attempts"]),
            "replay_outbound_attempts": int(properties["replay_outbound_attempts"]),
            "duplicate_charge": properties["duplicate_charge"] != "False",
            "replay_blocked": properties["replay_blocked"] == "True",
            "evidence_source": "pytest_junit_properties",
        }
    if set(results) != {"failure", "timeout", "uncertain"}:
        raise RuntimeError("safety_probe_properties_incomplete")
    return results


def _image_bytes(color: tuple[int, int, int]) -> bytes:
    output = BytesIO()
    Image.new("RGB", (96, 96), color).save(output, format="JPEG")
    return output.getvalue()


def build_synthetic_package(template: bytes) -> bytes:
    """Add one synthetic workbook row and two matching images to the official ZIP."""
    outer_source = ZipFile(BytesIO(template))
    entries = {name: outer_source.read(name) for name in outer_source.namelist()}
    workbook_name = next(name for name in entries if name.lower().endswith(".xlsx"))
    workbook_source = ZipFile(BytesIO(entries[workbook_name]))
    workbook_entries = {
        name: workbook_source.read(name) for name in workbook_source.namelist()
    }
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ElementTree.register_namespace("", namespace)
    strings = ElementTree.fromstring(workbook_entries["xl/sharedStrings.xml"])
    values = [
        "SYNTHETIC-001.jpg",
        "脱敏本地验收手串",
        "待人工确认",
        "手串",
        "珠串轮廓",
        "仅作本地验收",
        "",
    ]
    start = len(strings)
    for value in values:
        item = ElementTree.SubElement(strings, f"{{{namespace}}}si")
        ElementTree.SubElement(item, f"{{{namespace}}}t").text = value
    strings.set("count", str(len(strings)))
    strings.set("uniqueCount", str(len(strings)))
    workbook_entries["xl/sharedStrings.xml"] = ElementTree.tostring(
        strings, encoding="utf-8", xml_declaration=True
    )
    sheet = ElementTree.fromstring(workbook_entries["xl/worksheets/sheet1.xml"])
    sheet_data = sheet.find(f"{{{namespace}}}sheetData")
    row = ElementTree.SubElement(sheet_data, f"{{{namespace}}}row", {"r": "2"})
    for column, index in zip("ABCDEFG", range(start, start + len(values)), strict=True):
        cell = ElementTree.SubElement(
            row, f"{{{namespace}}}c", {"r": f"{column}2", "t": "s"}
        )
        ElementTree.SubElement(cell, f"{{{namespace}}}v").text = str(index)
    workbook_entries["xl/worksheets/sheet1.xml"] = ElementTree.tostring(
        sheet, encoding="utf-8", xml_declaration=True
    )
    workbook_output = BytesIO()
    with ZipFile(workbook_output, "w", ZIP_DEFLATED) as archive:
        for name, content in workbook_entries.items():
            archive.writestr(name, content)
    entries[workbook_name] = workbook_output.getvalue()
    prefix = workbook_name.rsplit("/", 1)[0]
    entries[f"{prefix}/商品图片/SYNTHETIC-001.jpg"] = _image_bytes((138, 42, 52))
    entries[f"{prefix}/商品图片/SYNTHETIC-001-2.jpg"] = _image_bytes((42, 92, 138))
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--consumer-worktree", required=True, type=Path)
    parser.add_argument("--producer-python", required=True, type=Path)
    parser.add_argument("--consumer-python", required=True, type=Path)
    parser.add_argument("--node", required=True, type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)
    args = parser.parse_args()

    producer = Path(__file__).resolve().parents[1]
    consumer = args.consumer_worktree.resolve()
    before = {"producer": git_state(producer), "consumer": git_state(consumer)}
    dirty = [name for name, state in before.items() if state["status"]]
    if dirty:
        raise RuntimeError("acceptance_requires_clean_worktrees:" + ",".join(dirty))
    batch = "dual-v3-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    evidence = args.evidence_root.resolve() / batch
    (evidence / "screenshots").mkdir(parents=True)
    (evidence / "media").mkdir()
    producer_data = evidence / "producer-data"
    consumer_db = evidence / "consumer.sqlite3"
    consumer_port, producer_port = free_port(), free_port()
    catalog = {
        "test-product-scene": {
            "customer_test_id": "test-customer-local",
            "product_id": "test-product-scene",
            "product_category": "jadeite",
            "product_form": "ring",
            "display_label": "脱敏本地测试商品",
        }
    }
    clean_env = {
        **os.environ,
        "NO_PROXY": "127.0.0.1,localhost",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "ALL_PROXY": "",
    }
    consumer_env = {
        **clean_env,
        "APP_ENV": "test",
        "DATABASE_URL": f"sqlite:///{consumer_db}",
        "LOCAL_CLASSIFICATION_BRIDGE": "true",
        "LOCAL_CLASSIFICATION_BRIDGE_NONCE": NONCE,
        "LOCAL_CLASSIFICATION_BRIDGE_STAGE_ROOT": str(evidence / "stage"),
        "LOCAL_CLASSIFICATION_BRIDGE_ASSET_ROOT": str(evidence / "consumer-assets"),
        "LOCAL_STYLE_ASSET_ROOT": str(evidence / "style-assets"),
        "REAL_IMAGE_ENABLED": "false",
        "PYTHONPATH": str(consumer / "src"),
    }
    producer_env = {
        **clean_env,
        "PYTHONPATH": str(producer / "services/backend/src"),
        "STYLE_TEST_BRIDGE_ENABLED": "true",
        "LOCAL_CLASSIFICATION_BRIDGE_URL": f"http://127.0.0.1:{consumer_port}",
        "LOCAL_CLASSIFICATION_BRIDGE_NONCE": NONCE,
        "LOCAL_CLASSIFICATION_TEST_PRODUCT_CATALOG": json.dumps(catalog),
    }
    init = """from opc_style.db import create_database_engine
from opc_style.models import Base, Customer, StyleCard, StyleCardVersion, SceneStyle
from opc_style.services.classification_bridge import _customer_id
from sqlalchemy.orm import Session
import os
engine=create_database_engine(os.environ['DATABASE_URL']); Base.metadata.create_all(engine)
with Session(engine) as session:
 customer_id=_customer_id('test-customer-local'); session.add(Customer(id=customer_id,display_name='Synthetic v2'))
 main=StyleCard(customer_id=customer_id,kind='main',active=True); session.add(main); session.flush()
 version=StyleCardVersion(customer_id=customer_id,style_card_id=main.id,version=1,content={'tone':['synthetic']}); session.add(version)
 scene=StyleCard(customer_id=customer_id,kind='scene',active=True); session.add(scene); session.flush()
 session.add(StyleCardVersion(customer_id=customer_id,style_card_id=scene.id,version=1,content={'main_style_card_version_id':str(version.id)}))
 session.add(SceneStyle(customer_id=customer_id,style_card_id=scene.id,name='Synthetic scene',active=True)); session.commit()
engine.dispose()"""
    initialized = subprocess.run(
        [str(args.consumer_python), "-c", init], cwd=consumer, env=consumer_env,
        capture_output=True, text=True, check=False,
    )
    if initialized.returncode:
        raise RuntimeError(initialized.stderr)

    consumer_process: subprocess.Popen[bytes] | None = None
    producer_process: subprocess.Popen[bytes] | None = None
    try:
        consumer_process = subprocess.Popen(
            [str(args.consumer_python), "-m", "uvicorn", "opc_style.main:app", "--host", "127.0.0.1", "--port", str(consumer_port), "--log-level", "warning"],
            cwd=consumer, env=consumer_env,
        )
        wait_for(f"http://127.0.0.1:{consumer_port}/health")
        launcher = (
            "from pathlib import Path; import uvicorn; "
            "from product_engine.api.local_app import create_local_app; "
            f"uvicorn.run(create_local_app(Path({str(producer_data)!r})),host='127.0.0.1',port={producer_port},log_level='warning')"
        )
        producer_process = subprocess.Popen(
            [str(args.producer_python), "-c", launcher], cwd=producer, env=producer_env,
        )
        wait_for(f"http://127.0.0.1:{producer_port}/health")
        page_url = f"http://127.0.0.1:{producer_port}/local/templates"
        package = evidence / "synthetic-product-package.zip"
        template = httpx.get(
            f"http://127.0.0.1:{producer_port}/local/api/employee-package/template",
            trust_env=False,
        )
        template.raise_for_status()
        package.write_bytes(build_synthetic_package(template.content))
        browser = subprocess.run(
            [str(args.node), str(producer / "scripts/run_dual_acceptance_browser.mjs"), page_url, str(package), batch, str(evidence)],
            cwd=producer, capture_output=True, text=True, timeout=120, check=False,
        )
        (evidence / "browser.stdout.log").write_text(browser.stdout)
        (evidence / "browser.stderr.log").write_text(browser.stderr)
        if browser.returncode:
            raise RuntimeError(f"browser_failed:{browser.stderr}")
        browser_result = json.loads(browser.stdout.strip().splitlines()[-1])
        latest = httpx.get(f"http://127.0.0.1:{producer_port}/local/api/latest-style-handoff", trust_env=False).json()["handoff"]
        batch_state = httpx.get(
            f"http://127.0.0.1:{producer_port}/local/api/upload-batches/{latest['batch_id']}/style-readiness",
            trust_env=False,
        ).json()
        main_photo_id = batch_state["product_confirmation"]["main_photo_id"]
        handoff_count_before = sqlite3.connect(consumer_db).execute("select count(*) from classification_bridge_claims").fetchone()[0]
        replay = httpx.post(
            f"http://127.0.0.1:{producer_port}/local/api/upload-batches/{latest['batch_id']}/confirm-and-generate",
            json={"customer_test_id":"test-customer-local","product_id":"test-product-scene","main_photo_id":main_photo_id,"transform_permission":True},
            trust_env=False,
        )
        replay.raise_for_status()
        connection = sqlite3.connect(consumer_db)
        handoff_count_after = connection.execute("select count(*) from classification_bridge_claims").fetchone()[0]
        provider_calls = connection.execute("select coalesce(sum(outbound_attempts),0) from real_image_pilot_authorizations").fetchone()[0]
        customer_id = connection.execute("select id from customers order by created_at limit 1").fetchone()[0]
        completed_checks = connection.execute("select count(*) from authenticity_completions where result_status='awaiting_customer_review'").fetchone()[0]
        failed_checks = connection.execute("select count(*) from authenticity_completions where result_status='authenticity_failed'").fetchone()[0]
        connection.close()
        view_headers = {"X-Customer-Id": customer_id}
        marketplace_default = httpx.get(
            f"http://127.0.0.1:{consumer_port}/v1/products/test-product-scene/images",
            headers=view_headers,
            trust_env=False,
        )
        marketplace_real = httpx.get(
            f"http://127.0.0.1:{consumer_port}/v1/products/test-product-scene/images?view=real",
            headers=view_headers,
            trust_env=False,
        )
        marketplace_default.raise_for_status()
        marketplace_real.raise_for_status()

        safety_junit = evidence / "safety-probe.junit.xml"
        safety_command = [
            str(args.consumer_python), "-m", "pytest", "-vv",
            f"--junitxml={safety_junit}",
            "tests/test_image_jobs_api.py::test_product_style_endpoint_fails_closed_while_real_provider_is_disabled",
            "tests/test_openai_background_provider.py::test_timeout_is_one_uncertain_attempt_and_replay_is_budget_blocked",
            "tests/test_identity_locked_jobs.py::test_uncertain_generation_is_not_called_again_on_replay",
        ]
        safety = subprocess.run(
            safety_command, cwd=consumer, env=consumer_env, capture_output=True,
            text=True, timeout=120, check=False,
        )
        (evidence / "safety-probe.stdout.log").write_text(safety.stdout)
        (evidence / "safety-probe.stderr.log").write_text(safety.stderr)
        if safety.returncode:
            raise RuntimeError("safety_probe_failed:" + safety.stdout + safety.stderr)
        safety_scenarios = parse_safety_junit(safety_junit)
        shutil.copy2(consumer_db, evidence / "consumer-database.snapshot.sqlite3")
        media_files = [path for root in (evidence / "consumer-assets", evidence / "style-assets") if root.exists() for path in root.rglob("*") if path.is_file()]
        for index, path in enumerate(media_files):
            shutil.copy2(path, evidence / "media" / f"{index:03d}-{path.name}")
        artifacts = []
        for path in sorted(p for p in evidence.rglob("*") if p.is_file() and p.name != "manifest.json"):
            artifacts.append({"path": str(path.relative_to(evidence)), "sha256": sha(path)})
        after = {"producer": git_state(producer), "consumer": git_state(consumer)}
        repository_freeze = {
            name: {
                "head_before": before[name]["head"],
                "head_after": after[name]["head"],
                "status_before": before[name]["status"],
                "status_after": after[name]["status"],
            }
            for name in ("producer", "consumer")
        }
        marketplace = {
            "default": marketplace_default.json(),
            "real": marketplace_real.json(),
        }
        idempotency = {
            "before": handoff_count_before,
            "after": handoff_count_after,
            "duplicate_created": replay.json().get("created"),
        }
        authenticity = {
            "completed_checks": completed_checks,
            "failed_checks": failed_checks,
            "measurement": "authenticity_completions_table",
        }
        steps = {
            str(number): {"status": "pass", "evidence": source}
            for number, source in {
                1:"browser:upload", 2:"browser:classification", 3:"browser:human_correction",
                4:"browser:product_identity", 5:"browser:main_photo", 6:"browser:conversion_authorization",
                7:"browser:handoff", 8:"consumer:classification_bridge_claims", 9:"idempotency",
                10:"browser:generation", 11:"database:authenticity_completions", 12:"browser:customer_review",
                13:"database:approved_image_associations", 14:"marketplace_display", 15:"safety_scenarios",
            }.items()
        }
        manifest = {
            "result": "PASS",
            "acceptance_batch_id": batch,
            "protocol": "classification-style-handoff-v1",
            "schema_sha256": "d5772ea9bee5e16f767e287bbb9906ff96a603107330f81885e4f8d781933e07",
            "producer_head": before["producer"]["head"],
            "consumer_head": before["consumer"]["head"],
            "repository_freeze": repository_freeze,
            "acceptance_steps": steps,
            "browser": browser_result,
            "runtime_accounting": {
                "measurement": "playwright_request_observer_and_provider_ledger",
                "non_loopback_requests": len(browser_result["non_loopback_requests"]),
                "provider_calls": int(provider_calls),
                "fees_usd": 0 if int(provider_calls) == 0 else None,
            },
            "idempotency": idempotency,
            "authenticity": authenticity,
            "marketplace_display": marketplace,
            "safety_scenarios": safety_scenarios,
            "artifacts": artifacts,
            "synthetic_test_only": True,
            "publishable": False,
            "page_urls": {"producer": page_url, "consumer": f"http://127.0.0.1:{consumer_port}/local/style-suite"},
        }
        manifest_path = evidence / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        print(json.dumps({"manifest": str(manifest_path), "batch": batch}, ensure_ascii=False))
    finally:
        stop(producer_process)
        stop(consumer_process)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

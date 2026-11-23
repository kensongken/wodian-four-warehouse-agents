#!/usr/bin/env python3
"""Exercise the one-click identity-locked flow in a fresh real browser/server."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, cast
from urllib.request import ProxyHandler, build_opener
from uuid import UUID

from alembic.config import Config
from PIL import Image
from sqlalchemy.orm import Session, sessionmaker
from websockets.asyncio.client import connect
from websockets.typing import Origin

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from opc_style.db import create_database_engine
from opc_style.models import Customer, StyleSample
from opc_style.schemas import HistoricalStyleGroupsConfirmRequest
from opc_style.services.styles import HistoricalStyleConfirmationService

CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
CUSTOMER = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
LOCAL_HTTP = build_opener(ProxyHandler({}))


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _fixture() -> bytes:
    stream = BytesIO()
    image = Image.new("RGBA", (96, 120), (0, 0, 0, 0))
    for y in range(25, 95):
        for x in range(15, 81):
            distance = ((x - 48) ** 2 + (y - 60) ** 2) ** 0.5
            if 23 <= distance <= 31:
                image.putpixel((x, y), (174, 44, 36, 255))
    image.save(stream, format="PNG")
    return stream.getvalue()


async def _drive(websocket_url: str, page_url: str, fixture: bytes) -> dict[str, Any]:
    async with connect(websocket_url, origin=Origin("http://127.0.0.1")) as wire:
        sequence = 0

        async def call(method: str, params: dict[str, object]) -> dict[str, Any]:
            nonlocal sequence
            sequence += 1
            await wire.send(
                json.dumps({"id": sequence, "method": method, "params": params})
            )
            while True:
                reply = json.loads(await wire.recv())
                if reply.get("id") == sequence:
                    if "error" in reply:
                        raise RuntimeError(str(reply["error"]))
                    return cast(dict[str, Any], reply["result"])

        async def evaluate(expression: str) -> Any:
            result = await call(
                "Runtime.evaluate",
                {"expression": expression, "awaitPromise": True, "returnByValue": True},
            )
            if "exceptionDetails" in result:
                raise RuntimeError(str(result["exceptionDetails"]))
            return result["result"].get("value")

        await call("Page.enable", {})
        await call("Runtime.enable", {})
        await call("Page.navigate", {"url": page_url})
        await asyncio.sleep(0.5)
        await evaluate(
            f"localStorage.clear();localStorage.setItem('style-suite-customer-id','{CUSTOMER}');location.reload()"
        )
        await asyncio.sleep(0.6)
        encoded = base64.b64encode(fixture).decode()
        result = await evaluate(
            rf"""(async()=>{{
const wait=async(test,label)=>{{for(let i=0;i<400;i++){{if(test())return;await new Promise(r=>setTimeout(r,50));}}throw Error('timeout:'+label+':'+identityStatus.textContent+':'+productStatus.textContent+':'+maskStatus.textContent)}};
const bytes=Uint8Array.from(atob('{encoded}'),c=>c.charCodeAt(0));
const file=new File([bytes],'identity-browser.png',{{type:'image/png'}});
const transfer=new DataTransfer();transfer.items.add(file);
Object.defineProperty(product,'files',{{value:transfer.files,configurable:true}});
product.dispatchEvent(new Event('change',{{bubbles:true}}));
await wait(()=>productStatus.textContent.includes('上传成功')&&!maskPrepare.disabled,'upload');
maskPrepare.click();
await wait(()=>!maskWorkspace.hidden&&maskCanvas.width>0,'mask-preview');
const bounds=maskCanvas.getBoundingClientRect();
maskDrawing=true;eraseMaskAt({{clientX:bounds.left+bounds.width/2,clientY:bounds.top+bounds.height*.25}});maskDrawing=false;
maskConfirm.click();
await wait(()=>Boolean(reviewedSubjectSealId)&&!identityGenerate.disabled,'mask-confirm');
identityGenerate.click();
await wait(()=>Boolean(identityPreview)&&!identityGenerate.disabled,'generation-preview');
identityGenerate.click();
await wait(()=>document.querySelectorAll('#cards img.identity-candidate').length===3,'candidates');
await wait(()=>[...document.querySelectorAll('#cards img.identity-candidate')].every(x=>x.complete&&x.naturalWidth>0),'images');
const jobId=localStorage.getItem('identity-locked-job-id');
const toggle=document.querySelector('.identity-toggle'),image=document.querySelector('.identity-candidate');
toggle.click();await wait(()=>image.src.includes('/original-content')&&image.complete&&image.naturalWidth>0,'original');
const originalLoaded=image.naturalWidth>0;
toggle.click();await wait(()=>image.src.includes('/candidates/')&&image.complete&&image.naturalWidth>0,'style');
return {{job_id:jobId,reviewed_subject_seal_id:reviewedSubjectSealId,mask_reviewed:maskStatus.textContent.includes('商品主体已确认'),candidate_count:document.querySelectorAll('#cards img.identity-candidate').length,all_candidates_loaded:[...document.querySelectorAll('#cards img.identity-candidate')].every(x=>x.complete&&x.naturalWidth>0),original_loaded:originalLoaded,lock_text:identityStatus.textContent,button_label:identityGenerate.textContent,page_url:location.href}};
}})()"""
        )
        await call("Page.reload", {})
        await asyncio.sleep(0.6)
        refreshed = await evaluate(
            """(async()=>{const wait=async(test,label)=>{for(let i=0;i<400;i++){if(test())return;await new Promise(r=>setTimeout(r,50));}throw Error('timeout:'+label+':'+identityStatus.textContent)};await wait(()=>document.querySelectorAll('#cards img.identity-candidate').length===3,'refresh');await wait(()=>[...document.querySelectorAll('#cards img.identity-candidate')].every(x=>x.complete&&x.naturalWidth>0),'refresh-images');const toggle=document.querySelector('.identity-toggle'),image=document.querySelector('.identity-candidate');toggle.click();await wait(()=>image.src.includes('/original-content')&&image.complete&&image.naturalWidth>0,'refresh-original');return {refresh_candidate_count:document.querySelectorAll('#cards img.identity-candidate').length,refresh_original_loaded:image.naturalWidth>0,refresh_lock_text:identityStatus.textContent};})()"""
        )
        return {**cast(dict[str, Any], result), **cast(dict[str, Any], refreshed)}


def main() -> int:
    if not CHROME.exists():
        raise RuntimeError("Google Chrome is required for browser acceptance")
    root = Path(mkdtemp(prefix="identity-locked-browser-"))
    database = root / "browser.db"
    assets = root / "assets"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "head")
    engine = create_database_engine(f"sqlite:///{database}")
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sessions() as session:
        sample = StyleSample(
            customer_id=CUSTOMER,
            photo_id="browser-style-evidence",
            authorized_at=datetime.now(UTC),
            consent_provenance="synthetic-browser-acceptance",
            consent_text_version="browser-acceptance-v1",
            content_sha256="b" * 64,
            mime_type="image/png",
            width=96,
            height=120,
            asset_store_namespace="synthetic-browser-only",
        )
        session.add_all(
            [Customer(id=CUSTOMER, display_name="Browser acceptance"), sample]
        )
        session.commit()
        HistoricalStyleConfirmationService(session).confirm(
            CUSTOMER,
            HistoricalStyleGroupsConfirmRequest.model_validate(
                {
                    "confirmation_key": "identity-browser-style-v1",
                    "confirmed": True,
                    "groups": [
                        {
                            "name": "browser-tabletop",
                            "proposal_keys": ["browser-tabletop"],
                            "evidence_sample_ids": [str(sample.id)],
                            "slots": [
                                {
                                    "slot_key": "hero",
                                    "label": "主图",
                                    "rules": {
                                        "aspect_ratio": "4:5",
                                        "shot_distance": "close",
                                        "camera_angle": "front",
                                        "background": ["stone"],
                                        "negative_rules": ["product", "jewelry"],
                                    },
                                }
                            ],
                        }
                    ],
                }
            ),
        )
    engine.dispose()
    app_port, debug_port = _port(), _port()
    while app_port == debug_port:
        debug_port = _port()
    environment = {
        **os.environ,
        "APP_ENV": "test",
        "DATABASE_URL": f"sqlite:///{database}",
        "LOCAL_STYLE_ASSET_ROOT": str(assets),
        "REAL_IMAGE_ENABLED": "false",
        "PYTHONPATH": str(ROOT / "src"),
        "NO_PROXY": "127.0.0.1,localhost",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
    }
    launcher = (
        "import uvicorn\n"
        "from opc_style.main import create_app\n"
        "from opc_style.api.routes_image_jobs import get_identity_subject_segmenter\n"
        "from opc_style.domain.subject_seal import DeterministicLocalSubjectSegmenter\n"
        "app=create_app()\n"
        "app.dependency_overrides[get_identity_subject_segmenter]=DeterministicLocalSubjectSegmenter\n"
        f"uvicorn.run(app,host='127.0.0.1',port={app_port},log_level='warning')\n"
    )
    python = ROOT / ".venv/bin/python"
    if not python.exists():
        python = Path(sys.executable)
    server = subprocess.Popen(
        [str(python), "-c", launcher],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    chrome = subprocess.Popen(
        [
            str(CHROME),
            "--headless=new",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={debug_port}",
            f"--user-data-dir={root / 'chrome'}",
            "--remote-allow-origins=*",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-sync",
            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
            "--no-first-run",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 60
        websocket_url = None
        while time.monotonic() < deadline:
            try:
                with LOCAL_HTTP.open(
                    f"http://127.0.0.1:{debug_port}/json/list", timeout=0.3
                ) as response:
                    websocket_url = json.load(response)[0]["webSocketDebuggerUrl"]
                with LOCAL_HTTP.open(
                    f"http://127.0.0.1:{app_port}/health", timeout=0.3
                ) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        if websocket_url is None:
            raise RuntimeError("browser/server startup failed")
        evidence = asyncio.run(
            _drive(
                websocket_url,
                f"http://127.0.0.1:{app_port}/local/style-suite",
                _fixture(),
            )
        )
        evidence.update(
            {
                "status": "passed",
                "acceptance_batch_id": root.name,
                "server_port": app_port,
                "health_http": 200,
                "page_http": 200,
                "fresh_browser_profile": True,
                "segmentation_adapter": "deterministic-local-subject-v2",
                "synthetic_test_only": True,
                "publishable": False,
                "external_network_calls": 0,
                "real_model_calls": 0,
                "fees_usd": 0,
                "database_path": str(database),
                "asset_root": str(assets),
            }
        )
        report = root / "identity-locked-browser-acceptance.json"
        report.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps(
                {**evidence, "report_path": str(report)}, ensure_ascii=False, indent=2
            )
        )
        return 0
    finally:
        chrome.terminate()
        server.terminate()
        for process in (chrome, server):
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())

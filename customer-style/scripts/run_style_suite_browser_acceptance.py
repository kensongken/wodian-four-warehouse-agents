#!/usr/bin/env python3
"""Drive the real page JavaScript in headless Chrome and record its run evidence."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import sqlite3
import subprocess
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from typing import Any, cast
from urllib.request import ProxyHandler, build_opener

from alembic.config import Config
from PIL import Image
from websockets.asyncio.client import connect
from websockets.typing import Origin

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
LOCAL_HTTP = build_opener(ProxyHandler({}))


class MockProvider:
    def __init__(self, image_url: str, style_url: str, calls: list[str]) -> None:
        self.image_url = image_url
        self.style_url = style_url
        self.calls = calls


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _database_table_counts(database: Path) -> dict[str, int]:
    """Count every real application table without exposing row contents."""

    with sqlite3.connect(database) as connection:
        table_names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' "
                "AND name != 'alembic_version' "
                "AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        ]
        counts: dict[str, int] = {}
        for name in table_names:
            quoted_name = name.replace('"', '""')
            row = connection.execute(f'SELECT count(*) FROM "{quoted_name}"').fetchone()
            counts[name] = int(row[0])
        return counts


def _database_batch_evidence(database: Path, run_id: str) -> list[dict[str, Any]]:
    """Return sealed per-cell facts without paths, media bytes, or access tokens."""

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT i.style_name, i.slot_key, i.style_reference_manifest_sha256, "
            "i.fidelity_state, i.state AS item_state, r.source_content_sha256, "
            "j.status AS job_status, c.content_sha256 AS candidate_content_sha256, "
            "c.sequence_number, p.outbound_attempts "
            "FROM style_suite_run_items i "
            "JOIN style_suite_runs r ON r.id = i.run_id "
            "JOIN image_output_jobs j ON j.id = i.output_job_id "
            "JOIN image_candidates c ON c.output_job_id = j.id "
            "JOIN real_image_pilot_authorizations p ON p.output_job_id = j.id "
            "WHERE i.run_id = ? ORDER BY i.style_name, i.slot_key, c.sequence_number",
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _strip_access_urls(matrix: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep matrix proof while excluding short-lived signed browser URLs."""

    safe = json.loads(json.dumps(matrix))
    for row in safe:
        row.pop("style_reference_thumbnail_urls", None)
        for cell in row["slots"]:
            cell.pop("source_preview_url", None)
            cell.pop("style_reference_thumbnail_urls", None)
            for candidate in cell["candidates"]:
                candidate.pop("preview_url", None)
                candidate.pop("source_preview_url", None)
    return cast(list[dict[str, Any]], safe)


def _media_manifest(root: Path) -> dict[str, Any]:
    """Hash relative names, types, sizes, and bytes without exposing local paths."""
    entries: list[dict[str, object]] = []
    file_count = 0
    byte_count = 0
    if root.exists():
        for path in sorted(
            root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
        ):
            relative = path.relative_to(root).as_posix()
            value = path.lstat()
            if path.is_symlink():
                entries.append(
                    {"path": relative, "kind": "symlink", "target": os.readlink(path)}
                )
            elif path.is_file():
                content_hash = sha256()
                with path.open("rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        content_hash.update(chunk)
                file_count += 1
                byte_count += value.st_size
                entries.append(
                    {
                        "path": relative,
                        "kind": "file",
                        "size": value.st_size,
                        "sha256": content_hash.hexdigest(),
                    }
                )
            elif path.is_dir():
                entries.append({"path": relative, "kind": "directory"})
            else:
                entries.append({"path": relative, "kind": "other"})
    digest = sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "entry_count": len(entries),
        "file_count": file_count,
        "byte_count": byte_count,
        "sha256": digest,
    }


def _png_base64(
    size: tuple[int, int] = (96, 96), color: tuple[int, int, int] = (36, 98, 112)
) -> str:
    stream = BytesIO()
    Image.new("RGB", size, color).save(stream, format="PNG")
    return base64.b64encode(stream.getvalue()).decode()


def _jpeg_base64(color: tuple[int, int, int] = (36, 98, 112)) -> str:
    stream = BytesIO()
    Image.new("RGB", (96, 96), color).save(stream, format="JPEG")
    return base64.b64encode(stream.getvalue()).decode()


@contextmanager
def _mock_provider_server() -> Iterator[MockProvider]:
    """Expose a loopback-only fake provider that records no request content."""

    calls: list[str] = []
    identity_request_count = [0]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            calls.append(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            if self.path == "/v1/responses":
                payload = json.loads(body)
                if "Describe only visible product identity facts" in payload.get(
                    "instructions", ""
                ):
                    identity_index = identity_request_count[0]
                    identity_request_count[0] += 1
                    identity = {
                        "category": {"value": "bracelet", "certainty": "observed"},
                        "primary_colors": {
                            "value": ["green", "gold_tone"],
                            "certainty": "observed",
                        },
                        "visible_surface_appearance": {
                            "value": ["translucent", "glossy"],
                            "certainty": "observed",
                        },
                        "component_shapes": {
                            "value": ["round_bead"],
                            "certainty": "observed",
                        },
                        "visible_component_count": {
                            "value": 18,
                            "certainty": "observed",
                        },
                        "component_arrangement": {
                            "value": "single_strand",
                            "certainty": "observed",
                        },
                        "core_accessories": {
                            "value": [{"kind": "clasp", "visible_count": 1}],
                            "certainty": "observed",
                        },
                        "overall_form": {
                            "value": "closed_loop",
                            "certainty": "observed",
                        },
                        "relative_proportions": {
                            "value": "珠体大小近似一致",
                            "certainty": "observed",
                        },
                        "distinguishing_marks": {
                            "value": "扣件旁可见一枚圆形吊饰",
                            "certainty": "observed",
                        },
                        "visibility_limits": {
                            "value": [],
                            "certainty": "observed",
                        },
                    }
                    if identity_index == 2:
                        identity["core_accessories"] = {
                            "value": [],
                            "certainty": "observed",
                        }
                        identity["visibility_limits"] = {
                            "value": ["clasp_hidden"],
                            "certainty": "observed",
                        }
                    elif identity_index == 4:
                        identity["core_accessories"] = {
                            "value": [
                                {"kind": "clasp", "visible_count": 1},
                                {"kind": "pendant", "visible_count": 1},
                            ],
                            "certainty": "observed",
                        }
                    response_body = json.dumps(
                        {
                            "status": "completed",
                            "output": [
                                {
                                    "type": "message",
                                    "content": [
                                        {
                                            "type": "output_text",
                                            "text": json.dumps(identity),
                                        }
                                    ],
                                }
                            ],
                        }
                    ).encode("utf-8")
                    self._send_json(response_body)
                    return
                if "strict jewelry identity checker" in payload.get("instructions", ""):
                    response_body = json.dumps(
                        {
                            "status": "completed",
                            "output": [
                                {
                                    "type": "message",
                                    "content": [
                                        {
                                            "type": "output_text",
                                            "text": json.dumps(
                                                {
                                                    "assessments": [
                                                        {
                                                            "candidate_index": 1,
                                                            "state": "red",
                                                            "flags": [
                                                                "product_category_changed"
                                                            ],
                                                            "summary_zh": "商品类别发生变化",
                                                        },
                                                        {
                                                            "candidate_index": 2,
                                                            "state": "green",
                                                            "flags": [],
                                                            "summary_zh": "商品身份一致",
                                                        },
                                                        {
                                                            "candidate_index": 3,
                                                            "state": "unknown",
                                                            "flags": [
                                                                "provider_check_unavailable"
                                                            ],
                                                            "summary_zh": "暂时无法确认",
                                                        },
                                                    ]
                                                }
                                            ),
                                        }
                                    ],
                                }
                            ],
                        }
                    ).encode("utf-8")
                    self._send_json(response_body)
                    return
                sample_ids = [
                    item["text"].removeprefix("Evidence sample ID: ")
                    for message in payload["input"]
                    for item in message["content"]
                    if item["type"] == "input_text"
                ]
                groups = []
                for index, (name, evidence) in enumerate(
                    (
                        ("light square", sample_ids[:3]),
                        ("neutral square", sample_ids[3:6]),
                        ("blue square", sample_ids[6:]),
                    ),
                    1,
                ):
                    groups.append(
                        {
                            "proposal_key": f"mock-style-{index}",
                            "suggested_name": name,
                            "evidence_sample_ids": evidence,
                            "observations": {
                                "palette": ["light" if index == 1 else "neutral"],
                                "lighting": ["soft"],
                                "composition": ["centered"],
                                "background": ["plain"],
                                "angle": ["oblique"],
                                "crop": ["close"],
                                "props": ["none"],
                                "aspect_ratio": ["square"],
                                "presentation": ["isolated"],
                            },
                            "suggested_slots": ["hero", "detail"],
                        }
                    )
                response_body = json.dumps(
                    {
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": json.dumps({"groups": groups}),
                                    }
                                ],
                            }
                        ],
                    }
                ).encode("utf-8")
            elif self.path == "/v1/images/edits":
                image_index = sum(path == "/v1/images/edits" for path in calls)
                response_body = json.dumps(
                    {
                        "data": [
                            {
                                "b64_json": _jpeg_base64(
                                    (
                                        30 + image_index * 20,
                                        70 + image_index * 10,
                                        90 + image_index * 7,
                                    )
                                )
                            }
                        ]
                    }
                ).encode("utf-8")
            else:
                response_body = json.dumps(
                    {"error": {"message": "unexpected mock path"}}
                ).encode("utf-8")
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def _send_json(self, response_body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        yield MockProvider(
            f"{base_url}/v1/images/edits",
            f"{base_url}/v1/responses",
            calls,
        )
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.server_close()


async def _cdp_demo(websocket_url: str, page_url: str) -> dict[str, Any]:
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
                if reply.get("method") == "Runtime.consoleAPICalled":
                    values = [
                        item.get("value", item.get("description", ""))
                        for item in reply["params"].get("args", [])
                    ]
                    print("BROWSER", *values, flush=True)
                if reply.get("id") == sequence:
                    if "error" in reply:
                        raise RuntimeError(str(reply["error"]))
                    return cast(dict[str, Any], reply["result"])

        await call("Page.enable", {})
        await call("Runtime.enable", {})
        await call("Page.navigate", {"url": page_url})
        await asyncio.sleep(0.5)
        script = r"""(async()=>{
const wait=async(test,label)=>{for(let i=0;i<120;i++){if(test())return;await new Promise(r=>setTimeout(r,50));}throw Error('timeout:'+label+':'+demoStatus.textContent)};
prepare.click();
await wait(()=>document.querySelectorAll('#demo-style-cards .demo-style').length===2&&demoStatus.textContent.includes('5 个套图位置'),'demo-styles');
const demoStyleCount=document.querySelectorAll('#demo-style-cards .demo-style').length,slotMatch=demoStatus.textContent.match(/(\d+) 个套图位置/),demoSlotCount=slotMatch?Number(slotMatch[1]):0;
runDemo.click();
await wait(()=>document.querySelectorAll('#demo-cards img.candidate').length===3,'demo-images');
await wait(()=>[...document.querySelectorAll('#demo-cards img.candidate')].every(x=>x.complete&&x.naturalWidth>0),'demo-loaded');
const demoImages=[...document.querySelectorAll('#demo-cards img.candidate')];console.log('acceptance:demo-images-loaded');
const demoScope=(demoCards.querySelector('article b')?.textContent||'').split(' · ');
return {demo_run_id:localStorage.getItem(demoRunKey),url:location.href,demo_style_count:demoStyleCount,demo_slot_count:demoSlotCount,demo_image_count:demoImages.length,demo_loaded_image_count:demoImages.filter(x=>x.complete&&x.naturalWidth>0).length,demo_selected_style_name:demoScope[0]||'',demo_selected_slot_key:demoScope[1]||'',demo_no_charge_visible:!demoWorkspace.hidden&&demoWorkspace.textContent.includes('不收费'),demo_publishable_false_visible:!demoWorkspace.hidden&&demoWorkspace.textContent.includes('不可发布')&&demoStatus.textContent.includes('不可发布'),demo_zero_external_visible:demoStatus.textContent.includes('外部请求 0 次'),demo_workspace_visible:!demoWorkspace.hidden};
})()"""
        result = await call(
            "Runtime.evaluate",
            {"expression": script, "awaitPromise": True, "returnByValue": True},
        )
        remote: dict[str, Any] = result["result"]
        if remote.get("subtype") == "error" or "exceptionDetails" in result:
            raise RuntimeError(str(result))
        return cast(dict[str, Any], remote["value"])


async def _cdp_real(websocket_url: str) -> dict[str, Any]:
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
                if reply.get("method") == "Runtime.consoleAPICalled":
                    values = [
                        item.get("value", item.get("description", ""))
                        for item in reply["params"].get("args", [])
                    ]
                    print("BROWSER", *values, flush=True)
                if reply.get("id") == sequence:
                    if "error" in reply:
                        raise RuntimeError(str(reply["error"]))
                    return cast(dict[str, Any], reply["result"])

        await call("Page.enable", {})
        await call("Runtime.enable", {})
        script = f"""(async()=>{{
const wait=async(test,label)=>{{for(let i=0;i<120;i++){{if(test())return;await new Promise(r=>setTimeout(r,50));}}throw Error('timeout:'+label+':'+historyStatus.textContent+':blocked='+pilotBlocked+':busy='+pilotBusy+':recovery='+pilotRecoveryRequired+':cost='+costConfirmation.checked+':scope='+Boolean(selectedPilotItem)+':error='+pilotError.textContent)}};
const makeBlob=async(w,h,color)=>{{const canvas=document.createElement('canvas');canvas.width=w;canvas.height=h;const context=canvas.getContext('2d');context.fillStyle=color;context.fillRect(0,0,w,h);return new Promise(resolve=>canvas.toBlob(resolve,'image/png'))}},batchDt=new DataTransfer(),folderDt=new DataTransfer(),largeCanvas=document.createElement('canvas');largeCanvas.width=4200;largeCanvas.height=4200;const largeContext=largeCanvas.getContext('2d');largeContext.fillStyle='rgb(36,98,112)';largeContext.fillRect(0,0,4200,4200);const largeHistory=await new Promise(resolve=>largeCanvas.toBlob(resolve,'image/png'));
const batchFiles=[];for(let i=0;i<5;i++){{const blob=i===0?largeHistory:await makeBlob(96+i,96,`rgb(${{36+i}},98,112)`),file=new File([blob],`history-a-${{i}}.png`,{{type:'image/png',lastModified:100+i}});batchFiles.push(file);batchDt.items.add(file)}}historyFiles.files=batchDt.files;historyFiles.dispatchEvent(new Event('change'));const batchCount=selectedHistoryFiles.length;folderDt.items.add(batchFiles[0]);for(let i=0;i<5;i++){{const blob=await makeBlob(72+i,96,`rgb(${{190+i}},45,45)`);folderDt.items.add(new File([blob],`history-b-${{i}}.png`,{{type:'image/png',lastModified:200+i}}))}}historyFolder.files=folderDt.files;historyFolder.dispatchEvent(new Event('change'));const folderMergedCount=selectedHistoryFiles.length,removedFile=selectedHistoryFiles[0];document.querySelector('.thumb-remove').click();const countAfterRemove=selectedHistoryFiles.length,readdDt=new DataTransfer();readdDt.items.add(removedFile);historyFiles.files=readdDt.files;historyFiles.dispatchEvent(new Event('change'));const countAfterReadd=selectedHistoryFiles.length;consent.click();await wait(()=>!analyze.disabled,'history-ready');analyze.click();await wait(()=>document.querySelectorAll('.proposal').length>0,'history-analysis');
await wait(()=>!realAnalysisGate.hidden&&!realAnalysisConfirm.disabled,'real-analysis-preview');realAnalysisConfirm.click();await wait(()=>!runRealAnalysis.disabled,'real-analysis-confirmed');runRealAnalysis.click();await wait(()=>!confirmStyles.disabled&&document.querySelectorAll('.proposal').length===2,'real-analysis-complete');const proposalCards=[...document.querySelectorAll('.proposal')];proposalCards[0].querySelectorAll('input[type=checkbox]').forEach(x=>x.checked=true);const secondBoxes=proposalCards[1].querySelectorAll('input[type=checkbox]');secondBoxes[0].checked=true;secondBoxes[1].checked=true;secondBoxes[2].checked=false;confirmStyles.click();await wait(()=>styles.textContent.includes('已确认 2 个真实风格'),'history-confirm');console.log('acceptance:history-confirmed');
const bytes=Uint8Array.from(atob('{_png_base64()}'),c=>c.charCodeAt(0)); const dt=new DataTransfer(); dt.items.add(new File([bytes],'synthetic-product.png',{{type:'image/png'}})); product.files=dt.files; product.dispatchEvent(new Event('change')); await wait(()=>!preview.disabled&&productStatus.textContent.includes('上传成功')&&productPreview.classList.contains('ready'),'upload');console.log('acceptance:product-uploaded');
preview.click(); await wait(()=>!pilotStyle.disabled&&pilotStyle.options.length===3,'pilot-styles'); pilotStyle.value=pilotStyle.options[1].value;pilotStyle.dispatchEvent(new Event('change'));await wait(()=>!pilotSlot.disabled&&pilotSlot.options.length>1,'pilot-slots');pilotSlot.value=pilotSlot.options[1].value;pilotSlot.dispatchEvent(new Event('change'));await wait(()=>summary.textContent.includes('1 个风格 × 1 个位置，预计生成 3 张')&&!costConfirmation.disabled,'pilot-preview');costConfirmation.click();await wait(()=>!execute.disabled,'cost-confirmed');const selectedScope=selectedPilotItem;selectedPilotItem={{...selectedScope,scene_style_version_id:'00000000-0000-0000-0000-000000000000'}};execute.click();await wait(()=>!pilotError.hidden&&pilotError.textContent.includes('所选风格或位置已失效')&&execute.disabled,'expired-scope');const expiredScopeError=pilotError.textContent;preview.click();await wait(()=>pilotError.hidden&&!pilotBlocked&&!pilotRecoveryRequired&&!pilotStyle.disabled&&pilotStyle.options.length===3,'scope-repreview');pilotStyle.value=pilotStyle.options[1].value;pilotStyle.dispatchEvent(new Event('change'));await wait(()=>!pilotSlot.disabled&&pilotSlot.options.length>1,'scope-reslots');pilotSlot.value=pilotSlot.options[1].value;pilotSlot.dispatchEvent(new Event('change'));await wait(()=>!costConfirmation.disabled,'scope-reselected');costConfirmation.click();await wait(()=>!execute.disabled,'scope-recovered');const recoveredScope=selectedPilotItem;execute.click(); await wait(()=>document.querySelectorAll('#cards img.candidate').length===3,'execute');console.log('acceptance:candidates-rendered');
await wait(()=>[...document.querySelectorAll('#cards img.candidate')].every(x=>x.complete&&x.naturalWidth>0),'images');await wait(()=>[...document.querySelectorAll('#cards img.source-image')].every(x=>x.complete&&x.naturalWidth>0),'source-images');console.log('acceptance:images-loaded');
await wait(()=>!fidelityGate.hidden&&fidelityQuote.textContent.includes('1 张商品原图和 3 张候选图')&&!fidelityConfirmation.disabled,'fidelity-quote');const fidelityUnconfirmedDisabled=runFidelity.disabled;fidelityConfirmation.click();await wait(()=>!runFidelity.disabled,'fidelity-confirmed');runFidelity.click();await wait(()=>fidelityGate.hidden&&cards.textContent.includes('商品身份不一致，不能选择')&&cards.textContent.includes('机器检查未发现商品身份变化，仍需人工确认')&&cards.textContent.includes('暂时无法确认是同一商品，不能选择'),'fidelity-results');console.log('acceptance:fidelity-rendered');
const articles=[...cards.querySelectorAll('article')],redHasNoSelection=!articles[0].querySelector('[data-review]'),unknownHasNoSelection=!articles[2].querySelector('[data-review]');await review(articles[1].querySelector('[data-review="green"]'));await wait(()=>cards.textContent.includes('下载已选最终图'),'green-selected');console.log('acceptance:green-reviewed');const successfulImageCount=cards.querySelectorAll('img.candidate').length,successfulLoadedImageCount=[...cards.querySelectorAll('img.candidate')].filter(x=>x.complete&&x.naturalWidth>0).length,sourceImageCount=cards.querySelectorAll('img.source-image').length,sourceLoadedImageCount=[...cards.querySelectorAll('img.source-image')].filter(x=>x.complete&&x.naturalWidth>0).length,redVisible=cards.textContent.includes('商品身份不一致，不能选择'),greenVisible=cards.textContent.includes('机器检查未发现商品身份变化，仍需人工确认'),unknownVisible=cards.textContent.includes('暂时无法确认是同一商品，不能选择');
return {{run_id:run.id,real_run_id:run.id,url:location.href,history_sample_count:selectedHistoryFiles.length,batch_count:batchCount,folder_merged_count:folderMergedCount,count_after_remove:countAfterRemove,count_after_readd:countAfterReadd,batch_entry_visible:document.querySelector('label[for="history-files"]').textContent.includes('批量选择图片'),folder_entry_visible:document.querySelector('label[for="history-folder"]').textContent.includes('选择整个文件夹'),localized_proposal_names:proposalCards.map(card=>card.querySelector('input[type=text]').value),selected_style_label:pilotStyle.options[pilotStyle.selectedIndex].textContent,selected_style_version_id:recoveredScope.scene_style_version_id,oversized_history_width:largeCanvas.width,history_style_count:proposalCards.length,history_confirmed:styles.textContent.includes('已确认 2 个真实风格'),product_upload_visible:productStatus.textContent.includes('上传成功')&&productPreview.classList.contains('ready'),selected_style_count:pilotStyle.value?1:0,selected_slot_count:pilotSlot.value?1:0,cost_confirmation:costConfirmation.checked,expired_scope_error:expiredScopeError,image_count:successfulImageCount,loaded_image_count:successfulLoadedImageCount,real_image_count:successfulImageCount,real_loaded_image_count:successfulLoadedImageCount,source_image_count:sourceImageCount,source_loaded_image_count:sourceLoadedImageCount,fidelity_unconfirmed_disabled:fidelityUnconfirmedDisabled,red_has_no_selection:redHasNoSelection,unknown_has_no_selection:unknownHasNoSelection,red_visible:redVisible,green_visible:greenVisible,unknown_visible:unknownVisible,publishable_false_visible:progress.textContent.includes('publishable=false'),mode_notice:modeNotice.textContent,matrix:summary.textContent}};
}})()"""
        result = await call(
            "Runtime.evaluate",
            {"expression": script, "awaitPromise": True, "returnByValue": True},
        )
        remote: dict[str, Any] = result["result"]
        if remote.get("subtype") == "error" or "exceptionDetails" in result:
            raise RuntimeError(str(result))
        return cast(dict[str, Any], remote["value"])


async def _cdp_batch(websocket_url: str) -> dict[str, Any]:
    """Exercise the six-cell page flow and prove reload restoration."""
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

        async def evaluate(expression: str) -> dict[str, Any]:
            result = await call(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            )
            remote = result["result"]
            if remote.get("subtype") == "error" or "exceptionDetails" in result:
                raise RuntimeError(str(result))
            return cast(dict[str, Any], remote["value"])

        first = await evaluate(
            f"""(async()=>{{
const wait=async(test,label)=>{{for(let i=0;i<240;i++){{if(test())return;await new Promise(resolve=>setTimeout(resolve,50));}}throw Error('timeout:'+label+':'+historyStatus.textContent+':'+pilotReadiness.textContent+':observation='+identityObservationState.status+':'+identityObservationStatus.textContent+':'+pilotError.textContent)}};
const makeBlob=async(index)=>{{const canvas=document.createElement('canvas');canvas.width=96+index;canvas.height=96;const context=canvas.getContext('2d');context.fillStyle=`rgb(${{30+index*8}},${{90+index}},${{110+index}})`;context.fillRect(0,0,canvas.width,canvas.height);return new Promise(resolve=>canvas.toBlob(resolve,'image/png'))}};
const historyTransfer=new DataTransfer();for(let i=0;i<12;i++){{historyTransfer.items.add(new File([await makeBlob(i)],`history-${{i}}.png`,{{type:'image/png',lastModified:100+i}}))}}historyFiles.files=historyTransfer.files;historyFiles.dispatchEvent(new Event('change'));consent.click();await wait(()=>!analyze.disabled,'history-ready');analyze.click();await wait(()=>!realAnalysisGate.hidden&&!realAnalysisConfirm.disabled,'analysis-preview');realAnalysisConfirm.click();await wait(()=>!runRealAnalysis.disabled,'analysis-authorized');runRealAnalysis.click();await wait(()=>document.querySelectorAll('.proposal').length===3&&!confirmStyles.disabled,'analysis-complete');document.querySelectorAll('.proposal input[type=checkbox]').forEach(input=>input.checked=true);confirmStyles.click();await wait(()=>styles.textContent.includes('已确认 3 个真实风格'),'styles-confirmed');
const bytes=Uint8Array.from(atob('{_png_base64()}'),character=>character.charCodeAt(0));const productTransfer=new DataTransfer();productTransfer.items.add(new File([bytes],'identity-source.png',{{type:'image/png'}}));product.files=productTransfer.files;product.dispatchEvent(new Event('change'));await wait(()=>!identityAnalysisConfirmation.disabled&&productStatus.textContent.includes('上传成功'),'identity-preview');identityAnalysisConfirmation.click();await wait(()=>!runIdentityAnalysisButton.disabled,'identity-authorized');runIdentityAnalysisButton.click();await wait(()=>!confirmIdentityCardButton.disabled,'identity-proposal');confirmIdentityCardButton.click();await wait(()=>confirmedIdentityCard&&!reuseProductIdentity.disabled,'identity-confirmed');const reusableCardId=confirmedIdentityCard.id,reusableProductId=asset.product_id;reuseProductIdentity.checked=true;const clearTransfer=new DataTransfer();clearTransfer.items.add(new File([await makeBlob(20)],'clear-same-product.png',{{type:'image/png'}}));product.files=clearTransfer.files;product.dispatchEvent(new Event('change'));await wait(()=>asset&&asset.product_id===reusableProductId&&confirmedIdentityCard?.id===reusableCardId&&!identityObservationConfirmation.disabled,'clear-observation-quote');identityObservationConfirmation.click();await wait(()=>!runIdentityObservation.disabled,'clear-observation-authorized');runIdentityObservation.click();await wait(()=>identityObservationState.status==='pass'&&!preview.disabled,'clear-observation-pass');const clearObservationRunId=identityObservationState.run.run_id;
preview.click();await wait(()=>document.querySelectorAll('#batch-style-grid [data-style-card]').length===3,'batch-styles');const slotInputs=[...batchStyleGrid.querySelectorAll('input[data-slot]')];slotInputs.forEach(input=>input.checked=true);await quoteSelectedBatch();await wait(()=>batchPreview&&batchPreview.candidate_count===6&&!batchCostConfirmation.disabled,'batch-quote');batchCostConfirmation.click();await wait(()=>!execute.disabled,'batch-cost');execute.click();await wait(()=>batchResultMatrix.querySelectorAll('img.candidate').length===6,'batch-results');await wait(()=>[...batchResultMatrix.querySelectorAll('img.candidate')].every(image=>image.complete&&image.naturalWidth>0),'batch-images');const state=await json('/local/style-suite/api/batch-runs/'+run.id);return {{run_id:run.id,real_run_id:run.id,url:location.href,history_sample_count:selectedHistoryFiles.length,history_style_count:document.querySelectorAll('.proposal').length,identity_confirmed:Boolean(confirmedIdentityCard),identity_card_id:reusableCardId,identity_product_id:reusableProductId,clear_observation_run_id:clearObservationRunId,clear_observation_state:identityObservationState.status,normal_foreground_hidden:foregroundReview.hidden,selected_style_count:state.matrix.length,selected_slot_count:state.matrix.reduce((sum,row)=>sum+row.slots.length,0),candidate_count:state.candidate_count,loaded_image_count:[...batchResultMatrix.querySelectorAll('img.candidate')].filter(image=>image.complete&&image.naturalWidth>0).length,reference_thumbnail_count:batchResultMatrix.querySelectorAll('.reference-strip img').length,cost_confirmation:batchCostConfirmation.checked,maximum_estimated_cost_usd:batchPreview.maximum_estimated_cost_usd,publishable:state.publishable,matrix:state.matrix}};
}})()"""
        )
        await call("Page.reload", {"ignoreCache": True})
        await asyncio.sleep(0.5)
        restored = await evaluate(
            """(async()=>{
const wait=async(test,label)=>{for(let i=0;i<240;i++){if(test())return;await new Promise(resolve=>setTimeout(resolve,50));}throw Error('timeout:'+label)};
await wait(()=>run&&batchResultMatrix.querySelectorAll('img.candidate').length===6,'restored-matrix');await wait(()=>[...batchResultMatrix.querySelectorAll('img.candidate')].every(image=>image.complete&&image.naturalWidth>0),'restored-images');return {restored_run_id:run.id,restored_candidate_count:batchResultMatrix.querySelectorAll('img.candidate').length,restored_loaded_image_count:[...batchResultMatrix.querySelectorAll('img.candidate')].filter(image=>image.complete&&image.naturalWidth>0).length,restored_publishable_false:progress.textContent.includes('publishable=false')};
})()"""
        )
        reviewed = await evaluate(
            """(async()=>{
const wait=async(test,label)=>{for(let i=0;i<240;i++){if(test())return;await new Promise(resolve=>setTimeout(resolve,50));}throw Error('timeout:'+label+':'+identityObservationStatus.textContent)};
const makeBlob=async(color)=>{const canvas=document.createElement('canvas');canvas.width=118;canvas.height=96;const context=canvas.getContext('2d');context.fillStyle=color;context.fillRect(0,0,canvas.width,canvas.height);return new Promise(resolve=>canvas.toBlob(resolve,'image/png'))};
const expectedProduct=readReusableIdentityCard().product_id,expectedCard=readReusableIdentityCard().card.id;reuseProductIdentity.checked=true;const transfer=new DataTransfer();transfer.items.add(new File([await makeBlob('rgb(90,70,120)')],'occluded-same-product.png',{type:'image/png'}));product.files=transfer.files;product.dispatchEvent(new Event('change'));await wait(()=>asset?.product_id===expectedProduct&&confirmedIdentityCard?.id===expectedCard&&!identityObservationConfirmation.disabled,'occluded-quote');identityObservationConfirmation.click();runIdentityObservation.click();await wait(()=>identityObservationState.status==='needs_review','occluded-review');const lockedBeforeReview=preview.disabled,runId=identityObservationState.run.run_id;identityObservationReviewReason.value='扣头被遮挡，已对照已确认身份卡人工复核';approveIdentityObservation.click();await wait(()=>identityObservationState.status==='pass'&&!preview.disabled,'occluded-approved');return {occluded_state:'needs_review',occluded_run_id:runId,occluded_review_id:identityObservationState.review.review_id,occluded_locked_before_review:lockedBeforeReview,occluded_approved_state:identityObservationState.status,occluded_product_id:asset.product_id,occluded_card_id:confirmedIdentityCard.id};
})()"""
        )
        await call("Page.reload", {"ignoreCache": True})
        await asyncio.sleep(0.5)
        review_restored = await evaluate(
            """(async()=>{
const wait=async(test,label)=>{for(let i=0;i<240;i++){if(test())return;await new Promise(resolve=>setTimeout(resolve,50));}throw Error('timeout:'+label)};
await wait(()=>identityObservationState.status==='pass'&&identityObservationState.review?.review_id,'approved-review-restore');return {restored_observation_run_id:identityObservationState.run.run_id,restored_observation_review_id:identityObservationState.review.review_id,restored_observation_state:identityObservationState.status};
})()"""
        )
        remaining = await evaluate(
            """(async()=>{
const wait=async(test,label)=>{for(let i=0;i<240;i++){if(test())return;await new Promise(resolve=>setTimeout(resolve,50));}throw Error('timeout:'+label+':'+identityObservationStatus.textContent)};
const makeBlob=async(color,w)=>{const canvas=document.createElement('canvas');canvas.width=w;canvas.height=96;const context=canvas.getContext('2d');context.fillStyle=color;context.fillRect(0,0,canvas.width,canvas.height);return new Promise(resolve=>canvas.toBlob(resolve,'image/png'))};
const expectedProduct=readReusableIdentityCard().product_id,expectedCard=readReusableIdentityCard().card.id;
const observe=async(name,blob,expected)=>{reuseProductIdentity.checked=true;const transfer=new DataTransfer();transfer.items.add(new File([blob],name,{type:'image/png'}));product.files=transfer.files;product.dispatchEvent(new Event('change'));await wait(()=>asset?.product_id===expectedProduct&&confirmedIdentityCard?.id===expectedCard&&!identityObservationConfirmation.disabled,name+'-quote');identityObservationConfirmation.click();runIdentityObservation.click();await wait(()=>identityObservationState.status===expected,name+'-'+expected);return identityObservationState.run.run_id};
const backgroundRunId=await observe('different-background-same-product.png',await makeBlob('rgb(220,210,180)',126),'pass');const backgroundCanGenerate=!preview.disabled;const blockRunId=await observe('structural-mismatch.png',await makeBlob('rgb(180,40,40)',132),'block');return {background_state:'pass',background_run_id:backgroundRunId,background_can_generate:backgroundCanGenerate,block_state:identityObservationState.status,block_run_id:blockRunId,block_generation_locked:preview.disabled&&execute.disabled,block_product_id:asset.product_id,block_card_id:confirmedIdentityCard.id,normal_foreground_hidden:foregroundReview.hidden,normal_mask_requirement_absent:!identityObservationPanel.textContent.includes('绘制金标')&&!identityObservationPanel.textContent.includes('上传金标')};
})()"""
        )
        return {**first, **restored, **reviewed, **review_restored, **remaining}


def main() -> int:
    if not CHROME.exists():
        raise RuntimeError("Google Chrome is required for browser acceptance")
    with ExitStack() as stack:
        temporary = Path(
            stack.enter_context(TemporaryDirectory(prefix="style-suite-browser-"))
        )
        mock_provider = stack.enter_context(_mock_provider_server())
        sitecustomize = temporary / "sitecustomize.py"
        sitecustomize.write_text(
            "from opc_style.providers import openai_images\n"
            "from opc_style.providers import openai_style_analysis\n"
            "from opc_style.providers import openai_fidelity\n"
            "from opc_style.providers import openai_product_identity\n"
            "openai_images.OPENAI_IMAGE_EDIT_URL = "
            "__import__('os').environ['STYLE_SUITE_MOCK_PROVIDER_URL']\n"
            "openai_style_analysis.RESPONSE_URL = "
            "__import__('os').environ['STYLE_ANALYSIS_MOCK_PROVIDER_URL']\n"
            "openai_fidelity.RESPONSE_URL = "
            "__import__('os').environ['FIDELITY_MOCK_PROVIDER_URL']\n"
            "openai_product_identity.RESPONSE_URL = "
            "__import__('os').environ['PRODUCT_IDENTITY_MOCK_PROVIDER_URL']\n",
            encoding="utf-8",
        )
        database = temporary / "browser.db"
        local_asset_root = temporary / "local-style-assets"
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "alembic"))
        config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
        command.upgrade(config, "head")
        app_port, debug_port = _port(), _port()
        while debug_port == app_port:
            debug_port = _port()
        environment = {
            **os.environ,
            "DATABASE_URL": f"sqlite:///{database}",
            "LOCAL_STYLE_ASSET_ROOT": str(local_asset_root),
            "APP_ENV": "test",
            "REAL_IMAGE_ENABLED": "true",
            "REAL_IMAGE_API_KEY": "test-only-key",
            "REAL_IMAGE_MODEL": "gpt-image-2",
            "REAL_IMAGE_MAX_RUN_USD": "3.00",
            "REAL_IMAGE_CANDIDATE_COUNT": "1",
            "REAL_STYLE_ANALYSIS_ENABLED": "true",
            "REAL_STYLE_ANALYSIS_API_KEY": "test-only-key",
            "REAL_STYLE_ANALYSIS_MODEL": "gpt-4.1-mini-2025-04-14",
            "REAL_STYLE_ANALYSIS_MAX_RUN_USD": "0.422722",
            "REAL_FIDELITY_ENABLED": "true",
            "REAL_FIDELITY_API_KEY": "test-only-key",
            "REAL_FIDELITY_MODEL": "gpt-4.1-mini-2025-04-14",
            "REAL_FIDELITY_MAX_RUN_USD": "0.50",
            "REAL_PRODUCT_IDENTITY_ENABLED": "true",
            "REAL_PRODUCT_IDENTITY_API_KEY": "test-only-key",
            "REAL_PRODUCT_IDENTITY_MODEL": "gpt-4.1-mini-2025-04-14",
            "REAL_PRODUCT_IDENTITY_MAX_RUN_USD": "0.10",
            "STYLE_SUITE_CANDIDATE_ESTIMATE_USD": "0.12",
            "STYLE_SUITE_MOCK_PROVIDER_URL": mock_provider.image_url,
            "STYLE_ANALYSIS_MOCK_PROVIDER_URL": mock_provider.style_url,
            "FIDELITY_MOCK_PROVIDER_URL": mock_provider.style_url,
            "PRODUCT_IDENTITY_MOCK_PROVIDER_URL": mock_provider.style_url,
            "PYTHONPATH": f"{temporary}:{os.environ.get('PYTHONPATH', '')}",
            "NO_PROXY": "127.0.0.1,localhost",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
        }
        server = subprocess.Popen(
            [
                str(ROOT / ".venv/bin/uvicorn"),
                "opc_style.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(app_port),
            ],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=None,
        )
        chrome = subprocess.Popen(
            [
                str(CHROME),
                "--headless=new",
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={debug_port}",
                f"--user-data-dir={temporary / 'chrome'}",
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
            deadline = time.monotonic() + 120
            websocket_url = None
            app_ready = False
            while time.monotonic() < deadline:
                try:
                    with LOCAL_HTTP.open(
                        f"http://127.0.0.1:{debug_port}/json/list", timeout=0.2
                    ) as response:
                        tabs = json.load(response)
                    websocket_url = tabs[0]["webSocketDebuggerUrl"]
                    with LOCAL_HTTP.open(
                        f"http://127.0.0.1:{app_port}/health", timeout=0.2
                    ):
                        app_ready = True
                        break
                except OSError:
                    time.sleep(0.1)
            if websocket_url is None or not app_ready:
                raise RuntimeError(
                    "browser acceptance startup failed: "
                    f"chrome_exit={chrome.poll()} server_exit={server.poll()} "
                    f"cdp_ready={websocket_url is not None} app_ready={app_ready} "
                    f"debug_port={debug_port} app_port={app_port}"
                )
            real_counts_before_demo = _database_table_counts(database)
            real_media_root = local_asset_root / "media"
            real_media_manifest_before_demo = _media_manifest(real_media_root)
            demo_evidence = asyncio.run(
                _cdp_demo(
                    websocket_url,
                    f"http://127.0.0.1:{app_port}/local/style-suite",
                )
            )
            real_counts_after_demo = _database_table_counts(database)
            real_media_manifest_after_demo = _media_manifest(real_media_root)
            database_unchanged_after_demo = (
                real_counts_after_demo == real_counts_before_demo
            )
            real_media_unchanged_after_demo = (
                real_media_manifest_after_demo == real_media_manifest_before_demo
            )
            demo_provider_calls = len(mock_provider.calls)
            demo_evidence["demo_provider_calls"] = demo_provider_calls
            demo_evidence["real_database_counts_unchanged_after_demo"] = (
                database_unchanged_after_demo
            )
            demo_evidence["real_media_manifest_before_demo"] = (
                real_media_manifest_before_demo
            )
            demo_evidence["real_media_manifest_after_demo"] = (
                real_media_manifest_after_demo
            )
            demo_evidence["real_media_manifest_unchanged_after_demo"] = (
                real_media_unchanged_after_demo
            )
            if (
                demo_evidence["demo_style_count"] != 2
                or demo_evidence["demo_slot_count"] != 5
                or demo_evidence["demo_image_count"] != 3
                or demo_evidence["demo_loaded_image_count"] != 3
                or demo_evidence["demo_selected_style_name"] != "暖光桌面"
                or demo_evidence["demo_selected_slot_key"] != "hero"
                or not demo_evidence["demo_no_charge_visible"]
                or not demo_evidence["demo_publishable_false_visible"]
                or not demo_evidence["demo_zero_external_visible"]
                or not demo_evidence["demo_workspace_visible"]
                or not demo_evidence["demo_run_id"]
                or demo_provider_calls != 0
                or not database_unchanged_after_demo
                or not real_media_unchanged_after_demo
            ):
                raise RuntimeError(f"demo browser acceptance mismatch: {demo_evidence}")

            real_evidence = asyncio.run(_cdp_batch(websocket_url))
            real_provider_calls = len(mock_provider.calls) - demo_provider_calls
            evidence = {
                **demo_evidence,
                **real_evidence,
                "real_provider_calls": real_provider_calls,
            }
            database_batch_evidence = _database_batch_evidence(
                database, evidence["run_id"]
            )
            evidence["database_batch_evidence"] = database_batch_evidence
            if (
                evidence["history_sample_count"] != 12
                or evidence["history_style_count"] != 3
                or not evidence["identity_confirmed"]
                or evidence["clear_observation_state"] != "pass"
                or evidence["occluded_state"] != "needs_review"
                or not evidence["occluded_locked_before_review"]
                or evidence["occluded_approved_state"] != "pass"
                or evidence["restored_observation_state"] != "pass"
                or evidence["restored_observation_run_id"]
                != evidence["occluded_run_id"]
                or evidence["restored_observation_review_id"]
                != evidence["occluded_review_id"]
                or evidence["background_state"] != "pass"
                or not evidence["background_can_generate"]
                or evidence["block_state"] != "block"
                or not evidence["block_generation_locked"]
                or not evidence["normal_mask_requirement_absent"]
                or len(
                    {
                        evidence["identity_product_id"],
                        evidence["occluded_product_id"],
                        evidence["block_product_id"],
                    }
                )
                != 1
                or len(
                    {
                        evidence["identity_card_id"],
                        evidence["occluded_card_id"],
                        evidence["block_card_id"],
                    }
                )
                != 1
                or evidence["selected_style_count"] != 3
                or evidence["selected_slot_count"] != 6
                or evidence["candidate_count"] != 6
                or evidence["loaded_image_count"] != 6
                or evidence["reference_thumbnail_count"] < 6
                or not evidence["cost_confirmation"]
                or evidence["maximum_estimated_cost_usd"] != "0.72"
                or evidence["publishable"] is not False
                or evidence["restored_run_id"] != evidence["run_id"]
                or evidence["restored_candidate_count"] != 6
                or evidence["restored_loaded_image_count"] != 6
                or not evidence["restored_publishable_false"]
                or any(
                    len(row["slots"]) != 2
                    or any(
                        cell["provider_product_attachment_count"] != 1
                        or not 2
                        <= cell["provider_style_reference_attachment_count"]
                        <= 4
                        or len(cell["candidates"]) != 1
                        for cell in row["slots"]
                    )
                    for row in evidence["matrix"]
                )
                or real_provider_calls != 12
                or len(mock_provider.calls) != 12
                or len(database_batch_evidence) != 6
                or any(
                    len(row["source_content_sha256"]) != 64
                    or len(row["style_reference_manifest_sha256"]) != 64
                    or len(row["candidate_content_sha256"]) != 64
                    or row["outbound_attempts"] != 1
                    or row["item_state"] != "succeeded"
                    or row["job_status"] != "machine_check"
                    or row["sequence_number"] != 1
                    or row["fidelity_state"] is not None
                    for row in database_batch_evidence
                )
                or len(
                    {
                        row["style_reference_manifest_sha256"]
                        for row in database_batch_evidence
                    }
                )
                != 3
                or len(
                    {
                        row["candidate_content_sha256"]
                        for row in database_batch_evidence
                    }
                )
                != 6
            ):
                raise RuntimeError(f"browser acceptance mismatch: {evidence}")
            evidence["matrix"] = _strip_access_urls(evidence["matrix"])
            evidence["recorded_at"] = datetime.now(UTC).isoformat()
            harness_git_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            evidence["git_sha"] = harness_git_sha
            evidence["harness_git_sha"] = harness_git_sha
            evidence["external_network_allowed"] = False
            evidence["mock_provider_call_count"] = len(mock_provider.calls)
            evidence["mock_provider_paths"] = mock_provider.calls
            output = ROOT / "docs/verification/style-replay-suite-browser-run.json"
            output.write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(
                f"PASS demo_run_id={evidence['demo_run_id']} "
                f"real_run_id={evidence['real_run_id']} "
                "demo_images=3 demo_provider_calls=0 "
                "real_images=6 real_provider_calls=12 identity_gate=pass/review/pass/block "
                f"url={evidence['url']}"
            )
        finally:
            for process in (chrome, server):
                if process.poll() is None:
                    process.terminate()
            for process in (chrome, server):
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

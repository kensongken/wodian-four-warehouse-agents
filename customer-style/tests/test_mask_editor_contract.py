"""Executable Node contracts for deterministic browser mask editing."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from opc_style.domain.product_foreground import (
    mask_edit_manifest_sha256,
    mask_pixels_sha256,
)
from opc_style.web.mask_editor_contract import MASK_EDITOR_CONTRACT_SCRIPT
from opc_style.web.style_suite_page import STYLE_SUITE_PAGE


def _node() -> str:
    executable = shutil.which("node")
    if executable is None:
        bundled = (
            Path.home()
            / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
        )
        if bundled.exists():
            executable = str(bundled)
    if executable is None:
        pytest.skip("Node is unavailable for executable mask-editor tests")
    return executable


def _run(scenario: str) -> Any:
    completed = subprocess.run(
        [_node(), "-e", f"{MASK_EDITOR_CONTRACT_SCRIPT}\n{scenario}"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_contract_is_embedded_in_the_served_page() -> None:
    contract = STYLE_SUITE_PAGE.split("/* mask-editor-contract:start */", 1)[1].split(
        "/* mask-editor-contract:end */", 1
    )[0]
    assert "maskEditorFloodErase" in contract
    assert "createMaskEditorHistory" in contract


def test_wand_uses_eight_neighbors_and_ignores_clicks_outside_mask() -> None:
    result = _run(
        """
const mask=new Uint8Array(25);
mask[1+1*5]=255; mask[2+2*5]=255; mask[3+3*5]=255; mask[4+1*5]=255;
const erased=maskEditorFloodErase(mask,5,5,1,1);
const outside=maskEditorFloodErase(mask,5,5,0,0);
console.log(JSON.stringify({
  changed:erased.changed,
  remaining:[...erased.mask].filter(Boolean).length,
  disconnected:erased.mask[4+1*5],
  outsideChanged:outside.changed,
  originalCount:[...mask].filter(Boolean).length
}));
"""
    )
    assert result == {
        "changed": True,
        "remaining": 1,
        "disconnected": 255,
        "outsideChanged": False,
        "originalCount": 4,
    }


def test_brush_erases_restores_and_interpolates_pointer_movement() -> None:
    result = _run(
        """
const full=new Uint8Array(9*5).fill(255);
const erased=maskEditorBrush(full,9,5,{x:1,y:2},{x:7,y:2},0,0);
const restored=maskEditorBrush(erased.mask,9,5,{x:3,y:2},{x:5,y:2},0,255);
console.log(JSON.stringify({
  erased:[...erased.mask.slice(2*9,3*9)],
  restored:[...restored.mask.slice(2*9,3*9)],
  changed:erased.changed&&restored.changed
}));
"""
    )
    assert result["erased"] == [255, 0, 0, 0, 0, 0, 0, 0, 255]
    assert result["restored"] == [255, 0, 0, 255, 255, 255, 0, 0, 255]
    assert result["changed"] is True


def test_source_point_accounts_for_css_size_zoom_pan_and_outside() -> None:
    result = _run(
        """
const rect={left:100,top:50,width:400,height:200};
const view={zoom:2,panX:20,panY:-10};
console.log(JSON.stringify({
  point:maskEditorSourcePoint(320,140,rect,1000,500,view),
  outside:maskEditorSourcePoint(90,40,rect,1000,500,view)
}));
"""
    )
    assert result["point"] == {"x": 250, "y": 125}
    assert result["outside"] is None


def test_history_supports_undo_redo_reset_branch_noop_and_fifty_step_cap() -> None:
    result = _run(
        """
const initial=new Uint8Array([0,0]);
const history=createMaskEditorHistory(initial,50);
const noop=history.push(new Uint8Array([0,0]));
history.push(new Uint8Array([255,0]));
history.push(new Uint8Array([255,255]));
const undo=[...history.undo()];
const redo=[...history.redo()];
history.undo();
history.push(new Uint8Array([0,255]));
const branchRedo=history.canRedo();
const resetChanged=history.reset();
const undoReset=[...history.undo()];
for(let i=0;i<60;i++) history.push(new Uint8Array([i%2?255:0,(i+1)%2?255:0]));
let undoCount=0; while(history.canUndo()){history.undo();undoCount++;}
console.log(JSON.stringify({noop,undo,redo,branchRedo,resetChanged,undoReset,undoCount}));
"""
    )
    assert result == {
        "noop": False,
        "undo": [255, 0],
        "redo": [255, 255],
        "branchRedo": False,
        "resetChanged": True,
        "undoReset": [0, 255],
        "undoCount": 50,
    }


def test_layer_set_keeps_component_edits_histories_and_drafts_isolated() -> None:
    result = _run(
        """
const overall=new Uint8Array(25);overall[12]=255;overall[13]=255;
const layers=createMaskEditorLayerSet(overall,['clasp','pendant']);
layers.select('clasp');
layers.commit(new Uint8Array([0,0,0,0,0,0,0,0,0,0,0,0,255,0,0,0,0,0,0,0,0,0,0,0,0]),'restore_stroke_count');
layers.select('pendant');
layers.commit(new Uint8Array([0,0,0,0,0,0,0,0,0,0,0,0,0,255,0,0,0,0,0,0,0,0,0,0,0]),'restore_stroke_count');
const snapshot=layers.snapshot();
const restored=createMaskEditorLayerSet(overall,['clasp','pendant']);
const restoredOk=restored.restore(snapshot);
restored.select('clasp');const clasp=[...restored.current().mask];
restored.select('pendant');const pendant=[...restored.current().mask];
restored.undo();const pendantAfterUndo=[...restored.current().mask];
restored.select('clasp');const claspAfterPendantUndo=[...restored.current().mask];
console.log(JSON.stringify({restoredOk,clasp,pendant,pendantAfterUndo,claspAfterPendantUndo,names:restored.names()}));
"""
    )
    assert result["restoredOk"] is True
    assert result["names"] == ["overall", "clasp", "pendant"]
    assert result["clasp"][12] == 255
    assert result["pendant"][13] == 255
    assert not any(result["pendantAfterUndo"])
    assert result["claspAfterPendantUndo"][12] == 255


def test_layer_set_can_reopen_saved_component_masks_as_each_layers_base() -> None:
    result = _run(
        """
const overall=new Uint8Array(25);overall[12]=255;overall[13]=255;
const clasp=new Uint8Array(25);clasp[12]=255;
const layers=createMaskEditorLayerSet(overall,['clasp'],50,{clasp});
layers.select('clasp');
const before=[...layers.current().mask];
const metrics=maskEditorMetrics(layers.current().baseMask,layers.current().mask,5,5);
const gate=maskEditorLayerSaveGate(layers,5,5,false,true);
console.log(JSON.stringify({before,metrics,gate}));
"""
    )
    assert result["before"][12] == 255
    assert result["metrics"]["unchanged"] is True
    assert result["gate"]["canSave"] is True


def test_layer_save_gate_requires_every_component_inside_overall() -> None:
    result = _run(
        """
const overall=new Uint8Array(25);overall[12]=255;overall[13]=255;
const missing=createMaskEditorLayerSet(overall,['clasp']);
const missingGate=maskEditorLayerSaveGate(missing,5,5,false,true);
missing.select('clasp');
const outside=new Uint8Array(25);outside[6]=255;missing.commit(outside,'restore_stroke_count');
const outsideGate=maskEditorLayerSaveGate(missing,5,5,false,true);
missing.undo();const inside=new Uint8Array(25);inside[12]=255;missing.commit(inside,'restore_stroke_count');
const validGate=maskEditorLayerSaveGate(missing,5,5,false,true);
console.log(JSON.stringify({missingGate,outsideGate,validGate}));
"""
    )
    assert result["missingGate"] == {
        "canSave": False,
        "layerName": "clasp",
        "message": "请完成搭扣图层",
    }
    assert result["outsideGate"] == {
        "canSave": False,
        "layerName": "clasp",
        "message": "搭扣不能超出整体商品边界",
    }
    assert result["validGate"] == {
        "canSave": True,
        "layerName": "overall",
        "message": "所有金标图层可以保存",
    }


def test_component_can_be_automatically_included_in_overall() -> None:
    result = _run(
        """
const overall=new Uint8Array(25);overall[12]=255;
const layers=createMaskEditorLayerSet(overall,['ear_hook']);
layers.select('ear_hook');
const hook=new Uint8Array(25);hook[6]=255;hook[12]=255;
layers.commit(hook,'restore_stroke_count');
const changed=layers.includeComponentsInOverall();
const gate=maskEditorLayerSaveGate(layers,5,5,false,true);
console.log(JSON.stringify({changed,overall:[...layers.get('overall').mask],gate,active:layers.activeName()}));
"""
    )
    assert result["changed"] is True
    assert result["overall"][6] == 255
    assert result["overall"][12] == 255
    assert result["active"] == "ear_hook"
    assert result["gate"] == {
        "canSave": True,
        "layerName": "overall",
        "message": "所有金标图层可以保存",
    }


def test_component_reset_removes_only_its_automatic_overall_contribution() -> None:
    result = _run(
        """
const overall=new Uint8Array(25);overall[12]=255;
const layers=createMaskEditorLayerSet(overall,['ear_hook']);
layers.select('ear_hook');
const hook=new Uint8Array(25);hook[6]=255;hook[12]=255;
layers.commit(hook,'restore_stroke_count');layers.includeComponentsInOverall();
const afterInclude=[...layers.get('overall').mask];
layers.reset();layers.includeComponentsInOverall();
const afterReset=[...layers.get('overall').mask];
console.log(JSON.stringify({afterInclude,afterReset,active:layers.activeName()}));
"""
    )
    assert result["afterInclude"][6] == 255
    assert result["afterReset"][6] == 0
    assert result["afterReset"][12] == 255
    assert result["active"] == "ear_hook"


def test_metrics_are_authoritative_for_components_edges_and_large_changes() -> None:
    result = _run(
        """
const base=new Uint8Array(100);
for(let y=2;y<8;y++)for(let x=2;x<8;x++)base[y*10+x]=255;
const edited=base.slice();
for(let y=2;y<5;y++)for(let x=2;x<5;x++)edited[y*10+x]=0;
edited[9*10+9]=255;
const metrics=maskEditorMetrics(base,edited,10,10);
console.log(JSON.stringify(metrics));
"""
    )
    assert result["addedPixelCount"] == 1
    assert result["deletedPixelCount"] == 9
    assert result["componentCount"] == 2
    assert result["touchesEdge"] is True
    assert result["empty"] is False
    assert result["full"] is False
    assert result["unchanged"] is False
    assert result["largeChangeWarning"] is True


def test_browser_encoder_emits_server_readable_single_channel_png() -> None:
    result = _run(
        """
(async()=>{
  const mask=new Uint8Array(64);
  for(let y=2;y<6;y++)for(let x=2;x<6;x++)mask[y*8+x]=255;
  const blob=await maskEditorMaskPngBlob(mask,8,8);
  const bytes=new Uint8Array(await blob.arrayBuffer());
  console.log(JSON.stringify({png:Buffer.from(bytes).toString('base64'),pixelDigest:await maskEditorSha256(mask)}));
})().catch(error=>{console.error(error);process.exit(1)});
"""
    )
    png = base64.b64decode(result["png"])
    with Image.open(BytesIO(png)) as image:
        image.load()
        assert image.mode == "L"
        assert image.size == (8, 8)
    assert mask_pixels_sha256(png, 8, 8) == result["pixelDigest"]


def test_browser_and_server_compute_the_same_edit_manifest() -> None:
    payload = {
        "asset_id": "00000000-0000-0000-0000-000000000002",
        "base_manifest_sha256": "b" * 64,
        "base_mask_sha256": "c" * 64,
        "base_version_id": "00000000-0000-0000-0000-000000000003",
        "customer_id": "00000000-0000-0000-0000-000000000001",
        "editor_version": "human-mask-editor-v1",
        "erase_stroke_count": 2,
        "magic_wand_count": 1,
        "restore_stroke_count": 3,
        "result_mask_pixels_sha256": "d" * 64,
        "source_content_sha256": "a" * 64,
    }
    browser = _run(
        f"maskEditorManifestSha256({json.dumps(payload)}).then(digest=>console.log(JSON.stringify(digest)));"
    )
    server = mask_edit_manifest_sha256(
        customer_id=payload["customer_id"],
        asset_id=payload["asset_id"],
        base_version_id=payload["base_version_id"],
        source_content_sha256=payload["source_content_sha256"],
        base_manifest_sha256=payload["base_manifest_sha256"],
        base_mask_sha256=payload["base_mask_sha256"],
        result_mask_pixels_sha256=payload["result_mask_pixels_sha256"],
        editor_version=payload["editor_version"],
        magic_wand_count=payload["magic_wand_count"],
        erase_stroke_count=payload["erase_stroke_count"],
        restore_stroke_count=payload["restore_stroke_count"],
    )
    assert browser == server

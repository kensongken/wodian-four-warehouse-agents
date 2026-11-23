"""Browser-page contracts for the recoverable local foreground mask editor."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

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
        pytest.skip("Node is unavailable for executable mask-editor page tests")
    return executable


def _ui_contract() -> str:
    mask_contract = STYLE_SUITE_PAGE.split(
        "/* mask-editor-contract:start */", 1
    )[1].split("/* mask-editor-contract:end */", 1)[0]
    ui_contract = STYLE_SUITE_PAGE.split(
        "/* mask-editor-ui-contract:start */", 1
    )[1].split("/* mask-editor-ui-contract:end */", 1)[0]
    return f"{mask_contract}\n{ui_contract}"


def _run(scenario: str) -> Any:
    completed = subprocess.run(
        [_node(), "-e", f"{_ui_contract()}\n{scenario}"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_editor_markup_has_scoped_accessible_tools_and_keeps_generation_gated() -> None:
    for fragment in (
        'id="edit-foreground-mask"',
        '>修正遮罩<',
        'id="mask-editor"',
        'id="mask-editor-canvas"',
        'data-mask-tool="wand"',
        'data-mask-tool="erase"',
        'data-mask-tool="restore"',
        'data-mask-view="outline"',
        'data-mask-view="cutout"',
        'data-mask-view="fill"',
        'id="mask-brush-size"',
        'id="mask-zoom"',
        'id="mask-pan"',
        'id="mask-undo"',
        'id="mask-redo"',
        'id="mask-reset"',
        'id="mask-save"',
        'id="mask-editor-status"',
        'id="mask-editor-layers"',
        'id="mask-editor-layer-name"',
    ):
        assert fragment in STYLE_SUITE_PAGE
    assert "confirmedForeground=undefined" in STYLE_SUITE_PAGE
    assert "updatePilotEnabled()" in STYLE_SUITE_PAGE
    assert "maskEditorViewMode='outline'" in STYLE_SUITE_PAGE
    assert "context.globalCompositeOperation='destination-in'" in STYLE_SUITE_PAGE


def test_workbench_component_masks_are_drawn_as_layers_not_uploaded_files() -> None:
    assert 'id="mask-component-files"' not in STYLE_SUITE_PAGE
    assert "createMaskEditorLayerSet" in STYLE_SUITE_PAGE
    assert "maskEditorSelectLayer" in STYLE_SUITE_PAGE
    assert "componentBlobs" in STYLE_SUITE_PAGE
    assert "form.append('component_masks',componentBlobs.get(name)" in STYLE_SUITE_PAGE
    assert "form.append('component_names',name)" in STYLE_SUITE_PAGE
    assert "kind=component&component_name=" in STYLE_SUITE_PAGE
    assert "componentBaseMasks" in STYLE_SUITE_PAGE


def test_save_gate_rejects_invalid_masks_and_surfaces_large_change_warning() -> None:
    result = _run(
        """
const valid={empty:false,full:false,unchanged:false,touchesEdge:false,largeChangeWarning:false};
const cases={
  valid:maskEditorSaveGate(valid,false),
  reviewedProposal:maskEditorSaveGate({...valid,unchanged:true},false,true),
  empty:maskEditorSaveGate({...valid,empty:true},false),
  full:maskEditorSaveGate({...valid,full:true},false),
  unchanged:maskEditorSaveGate({...valid,unchanged:true},false),
  edge:maskEditorSaveGate({...valid,touchesEdge:true},false),
  warning:maskEditorSaveGate({...valid,largeChangeWarning:true},false),
  busy:maskEditorSaveGate(valid,true)
};
console.log(JSON.stringify(cases));
"""
    )
    assert result["valid"] == {"canSave": True, "message": "有未保存修改"}
    assert result["reviewedProposal"] == {
        "canSave": True,
        "message": "机器轮廓未修改，可直接确认为整体金标",
    }
    assert result["warning"]["canSave"] is True
    assert "15%" in result["warning"]["message"]
    for case in ("empty", "full", "unchanged", "edge", "busy"):
        assert result[case]["canSave"] is False


def test_workbench_gold_can_accept_an_unchanged_proposal_without_fake_edits() -> None:
    assert "state.target.kind==='workbench-gold'&&Boolean(state.target.maskUrl)" in STYLE_SUITE_PAGE
    assert "maskEditorLayerSaveGate(layerSet,width,height,state.busy,allowUnchanged)" in STYLE_SUITE_PAGE
    assert "maskSave.textContent=state.target.kind==='workbench-gold'?'保存全部金标'" in STYLE_SUITE_PAGE
    assert ".mask-editor[hidden],.mask-editor-layers[hidden]{display:none}" in STYLE_SUITE_PAGE


def test_component_edits_are_automatically_included_in_overall_mask() -> None:
    assert "if(activeName!=='overall')layers.includeComponentsInOverall()" in STYLE_SUITE_PAGE
    assert "if(target.kind==='workbench-gold')layers.includeComponentsInOverall()" in STYLE_SUITE_PAGE
    assert "无需重复涂抹" in STYLE_SUITE_PAGE


def test_editor_initial_zoom_fits_large_source_inside_the_viewport() -> None:
    result = _run(
        """
console.log(JSON.stringify({
  square:maskEditorFitZoom(1024,1024,680,520),
  small:maskEditorFitZoom(400,300,680,520),
  tall:maskEditorFitZoom(800,1600,680,520)
}));
"""
    )
    assert result == {"square": 50, "small": 100, "tall": 32}
    assert 'id="mask-zoom" type="range" min="25"' in STYLE_SUITE_PAGE
    assert "maskZoom.value=String(maskEditorFitZoom" in STYLE_SUITE_PAGE
    assert ".mask-editor canvas{margin-inline:auto}" in STYLE_SUITE_PAGE


def test_multipart_save_contains_only_mask_and_bound_edit_metadata() -> None:
    for field in (
        "mask",
        "base_version_id",
        "base_manifest_sha256",
        "edit_manifest_sha256",
        "editor_version",
        "idempotency_key",
        "magic_wand_count",
        "erase_stroke_count",
        "restore_stroke_count",
    ):
        assert f"form.append('{field}'" in STYLE_SUITE_PAGE
    assert "form.append('original'" not in STYLE_SUITE_PAGE
    assert "form.append('foreground'" not in STYLE_SUITE_PAGE
    assert "human-mask-editor-v1" in STYLE_SUITE_PAGE


def test_saved_response_is_session_scoped_and_requires_fresh_confirmation() -> None:
    result = _run(
        """
const left=maskEditorSavedState({sessionId:'left',version:null},'left',{id:'v2',version:2});
const right=maskEditorSavedState({sessionId:'right',version:null},'right',{id:'v3',version:3});
const stale=maskEditorSavedState({sessionId:'left',version:null},'other',{id:'wrong'});
console.log(JSON.stringify({left,right,stale}));
"""
    )
    assert result["left"]["version"]["id"] == "v2"
    assert result["right"]["version"]["id"] == "v3"
    assert result["left"]["confirmed"] is False
    assert result["left"]["generationLocked"] is True
    assert result["stale"] is None
    assert "renderForegroundVersion(saved)" in STYLE_SUITE_PAGE
    assert "foregroundConfirmation.checked=false" in STYLE_SUITE_PAGE
    assert "versions.find(item=>item.state!=='rejected')" in STYLE_SUITE_PAGE


def test_drafts_restore_only_exact_scope_and_storage_failure_keeps_edit_usable() -> None:
    result = _run(
        """
const scope={customerId:'c',assetId:'a',baseVersionId:'v1',sourceSha256:'s',baseMaskSha256:'m',editorVersion:'human-mask-editor-v1'};
const exact={scope:{...scope},mask:[0,255]};
const mismatch={scope:{...scope,baseMaskSha256:'other'},mask:[0,255]};
const failure=maskEditorDraftFailureState({mask:[0,255],canSave:true});
console.log(JSON.stringify({
  key:maskEditorDraftKey(scope),
  exact:maskEditorDraftMatches(exact,scope),
  mismatch:maskEditorDraftMatches(mismatch,scope),
  failure
}));
"""
    )
    assert result["exact"] is True
    assert result["mismatch"] is False
    assert all(value in result["key"] for value in ("c", "a", "v1", "s", "m"))
    assert result["failure"]["mask"] == [0, 255]
    assert result["failure"]["canSave"] is True
    assert "刷新后无法恢复" in result["failure"]["warning"]
    for function_name in (
        "openMaskEditorDraftDatabase",
        "readMaskEditorDraft",
        "writeMaskEditorDraft",
        "deleteMaskEditorDraft",
    ):
        assert function_name in STYLE_SUITE_PAGE


def test_pointer_cancel_ends_stroke_and_touch_drawing_cannot_scroll_page() -> None:
    assert "touch-action:none" in STYLE_SUITE_PAGE
    assert "onpointercancel=endMaskEditorStroke" in STYLE_SUITE_PAGE
    assert "setPointerCapture" in STYLE_SUITE_PAGE
    assert "event.preventDefault()" in STYLE_SUITE_PAGE


def test_complete_inline_page_script_has_valid_javascript_syntax() -> None:
    script = STYLE_SUITE_PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
    completed = subprocess.run(
        [_node(), "--check", "-"],
        input=script,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr

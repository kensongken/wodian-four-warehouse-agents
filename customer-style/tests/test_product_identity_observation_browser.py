"""Browser contract for the reusable product-identity observation gate."""

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
        pytest.skip("Node is unavailable for executable browser contract tests")
    return executable


def _contract() -> str:
    return STYLE_SUITE_PAGE.split(
        "/* identity-observation-state-contract:start */", 1
    )[1].split("/* identity-observation-state-contract:end */", 1)[0]


def _run(scenario: str) -> Any:
    completed = subprocess.run(
        [_node(), "-e", f"{_contract()}\n{scenario}"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_page_has_reusable_structure_gate_and_paid_authority_controls() -> None:
    for fragment in (
        'id="identity-observation-panel"',
        'id="identity-observation-status"',
        'id="identity-observation-confirmation"',
        'id="run-identity-observation"',
        'id="identity-observation-review"',
        'id="identity-observation-review-reason"',
        'id="approve-identity-observation"',
        'id="reject-identity-observation"',
        "identity-observations/current",
        "identity-observations/preview",
        "identity-observation-runs",
        "identity-observation-runs/${identityObservationState.run.run_id}/reviews",
        "商品结构核对通过",
        "部分结构被遮挡，需要人工复核",
        "商品结构不一致，已阻止生成",
        "本次核对可能已计费，系统不会自动重试",
    ):
        assert fragment in STYLE_SUITE_PAGE


def test_card_source_photo_is_presented_as_free_local_verification() -> None:
    for fragment in (
        "原始身份卡来源图",
        "本地核对",
        "不发送给模型",
        "不收费",
        "quoted.input_image_count===0",
    ):
        assert fragment in STYLE_SUITE_PAGE


def test_same_product_new_photo_explicitly_reuses_prior_product_and_card() -> None:
    for fragment in (
        'id="reuse-product-identity"',
        "这是同一件商品的新照片",
        "reusableIdentityCardKey",
        "readReusableIdentityCard",
        "savedIdentity.product_id",
        "savedIdentity.card",
        "localStorage.setItem(reusableIdentityCardKey",
    ):
        assert fragment in STYLE_SUITE_PAGE
    assert "reuseProductIdentity.checked&&savedIdentity" in STYLE_SUITE_PAGE


def test_restored_confirmed_card_reenables_same_product_photo_reuse() -> None:
    restored_identity = STYLE_SUITE_PAGE.split(
        "async function loadIdentityState()", 1
    )[1].split("async function previewIdentityAnalysis()", 1)[0]

    assert "localStorage.setItem(reusableIdentityCardKey" in restored_identity
    assert "refreshReusableIdentityChoice()" in restored_identity


def test_observation_evidence_is_bound_into_batch_preview() -> None:
    assert (
        "product_identity_observation_run_id:identityObservationState.run.run_id"
        in STYLE_SUITE_PAGE
    )
    assert (
        "product_identity_observation_review_id:identityObservationState.review?"
        ".review_id||null" in STYLE_SUITE_PAGE
    )
    assert "identityObservationReady()" in STYLE_SUITE_PAGE
    assert "else if(!confirmedForeground)reason='" not in STYLE_SUITE_PAGE


def test_state_machine_rejects_stale_results_and_only_allows_valid_evidence() -> None:
    result = _run(
        """
let state=createIdentityObservationState(3,'asset-b','card-b');
const stale=reduceIdentityObservationState(state,{type:'run',generation:2,assetId:'asset-a',cardId:'card-a',run:{run_id:'stale',decision:'pass'}});
state=reduceIdentityObservationState(state,{type:'run',generation:3,assetId:'asset-b',cardId:'card-b',run:{run_id:'run-b',decision:'needs_review'}});
const beforeReview=identityObservationEvidenceReady(state,'asset-b','card-b');
state=reduceIdentityObservationState(state,{type:'review',generation:3,assetId:'asset-b',cardId:'card-b',review:{review_id:'review-b',decision:'approved'}});
console.log(JSON.stringify({staleSame:stale===state || stale.status==='not_checked',beforeReview,afterReview:identityObservationEvidenceReady(state,'asset-b','card-b'),wrongAsset:identityObservationEvidenceReady(state,'asset-a','card-b')}));
"""
    )
    assert result == {
        "staleSame": True,
        "beforeReview": False,
        "afterReview": True,
        "wrongAsset": False,
    }


def test_uncertain_state_is_locked_and_never_dispatchable() -> None:
    result = _run(
        """
let state=createIdentityObservationState(1,'asset-a','card-a');
state=reduceIdentityObservationState(state,{type:'uncertain',generation:1,assetId:'asset-a',cardId:'card-a'});
console.log(JSON.stringify({status:state.status,ready:identityObservationEvidenceReady(state,'asset-a','card-a'),retry:identityObservationCanDispatch(state)}));
"""
    )
    assert result == {"status": "uncertain", "ready": False, "retry": False}


def test_reload_reconciles_uncertain_state_with_server_audit() -> None:
    restore = STYLE_SUITE_PAGE.split(
        "async function loadCurrentIdentityObservation()", 1
    )[1].split("async function executeIdentityObservation()", 1)[0]
    execute = STYLE_SUITE_PAGE.split(
        "async function executeIdentityObservation()", 1
    )[1].split("async function reviewIdentityObservation", 1)[0]

    assert "identity-observations/current" in restore
    assert "localStorage.getItem(identityObservationUncertainKey())" not in restore
    assert "localStorage.setItem(identityObservationUncertainKey()" not in execute

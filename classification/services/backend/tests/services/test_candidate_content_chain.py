import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from product_engine.services.mall_candidate_imports import (
    MallCandidateImportFailure,
    build_mall_candidate_import,
)
from product_engine.services.product_passport_drafts import build_product_passport_draft
from product_engine.services.training_label_versions import (
    PriceRangeInput,
    ServiceRuleInput,
    TrainingLabelInput,
    TrainingLabelVersion,
    TrainingLabelVersionStore,
)


def complete_human_version(**updates) -> TrainingLabelVersion:
    values = {
        "sha256": "a" * 64,
        "main_image_template_code": "LOCAL-GREEN-BRACELET",
        "product_type": "first_sale_bead",
        "inventory_status": "in_stock",
        "material_label": "绿松",
        "product_form": "手串",
        "visual_observations": ["绿色调明显", "珠体排列成环"],
        "selling_points": ["近景实拍展示珠体整体轮廓"],
        "specification_note": "珠径 5 mm；尺寸由训练员测量",
        "quality_note": "图片可见颜色分布较均匀，仅为观察记录",
        "defect_note": "图片可见轻微表面纹理",
        "price_range": PriceRangeInput(
            minimum="1200",
            maximum="1500",
            basis="训练员录入的同品类价格标准",
        ),
        "service_rules": [
            ServiceRuleInput(
                service_code="scan_identity",
                eligibility="购买后可用",
                customer_summary="购买后可扫码进入身份与服务页面",
            )
        ],
        "version_id": uuid4(),
        "template_code": "LOCAL-GREEN-BRACELET",
        "version": 3,
        "previous_version_id": uuid4(),
        "rollback_target_version_id": None,
        "change_source": "trainer_confirmed",
        "material_evidence": "trainer_confirmed",
        "created_at": datetime(2026, 8, 19, 9, 0, tzinfo=UTC),
        "can_rollback": True,
    }
    values.update(updates)
    return TrainingLabelVersion.model_validate(values)


def test_complete_human_edit_builds_candidate_content_package() -> None:
    draft = build_product_passport_draft(complete_human_version())

    candidate = build_mall_candidate_import(draft)

    assert candidate.main_image.model_dump() == {
        "template_code": "LOCAL-GREEN-BRACELET",
        "role": "main",
        "source": "album_classified_image",
        "review_state": "human_confirmed",
    }
    assert candidate.price_advisory.model_dump() == {
        "currency": "CNY",
        "minimum": "1200",
        "maximum": "1500",
        "basis": "训练员录入的同品类价格标准",
        "semantics": "advisory_range_not_fixed_price",
    }
    assert candidate.approved_attributes.selling_points == ["近景实拍展示珠体整体轮廓"]
    assert candidate.candidate_only is True
    assert candidate.automatic_publication_allowed is False
    assert candidate.verification_boundaries.authenticity == "not_verified"
    assert candidate.verification_boundaries.origin == "not_verified"
    assert candidate.verification_boundaries.quality == "not_verified"


def test_removing_main_image_from_new_human_version_rejects_candidate_package() -> None:
    draft = build_product_passport_draft(
        complete_human_version(main_image_template_code=None, version=4)
    )

    assert [blocker.code for blocker in draft.blockers] == ["main_image_missing"]
    with pytest.raises(MallCandidateImportFailure) as error:
        build_mall_candidate_import(draft)

    assert error.value.remaining_blockers == ["main_image_missing"]


def test_main_image_reference_must_belong_to_current_album_product() -> None:
    draft = build_product_passport_draft(
        complete_human_version(main_image_template_code="LOCAL-OTHER-PRODUCT")
    )

    assert [blocker.code for blocker in draft.blockers] == ["main_image_mismatch"]
    with pytest.raises(MallCandidateImportFailure) as error:
        build_mall_candidate_import(draft)

    assert error.value.remaining_blockers == ["main_image_mismatch"]


def test_exact_image_label_reuse_does_not_reuse_product_main_image(tmp_path) -> None:
    initial_path = tmp_path / "training-labels.jsonl"
    initial_path.write_text(
        json.dumps(
            {
                "template_code": "LOCAL-GREEN",
                "sha256": "a" * 64,
                "material_label": "绿松",
                "product_form": "手串",
                "visual_observations": ["绿色调明显"],
                "selling_points": ["近景实拍展示珠体轮廓"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    store = TrainingLabelVersionStore(
        tmp_path / "training-label-versions.jsonl",
        initial_path,
    )
    source = store.save(
        "LOCAL-GREEN",
        1,
        TrainingLabelInput(
            sha256="a" * 64,
            main_image_template_code="LOCAL-GREEN",
            material_label="绿松",
            product_form="手串",
            visual_observations=["绿色调明显"],
            selling_points=["近景实拍展示珠体轮廓"],
        ),
    )

    reused = store.register_initial(
        "LOCAL-GREEN-COPY",
        TrainingLabelInput(
            sha256="a" * 64,
            material_label="待确认",
            product_form="待确认",
            visual_observations=["待训练员确认图片特征"],
            selling_points=["待训练员补充销售卖点"],
        ),
    )

    assert source.main_image_template_code == "LOCAL-GREEN"
    assert reused.match_method == "exact_sha256"
    assert reused.main_image_template_code is None


def test_synthetic_candidate_evidence_fixture_matches_runtime_chain() -> None:
    candidate = build_mall_candidate_import(build_product_passport_draft(complete_human_version()))
    rejected_draft = build_product_passport_draft(
        complete_human_version(main_image_template_code=None, version=4)
    )
    with pytest.raises(MallCandidateImportFailure) as error:
        build_mall_candidate_import(rejected_draft)

    actual = {
        "evidence_scope": "synthetic_technical_readiness_only",
        "positive_chain": {
            "main_image": candidate.main_image.model_dump(),
            "price_advisory": candidate.price_advisory.model_dump(),
            "selling_point_candidates": candidate.approved_attributes.selling_points,
            "candidate_only": candidate.candidate_only,
            "automatic_publication_allowed": candidate.automatic_publication_allowed,
            "verification_boundaries": candidate.verification_boundaries.model_dump(),
        },
        "negative_chain": {
            "remaining_blockers": error.value.remaining_blockers,
            "candidate_import_allowed": False,
        },
    }
    fixture_path = (
        Path(__file__).resolve().parents[4] / "fixtures/v0.1/candidate-content-chain-proof.json"
    )

    assert actual == json.loads(fixture_path.read_text(encoding="utf-8"))

import json

import pytest

from product_engine.services.training_label_versions import (
    PriceRangeInput,
    ServiceRuleInput,
    TrainingLabelFailure,
    TrainingLabelInput,
    TrainingLabelVersionStore,
)


def make_store(tmp_path):
    initial_path = tmp_path / "training-labels.jsonl"
    initial_path.write_text(
        json.dumps(
            {
                "template_code": "LOCAL-GREEN",
                "sha256": "a" * 64,
                "material_label": "绿松",
                "product_form": "方圆珠手串",
                "visual_observations": ["绿色调明显"],
                "selling_points": ["方圆珠造型饱满"],
                "price_status": "pending_standard",
                "price_reason": "待录入绿松价格标准",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    version_path = tmp_path / "training-label-versions.jsonl"
    return TrainingLabelVersionStore(version_path, initial_path), version_path, initial_path


def changed_values(**updates) -> TrainingLabelInput:
    values = {
        "sha256": "a" * 64,
        "material_label": "绿松",
        "product_form": "方圆珠手串",
        "visual_observations": ["绿色调明显", "表面高光清晰"],
        "selling_points": ["近景实拍展示表面光泽"],
        "specification_note": "尺寸待测量",
        "quality_note": "仅为训练员观察备注",
        "defect_note": "可见轻微表面纹理",
        "price_range": None,
    }
    values.update(updates)
    return TrainingLabelInput(**values)


def test_existing_green_label_is_mapped_to_version_one(tmp_path) -> None:
    store, _, _ = make_store(tmp_path)

    current = store.current("LOCAL-GREEN")

    assert current is not None
    assert current.version == 1
    assert current.material_label == "绿松"
    assert current.training_state == "label_confirmed_model_not_trained"
    assert current.quality_verification == "not_verified"
    assert current.warehouse_verification == "pending_warehouse_verification"


def test_exact_suggested_price_is_saved_as_a_single_value_range(tmp_path) -> None:
    store, _, _ = make_store(tmp_path)

    saved = store.save(
        "LOCAL-GREEN",
        1,
        changed_values(
            price_range=PriceRangeInput(
                minimum="241",
                maximum="241",
                basis="训练员输入的内部建议价",
            )
        ),
    )

    assert saved.price_range is not None
    assert saved.price_range.minimum == "241.00"
    assert saved.price_range.maximum == "241.00"


def test_specific_historical_version_can_be_read_for_authoritative_review(tmp_path) -> None:
    store, _, _ = make_store(tmp_path)
    second = store.save("LOCAL-GREEN", 1, changed_values())

    first = store.version("LOCAL-GREEN", 1)
    historical_second = store.version("LOCAL-GREEN", 2)
    missing = store.version("LOCAL-GREEN", 99)

    assert first is not None
    assert first.version == 1
    assert historical_second == second
    assert missing is None


def test_duplicate_historical_version_fails_closed(tmp_path) -> None:
    store, version_path, _ = make_store(tmp_path)
    second = store.save("LOCAL-GREEN", 1, changed_values())
    duplicate = second.model_copy(update={"quality_note": "不同的同版内容"})
    with version_path.open("a", encoding="utf-8") as stream:
        stream.write(duplicate.model_dump_json() + "\n")

    with pytest.raises(TrainingLabelFailure, match="duplicate_training_version"):
        store.version("LOCAL-GREEN", 2)


def test_new_template_reuses_confirmed_label_for_exact_sha256(tmp_path) -> None:
    store, _, _ = make_store(tmp_path)

    reused = store.register_initial(
        "LOCAL-GREEN-COPY",
        changed_values(
            material_label="待确认",
            product_form="待确认",
            visual_observations=["待训练员确认图片特征"],
            selling_points=["待训练员补充销售卖点"],
        ),
    )

    assert reused.template_code == "LOCAL-GREEN-COPY"
    assert reused.version == 1
    assert reused.material_label == "绿松"
    assert reused.product_form == "方圆珠手串"
    assert reused.match_method == "exact_sha256"
    assert reused.matched_from_template_code == "LOCAL-GREEN"
    assert reused.training_state == "label_confirmed_model_not_trained"
    assert reused.authenticity_verification == "not_verified"


def test_exact_match_never_reuses_business_product_type(tmp_path) -> None:
    store, _, _ = make_store(tmp_path)
    source = store.save(
        "LOCAL-GREEN",
        1,
        changed_values(product_type="first_sale_bead"),
    )

    reused = store.register_initial(
        "LOCAL-GREEN-COPY",
        changed_values(
            product_type=None,
            material_label="待确认",
            product_form="待确认",
            visual_observations=["待训练员确认图片特征"],
            selling_points=["待训练员补充销售卖点"],
        ),
    )

    assert source.product_type == "first_sale_bead"
    assert reused.match_method == "exact_sha256"
    assert reused.product_type is None


def test_exact_match_never_reuses_main_image_selection(tmp_path) -> None:
    store, _, _ = make_store(tmp_path)
    source = store.save(
        "LOCAL-GREEN",
        1,
        changed_values(main_image_template_code="LOCAL-GREEN"),
    )

    reused = store.register_initial(
        "LOCAL-GREEN-COPY",
        changed_values(
            main_image_template_code=None,
            material_label="待确认",
            product_form="待确认",
            visual_observations=["待训练员确认图片特征"],
            selling_points=["待训练员补充销售卖点"],
        ),
    )

    assert source.main_image_template_code == "LOCAL-GREEN"
    assert reused.match_method == "exact_sha256"
    assert reused.main_image_template_code is None


def test_exact_match_never_reuses_inventory_status(tmp_path) -> None:
    store, _, _ = make_store(tmp_path)
    source = store.save(
        "LOCAL-GREEN",
        1,
        changed_values(inventory_status="in_stock"),
    )

    reused = store.register_initial(
        "LOCAL-GREEN-COPY",
        changed_values(
            inventory_status=None,
            material_label="待确认",
            product_form="待确认",
            visual_observations=["待训练员确认图片特征"],
            selling_points=["待训练员补充销售卖点"],
        ),
    )

    assert source.inventory_status == "in_stock"
    assert reused.match_method == "exact_sha256"
    assert reused.inventory_status is None


def test_exact_match_never_reuses_merchant_service_rules(tmp_path) -> None:
    store, _, _ = make_store(tmp_path)
    source = store.save(
        "LOCAL-GREEN",
        1,
        changed_values(
            service_rules=[
                ServiceRuleInput(
                    service_code="scan_identity",
                    eligibility="购买后可用",
                    customer_summary="购买后可扫码进入身份与服务页面",
                )
            ]
        ),
    )

    reused = store.register_initial(
        "LOCAL-GREEN-COPY",
        changed_values(
            material_label="待确认",
            product_form="待确认",
            visual_observations=["待训练员确认图片特征"],
            selling_points=["待训练员补充销售卖点"],
        ),
    )

    assert len(source.service_rules) == 1
    assert reused.match_method == "exact_sha256"
    assert reused.service_rules == []


def test_system_placeholder_is_not_reused_for_exact_sha256(tmp_path) -> None:
    store = TrainingLabelVersionStore(
        tmp_path / "training-label-versions.jsonl",
        tmp_path / "training-labels.jsonl",
    )
    placeholder = changed_values(
        material_label="待确认",
        product_form="待确认",
        visual_observations=["待训练员确认图片特征"],
        selling_points=["待训练员补充销售卖点"],
    )
    store.register_initial("LOCAL-PENDING-ONE", placeholder)

    second = store.register_initial("LOCAL-PENDING-TWO", placeholder)

    assert second.material_label == "待确认"
    assert second.match_method is None
    assert second.matched_from_template_code is None


def test_exact_match_reuses_confirmed_material_while_form_remains_pending(tmp_path) -> None:
    initial_path = tmp_path / "training-labels.jsonl"
    initial_path.write_text(
        json.dumps(
            {
                "template_code": "LOCAL-PARTIAL-GREEN",
                "sha256": "c" * 64,
                "material_label": "绿松",
                "product_form": "待确认",
                "visual_observations": ["方圆珠手串形态"],
                "selling_points": ["近景实拍展示表面光泽"],
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

    reused = store.register_initial(
        "LOCAL-PARTIAL-GREEN-COPY",
        changed_values(
            sha256="c" * 64,
            material_label="待确认",
            product_form="待确认",
            visual_observations=["待训练员确认图片特征"],
            selling_points=["待训练员补充销售卖点"],
        ),
    )

    assert reused.material_label == "绿松"
    assert reused.product_form == "待确认"
    assert reused.match_method == "exact_sha256"
    assert reused.matched_from_template_code == "LOCAL-PARTIAL-GREEN"


def test_backfill_upgrades_old_system_placeholder_without_overwriting_history(
    tmp_path,
) -> None:
    version_path = tmp_path / "training-label-versions.jsonl"
    initial_path = tmp_path / "training-labels.jsonl"
    store = TrainingLabelVersionStore(version_path, initial_path)
    placeholder = changed_values(
        sha256="d" * 64,
        material_label="待确认",
        product_form="待确认",
        visual_observations=["待训练员确认图片特征"],
        selling_points=["待训练员补充销售卖点"],
    )
    old_pending = store.register_initial("LOCAL-OLD-PENDING", placeholder)
    with initial_path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "template_code": "LOCAL-CONFIRMED-LATER",
                    "sha256": "d" * 64,
                    "material_label": "绿松",
                    "product_form": "待确认",
                    "visual_observations": ["方圆珠手串形态"],
                    "selling_points": ["绿色调醒目"],
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    backfilled = TrainingLabelVersionStore(version_path, initial_path).backfill_exact_matches()
    reloaded = TrainingLabelVersionStore(version_path, initial_path)
    upgraded = reloaded.current("LOCAL-OLD-PENDING")

    assert old_pending.version == 1
    assert len(backfilled) == 1
    assert upgraded is not None
    assert upgraded.version == 2
    assert upgraded.previous_version_id == old_pending.version_id
    assert upgraded.material_label == "绿松"
    assert upgraded.product_form == "待确认"
    assert upgraded.change_source == "exact_match_reused"
    assert upgraded.matched_from_template_code == "LOCAL-CONFIRMED-LATER"
    assert upgraded.can_rollback is True
    rolled_back = reloaded.rollback("LOCAL-OLD-PENDING", upgraded.version)
    assert rolled_back.version == 3
    assert rolled_back.material_label == "待确认"


def test_backfill_reads_version_history_only_once(tmp_path, monkeypatch) -> None:
    version_path = tmp_path / "training-label-versions.jsonl"
    initial_path = tmp_path / "training-labels.jsonl"
    store = TrainingLabelVersionStore(version_path, initial_path)
    store.register_initial(
        "LOCAL-PENDING",
        changed_values(
            sha256="e" * 64,
            material_label="待确认",
            product_form="待确认",
            visual_observations=["待训练员确认图片特征"],
            selling_points=["待训练员补充销售卖点"],
        ),
    )
    store.register_initial(
        "LOCAL-CONFIRMED",
        changed_values(sha256="e" * 64),
        source="merchant_imported",
    )
    original_read_versions = store._read_versions
    read_count = 0

    def counted_read_versions(template_code=None):
        nonlocal read_count
        read_count += 1
        return original_read_versions(template_code)

    monkeypatch.setattr(store, "_read_versions", counted_read_versions)

    backfilled = store.backfill_exact_matches()

    assert len(backfilled) == 1
    assert read_count == 1


def test_save_appends_version_without_overwriting_initial_record(tmp_path) -> None:
    store, version_path, initial_path = make_store(tmp_path)
    before = initial_path.read_text(encoding="utf-8")

    saved = store.save("LOCAL-GREEN", 1, changed_values())

    assert saved.version == 2
    assert saved.change_source == "trainer_confirmed"
    assert saved.can_rollback is True
    assert initial_path.read_text(encoding="utf-8") == before
    assert len(version_path.read_text(encoding="utf-8").splitlines()) == 2


@pytest.mark.parametrize(
    "product_type",
    ["first_sale_bead", "legacy_material", "redesign_output"],
)
def test_product_type_is_versioned_and_rollback_restores_previous_value(
    tmp_path,
    product_type,
) -> None:
    store, _, _ = make_store(tmp_path)

    saved = store.save(
        "LOCAL-GREEN",
        1,
        changed_values(product_type=product_type),
    )
    rolled_back = store.rollback("LOCAL-GREEN", saved.version)

    assert saved.product_type == product_type
    assert rolled_back.product_type is None


@pytest.mark.parametrize(
    "inventory_status",
    ["in_stock", "reserved", "out_of_stock"],
)
def test_inventory_status_is_versioned_and_rollback_restores_previous_value(
    tmp_path,
    inventory_status,
) -> None:
    store, _, _ = make_store(tmp_path)

    saved = store.save(
        "LOCAL-GREEN",
        1,
        changed_values(inventory_status=inventory_status),
    )
    rolled_back = store.rollback("LOCAL-GREEN", saved.version)

    assert saved.inventory_status == inventory_status
    assert rolled_back.inventory_status is None


def test_service_rules_are_versioned_and_rollback_restores_empty_list(tmp_path) -> None:
    store, _, _ = make_store(tmp_path)
    rule = ServiceRuleInput(
        service_code="scan_identity",
        eligibility="购买后可用",
        customer_summary="购买后可扫码进入身份与服务页面",
    )

    saved = store.save("LOCAL-GREEN", 1, changed_values(service_rules=[rule]))
    rolled_back = store.rollback("LOCAL-GREEN", saved.version)

    assert saved.service_rules == [rule]
    assert rolled_back.service_rules == []


def test_stale_base_version_does_not_append(tmp_path) -> None:
    store, version_path, _ = make_store(tmp_path)
    store.save("LOCAL-GREEN", 1, changed_values())
    before = version_path.read_bytes()

    with pytest.raises(TrainingLabelFailure, match="version_conflict"):
        store.save("LOCAL-GREEN", 1, changed_values(material_label="绿松石"))

    assert version_path.read_bytes() == before


@pytest.mark.parametrize(
    ("updates", "error_code"),
    [
        ({"material_label": "  "}, "required_field_missing"),
        ({"selling_points": ["保证升值"]}, "forbidden_unsupported_claim"),
        (
            {
                "service_rules": [
                    ServiceRuleInput(
                        service_code="scan_identity",
                        eligibility="购买后可用",
                        customer_summary="保证升值",
                    )
                ]
            },
            "forbidden_unsupported_claim",
        ),
        (
            {
                "service_rules": [
                    ServiceRuleInput(
                        service_code="scan_identity",
                        eligibility="购买后可用",
                        customer_summary=" ",
                    )
                ]
            },
            "invalid_service_rule",
        ),
        (
            {
                "price_range": PriceRangeInput(
                    currency="CNY",
                    minimum="1200.00",
                    maximum="1199.00",
                    basis="训练员价格标准",
                )
            },
            "invalid_price_range",
        ),
    ],
)
def test_invalid_training_label_does_not_append(tmp_path, updates, error_code) -> None:
    store, version_path, _ = make_store(tmp_path)

    with pytest.raises(TrainingLabelFailure, match=error_code):
        store.save("LOCAL-GREEN", 1, changed_values(**updates))

    assert not version_path.exists()


def test_rollback_appends_a_new_version_with_previous_content(tmp_path) -> None:
    store, version_path, _ = make_store(tmp_path)
    version_one = store.current("LOCAL-GREEN")
    version_two = store.save("LOCAL-GREEN", 1, changed_values(material_label="绿松石"))

    rolled_back = store.rollback("LOCAL-GREEN", version_two.version)

    assert version_one is not None
    assert rolled_back.version == 3
    assert rolled_back.change_source == "trainer_rollback"
    assert rolled_back.previous_version_id == version_two.version_id
    assert rolled_back.rollback_target_version_id == version_one.version_id
    assert rolled_back.material_label == version_one.material_label
    assert len(version_path.read_text(encoding="utf-8").splitlines()) == 3


def test_rollback_with_only_one_version_is_rejected(tmp_path) -> None:
    store, version_path, _ = make_store(tmp_path)

    with pytest.raises(TrainingLabelFailure, match="no_previous_version"):
        store.rollback("LOCAL-GREEN", 1)

    assert not version_path.exists()

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session


def test_migration_creates_mall_style_receipt_without_secret_or_image_bytes(
    migrated_session: Session,
) -> None:
    inspector = inspect(migrated_session.bind)
    assert {
        "mall_style_handoff_receipts",
        "mall_style_source_asset_bindings",
    }.issubset(inspector.get_table_names())

    receipt_columns = {
        column["name"]
        for column in inspector.get_columns("mall_style_handoff_receipts")
    }
    assert {
        "merchant_id",
        "opc_id",
        "product_entry_submission_id",
        "product_entry_draft_version",
        "product_entry_draft_sha256",
        "checked_payload_sha256",
        "status",
        "generation_job_id",
        "publishable",
    }.issubset(receipt_columns)
    assert "signing_secret" not in receipt_columns
    assert "image_bytes" not in receipt_columns

    migrated_session.execute(
        text(
            "INSERT INTO mall_style_handoff_receipts ("
            "id, handoff_id, merchant_id, opc_id, product_entry_submission_id, "
            "product_entry_draft_version, product_entry_draft_sha256, "
            "correlation_id, idempotency_key, checked_payload_sha256, status, "
            "scene_style_id, style_card_version_id, slot_key, candidate_count, "
            "maximum_cost_usd, generation_job_id, publishable, payload, "
            "received_at, updated_at) VALUES ("
            "'11111111-1111-4111-8111-111111111111', "
            "'22222222-2222-4222-8222-222222222222', "
            "'33333333-3333-4333-8333-333333333333', "
            "'44444444-4444-4444-8444-444444444444', "
            "'55555555-5555-4555-8555-555555555555', 1, :draft_digest, "
            "'correlation', 'mall-style:submission', :digest, "
            "'awaiting_product_photo', "
            "'66666666-6666-4666-8666-666666666666', "
            "'77777777-7777-4777-8777-777777777777', 'hero', 3, 0.36, "
            "NULL, 0, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {"digest": "a" * 64, "draft_digest": "9" * 64},
    )
    migrated_session.flush()


def test_results_migration_has_owner_confirmation_and_immutable_fee_facts(
    migrated_session: Session,
) -> None:
    inspector = inspect(migrated_session.bind)
    assert {
        "mall_style_subject_confirmations",
        "mall_style_generation_queue_items",
    }.issubset(inspector.get_table_names())
    confirmation = {
        row["name"] for row in inspector.get_columns("mall_style_subject_confirmations")
    }
    assert {
        "source_sha256",
        "base_mask_sha256",
        "reviewed_mask_sha256",
        "owner_actor_reference",
        "audit_event_id",
    } <= confirmation
    queue = {
        row["name"]
        for row in inspector.get_columns("mall_style_generation_queue_items")
    }
    assert {
        "provider",
        "model",
        "owner_actor_reference",
        "actor_role",
        "approval_audit_event_id",
        "approval_expires_at",
        "generation_job_id",
        "rights_permission_event_id",
    } <= queue
    assert not {"signing_secret", "image_bytes", "api_key"} & (queue | confirmation)
    checks = {
        row["name"]
        for row in inspector.get_check_constraints("mall_style_generation_queue_items")
    }
    assert {
        "ck_mall_style_generation_owner",
        "ck_mall_style_generation_not_publishable",
        "ck_mall_style_generation_status",
    } <= checks

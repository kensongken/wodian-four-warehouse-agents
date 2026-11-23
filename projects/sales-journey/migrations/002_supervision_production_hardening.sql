-- Additive production hardening. Never replace or re-run 001 in an existing database.
-- This rebuild deliberately fails if a legacy row lacks a trustworthy card binding;
-- operators must review/backfill or quarantine that row before retrying the migration.
-- D1 owns the migration transaction. Defer FK checks until its transaction finishes.

PRAGMA defer_foreign_keys = ON;

CREATE TABLE follow_ups_v2 (
  id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  customer_id TEXT NOT NULL,
  employee_user_id TEXT NOT NULL,
  idempotency_key TEXT,
  request_fingerprint TEXT,
  customer_message_vault_asset_id TEXT NOT NULL,
  customer_message_summary TEXT NOT NULL,
  planned_follow_up_at TEXT NOT NULL,
  actual_follow_up_at TEXT,
  result TEXT NOT NULL CHECK (result IN ('pending', 'interested', 'won', 'lost')),
  next_action TEXT NOT NULL,
  decision_card_id TEXT NOT NULL,
  card_instance_id TEXT NOT NULL CHECK (length(trim(card_instance_id)) > 0),
  reply_usage TEXT NOT NULL CHECK (reply_usage IN ('adopted', 'edited', 'not_used')),
  chosen_reply_id TEXT,
  final_edited_text TEXT,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, id, customer_id),
  CHECK (
    idempotency_key IS NULL OR (
      length(idempotency_key) = 69
      AND substr(idempotency_key, 1, 5) = 'idem_'
      AND substr(idempotency_key, 6) NOT GLOB '*[^0-9a-f]*'
    )
  ),
  CHECK (
    request_fingerprint IS NULL OR (
      length(request_fingerprint) = 67
      AND substr(request_fingerprint, 1, 3) = 'fp_'
      AND substr(request_fingerprint, 4) NOT GLOB '*[^0-9a-f]*'
    )
  ),
  FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id),
  FOREIGN KEY (tenant_id, employee_user_id) REFERENCES users (tenant_id, id),
  FOREIGN KEY (tenant_id, customer_message_vault_asset_id)
    REFERENCES vault_assets (tenant_id, id)
);

INSERT INTO follow_ups_v2 (
  id, tenant_id, customer_id, employee_user_id, idempotency_key,
  request_fingerprint, customer_message_vault_asset_id, customer_message_summary,
  planned_follow_up_at, actual_follow_up_at, result, next_action, decision_card_id,
  card_instance_id, reply_usage, chosen_reply_id, final_edited_text, version, created_at
)
SELECT
  id, tenant_id, customer_id, employee_user_id, idempotency_key,
  request_fingerprint, customer_message_vault_asset_id, customer_message_summary,
  planned_follow_up_at, actual_follow_up_at, result, next_action, decision_card_id,
  card_instance_id, reply_usage, chosen_reply_id, final_edited_text, version, created_at
FROM follow_ups;

CREATE TABLE decision_usages_v2 (
  id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  customer_id TEXT NOT NULL,
  follow_up_id TEXT NOT NULL,
  employee_user_id TEXT NOT NULL,
  decision_card_id TEXT NOT NULL,
  card_instance_id TEXT NOT NULL CHECK (length(trim(card_instance_id)) > 0),
  card_digest TEXT NOT NULL CHECK (
    length(card_digest) = 71
    AND substr(card_digest, 1, 7) = 'sha256_'
    AND substr(card_digest, 8) NOT GLOB '*[^0-9a-f]*'
  ),
  scenario TEXT NOT NULL,
  delivery_mode TEXT NOT NULL CHECK (delivery_mode = 'copy_only'),
  provided_reply_ids_json TEXT NOT NULL CHECK (json_valid(provided_reply_ids_json)),
  reply_usage TEXT NOT NULL CHECK (reply_usage IN ('adopted', 'edited', 'not_used')),
  chosen_reply_id TEXT,
  final_edited_text TEXT,
  copied_at TEXT,
  safety_status TEXT CHECK (safety_status IS NULL OR safety_status IN ('safe', 'reviewRequired')),
  safety_rule_ids_json TEXT CHECK (
    safety_rule_ids_json IS NULL OR json_valid(safety_rule_ids_json)
  ),
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id),
  FOREIGN KEY (tenant_id, follow_up_id, customer_id)
    REFERENCES follow_ups_v2 (tenant_id, id, customer_id),
  FOREIGN KEY (tenant_id, employee_user_id) REFERENCES users (tenant_id, id)
);

INSERT INTO decision_usages_v2 (
  id, tenant_id, customer_id, follow_up_id, employee_user_id, decision_card_id,
  card_instance_id, card_digest, scenario, delivery_mode, provided_reply_ids_json,
  reply_usage, chosen_reply_id, final_edited_text, copied_at, safety_status,
  safety_rule_ids_json, version, created_at
)
SELECT
  id, tenant_id, customer_id, follow_up_id, employee_user_id, decision_card_id,
  card_instance_id, card_digest, scenario, delivery_mode, provided_reply_ids_json,
  reply_usage, chosen_reply_id, final_edited_text, copied_at, safety_status,
  safety_rule_ids_json, version, created_at
FROM decision_usages;

DROP TABLE decision_usages;
DROP TABLE follow_ups;
ALTER TABLE follow_ups_v2 RENAME TO follow_ups;
ALTER TABLE decision_usages_v2 RENAME TO decision_usages;

CREATE INDEX idx_follow_ups_tenant_customer
  ON follow_ups (tenant_id, customer_id, created_at);
CREATE INDEX idx_follow_ups_tenant_employee
  ON follow_ups (tenant_id, employee_user_id, planned_follow_up_at);
CREATE UNIQUE INDEX uq_follow_ups_idempotency
  ON follow_ups (tenant_id, employee_user_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
CREATE INDEX idx_decision_usages_tenant_customer
  ON decision_usages (tenant_id, customer_id, created_at);
CREATE INDEX idx_decision_usages_tenant_follow_up
  ON decision_usages (tenant_id, follow_up_id);

CREATE TRIGGER follow_ups_insert_only_update
BEFORE UPDATE ON follow_ups BEGIN
  SELECT RAISE(ABORT, 'follow_ups are insert-only');
END;
CREATE TRIGGER follow_ups_insert_only_delete
BEFORE DELETE ON follow_ups BEGIN
  SELECT RAISE(ABORT, 'follow_ups are insert-only');
END;
CREATE TRIGGER decision_usages_insert_only_update
BEFORE UPDATE ON decision_usages BEGIN
  SELECT RAISE(ABORT, 'decision_usages are insert-only');
END;
CREATE TRIGGER decision_usages_insert_only_delete
BEFORE DELETE ON decision_usages BEGIN
  SELECT RAISE(ABORT, 'decision_usages are insert-only');
END;

PRAGMA defer_foreign_keys = OFF;

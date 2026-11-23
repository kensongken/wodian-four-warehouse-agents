PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  email TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('owner', 'employee')),
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, email)
);

CREATE TABLE IF NOT EXISTS customers (
  id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  employee_user_id TEXT NOT NULL,
  idempotency_key TEXT,
  request_fingerprint TEXT,
  customer_fingerprint TEXT,
  customer_number TEXT NOT NULL,
  customer_alias TEXT NOT NULL,
  category TEXT NOT NULL,
  planned_follow_up_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'interested', 'won', 'lost')),
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, customer_number),
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
  CHECK (
    customer_fingerprint IS NULL OR (
      length(customer_fingerprint) = 67
      AND substr(customer_fingerprint, 1, 3) = 'fp_'
      AND substr(customer_fingerprint, 4) NOT GLOB '*[^0-9a-f]*'
    )
  ),
  FOREIGN KEY (tenant_id, employee_user_id) REFERENCES users (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS follow_ups (
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
  card_instance_id TEXT,
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

CREATE TABLE IF NOT EXISTS decision_usages (
  id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  customer_id TEXT NOT NULL,
  follow_up_id TEXT NOT NULL,
  employee_user_id TEXT NOT NULL,
  decision_card_id TEXT NOT NULL,
  card_instance_id TEXT,
  card_digest TEXT,
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
  CHECK (
    card_digest IS NULL OR (
      length(card_digest) = 71
      AND substr(card_digest, 1, 7) = 'sha256_'
      AND substr(card_digest, 8) NOT GLOB '*[^0-9a-f]*'
    )
  ),
  FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id),
  FOREIGN KEY (tenant_id, follow_up_id, customer_id)
    REFERENCES follow_ups (tenant_id, id, customer_id),
  FOREIGN KEY (tenant_id, employee_user_id) REFERENCES users (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS corrections (
  id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  customer_id TEXT NOT NULL,
  follow_up_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  idempotency_key TEXT,
  request_fingerprint TEXT,
  reason TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, id),
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
  FOREIGN KEY (tenant_id, follow_up_id) REFERENCES follow_ups (tenant_id, id),
  FOREIGN KEY (tenant_id, actor_user_id) REFERENCES users (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS vault_assets (
  id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  customer_id TEXT NOT NULL,
  uploaded_by_user_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('customer_identity', 'chat_text', 'product_image')),
  vault_key TEXT NOT NULL,
  byte_size INTEGER CHECK (byte_size IS NULL OR byte_size >= 0),
  uploaded_at TEXT NOT NULL,
  delete_after TEXT NOT NULL,
  deleted_at TEXT,
  deleted_by_user_id TEXT,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, vault_key),
  CHECK (delete_after = strftime('%Y-%m-%dT%H:%M:%fZ', uploaded_at, '+30 days')),
  FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id),
  FOREIGN KEY (tenant_id, uploaded_by_user_id) REFERENCES users (tenant_id, id),
  FOREIGN KEY (tenant_id, deleted_by_user_id) REFERENCES users (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS vault_upload_daily_quotas (
  tenant_id TEXT NOT NULL,
  upload_day TEXT NOT NULL,
  used_bytes INTEGER NOT NULL CHECK (used_bytes >= 0),
  byte_limit INTEGER NOT NULL CHECK (byte_limit > 0),
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, upload_day),
  CHECK (used_bytes <= byte_limit),
  CHECK (length(upload_day) = 10)
);

CREATE TABLE IF NOT EXISTS vault_pending_uploads (
  id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  customer_id TEXT NOT NULL,
  uploaded_by_user_id TEXT NOT NULL,
  vault_key TEXT NOT NULL,
  byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
  uploaded_at TEXT NOT NULL,
  delete_after TEXT NOT NULL,
  upload_day TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, vault_key),
  CHECK (delete_after = strftime('%Y-%m-%dT%H:%M:%fZ', uploaded_at, '+30 days')),
  CHECK (upload_day = substr(uploaded_at, 1, 10)),
  FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id),
  FOREIGN KEY (tenant_id, uploaded_by_user_id) REFERENCES users (tenant_id, id),
  FOREIGN KEY (tenant_id, upload_day)
    REFERENCES vault_upload_daily_quotas (tenant_id, upload_day)
);

CREATE TRIGGER IF NOT EXISTS vault_pending_uploads_release_quota
AFTER DELETE ON vault_pending_uploads
WHEN NOT EXISTS (
  SELECT 1 FROM vault_assets
  WHERE tenant_id = OLD.tenant_id AND id = OLD.id
)
BEGIN
  UPDATE vault_upload_daily_quotas
  SET used_bytes = MAX(0, used_bytes - OLD.byte_size), version = version + 1
  WHERE tenant_id = OLD.tenant_id AND upload_day = OLD.upload_day;
END;

CREATE TABLE IF NOT EXISTS customer_vault_identities (
  id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  customer_id TEXT NOT NULL,
  vault_asset_id TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (tenant_id, customer_id, vault_asset_id),
  FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id),
  FOREIGN KEY (tenant_id, vault_asset_id) REFERENCES vault_assets (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  actor_user_id TEXT,
  action TEXT NOT NULL,
  target_kind TEXT NOT NULL,
  target_id TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  details_json TEXT CHECK (details_json IS NULL OR json_valid(details_json)),
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, actor_user_id) REFERENCES users (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS risk_alerts (
  id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  customer_id TEXT,
  employee_user_id TEXT,
  detected_by_user_id TEXT,
  kind TEXT NOT NULL CHECK (kind IN (
    'overdue_follow_up', 'missing_result', 'duplicate_customer',
    'sales_safety', 'retention_failed'
  )),
  status TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
  resolved_at TEXT,
  resolved_by_user_id TEXT,
  resolution_note TEXT,
  version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
  created_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, id),
  FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id),
  FOREIGN KEY (tenant_id, employee_user_id) REFERENCES users (tenant_id, id),
  FOREIGN KEY (tenant_id, detected_by_user_id) REFERENCES users (tenant_id, id),
  FOREIGN KEY (tenant_id, resolved_by_user_id) REFERENCES users (tenant_id, id)
);

CREATE INDEX IF NOT EXISTS idx_users_tenant_email
  ON users (tenant_id, email);
CREATE INDEX IF NOT EXISTS idx_customers_tenant_employee
  ON customers (tenant_id, employee_user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_customers_tenant_fingerprint
  ON customers (tenant_id, customer_fingerprint);
CREATE UNIQUE INDEX IF NOT EXISTS uq_customers_idempotency
  ON customers (tenant_id, employee_user_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_follow_ups_tenant_customer
  ON follow_ups (tenant_id, customer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_follow_ups_tenant_employee
  ON follow_ups (tenant_id, employee_user_id, planned_follow_up_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_follow_ups_idempotency
  ON follow_ups (tenant_id, employee_user_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_decision_usages_tenant_customer
  ON decision_usages (tenant_id, customer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_decision_usages_tenant_follow_up
  ON decision_usages (tenant_id, follow_up_id);
CREATE INDEX IF NOT EXISTS idx_corrections_tenant_customer
  ON corrections (tenant_id, customer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_corrections_tenant_follow_up
  ON corrections (tenant_id, follow_up_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_corrections_idempotency
  ON corrections (tenant_id, actor_user_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_vault_assets_tenant_customer
  ON vault_assets (tenant_id, customer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_vault_assets_tenant_expiry
  ON vault_assets (tenant_id, deleted_at, delete_after);
CREATE INDEX IF NOT EXISTS idx_vault_pending_uploads_tenant_expiry
  ON vault_pending_uploads (tenant_id, delete_after, id);
CREATE INDEX IF NOT EXISTS idx_customer_vault_identities_tenant_customer
  ON customer_vault_identities (tenant_id, customer_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_target
  ON audit_events (tenant_id, target_kind, target_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_risk_alerts_tenant_status
  ON risk_alerts (tenant_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_risk_alerts_tenant_customer
  ON risk_alerts (tenant_id, customer_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_risk_alerts_identity
  ON risk_alerts (
    tenant_id,
    kind,
    COALESCE(customer_id, ''),
    COALESCE(employee_user_id, '')
  );

CREATE TRIGGER IF NOT EXISTS follow_ups_insert_only_update
BEFORE UPDATE ON follow_ups BEGIN
  SELECT RAISE(ABORT, 'follow_ups are insert-only');
END;
CREATE TRIGGER IF NOT EXISTS follow_ups_insert_only_delete
BEFORE DELETE ON follow_ups BEGIN
  SELECT RAISE(ABORT, 'follow_ups are insert-only');
END;

CREATE TRIGGER IF NOT EXISTS decision_usages_insert_only_update
BEFORE UPDATE ON decision_usages BEGIN
  SELECT RAISE(ABORT, 'decision_usages are insert-only');
END;
CREATE TRIGGER IF NOT EXISTS decision_usages_insert_only_delete
BEFORE DELETE ON decision_usages BEGIN
  SELECT RAISE(ABORT, 'decision_usages are insert-only');
END;

CREATE TRIGGER IF NOT EXISTS corrections_insert_only_update
BEFORE UPDATE ON corrections BEGIN
  SELECT RAISE(ABORT, 'corrections are insert-only');
END;
CREATE TRIGGER IF NOT EXISTS corrections_insert_only_delete
BEFORE DELETE ON corrections BEGIN
  SELECT RAISE(ABORT, 'corrections are insert-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_events_insert_only_update
BEFORE UPDATE ON audit_events BEGIN
  SELECT RAISE(ABORT, 'audit_events are insert-only');
END;
CREATE TRIGGER IF NOT EXISTS audit_events_insert_only_delete
BEFORE DELETE ON audit_events BEGIN
  SELECT RAISE(ABORT, 'audit_events are insert-only');
END;


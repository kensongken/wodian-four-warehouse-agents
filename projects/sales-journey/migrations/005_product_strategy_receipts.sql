CREATE TABLE product_strategy_receipts (
  tenant_id TEXT NOT NULL,
  outbox_event_id TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  source_event_id TEXT NOT NULL,
  product_code TEXT NOT NULL,
  strategy_input_version INTEGER NOT NULL,
  product_data_version INTEGER NOT NULL,
  content_sha256 TEXT NOT NULL,
  status TEXT NOT NULL,
  received_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, outbox_event_id),
  CHECK (length(tenant_id) = 36),
  CHECK (length(outbox_event_id) = 36),
  CHECK (length(source_event_id) = 36),
  CHECK (length(payload_sha256) = 64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  CHECK (length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
  CHECK (length(product_code) BETWEEN 1 AND 64),
  CHECK (strategy_input_version >= 1),
  CHECK (product_data_version >= 1),
  CHECK (status = 'preview_only')
);

CREATE INDEX idx_product_strategy_receipts_product_version
ON product_strategy_receipts (tenant_id, product_code, strategy_input_version);
